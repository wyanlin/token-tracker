"""OpenCode TUI 数据适配器：读 opencode 的 SQLite（~/.local/share/opencode/opencode.db）。

opencode 把会话与逐请求用量都落进 SQLite：
- `session` 表直接带聚合字段：tokens_input/output/reasoning/cache_read/cache_write、cost、
  model（`{"providerID":..,"id":..}` JSON）、directory、time_*（epoch 毫秒）。
- `message.data`（JSON）逐请求带 `role` / `tokens{total,input,output,reasoning,cache{read,write}}` / `cost`。

WAL 模式 + opencode 常驻时并发读安全：只读 URI + mode=ro，绝不写库。会话级 UsageEntry
形态对齐 codex.py：逐请求 usage 拆成 pricing_segments，让峰谷/阶梯计价照常生效。
"""

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .types import AgentInfo, UsageEntry, UsageSegment
from .util import opencode_home, project_from_cwd

OPENCODE_DIR = opencode_home()
OPENCODE_DB = os.path.join(OPENCODE_DIR, "opencode.db")


def detect() -> AgentInfo | None:
    if Path(OPENCODE_DB).is_file():
        return AgentInfo(id="opencode", name="OpenCode")
    return None


def load_entries(hours_back: int = 0) -> list[UsageEntry]:
    cutoff = None
    if hours_back > 0:
        cutoff = datetime.now(UTC) - timedelta(hours=hours_back)
    return _load_entries(cutoff, None)


def load_recent_entries(cutoff: datetime) -> list[UsageEntry]:
    """读取 cutoff 后仍写入的完整会话，供 `tt sessions` 渐进查找最近 N 条。"""
    return _load_entries(None, cutoff)


def _load_entries(start_cutoff: datetime | None, recent_cutoff: datetime | None) -> list[UsageEntry]:
    entry: dict[str, UsageEntry] = {}
    for row in _query_sessions():
        sid = row.get("id")
        if not sid:
            continue

        created = _ms_to_dt(row.get("time_created"))
        if created is None:
            continue
        updated = _ms_to_dt(row.get("time_updated")) or created
        if start_cutoff is not None and updated < start_cutoff:
            continue
        if recent_cutoff is not None and updated < recent_cutoff:
            continue

        if not _has_usage(row):
            continue

        segments, msg_count = _load_segments(sid)
        totals = _segment_totals(segments)

        # 会话聚合字段是权威（含 title 摘要 / subagent 等消息外的用量）；
        # 逐轮 segments 完整覆盖会话时才用于分档计价（cost.py 的 _segments_cover_entry 还会再校验一次）。
        input_tokens = _int(row.get("tokens_input"))
        output_tokens = _int(row.get("tokens_output"))
        cache_read = _int(row.get("tokens_cache_read"))
        cache_write = _int(row.get("tokens_cache_write"))
        reasoning = _int(row.get("tokens_reasoning"))
        if not _segments_cover(totals, input_tokens, output_tokens + reasoning, cache_read, cache_write):
            segments = []

        project = project_from_cwd(_native_path(row.get("directory") or "")) if row.get("directory") else "unknown"
        entry[sid] = UsageEntry(
            timestamp=created,
            session_id=sid,
            message_id=sid,
            request_id="",
            model=_session_model(row.get("model")),
            # reasoning 与 output 是两个独立桶、都属生成量：并入 output_tokens 走输出价（与 cost 口径一致）
            input_tokens=input_tokens,
            output_tokens=output_tokens + reasoning,
            cache_creation_tokens=cache_write,
            cache_read_tokens=cache_read,
            cost_usd=None,
            project=project,
            agent_id="opencode",
            message_count=max(msg_count, 1),
            session_end=updated,
            pricing_segments=tuple(segments),
        )
    return sorted(entry.values(), key=lambda e: e.timestamp)


def _segments_cover(
    totals: tuple[int, int, int, int],
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_write: int,
) -> bool:
    return totals == (input_tokens, output_tokens, cache_read, cache_write)


def _query_sessions() -> list[dict]:
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT id, directory, model, time_created, time_updated, "
            "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write "
            "FROM session"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def _load_segments(session_id: str) -> tuple[list[UsageSegment], int]:
    segments: list[UsageSegment] = []
    msg_count = 0
    try:
        conn = _connect()
        rows = conn.execute(
            "SELECT data FROM message WHERE session_id=? ORDER BY time_created", (session_id,)
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return segments, msg_count

    for row in rows:
        data = _parse_json(row[0])
        if not data or data.get("role") == "user":
            if data:
                msg_count += 1
            continue
        msg_count += 1
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            continue
        ts = _ms_to_dt(data.get("time", {}).get("created") if isinstance(data.get("time"), dict) else None)
        if ts is None:
            continue
        input_tokens = _int(tokens.get("input"))
        output_tokens = _int(tokens.get("output"))
        reasoning = _int(tokens.get("reasoning"))
        cache = tokens.get("cache")
        cached = _int(cache.get("read")) if isinstance(cache, dict) else 0
        written = _int(cache.get("write")) if isinstance(cache, dict) else 0
        if input_tokens == 0 and output_tokens == 0 and cached == 0 and written == 0:
            continue
        segments.append(
            UsageSegment(
                timestamp=ts,
                input_tokens=input_tokens,
                output_tokens=output_tokens + reasoning,
                cache_creation_tokens=written,
                cache_read_tokens=cached,
            )
        )
    return segments, msg_count


def _segment_totals(segments: list[UsageSegment]) -> tuple[int, int, int, int]:
    return (
        sum(s.input_tokens for s in segments),
        sum(s.output_tokens for s in segments),
        sum(s.cache_read_tokens for s in segments),
        sum(s.cache_creation_tokens for s in segments),
    )


def _has_usage(row: dict) -> bool:
    return any(
        _int(row.get(k)) > 0
        for k in ("tokens_input", "tokens_output", "tokens_reasoning", "tokens_cache_read", "tokens_cache_write")
    )


def _session_model(raw: object) -> str:
    if isinstance(raw, dict):
        mid = raw.get("id")
        return str(mid) if isinstance(mid, str) and mid else "unknown"
    data = _parse_json(raw)
    if isinstance(data, dict):
        mid = data.get("id") or data.get("modelID")
        if isinstance(mid, str) and mid:
            return mid
    return "unknown"


def _ms_to_dt(raw: object) -> datetime | None:
    if not isinstance(raw, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(raw / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _parse_json(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _native_path(p: str) -> str:
    """opencode 跨平台统一存正斜杠目录；Windows 上换算回反斜杠让 project_from_cwd 正确拆分。"""
    if os.sep == "\\":
        return p.replace("/", "\\")
    return p


def _connect() -> sqlite3.Connection:
    """只读连接：mode=ro 杜绝意外写库；WAL 下仍能读到 opencode 运行中的最新写入。"""
    conn = sqlite3.connect(f"file:{os.path.abspath(OPENCODE_DB)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn

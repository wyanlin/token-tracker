"""OpenCode adapter 测试：用临时 SQLite 复刻 opencode.db 的 session / message 结构。"""

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest

from token_tracker.adapters import opencode
from token_tracker.adapters.types import AgentInfo


def _now_ms(days_ago: float = 0) -> int:
    return int((time.time() - days_ago * 86400) * 1000)


def _assistant_msg(model: str, tokens: dict, created_ms: int) -> str:
    return json.dumps({
        "role": "assistant",
        "modelID": model,
        "tokens": tokens,
        "time": {"created": created_ms},
    })


def _make_db(tmp_path) -> sqlite3.Connection:
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY, project_id TEXT, directory TEXT, title TEXT, model TEXT,
            time_created INTEGER, time_updated INTEGER, agent TEXT, cost REAL,
            tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
            tokens_cache_read INTEGER, tokens_cache_write INTEGER
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT
        );
        """
    )
    return conn


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode, "OPENCODE_DB", str(tmp_path / "opencode.db"))
    monkeypatch.setattr(opencode, "OPENCODE_DIR", str(tmp_path))


def test_detect_returns_none_without_db(tmp_path):
    assert opencode.detect() is None


def test_detect_with_db(tmp_path):
    _make_db(tmp_path).close()
    info = opencode.detect()
    assert isinstance(info, AgentInfo)
    assert info.id == "opencode"


def test_load_entries_parses_session_and_segments(tmp_path):
    conn = _make_db(tmp_path)
    now = _now_ms()
    conn.execute(
        "INSERT INTO session (id, directory, model, time_created, time_updated, "
        "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("ses_1", "C:/repo/proj", '{"providerID":"opencode-go","id":"deepseek-v4-flash"}',
         now - 1000, now, 100, 30, 5, 200, 3),
    )
    # 两条 assistant 消息，用量与 session 聚合一致；reasoning 并入 output
    conn.executemany(
        "INSERT INTO message (id, session_id, time_created, data) VALUES (?,?,?,?)",
        [
            ("m1", "ses_1", now - 900,
             _assistant_msg("deepseek-v4-flash",
                            {"input": 40, "output": 10, "reasoning": 2,
                             "cache": {"read": 80, "write": 1}}, now - 900)),
            ("m2", "ses_1", now - 100,
             _assistant_msg("deepseek-v4-flash",
                            {"input": 60, "output": 20, "reasoning": 3,
                             "cache": {"read": 120, "write": 2}}, now - 100)),
            ("u3", "ses_1", now - 50, '{"role":"user"}'),
        ],
    )
    conn.commit()
    conn.close()

    entries = opencode.load_entries()
    assert len(entries) == 1
    e = entries[0]
    assert e.agent_id == "opencode"
    assert e.session_id == "ses_1"
    assert e.model == "deepseek-v4-flash"
    assert e.input_tokens == 100
    assert e.output_tokens == 35  # 30 + reasoning 5
    assert e.cache_read_tokens == 200
    assert e.cache_creation_tokens == 3
    assert e.message_count == 3
    assert e.project == "proj"
    # segments 覆盖会话 → 逐轮计价可用
    assert len(e.pricing_segments) == 2
    total_out = sum(s.output_tokens for s in e.pricing_segments)
    assert total_out == 35


def test_load_entries_drops_segments_when_not_covering(tmp_path):
    conn = _make_db(tmp_path)
    now = _now_ms()
    # 会话聚合含标题/子 agent 额外用量 → message 无法覆盖 → 不挂 pricing_segments（安全降级）
    conn.execute(
        "INSERT INTO session (id, directory, model, time_created, time_updated, "
        "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("ses_2", "/home/u/repo", '{"id":"gpt-5.5"}', now - 1000, now, 500, 40, 0, 0, 0),
    )
    conn.execute(
        "INSERT INTO message (id, session_id, time_created, data) VALUES (?,?,?,?)",
        ("m1", "ses_2", now - 900,
         _assistant_msg("gpt-5.5",
                        {"input": 100, "output": 10, "reasoning": 0,
                         "cache": {"read": 0, "write": 0}}, now - 900)),
    )
    conn.commit()
    conn.close()

    entries = opencode.load_entries()
    assert len(entries) == 1
    assert entries[0].pricing_segments == ()


def test_load_entries_skips_empty_sessions_and_filters_hours_back(tmp_path):
    conn = _make_db(tmp_path)
    now = _now_ms()
    conn.executemany(
        "INSERT INTO session (id, directory, model, time_created, time_updated, "
        "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("empty", "/repo", None, now - 1000, now, 0, 0, 0, 0, 0),
            ("old", "/repo", '{"id":"x"}', now - 3600_000 * 50, now - 3600_000 * 49, 10, 0, 0, 0, 0),
            ("recent", "/repo", '{"id":"x"}', now - 1000, now, 10, 5, 0, 0, 0),
        ],
    )
    conn.commit()
    conn.close()

    entries = opencode.load_entries(hours_back=48)
    assert [e.session_id for e in entries] == ["recent"]


def test_load_recent_entries_uses_time_updated(tmp_path):
    conn = _make_db(tmp_path)
    now = _now_ms()
    # 会话创建很早但最近仍写入 → load_recent_entries 应纳入（渐进扫描口径，对齐 codex）
    conn.execute(
        "INSERT INTO session (id, directory, model, time_created, time_updated, "
        "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("long", "/repo", '{"id":"x"}', now - 3600_000 * 100, now, 20, 5, 0, 0, 0),
    )
    conn.commit()
    conn.close()

    cutoff = datetime.now(UTC) - timedelta(hours=1)
    entries = opencode.load_recent_entries(cutoff)
    assert [e.session_id for e in entries] == ["long"]


def test_unknown_model_falls_back_to_unknown(tmp_path):
    conn = _make_db(tmp_path)
    now = _now_ms()
    conn.execute(
        "INSERT INTO session (id, directory, model, time_created, time_updated, "
        "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("ses_3", "/repo", "not-json", now - 1000, now, 10, 0, 0, 0, 0),
    )
    conn.commit()
    conn.close()
    assert opencode.load_entries()[0].model == "unknown"

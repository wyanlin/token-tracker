/**
 * tt-statusline — Token Tracker TUI statusline panel for OpenCode (sidebar slot).
 *
 * 在 OpenCode TUI 会话侧边栏渲染：
 *  - 当前会话累计 token / 成本 / 模型（数据来自 TUI 自身状态 api.state.session.messages，状态驱动刷新）
 *  - OpenCode Go 官方额度：5h / weekly / monthly 的真实使用率（官方 _server RPC，
 *    需 ~/.config/token-tracker/opencode-go.json 提供 workspace_id + auth cookie，60s 刷新）
 *
 * 模板烘焙：__OPENCODE_STATUSLINE_VERSION__ 由 hooks.py 的 _render_opencode_statusline_plugin()
 * 注入唯一版本号（= OPENCODE_STATUSLINE_HOOK_VERSION）。内容一变必须 bump，老用户自动重写。
 * 运行时 import（solid-js / @opentui/solid / fetch）由 opencode 插件宿主提供。
 */

/** @jsxImportSource @opentui/solid */
import os from "node:os"
import path from "node:path"
import { createSignal, onCleanup, onMount, Show } from "solid-js"
import type { JSX } from "@opentui/solid"
import type {
  TuiPlugin,
  TuiPluginApi,
  TuiPluginModule,
  TuiSlotPlugin,
  TuiSlotContext,
} from "@opencode-ai/plugin/tui"
import type { Message } from "@opencode-ai/sdk"

export const TT_VERSION = "__OPENCODE_STATUSLINE_VERSION__"
const GO_REFRESH_MS = 60000

const GO_ENDPOINT = "https://opencode.ai/_server"
const GO_AGG_ID = "c7389bd0e731f80f49593e5ee53835475f4e28594dd6bd83eb229bab753498cd"
const GO_UA = "Mozilla/5.0"

interface GoQuota {
  resetInSec: number
  usagePercent: number
}

interface Totals {
  input: number
  output: number
  reasoning: number
  cacheRead: number
  cacheWrite: number
  cost: number
  model: string
}

function fmtTokens(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`
  return `${n}`
}

function fmtCost(v: number): string {
  if (v <= 0) return "$0"
  if (v < 0.01) return `$${v.toFixed(4)}`
  if (v < 100) return `$${v.toFixed(2)}`
  return `$${Math.round(v)}`
}

function fmtReset(sec: number): string {
  if (sec <= 0) return ""
  const s = Math.round(sec)
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}d${h}h`
  if (h > 0) return `${h}h${m}m`
  return `${m}m`
}

// OpenCode Go 官方额度（5h / weekly / monthly 真实使用率）。未配置返回 null，请求失败返回 undefined。
async function loadGoQuota(): Promise<{ rolling: GoQuota; weekly: GoQuota; monthly: GoQuota } | null | undefined> {
  let cfg: { workspace_id?: string; auth_cookie?: string } = {}
  try {
    const cfgPath = path.join(os.homedir(), ".config", "token-tracker", "opencode-go.json")
    cfg = JSON.parse(await Bun.file(cfgPath).text()) ?? {}
  } catch {
    cfg = {}
  }
  const workspaceId = cfg.workspace_id
  const authCookie = cfg.auth_cookie
  if (!workspaceId || !authCookie) return null

  try {
    const res = await fetch(GO_ENDPOINT, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-server-id": GO_AGG_ID,
        "x-server-instance": "tt-statusline",
        "cookie": `auth=${authCookie}`,
        "user-agent": GO_UA,
      },
      body: JSON.stringify({
        t: { t: 9, i: 0, l: 1, a: [{ t: 1, s: workspaceId }], o: 0 },
        f: 31,
        m: [],
      }),
      signal: AbortSignal.timeout(10000),
    })
    const text = await res.text()
    if (!res.ok || text.includes("/auth/authorize")) return undefined

    const extract = (key: string): GoQuota | null => {
      const m = text.match(new RegExp(`${key}:[^}]*?\\{status:"[^"]*",resetInSec:(\\d+),usagePercent:([\\d.]+)`))
      if (!m) return null
      return { resetInSec: Number(m[1]), usagePercent: Number(m[2]) }
    }
    const rolling = extract("rollingUsage")
    const weekly = extract("weeklyUsage")
    const monthly = extract("monthlyUsage")
    if (!rolling || !weekly || !monthly) return undefined
    return { rolling, weekly, monthly }
  } catch {
    return undefined
  }
}

function statuslinePanel(props: { ctx: TuiSlotContext; sessionID: string; api: TuiPluginApi }): JSX.Element {
  const c = props.ctx.theme.current
  const sessionID = props.sessionID
  const api = props.api

  const [go, setGo] = createSignal<{ rolling: GoQuota; weekly: GoQuota; monthly: GoQuota } | null | undefined>()

  onMount(() => {
    const refreshGo = async () => setGo(await loadGoQuota())
    refreshGo()
    const gh = setInterval(refreshGo, GO_REFRESH_MS)
    onCleanup(() => clearInterval(gh))
  })

  const totals = (): Totals => {
    const t: Totals = { input: 0, output: 0, reasoning: 0, cacheRead: 0, cacheWrite: 0, cost: 0, model: "" }
    let msgs: Message[] = []
    try {
      msgs = api.state.session.messages(sessionID) ?? []
    } catch {
      msgs = []
    }
    for (const m of msgs) {
      if (m.role !== "assistant") continue
      t.cost += m.cost ?? 0
      const tk = m.tokens
      if (!tk) continue
      t.input += tk.input ?? 0
      t.output += tk.output ?? 0
      t.reasoning += tk.reasoning ?? 0
      const cache = tk.cache
      if (cache) {
        t.cacheRead += cache.read ?? 0
        t.cacheWrite += cache.write ?? 0
      }
      if (!t.model && m.modelID) t.model = m.modelID
    }
    return t
  }

  const totalTokens = () => {
    const t = totals()
    return t.input + t.output + t.reasoning + t.cacheRead + t.cacheWrite
  }

  const project = () => {
    try {
      const wt = api.state.path.worktree
      if (wt) {
        const parts = wt.split(/[\\/]/)
        return parts[parts.length - 1] || wt
      }
    } catch {
      /* state 未就绪 */
    }
    return ""
  }
  const branch = () => {
    try {
      return api.state.vcs?.branch ?? ""
    } catch {
      return ""
    }
  }

  const BAR_W = 10
  const quotaRow = (label: string, q: GoQuota | undefined): JSX.Element => {
    if (q === undefined) {
      return (
        <text height={1} selectable={false}>
          <span style={{ fg: c.textMuted }}>{label}</span>
          <span>{`  `}</span>
          <span style={{ fg: c.warning }}>?</span>
        </text>
      )
    }
    const pct = Math.min(q.usagePercent, 100)
    const filled = Math.round((pct / 100) * BAR_W)
    const bar = "\u2588".repeat(filled) + "\u2591".repeat(BAR_W - filled)
    const barColor = pct >= 90 ? c.error : pct >= 60 ? c.warning : c.success
    const reset = fmtReset(q.resetInSec)
    return (
      <text height={1} selectable={false}>
        <span style={{ fg: c.textMuted }}>{label}</span>
        <span>{` `}</span>
        <span style={{ fg: barColor }}>{bar}</span>
        <span>{` `}</span>
        <span style={{ fg: c.text }}>{pct >= 100 ? 100 : pct}%</span>
        <span style={{ fg: c.textMuted }}>{reset ? ` (${reset})` : ""}</span>
      </text>
    )
  }

  const rows = (): JSX.Element => {
    const t = totals()
    const quota = go()
    const sep = "\u2500".repeat(18)
    const muted = (s: string) => <span style={{ fg: c.textMuted }}>{s}</span>
    return (
      <>
        <text height={1} selectable={false}>
          <span style={{ fg: c.primary }}><b>Token Tracker</b></span>
          <span style={{ fg: c.textMuted }}> v{TT_VERSION}</span>
        </text>
        <text fg={c.borderSubtle} selectable={false}>{sep}</text>
        {(project() || branch()) && (
          <text height={1} selectable={false}>
            <span style={{ fg: c.secondary }}>{project() || "?"}</span>
            {branch() ? <span style={{ fg: c.text }}>({branch()})</span> : null}
          </text>
        )}
        <text height={1} selectable={false}>
          <span style={{ fg: c.primary }}>Total </span>
          <span style={{ fg: c.text }}><b>{fmtTokens(totalTokens())}</b></span>
          <span style={{ fg: c.primary }}>  Cost </span>
          <span style={{ fg: c.accent }}><b>{fmtCost(t.cost)}</b></span>
        </text>
        <text height={1} selectable={false}>
          {muted(`in ${fmtTokens(t.input)}  out ${fmtTokens(t.output + t.reasoning)}`)}
        </text>
        {(t.cacheRead > 0 || t.cacheWrite > 0) && (
          <text height={1} selectable={false}>
            {muted(`cache r ${fmtTokens(t.cacheRead)}  w ${fmtTokens(t.cacheWrite)}`)}
          </text>
        )}
        {t.model && (
          <text height={1} selectable={false}>
            <span style={{ fg: c.warning }}>Model </span>
            <span style={{ fg: c.text }}>{t.model}</span>
          </text>
        )}
        <text fg={c.borderSubtle} selectable={false}>{sep}</text>
        {quota === null && (
          <text fg={c.textMuted} selectable={false}>
            Go 额度未配置（~/.config/token-tracker/opencode-go.json）
          </text>
        )}
        {quota === undefined && (
          <>
            {quotaRow("5h", undefined)}
            {quotaRow("1w", undefined)}
            {quotaRow("1m", undefined)}
          </>
        )}
        {quota !== null && quota !== undefined && (
          <>
            {quotaRow("5h", quota.rolling)}
            {quotaRow("1w", quota.weekly)}
            {quotaRow("1m", quota.monthly)}
          </>
        )}
      </>
    )
  }

  return (
    <box border borderColor={c.borderSubtle} paddingLeft={1} paddingRight={1} flexDirection="column">
      <Show when={totalTokens() > 0 || go() !== undefined}>
        {rows()}
      </Show>
    </box>
  )
}

function createStatuslineSlot(api: TuiPluginApi): TuiSlotPlugin {
  return {
    order: 40,
    slots: {
      sidebar_content(ctx: TuiSlotContext, input: { session_id: string }): JSX.Element {
        return statuslinePanel({ ctx, sessionID: input.session_id, api })
      },
    },
  }
}

const tui: TuiPlugin = async (api) => {
  try {
    api.slots.register(createStatuslineSlot(api))
  } catch {
    /* 宿主 slots API 不可用时静默跳过 */
  }
}

const mod: TuiPluginModule & { id: string } = {
  id: "tt-statusline",
  tui,
}

export default mod
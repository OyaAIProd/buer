# BUER hook setup for Claude Code

## 0. Install

```bash
pip install -e .   # editable install — makes buer-server available as a command
```

Run once from the buer repo root.  Editable mode means code changes don't
require re-installing, but **a running buer-server process must be restarted
to pick up code changes** (see Running notes below).

## 1. Start the server

```bash
buer-server --db .buer/store.sqlite --port 7777 --transport streamable-http
```

Or with an absolute DB path (recommended):

```bash
buer-server --db /absolute/path/to/.buer/store.sqlite --port 7777 --host 127.0.0.1 --transport streamable-http
```

## 2. Register your project root

```python
from buer.store import Store
store = Store(".buer/store.sqlite")
store.get_or_create_project("/absolute/path/to/your/project")
```

## 3. Add the hook to `.claude/settings.json`

Place this file in your project root (or user-level `~/.claude/settings.json`):

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "curl -s -X POST http://127.0.0.1:7777/buer/post-edit -H 'Content-Type: application/json' -d @-"
          }
        ]
      }
    ]
  }
}
```

Change `7777` to the `--port` you started the server with.

## 4. Add BUER as an MCP server (for check_drift / get_user_alerts)

Add to `.claude/settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "buer": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:7777/mcp"
    }
  }
}
```

**Note**: the default transport is `streamable-http` (endpoint `/mcp`).
If you start the server with `--transport sse`, use `"type": "sse"` and
`"url": "http://127.0.0.1:7777/sse"` instead.

For stdio transport (no HTTP server needed, but hook POST path unavailable):

```json
{
  "mcpServers": {
    "buer": {
      "type": "stdio",
      "command": "buer-server",
      "args": ["--transport", "stdio", "--db", ".buer/store.sqlite"]
    }
  }
}
```

## 5. Verify

After making an edit, any incident output from the hook appears as an injected
context note.  You can also call `check_drift("/your/project/root")` directly
to see active incidents without waiting for a hook event.

User-level escalations (`escalated_user` state) are retrievable via
`get_user_alerts("/your/project/root")`.

## Running notes

**Code changes require a server restart.**  buer-server loads code at startup;
editable install means you don't need to re-install after changing BUER's own
detection code, but you *do* need to kill and restart the process:

```bash
kill $(lsof -ti:7777)
buer-server --db .buer/store.sqlite --port 7777 --transport streamable-http &
```

**Schema changes require a DB rebuild.**  If you modify `buer/schema.sql`
(add a table, change a column), the existing `.buer/store.sqlite` is **not**
migrated automatically.  During dogfooding the simplest fix is to delete and
recreate:

```bash
rm .buer/store.sqlite   # ⚠ clears all monitoring history
buer-server --db .buer/store.sqlite ...
```

**Keeping the server running after logout.**  Use a systemd user service so it
starts on boot, restarts on crash, and keeps running after logout:

```ini
# ~/.config/systemd/user/buer.service
[Unit]
Description=BUER MCP server
After=network.target

[Service]
ExecStart=/absolute/path/to/.local/bin/buer-server \
    --db /absolute/path/to/.buer/store.sqlite \
    --port 7777 --host 127.0.0.1 --transport streamable-http
WorkingDirectory=/absolute/path/to/project
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now buer.service
loginctl enable-linger $USER   # keep running after logout / reboot
```

## 6. v2.1 hook 生命周期三层（opt-in）

这三个 hook 共同构成 v2.1 的主动结构支持基础设施。全部 opt-in，可独立启用。

**<5s 约束**：所有三个 hook 端点设计为快速返回。`post-read` 只入队，`session-start` 只查询，`stop` 触发后台线程后立即返回——重分析（全库建图）在后台异步跑，不阻塞 hook 响应。

### 6a. SessionStart 粗层地图注入

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{
          "type": "command",
          "command": "curl -s -X POST http://127.0.0.1:7777/buer/session-start -H 'Content-Type: application/json' -d @-"
        }]
      }
    ]
  }
}
```

会话开始时注入粗层结构概览（如已有结构图）。source=resume 时同样执行，刷新 context。
尚无结构图时静默返回空（宁漏不误报）。

### 6b. PostToolUse Read|Grep|Glob 搭扫库车（opt-in）

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Read|Grep|Glob",
        "hooks": [{
          "type": "command",
          "command": "curl -s -X POST http://127.0.0.1:7777/buer/post-read -H 'Content-Type: application/json' -d @-"
        }]
      }
    ]
  }
}
```

每次读操作极轻处理（只入队，不同步建图）。BUER 搭 agent 扫库的车，增量收集"读了哪些文件"，
在 Stop 时异步建图。注意 matcher 隔离 bug（#20334）——端点对非目标工具静默放过。

### 6c. Stop 重分析 + user 警报汇总

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [{
          "type": "command",
          "command": "curl -s -X POST http://127.0.0.1:7777/buer/stop -H 'Content-Type: application/json' -d @-"
        }]
      }
    ]
  }
}
```

agent 一轮响应结束时：(1) 触发后台异步重分析（drain 待重算队列）；(2) 汇总本轮未送达的
user 级警报并输出（stdout → 用户可见）。
**stop_hook_active 防死循环**：端点在收到 stop_hook_active=true 时立即返回空，不执行任何逻辑，
防止 Stop hook 输出触发再次 Stop 的无限循环（已查证硬要求）。

### 6d. 完整三层 + 既有 hook 的 settings.json 示例

```json
{
  "hooks": {
    "SessionStart": [
      {"hooks": [{"type": "command", "command": "curl -s -X POST http://127.0.0.1:7777/buer/session-start -H 'Content-Type: application/json' -d @-"}]}
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [{"type": "command", "command": "curl -s -X POST http://127.0.0.1:7777/buer/post-edit -H 'Content-Type: application/json' -d @-"}]
      },
      {
        "matcher": "Read|Grep|Glob",
        "hooks": [{"type": "command", "command": "curl -s -X POST http://127.0.0.1:7777/buer/post-read -H 'Content-Type: application/json' -d @-"}]
      },
      {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "curl -s -X POST http://127.0.0.1:7777/buer/post-bash -H 'Content-Type: application/json' -d @-"}]
      }
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "curl -s -X POST http://127.0.0.1:7777/buer/stop -H 'Content-Type: application/json' -d @-"}]}
    ]
  }
}
```

## 7. Optional: real-time test capture (opt-in Bash hook)

Add a second PostToolUse entry that fires on every Bash tool call.  BUER
parses stdout for test results immediately — before the JUnit XML file lands
on disk — giving run-level pass/fail feedback within the same agent turn.

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [{"type": "command", "command": "curl -s -X POST http://127.0.0.1:7777/buer/post-edit -H 'Content-Type: application/json' -d @-"}]
      },
      {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "curl -s -X POST http://127.0.0.1:7777/buer/post-bash -H 'Content-Type: application/json' -d @-"}]
      }
    ]
  }
}
```

**Supported test runners**: pytest, jest, vitest, mocha, go test, cargo test,
npm/npx test.

**Honest boundary** — stdout capture provides run-level totals only:
- No per-test-case data (format too variable; 宁漏不误报).
- No coverage_map attribution (requires XML/lcov with line info).
- Parse failures are silent — unrecognized output is dropped, never guessed.
- `source='stdout'` marks these rows so consumers can apply appropriate trust.

**Dedup**: if a fresh JUnit XML run was ingested in the last 120 s, the
stdout run is silently skipped (XML is authoritative).  Same-command stdout
runs within 30 s are also deduplicated.

**Response is always empty** — no agent injection for stdout captures.

## Notes

- Other agents (Cursor, Cline, etc.) can POST to `/buer/post-edit` after each
  edit using the same payload format, or call `check_drift` via MCP tool.

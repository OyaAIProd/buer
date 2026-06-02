# BUER Setup

How to run the BUER server and wire it into Claude Code.

## Server Startup & Claude Code Hook Config

### Starting the server

```bash
# From the buer/ repo root:
python -m buer.mcp.server --host 127.0.0.1 --port 7777 --db .buer/store.sqlite

# Or via the installed console script (after pip install -e .):
buer-server --host 127.0.0.1 --port 7777 --db .buer/store.sqlite

# Transport options: streamable-http (default), sse, stdio
```

The server pre-warms the SQLite store on startup (creates schema if missing).  
Default DB path if `--db` omitted: `.buer/store.sqlite` (relative to cwd, or override with `BUER_DB` env var).

### Wiring Claude Code hooks

> **Prerequisite:** the hook commands below use `jq` to build JSON payloads.
> Install it first if needed: `sudo apt install jq` (Debian/Ubuntu) or
> `brew install jq` (macOS).

> **Platform:** the hook commands use shell syntax (`$(...)`, here-strings) and assume
> Linux or macOS. On Windows, run BUER and configure hooks inside WSL (Windows Subsystem
> for Linux); the native Windows shell is not supported for these hook commands.

Add to `.claude/settings.json` in the project being monitored (not inside `buer/` itself):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "curl -s -X POST http://127.0.0.1:7777/buer/session-start -H 'Content-Type: application/json' -d @- <<< \"$(jq -n --arg cwd \"$PWD\" --arg session_id \"$CLAUDE_CODE_SESSION_ID\" --arg source \"startup\" '{cwd:$cwd,session_id:$session_id,source:$source}')\""
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "curl -s -X POST http://127.0.0.1:7777/buer/post-edit -H 'Content-Type: application/json' -d @- <<< \"$(jq -n --arg cwd \"$PWD\" --arg session_id \"$CLAUDE_CODE_SESSION_ID\" --argjson tool_input \"$TOOL_INPUT\" '{cwd:$cwd,session_id:$session_id,tool_input:$tool_input}')\""
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "curl -s -X POST http://127.0.0.1:7777/buer/post-bash -H 'Content-Type: application/json' -d @- <<< \"$(jq -n --arg cwd \"$PWD\" --argjson tool_input \"$TOOL_INPUT\" --argjson tool_response \"$TOOL_RESPONSE\" '{cwd:$cwd,tool_input:$tool_input,tool_response:$tool_response}')\""
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Read",
        "hooks": [
          {
            "type": "command",
            "command": "curl -s -X POST http://127.0.0.1:7777/buer/post-read -H 'Content-Type: application/json' -d @- <<< \"$(jq -n --arg cwd \"$PWD\" --argjson tool_input \"$TOOL_INPUT\" '{cwd:$cwd,tool_input:$tool_input}')\""
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "curl -s -X POST http://127.0.0.1:7777/buer/stop -H 'Content-Type: application/json' -d @- <<< \"$(jq -n --arg cwd \"$PWD\" --arg session_id \"$CLAUDE_CODE_SESSION_ID\" '{cwd:$cwd,session_id:$session_id}')\""
          }
        ]
      }
    ]
  }
}
```

Or use the MCP transport (`--transport streamable-http`) and add BUER as an MCP server in `.claude/mcp.json`:

```json
{
  "mcpServers": {
    "buer": {
      "type": "http",
      "url": "http://127.0.0.1:7777/mcp"
    }
  }
}
```

**Quick smoke test** — after server starts:

```bash
curl -s http://127.0.0.1:7777/buer/health | python3 -m json.tool
```


---

## Notification Level

The only user-facing tuning knob. Controls **how often BUER proactively alerts you** — not how sensitive detection is (BUER always monitors at full sensitivity).

| Level | Behaviour |
|-------|-----------|
| `high` | More alerts, including minor issues. Escalation threshold tightened. |
| `medium` | **Default.** Balanced — matches current BUER behaviour. Most users stay here. |
| `low` | Only important alerts. Minor issues reported to agent only, not pushed to you. |
| `silent` | No proactive user alerts for efficiency issues. See below. |

### Entry point: just tell Claude Code in plain language

```
"BUER is too noisy, notify me less"   → agent calls set_notification_level(..., "low")
"Silence BUER"                        → set_notification_level(..., "silent")
"More alerts, be stricter"            → set_notification_level(..., "high")
"Back to normal"                      → set_notification_level(..., "medium")
```

Or directly via MCP tool: `set_notification_level("/your/project", "low")`

### Silent mode: "don't interrupt" not "turn off"

`silent` means no proactive push to you — BUER keeps working:
- **Agent channel works**: post-edit reminders to the agent are unaffected
- **Pull tools work**: `check_drift` and `get_user_alerts` still return full data
- **Integrity signals always surface**: test-cheating (`test_tampering`), scope-violations
  (`boundary_breach`, `task_scope_breach`) escalate even in silent mode — you use BUER
  partly to catch agent misbehaviour; silencing those defeats the purpose

---

## Running BUER as a Service (Recommended)

For long-term cost data collection, BUER must be running continuously. CC telemetry is
silently discarded if the server is down when a session starts — there is no buffering
or retry across session boundaries.

A systemd service handles start-on-boot and crash recovery automatically.

### Install

1. **Edit `deploy/buer.service`**: replace `YOUR_USERNAME` and `BUER_EXEC` with your
   actual username and the absolute path to `buer-server`:
   ```bash
   whoami                  # → YOUR_USERNAME
   which buer-server       # → BUER_EXEC  (e.g. /home/youruser/.local/bin/buer-server)
   ```

2. **Install and start**:
   ```bash
   sudo cp deploy/buer.service /etc/systemd/system/buer.service
   sudo systemctl daemon-reload
   sudo systemctl enable buer    # start on boot
   sudo systemctl start buer
   systemctl status buer         # confirm Active: running
   ```

3. **Verify it's accepting metrics**:
   ```bash
   nc -z 127.0.0.1 7777 && echo "up"
   curl -s http://127.0.0.1:7777/buer/health | python3 -m json.tool
   ```

### Logs

```bash
journalctl -u buer -f              # live tail
journalctl -u buer --since today
```

### Why the CC session must start after BUER

OTLP SDK is initialised once at session start and points to the configured endpoint for
the life of that session. If BUER is down when CC starts, that session's telemetry is
routed to a closed port and permanently lost. Start BUER first, then start CC.

---

## Cost Tracking (optional)

BUER can receive Claude Code telemetry metrics and show a savings report.
**Privacy**: BUER only receives numeric metrics (tokens/cost) — no conversation content, no code.

### Configure Claude Code to send telemetry to BUER

Add to `~/.claude/settings.json` (or project-level `.claude/settings.json`):

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:7777"
  }
}
```

BUER's MCP server must be running (`buer-server --port 7777`) when Claude Code sessions start.

### View the savings report

Via MCP tool call (in Claude Code with BUER MCP configured):

```
savings_report("/path/to/your/project")
```

Or with model for cost estimation when no telemetry data is available yet:

```
savings_report("/path/to/your/project", model="claude-sonnet-4-6")
```

### Three-tier report output

| State | Output |
|---|---|
| OTel data available | Real measured cost + estimated intervention savings |
| No OTel, model provided | All-estimated from edit rounds × list price |
| No OTel, no model | Engineering proxy metrics (rounds/interventions), no USD |

The report clearly labels what is measured vs. estimated. Intervention savings are always marked
`(estimate assumes the loop would have continued; actual may differ)`.

For subscription plans (Max/Pro), cost reflects API-equivalent value, not direct billing.

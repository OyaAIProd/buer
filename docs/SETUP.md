# BUER Setup

How to install BUER, keep it running, and wire it into Claude Code.

## Two ways to run BUER (read this first)

BUER works in two modes, and they do different things. Knowing the difference avoids the
common surprise of "I added it from the registry but nothing happens."

- **MCP Registry / stdio mode** — when you add BUER from an MCP registry, your client
  (Claude Code, Cursor, etc.) launches it as a stdio MCP server. This exposes the *query
  tools* only: `check_drift`, `get_user_alerts`, `savings_report`, `set_notification_level`.
  The agent can ask BUER things, but BUER is **not** watching edits. This mode alone does
  **not** give you drift monitoring.
- **HTTP server + hooks mode** — the core value (catching loops, regressions, test-tampering
  on every edit) runs over BUER's HTTP endpoints, fed by Claude Code hooks. This needs a
  persistent BUER server and the hook config below. **This is the mode you want for monitoring.**

The two are complementary: keep the HTTP server running for monitoring; the MCP query tools
are a bonus. The rest of this guide sets up the HTTP + hooks mode.

---

## 1. Install

```bash
pip install buer
# or, isolated:
pipx install buer
```

This gives you the `buer-server` command. Confirm the path (you'll need it for the service):

```bash
which buer-server     # e.g. ~/.local/bin/buer-server
```

## 2. Start the server

```bash
buer-server --host 127.0.0.1 --port 7777 --db ~/.buer/store.sqlite
```

Note the DB path: `~/.buer/store.sqlite`, a fixed per-user location, **not** a per-project
`.buer/`. A persistent server has no project working directory, so it uses one shared store
and tells projects apart by the `cwd` each hook sends. The store schema is created on first start.

### Project registration is automatic

BUER has no explicit "register project" step. A project is registered automatically
the first time a hook fires from its directory — including `session-start`, which
registers the project and builds a structural baseline as soon as a session opens in
a hook-configured directory. Configuring the hooks in a project IS the registration
intent; no separate opt-in is needed. If you do not want a directory monitored, do not
configure BUER hooks for it.

Quick check once it's up:

```bash
curl -s http://127.0.0.1:7777/buer/health | python3 -m json.tool
```

## How BUER collects data (reliable disk-only judgment)

BUER never reads test results or crash info from command output (stdout): Claude Code
truncates hook payloads at 10K with a front-biased sample, and does not fire PostToolUse
on failed commands. All judgment data comes from disk files and short, never-truncated
hook fields:

**Always-available self-contained core** (works with no git, no tests, no XML):
structural drift — define_loop / stuck_region / churn / boundary — is detected by reading
your source files from disk on every edit. This is BUER's reliable foundation and carries
mid-execution intervention; it needs nothing external.

**Test-aware layer (requires JUnit XML)**: regression, debug_loop, and test-crash
correlation read test results and tracebacks exclusively from JUnit XML on disk. Without
XML these signals are inactive (BUER warns once when it detects a test ran but found no XML).
To enable them, install the buer-runtime package in your project venv (auto-emits XML and
crash.log — see below), or configure your test runner to write junit.xml
(e.g. pytest --junitxml=.pytest_cache/junit.xml).

**Crash correlation**: test crashes come from the JUnit XML traceback; non-test Python
crashes come from a crash.log written by buer-runtime's excepthook. Both are complete
on-disk files — never truncated stdout.

**Why not PostToolUseFailure**: BUER does not use the PostToolUseFailure hook. Its error
payload is head-truncated (10K) with a green-biased sample, so it cannot reliably convey
which tests failed. Failure data comes from JUnit XML instead.

(buer-runtime install: a zero-dependency package in your project venv that auto-emits
JUnit XML and crash.log. `pip install buer-runtime` in the venv where your tests run.
The BUER server stays isolated — buer-runtime does not pull server dependencies.)

## 3. Keep it running (recommended)

BUER must run continuously to monitor edits and collect cost data. Use your platform's
user-level service manager — **no root / admin needed**, and it survives reboots and crashes.

### Linux (systemd user service)

Create `~/.config/systemd/user/buer.service` (replace the `ExecStart` path with your
`which buer-server` result):

```ini
[Unit]
Description=BUER — AI coding-agent guardrail

[Service]
ExecStart=%h/.local/bin/buer-server --host 127.0.0.1 --port 7777 --db %h/.buer/store.sqlite
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now buer
loginctl enable-linger "$USER"   # start on boot without an active login session
systemctl --user status buer     # confirm Active: running
```

### macOS (launchd LaunchAgent)

Create `~/Library/LaunchAgents/io.github.zengxzh.buer.plist` (replace the `buer-server`
path with your `which buer-server` result):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>io.github.zengxzh.buer</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/.local/bin/buer-server</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>7777</string>
    <string>--db</string><string>/Users/YOU/.buer/store.sqlite</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

```bash
launchctl load -w ~/Library/LaunchAgents/io.github.zengxzh.buer.plist
launchctl list | grep buer       # confirm it's loaded
```

### Windows (Task Scheduler, runs at logon)

In PowerShell (replace the path with your `where buer-server` result; `pythonw` avoids a
console window):

```powershell
schtasks /create /tn "BUER" /sc onlogon /tr ^
  "buer-server --host 127.0.0.1 --port 7777 --db %USERPROFILE%\.buer\store.sqlite"
```

Windows works natively here — no WSL required, because the hooks below use HTTP, not shell
commands.

---

## 4. Wire Claude Code hooks (HTTP)

Add to `.claude/settings.json` in the project you want monitored (not inside `buer/` itself).
These are **HTTP hooks**: Claude Code sends the native event JSON straight to BUER's endpoints
as the POST body. No `curl`, no `jq`, no shell — so this is identical on Linux, macOS, and Windows.

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "http", "url": "http://127.0.0.1:7777/buer/session-start" }] }
    ],
    "PostToolUse": [
      { "matcher": "Edit|Write|MultiEdit", "hooks": [{ "type": "http", "url": "http://127.0.0.1:7777/buer/post-edit" }] },
      { "matcher": "Bash",                 "hooks": [{ "type": "http", "url": "http://127.0.0.1:7777/buer/post-bash" }] },
      { "matcher": "Read",                 "hooks": [{ "type": "http", "url": "http://127.0.0.1:7777/buer/post-read" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "http", "url": "http://127.0.0.1:7777/buer/stop" }] }
    ]
  }
}
```

> **Why HTTP and not a curl command?** Earlier versions of this guide built the payload with
> `jq` from environment variables. Claude Code 2.1+ no longer sets those variables — it passes
> the event as JSON on stdin (command hooks) or as the POST body (HTTP hooks). HTTP hooks need
> no shell tooling, work on every OS, and BUER's endpoints parse Claude Code's native event
> JSON directly. The old `jq`-based config silently produces empty payloads on current
> Claude Code and should be replaced with the above.

Optionally also expose the query tools over MCP, in `.claude/mcp.json`:

```json
{ "mcpServers": { "buer": { "type": "http", "url": "http://127.0.0.1:7777/mcp" } } }
```

---

## 5. How alerts reach you

Understanding this helps set expectations honestly.

- **Stop hook (primary, reliable).** When the agent tries to finish a turn, BUER's Stop hook
  returns a `decision:block` with the pending alerts as the reason. Claude Code injects that
  reason into the agent's context and asks it to address the issues before stopping. This is
  the main delivery path: alerts surface at the end of the turn they occurred in. The Stop
  hook fires at most once per stretch of work (an anti-loop guard), so it won't trap your session.
- **Notification level (your tuning knob).** See the section below. Controls how many alerts
  you get; integrity alerts (cheating, scope-violation) always surface regardless.
- **Pull, any time.** Ask in plain language ("any BUER alerts?") and the agent calls
  `get_user_alerts`, or call it directly.
- **Not real-time per edit.** BUER does **not** rely on injecting an alert immediately after
  each edit. Claude Code's post-edit context-injection (`additionalContext`) currently has
  open reliability bugs, so depending on it would be fragile. Alerts are delivered at turn end
  via Stop instead — slightly delayed, but dependable.

---

## Precise Test–Define Association (opt-in)

By default, BUER associates test cases with production defines using **heuristic matching**:
function-name similarity (`test_parse_query_string` → `parse_query_string`) plus file-stem
matching. This needs no configuration and is enough for most signal detection.

You can upgrade to **precise tier** by running pytest with dynamic context tracing, which reads
which lines each test actually executed and builds exact test↔define mappings:

```bash
pytest --cov=. --cov-context=test --cov-report= --junitxml=junit.xml
```

This writes a `.coverage` SQLite file at the project root; BUER reads it automatically on the
next reconcile. Run `--junitxml` and `--cov-context=test` in the same invocation (JUnit is
ingested first to build the test-case index).

Precise tier is **narrower** — it links a test to a define only when the test executed lines
inside it — which cuts false positives in `regression` and `test_tampering`. The `test_tier`
field in incident details records which tier fired (`"precise"` / `"heuristic"`).

**Boundaries.** Precise tier needs standard pytest rootdir layout (so the dotted-module form of
the coverage path matches the JUnit classname). It falls back to heuristic **silently** when the
`coverage` package is absent, `.coverage` is missing, a `src`/custom layout breaks the path↔classname
match, or JUnit classnames were customized. In every fallback case BUER keeps working via heuristic
tier — **precise is an enhancement, not a prerequisite.**

---

## Notification Level

The single user-facing tuning knob. Controls **how often BUER proactively alerts you** — detection
always runs at full sensitivity.

| Level | Behaviour |
|-------|-----------|
| `high` | More alerts, including minor issues. Escalation threshold tightened. |
| `medium` | **Default.** Balanced. Most users stay here. |
| `low` | Only important alerts. Minor issues kept in the queue, not pushed to you. |
| `silent` | No proactive alerts for efficiency issues (see below). |

Just tell Claude Code in plain language — "BUER is too noisy, notify me less" → the agent calls
`set_notification_level(..., "low")`. Or call the MCP tool directly:
`set_notification_level("/your/project", "low")`.

**`silent` means "don't interrupt", not "turn off".** Pull tools (`check_drift`, `get_user_alerts`)
still return full data, and **integrity signals always surface** — `test_tampering`, `boundary_breach`,
`task_scope_breach` escalate even in silent mode. You use BUER partly to catch agent misbehaviour;
silencing those would defeat the purpose.

---

## Cost Tracking (optional)

BUER can receive Claude Code telemetry and show a savings report. **Privacy:** only numeric metrics
(tokens/cost) — no conversation content or code — reach BUER. Add to `~/.claude/settings.json`:

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

BUER must be running when a Claude Code session starts (telemetry is dropped if the port is closed
at session start — start BUER first). View the report via the `savings_report("/path")` MCP tool;
pass `model="claude-sonnet-4-6"` for an estimate when no telemetry exists yet. The report labels
measured vs. estimated figures; for Max/Pro subscriptions, cost reflects API-equivalent value.

---

## Uninstall / opt out

BUER only changes two things on your machine, both reversible:

**The service** (if you set it up in step 3):

```bash
# Linux
systemctl --user disable --now buer
rm ~/.config/systemd/user/buer.service && systemctl --user daemon-reload
loginctl disable-linger "$USER"

# macOS
launchctl unload -w ~/Library/LaunchAgents/io.github.zengxzh.buer.plist
rm ~/Library/LaunchAgents/io.github.zengxzh.buer.plist

# Windows
schtasks /delete /tn "BUER" /f
```

**The hooks** — delete the `"hooks"` block you added to `.claude/settings.json` (and the
`buer` entry in `.claude/mcp.json` if you added it).

**The data** — `rm -rf ~/.buer` removes the local store. Then `pip uninstall buer`.

BUER never modifies your source code and never writes outside `~/.buer` and the config files
you edited above.

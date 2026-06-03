# BUER (不二)
<!-- mcp-name: io.github.zengxzh/buer -->

*One intent, undivided. BUER.*

[![License: BSL 1.1](https://img.shields.io/badge/License-BSL%201.1-blue.svg)](LICENSE)
[![Protocol: MCP](https://img.shields.io/badge/Protocol-MCP-orange.svg)](https://modelcontextprotocol.io)

BUER is a **local reliability and bidirectional guardrail layer** for AI coding agents.

Unlike traditional LLM observability tools that monitor *what the agent says* (traces, tokens, prompts), BUER cold-heartedly monitors **what the agent actually does to your code** (AST modifications, dependency graphs, git states, and test outcomes).

BUER acts as a local structure monitor that works via a two-way loop:

* **Alerts (Reactive):** Stops the agent when it loops, drifts out of bounds, or cheats (e.g., tampering with tests to hide broken code).
* **Assists (Proactive):** Injects structural facts (blast radius, dependency briefing, safety-net warnings) directly into the agent's context *before* an edit happens.

**Everything is 100% auto-derived.** BUER reads off objective code facts from what the agent already produces (file modifications, git state, test output). The agent is never asked to declare its intent or cooperate. It works perfectly even with chaotic agents and vibe coders.

BUER's test–define association starts from heuristic name matching (always available, no setup) and upgrades to **precise tier** when pytest is run with `--cov-context=test` — see [docs/SETUP.md](docs/SETUP.md#precise-testdefine-association-opt-in) for details.

---

## ⚡ Why BUER? (The Agent Observability Gap)

When an AI agent gets stuck or starts breaking things, traditional MLOps tools (Langfuse, Braintrust, Phoenix) only see a series of "successful 200ms LLM API responses." They are blind to the code layer.

BUER bridges this gap by operating directly on a local Directed Acyclic Graph ($\mathcal{G}_D$) maintained via `tree-sitter`.

| The AI Agent Blindspots | How BUER Solves It (100% Auto-Derived) |
| :--- | :--- |
| **P1: Infinite Loops & Churn** | `stuck_region` / `define_loop` / `token_waste` — detects when code state fails to structurally converge |
| **P2: "Fix one, break ten"** | `regression` — links test failures back to the structural edits via call graph; upgrades to exact test↔define mapping when pytest `--cov-context=test` is present |
| **P3: Cheating / Faking Success** | `test_tampering` — escalates *immediately* to humans if the agent mutates test assertions to bypass failures |
| **P4: Scope Drift / Running Amok** | `boundary_breach` + `task_scope_breach` — rigid local guardrails |
| **P5: Lost Context on Resume** | `BUER Recap` — plain-language summary of the previous session's changes and unresolved issues |
| **P6: Blind to Collateral Damage** | `influence cone` — injects pre-edit blast radius previews into the agent's context |
| **P7: Unprotected Codebase** | `safety-net warning` / `add-tests assist` / `commit assist` — flags risk and offers concrete next steps |

Every signal above is built on an **objective code-fact anchor** (version chain, test outcome, file path, call graph). BUER does not infer intent and does not score the agent's output — it reports what is observably true in the code.

---

## ☸️ Origin: The Non-Dual Gateway

BUER takes its name from **不二法门** (*bù èr fǎ mén*), a core teaching from the *Vimalakīrti Sūtra*. **不二** ("not-two") represents non-duality — transcending the split of reality into opposing contradictions (broken/working, this/that). When the bodhisattvas attempt to define this non-dual gateway with words, Vimalakīrti answers with absolute silence. The truest gateway is the one that no longer divides.

BUER brings this philosophy to AI software engineering. It is grounded in **Structural Determination Theory (SDT)**. The dependency graph BUER maintains — $\mathcal{G}_D$ — is a directed acyclic graph whose nodes are *determinations* (the current structural state of each definition) and whose edges map producers to consumers.

By analyzing this objective graph and its history, BUER keeps the agent anchored to a single, coherent, undivided intent instead of spinning into self-contradiction.

Reference: Zeng, Xiaozhou (2026). *Structural Determination Theory: A Single-Axiom Framework for Reality, Time, Irreversibility, and Space.* PhilArchive. <https://philpapers.org/rec/ZENSDT>

---

## 🛠️ How It Works: The Bidirectional Loop

BUER runs as a local background server alongside execution environments like Claude Code. It hooks into the workflow via lightweight `POST` endpoints triggered after relevant agent actions:

```
              ┌──────────────────────────────┐
              │   AI Agent (Claude Code)     │
              └──────────────┬───────────────┘
                             │
 1. Hooks (post-edit, etc.)  │  2. Proactive Context
 Triggered Automatically     │     (Blast Radius, Briefings)
                             ▼
              ┌──────────────────────────────┐
              │         BUER Server          │
              │  (AST / Git / Test Monitor)  │
              └──────────────────────────────┘
```

### The Endpoints

* `/buer/session-start` — fired upon session initialization or resumption
* `/buer/post-edit` — intercepts file deltas after an Edit, Write, or MultiEdit
* `/buer/post-read` — captures read activity to track context usage
* `/buer/post-bash` — captures terminal execution, parsing test outputs and stack traces
* `/buer/stop` — triggered when the agent concludes its turn
* `/v1/metrics` — bridges OpenTelemetry cost/token metrics (optional)

### 1. Alerts (The Defense)

When a signal fires, BUER employs a **two-step escalation**: it first silently nudges the agent to self-correct by injecting context. If the issue persists, it escalates to you. **Integrity violations (like `test_tampering` or boundary breaches) bypass the nudge and escalate to you immediately.**

* `stuck_region` — agent keeps pounding the same code block without convergence
* `define_loop` — agent reverts a definition back to a structurally equivalent earlier state
* `test_tampering` — agent changes assertions to pass tests rather than fixing the functional code

### 2. Assists (The Offense)

Architected intentionally with three distinct channels (*Alerts, Health hints, Inline assists*), BUER helps shape the agent's work before mistakes happen:

* `structural_briefing` — pre-edit context: who depends on this code, and what does it depend on?
* `debug_range` — narrows debugging down to recent changes intersecting with the crash stack
* `safety-net` — warns the agent if it's modifying a definition with a massive blast radius

---

## 💰 Cost & Savings Tracking (Optional)

BUER features an honest, telemetry-backed financial compiler. By bridging Claude Code's OpenTelemetry metrics (which transmit raw token counts but *exclude* conversational content or code payloads) at `/v1/metrics`, BUER calculates your real expenditure against estimated intervention savings across three transparent tiers:

* **Layer 1 (Telemetry-Backed):** real measured API costs matched with calculated intervention savings
* **Layer 2 (Model-Estimated):** estimated from local edit rounds multiplied by LLM list prices
* **Layer 3 (Proxy Metrics):** strict engineering indicators (churn counters, iteration loops) without financial figures

---

## ⚙️ Installation & Setup

BUER requires **Python ≥ 3.10**. It parses Python, JavaScript, and TypeScript locally via embedded `tree-sitter` grammars — no heavy external compiler toolchains are required for its core features.

```bash
git clone https://github.com/zengxzh/buer.git
cd buer
pip install -e .
```

### Running the Server

Start the monitor locally:

```bash
buer-server --host 127.0.0.1 --port 7777 --db .buer/store.sqlite
```

To configure your agent environment to send hooks to BUER, or to hook up systemd service persistent runtimes, see [docs/SETUP.md](docs/SETUP.md).

### Required: `.buer/` data protection

BUER stores all its state in a local `.buer/` directory at your project root (a SQLite database in WAL mode). To keep this data safe, **BUER automatically adds `.buer/` to your project's `.gitignore`** the first time it runs in a git repository — creating `.gitignore` if none exists, or appending to it otherwise (your existing entries are never modified).

BUER also **monitors `.gitignore`**: if the entry is removed or the file is deleted, BUER re-adds `.buer/` on the next edit or session. This is intentional and **required** — without it, routine git operations would destroy BUER's data:

- `git clean -fd` would delete the `.buer/` directory entirely.
- `git checkout` / `git reset --hard` would roll the database back (or, with WAL files partially reverted, corrupt it) — losing all accumulated structural history.

If you deliberately want to track `.buer/` in git, add an explicit `!.buer/` line to your `.gitignore`; BUER respects that and will not override it.

---

## 🔒 Security & Privacy

BUER is built for local-first, privacy-respecting development. **BUER runs 100% on your local machine.** It never uploads your source code, file structures, prompts, or credentials to any third-party cloud servers.

---

## Status

Alpha — core implementation complete (graph, signals, reconcile, assists, recap); active development and dogfooding ongoing.

---

## 📄 License

This project is licensed under the **Business Source License 1.1 (BSL-1.1)**.

* **Permissions:** free for evaluation, development, and personal or internal business use — including monitoring your own or your organization's AI coding agents.
* **Restrictions:** cannot be deployed as a commercial hosted or managed service to third parties, nor as a commercial product whose primary value derives from BUER, without a separate commercial license.
* On **2030-06-02** (Change Date), the license converts to **Apache License, Version 2.0**.

See [LICENSE](LICENSE) for full terms.

---

## Author

Xiaozhou Zeng <leozeng@gmail.com>
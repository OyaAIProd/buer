"""BUER MCP server — §4.4 hook integration + §4.7 check_drift fallback.

Serves two surfaces on a single HTTP process:

  MCP protocol  (streamable-http at /mcp by default)
    check_drift(project_root)   — agent-initiated incident query  (§4.7)
    get_user_alerts(project_root) — user-pull escalation queue

  Hook endpoint  (plain HTTP POST at /buer/post-edit)
    Receives Claude Code PostToolUse events (Edit|Write|MultiEdit).
    Calls reconcile → advance_incidents → returns agent-channel injection text.

R2 compliance: the hook payload carries only "which files were edited" (file_path
from tool_input).  reconcile(store, project_id, changed_files) signature is
unchanged; the server never reads or forwards edit_type / reasoning / predecessor.

Transport: streamable-http by default.  stdio also supported for Claude Code
MCP stdio configuration (hook path is HTTP-only; check_drift works over stdio).

Entry point: buer-server (declared in pyproject.toml)

Hook configuration (add to .claude/settings.json in your project root):

  {
    "hooks": {
      "PostToolUse": [{
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [{
          "type": "command",
          "command": "curl -s -X POST http://127.0.0.1:7777/buer/post-edit -H 'Content-Type: application/json' -d @-"
        }]
      }]
    }
  }

Other agent environments (Cursor, Cline, etc.) can call check_drift directly
via MCP tool use, or POST to /buer/post-edit after each edit.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import Response

from buer import assists, boundary, delivery, git_utils, health, navigator, parse, pricing, signals, ts_dataflow
from buer.callgraph import _SRC_PATTERNS
from buer.navigator import THETA_CLARITY_NODES
from buer.reconcile import rebuild_call_edges_full, reconcile, reconcile_against_disk
from buer.stdout_parser import is_test_command
from buer.store import Store

# ── module-level state (overridable for tests) ───────────────────────────────

_store_override: Store | None = None
_db_path: str = os.environ.get("BUER_DB", ".buer/store.sqlite")

# Form-C dedup: tracks (project_id, session_id) pairs that already received a
# structure guide hint this session.  In-memory; resets on server restart (= new session).
_structure_guide_given: set[tuple[int, str]] = set()

# High-impact caller warning dedup: (project_id, session_id, define_name).
# Each define warned at most once per session per server process.
_high_impact_warned: set[tuple[int, str, str]] = set()

# Crash injection dedup: fingerprint → last-inject timestamp (seconds).
# Prevents the same crash from being injected more than once within the window.
_crash_inject_seen: dict[str, float] = {}
_CRASH_DEDUP_WINDOW = 60.0
_MAX_INJECT = 12  # hard cap: show at most this many suspects to prevent flood

COVERAGE_THRESHOLD = 0.95
_full_ingest_in_progress: set = set()
_full_ingest_lock = threading.Lock()


def _get_store() -> Store:
    if _store_override is not None:
        return _store_override
    return Store(_db_path)


def _set_store_for_testing(store: Store | None) -> None:
    """Inject a test store.  Call with None to restore production behaviour."""
    global _store_override
    _store_override = store


# ── FastMCP instance ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "buer",
    instructions=(
        "BUER reliability layer for AI coding agents.  "
        "Use check_drift to inspect active incidents before each edit batch. "
        "Use get_user_alerts to retrieve escalation notifications."
    ),
    host="127.0.0.1",
    port=7777,
)


# ── MCP tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
def set_notification_level(project_root: str, level: str) -> str:
    """Set BUER notification level for a project (high / medium / low / silent).

    Controls only how often BUER proactively alerts the user.
    Detection is unaffected — BUER keeps monitoring at full sensitivity.

    Use this when the user says things like:
      "notify me less" / "too many alerts"  → level="low"
      "silence BUER" / "stop interrupting"  → level="silent"
      "more alerts" / "be stricter"          → level="high"
      "back to normal" / "default"           → level="medium"

    Silent mode: no proactive user alerts for efficiency issues (stuck loops,
    wasted tokens, regressions). Agent channel and pull tools (check_drift,
    get_user_alerts) still work. Integrity signals (test-cheating,
    scope-violation) always surface regardless of level.
    """
    from buer.sensitivity import level_description, NOTIFICATION_LEVELS
    if level not in NOTIFICATION_LEVELS:
        return (
            f"[BUER] Unknown level {level!r}. "
            f"Choose one of: {', '.join(sorted(NOTIFICATION_LEVELS))}"
        )
    store = _get_store()
    pid = store.find_project_for_file(project_root)
    if pid is None:
        return f"[BUER] No project registered for: {project_root}"
    store.set_notification_level(pid, level)
    return f"[BUER] Notification level set to '{level}'. {level_description(level)}"


@mcp.tool()
def get_notification_level(project_root: str) -> str:
    """Return current BUER notification level for the project.

    Levels: high / medium (default) / low / silent.
    Call this to confirm the current setting before adjusting.
    """
    from buer.sensitivity import level_description
    store = _get_store()
    pid = store.find_project_for_file(project_root)
    if pid is None:
        return f"[BUER] No project registered for: {project_root}"
    level = store.get_notification_level(pid)
    return f"[BUER] Notification level: '{level}'. {level_description(level)}"


@mcp.tool()
def check_drift(project_root: str) -> str:
    """Return active BUER incident summaries for the project at project_root.

    Non-destructive: does not mark deliveries as taken.  Intended as a
    before-edit sanity check so the agent can orient to any open signals
    before making further changes (§4.7 no-hook fallback).
    """
    store = _get_store()
    pid = store.find_project_for_file(project_root)
    if pid is None:
        return f"[BUER] No project registered for: {project_root}"
    incs = store.open_incidents(pid)
    if not incs:
        return "[BUER] No active incidents."
    parts = ["[BUER] Active incidents:"]
    for inc in incs:
        parts.append(delivery.agent_message(inc))
    return "\n\n---\n".join(parts)


@mcp.tool()
def get_user_alerts(project_root: str) -> str:
    """Retrieve and clear pending user-level escalation alerts.

    Destructive: marks taken deliveries so they are not returned again.
    Intended to be called by the user (or a UI layer) to pull escalations.
    """
    store = _get_store()
    pid = store.find_project_for_file(project_root)
    if pid is None:
        return f"[BUER] No project registered for: {project_root}"
    deliveries = store.take_user_deliveries(pid)
    if not deliveries:
        return "[BUER] No pending user alerts."
    return "\n\n---\n".join(d["message"] for d in deliveries)


@mcp.tool()
def project_overview(project_root: str) -> str:
    """Return a project structure overview for onboarding (§4.8).

    Shows file/define counts, most-active modules, hub nodes (highest
    call-graph in-degree), active signals, and safety-net status.
    """
    store = _get_store()
    pid = store.find_project_for_file(project_root)
    if pid is None:
        return f"[BUER] No project registered for: {project_root}"
    return health.project_overview(store, pid, project_root)


@mcp.tool()
def buer_recap(project_root: str) -> str:
    """Show the BUER Recap — a summary of what happened in the previous session
    for this project: files changed, issues still unresolved, and how many issues
    BUER flagged/resolved. Relative time included.

    Call this when the user asks to see the last session recap, e.g.:
      "show the buer recap" / "what did we do last session" / "where did we leave off"
    Returns a plain-language recap, or a note if there's no previous session.
    """
    from buer.session_report import build_recap
    store = _get_store()
    pid = store.find_project_for_file(project_root) or store.get_or_create_project(project_root)
    recap = build_recap(store, pid)
    return recap if recap else "No previous session to recap (this may be the project's first session, or the last one had nothing worth recording)."


@mcp.tool()
def set_task_scope(
    project_root: str,
    allowed_globs: list[str],
    forbidden_globs: list[str] | None = None,
) -> str:
    """Declare the current task scope for opt-in range checking (§2.3).

    allowed_globs: whitelist — glob patterns relative to project root that are
        in-scope, e.g. ["user/**"].  BUER flags edits outside this list.
    forbidden_globs: blacklist — patterns that are always out-of-scope even if
        they match an allowed pattern, e.g. ["**/secrets.py"].  Optional.
        forbidden takes priority over allowed.

    This is the only opt-in information BUER requires (§1.5 exception).  The
    user (or agent acting on user's behalf) declares WHAT to work on; BUER
    then enforces the boundary.  This is not agent-declared structural metadata
    (edit_type / reasoning / predecessor) — the R2 red line is preserved.
    """
    store = _get_store()
    pid = store.find_project_for_file(project_root)
    if pid is None:
        return f"[BUER] No project registered for: {project_root}"
    if not allowed_globs:
        return "[BUER] allowed_globs cannot be empty."
    store.set_task_scope(pid, allowed_globs, forbidden_globs=forbidden_globs or [])
    result = f"[BUER] Task scope set: {', '.join(allowed_globs)}"
    if forbidden_globs:
        result += f"; forbidden: {', '.join(forbidden_globs)}"
    return result


@mcp.tool()
def clear_task_scope(project_root: str) -> str:
    """Clear the current task scope (§2.3). BUER stops range-checking after this."""
    store = _get_store()
    pid = store.find_project_for_file(project_root)
    if pid is None:
        return f"[BUER] No project registered for: {project_root}"
    store.clear_task_scope(pid)
    return "[BUER] Task scope cleared."


@mcp.tool()
def dismiss_safety_net_warning(project_root: str, net_type: str) -> str:
    """Permanently silence a safety-net warning (§2.9).

    net_type: 'no_tests' | 'no_git'

    Use this when you have intentionally decided not to add tests or version
    control for this project and don't want BUER to keep warning about it.
    """
    if net_type not in ("no_tests", "no_git"):
        return f"[BUER] Unknown net_type '{net_type}'. Use 'no_tests' or 'no_git'."
    store = _get_store()
    pid = store.find_project_for_file(project_root)
    if pid is None:
        return f"[BUER] No project registered for: {project_root}"
    store.dismiss_safety_net_warning(pid, net_type)
    return f"[BUER] Safety-net warning '{net_type}' dismissed for {project_root}."


@mcp.tool()
def assist_add_tests(project_root: str, target: str = "") -> str:
    """List uncovered defines ranked by importance and inject a structured test task (§4.9).

    用户触发（非 BUER 自作主张）: BUER gives list + priority, agent writes the tests.
    Ranking: caller count (blast_radius) > change frequency > is_core.
    Flags obvious shell tests (no assert); does not judge semantic quality.
    也提示先 git commit 一个回退点再开始（联动 §4.10）.
    无 SDT 对位、纯工程辅助.
    """
    store = _get_store()
    pid = store.find_project_for_file(project_root)
    if pid is None:
        return f"[BUER] No project registered for: {project_root}"
    return assists.assist_add_tests(store, pid, project_root, target=target or None)


@mcp.tool()
def acknowledge_commit(project_root: str) -> str:
    """Record that a git commit was just made, resetting BUER commit-timing tracking (§4.10).

    Call this after each git commit so BUER knows the current seq is a stable checkpoint.
    Allows the commit-timing assist to fire again after the next cluster of edits settles.
    无 SDT 对位、纯工程辅助.
    """
    store = _get_store()
    pid = store.find_project_for_file(project_root)
    if pid is None:
        return f"[BUER] No project registered for: {project_root}"
    return assists.acknowledge_commit(store, pid)


@mcp.tool()
def structural_briefing(project_root: str, target: str) -> str:
    """Return pre-edit structural context for a specific define (v2.1 §2 形态 B).

    target: "file_path::define_name" (preferred) or just "define_name".

    Returns: callers (blast direction), callees (dependency direction),
    Γ_R structural relatives (shared history/downstream), and connected-component
    size.  All from already-built 𝒢_D / call_edges — no new computation triggered.

    Accuracy: callers/callees from call_edges (static analysis); may undercount
    for Python/JS dynamic dispatch.  Γ_R / component depend on gd_edge coverage.

    Agent usage: call before editing a critical define to understand its structural
    context.  The local map is approximate (dynamic-language edges may be missing).
    Precise judgement remains with you.
    """
    store = _get_store()
    pid = store.find_project_for_file(project_root)
    if pid is None:
        return f"[BUER] No project registered for: {project_root}"
    project = store.get_project(pid)
    root = project["root_path"]
    reconcile_against_disk(store, pid, root)
    file_path, define_name = navigator.resolve_target(store, pid, target)
    if file_path is None:
        return f"[BUER] Define not found in project: {target}"
    return navigator.structural_briefing_text(store, pid, root, file_path, define_name)


@mcp.tool()
def analyze_dangling(file_path: str) -> str:
    """Detailed dangling-reference analysis for a TS/TSX file (on-demand, runs tsc).

    Detects calls to symbols that don't exist in the project — hallucinated APIs,
    renamed functions, or import paths that no longer resolve.  Uses the project's
    own TypeScript compiler for precise resolution (~1.5 s/file).

    Requires TS toolchain: tsconfig.json + node_modules/typescript + node in PATH.
    Falls back gracefully if toolchain is unavailable.

    Use when you need precise unresolved-call detection for a specific file.
    Not needed for routine edits — the global graph uses faster tree-sitter extraction.
    """
    store = _get_store()

    pid = store.find_project_for_file(file_path)
    if pid is None:
        return f"[BUER] No project registered for file: {file_path}"

    project = store.get_project(pid)
    root = project["root_path"]

    lang = parse.detect_language(file_path)
    if lang not in ("typescript", "tsx"):
        return f"[BUER] analyze_dangling only supports .ts/.tsx files (got: {file_path})"

    toolchain = ts_dataflow.detect_ts_toolchain(root)
    if not toolchain["available"]:
        return (
            f"[BUER] TS toolchain unavailable: {toolchain['reason']}.\n"
            "Needs: tsconfig.json + node_modules/typescript + node in PATH."
        )

    try:
        defines = parse.extract_defines(file_path)
    except Exception as e:
        return f"[BUER] Could not parse {file_path}: {e}"

    if not defines:
        return f"[BUER] No defines found in {file_path}"

    all_unresolved: list = []
    degraded_defines: list[str] = []
    for d in defines:
        result = ts_dataflow.run_ts_dataflow_analysis(root, file_path, d.qualified_name)
        if result["degraded"]:
            degraded_defines.append(d.qualified_name)
        else:
            all_unresolved.extend(result.get("unresolved_calls", []))

    # Get current determinations so detect_dangling_reference can record observations.
    affected = []
    for d in defines:
        det = store.current_version_determination(pid, file_path, d.qualified_name)
        if det:
            affected.append((file_path, d.qualified_name, det["id"]))

    if affected and all_unresolved:
        signals.detect_dangling_reference(store, pid, affected, root, all_unresolved)

    # Format report
    rel = file_path.replace(root, "").lstrip("/")
    lines = [f"[BUER] analyze_dangling: {rel}",
             f"  defines analyzed : {len(defines)}"]
    if degraded_defines:
        lines.append(f"  tsc errors (degraded): {degraded_defines}")
    if all_unresolved:
        lines.append(f"  unresolved calls ({len(all_unresolved)}):")
        for uc in all_unresolved:
            lines.append(
                f"    {uc.get('caller_define','?')} → {uc.get('callee_text','?')}"
                f"  (line {uc.get('line','?')})"
            )
    else:
        lines.append("  unresolved calls : 0  (no dangling references detected)")
    return "\n".join(lines)


@mcp.tool()
def debug_range(project_root: str, recent_sessions: int = 1) -> str:
    """出错时给出 debug 范围：最近 N 次会话的变更 + 当前调用图影响锥 + 三维优先级。

    把 debug 从全项目缩小到"最近改动 + 受影响区域"这一小片，
    避免在数千个 define 里大海捞针。

    影响锥来自 call_edges（当前静态调用图），反映"谁现在调用了被改的函数"，
    而非历史 gd_edges（历史分析更适合 version_chain 查询）。

    影响锥 > 5 个节点时，额外输出三维优先级排序（各 top 5）：
      ① δ 依赖锥大小 — 集成最复杂的点（最可能暴露复杂失效）
      ② 入度 — 故障扩散最广的枢纽（失效影响最多调用方）
      ③ 距离 — 距被改函数最近的直接受影响方

    recent_sessions: 合并最近 N 次会话的变更范围（默认 1）。
    """
    import time
    from buer import callgraph, influence

    store = _get_store()
    pid = store.find_project_for_file(project_root)
    if pid is None:
        return f"[BUER] No project registered for: {project_root}"

    sessions = store.recent_sessions(pid, recent_sessions)
    if not sessions:
        return "[BUER] No sessions recorded for this project."

    project = store.get_project(pid)
    root = project["root_path"] if project else project_root

    reconcile_against_disk(store, pid, root)

    start_seq = min(s["start_seq"] for s in sessions)
    end_seq = max(
        s["end_seq"] if s["end_seq"] is not None else store.max_seq(pid)
        for s in sessions
    )
    changes = store.changes_in_range(pid, start_seq, end_seq)

    # Build FQNs for changed defines, then BFS caller cone with depth tracking.
    # call_edges are current (rebuilt by reconcile) — correct for real-time triage.
    changed_fqns: set[str] = set()
    for r in changes:
        if r["define_name"]:
            mod = callgraph.module_name_of(r["file_path"], root)
            changed_fqns.add(callgraph._lang_fqn(r["file_path"], mod, r["define_name"]))

    cone_with_depth = influence.caller_cone_with_depth(store, pid, list(changed_fqns))
    cone_fqns_set = set(cone_with_depth.keys())
    cone_fqns = sorted(cone_with_depth)
    n_cone = len(cone_fqns)

    MAX_SHOW = 30
    root_prefix = root + "/"

    lines = [
        f"[BUER] debug range — last {len(sessions)} session(s), "
        f"seq {start_seq}→{end_seq}"
    ]
    lines.append(f"\nrecent session changes ({len(changes)} defines):")
    for r in changes[:MAX_SHOW]:
        rel = r["file_path"].replace(root_prefix, "")
        lines.append(f"  {rel}:{r['define_name']}  ({r['edit_type']})")
    if len(changes) > MAX_SHOW:
        lines.append(f"  ... and {len(changes) - MAX_SHOW} more")

    # Stack intersection layer (Thm 11.10 constraint filter)
    recent_stacks = store.recent_crash_stacks(pid, n=3)
    if recent_stacks:
        crash_fqns_union: set[str] = set()
        for row in recent_stacks:
            try:
                crash_fqns_union.update(json.loads(row["stack_fqns"]))
            except Exception:
                pass
        suspects = influence.intersect_cone_with_stack(cone_fqns_set, crash_fqns_union)
        if suspects:
            lines.append(
                f"\n⚡ crash-path precise suspects ({len(suspects)} — both affected by changes and on the crash stack):"
            )
            for i, fqn in enumerate(sorted(suspects), 1):
                lines.append(f"  {i}. {fqn}")
            lines.append(
                f"  if precise suspects yield nothing: the bug may be in a downstream function affected by the change but not directly crashing."
            )
            lines.append(
                f"  full influence cone ({n_cone} nodes), sorted by dependency density/call breadth, is shown below."
            )
        else:
            lines.append(
                "\n⚠ crash path and influence cone do not intersect: the change may not have triggered this crash, "
                "or the crash path does not reach the influence cone."
            )
            lines.append(
                f"  full downstream of changes ({n_cone} nodes): see influence cone + priority ranking below."
            )

    # Three-dimensional priority ranking — only when cone is large enough to matter.
    if n_cone > 5:
        t0 = time.perf_counter()
        ranks = influence.cone_priority_ranks(store, pid, cone_with_depth)
        delta_ms = (time.perf_counter() - t0) * 1000

        lines.append(f"\npriority checks (δ computed in {delta_ms:.0f}ms):")
        lines.append("  ① most complex integration points (δ dependency-cone size↑ → more integration complexity):")
        for fqn, val in ranks["delta"]:
            lines.append(f"    {fqn}  δ={val}")
        lines.append("  ② widest blast-radius hubs (in-degree↑ → failure propagates further):")
        for fqn, val in ranks["indegree"]:
            lines.append(f"    {fqn}  in-degree={val}")
        lines.append("  ③ most directly affected (distance from changed function↓ → more direct):")
        for fqn, depth in ranks["distance"]:
            lines.append(f"    {fqn}  depth={depth}")

    lines.append(
        f"\ndownstream callers in influence cone ({n_cone} defines"
        " — not directly changed but calling changed functions):"
    )
    for fqn in cone_fqns[:MAX_SHOW]:
        lines.append(f"  {fqn}")
    if n_cone > MAX_SHOW:
        lines.append(f"  ... and {n_cone - MAX_SHOW} more")

    lines.append(
        f"\n→ focus debugging on these {len(changes) + n_cone} defines, not the full project."
    )
    return "\n".join(lines)


@mcp.tool()
def list_snapshots(project_root: str) -> str:
    """List BUER snapshots for the current branch (git integration).

    Each snapshot is bound to a git commit and marks the structural state at
    that point. Use this to find a known-good commit before diffing or to see
    the commit history BUER has tracked. Newest first.
    """
    store = _get_store()
    branch = git_utils.get_current_branch(project_root) if git_utils.is_git_repo(project_root) else None
    pid = (
        store.find_project_for_file(project_root, branch=branch)
        if branch
        else store.find_project_for_file(project_root)
    )
    if pid is None:
        return f"[BUER] No project registered for: {project_root}"
    snaps = store.list_snapshots_for_branch(pid)
    if not snaps:
        return "[BUER] No snapshots yet for this branch."
    lines = [f"[BUER] Snapshots for branch '{branch or '(none)'}' (newest first):"]
    for s in snaps:
        commit_short = (s["commit_hash"] or "")[:8]
        lines.append(
            f"  {commit_short}  {(s['reason'] or '?'):<14}  seq={s['snapshot_at_seq']}  {s['taken_at']}"
        )
    return "\n".join(lines)


@mcp.tool()
def diff_snapshots(project_root: str, commit_a: str, commit_b: str) -> str:
    """Compare the define-level structure between two commits' snapshots.

    Shows which defines were added, removed, or structurally changed (fingerprint
    differs — covers signature, calls, side-effects, size, literals) between
    commit_a and commit_b. Use this to narrow a debugging search: instead of
    re-reading both versions, see exactly which functions/classes changed shape.

    commit_a, commit_b: git commit hashes (full or prefix; prefix matched against
    tracked snapshots). Order: a = earlier/baseline, b = later/current.

    Note: edge-set diffs (call graph topology) are not included — call_edges are
    stored as current-state only. Define fingerprint changes do reflect changes
    in a define's own outgoing calls (calls are a fingerprint feature).
    """
    store = _get_store()
    branch = git_utils.get_current_branch(project_root) if git_utils.is_git_repo(project_root) else None
    pid = (
        store.find_project_for_file(project_root, branch=branch)
        if branch
        else store.find_project_for_file(project_root)
    )
    if pid is None:
        return f"[BUER] No project registered for: {project_root}"

    snaps = store.list_snapshots_for_branch(pid, limit=200)

    def resolve(cprefix: str):
        for s in snaps:
            if s["commit_hash"] and s["commit_hash"].startswith(cprefix):
                return s
        return None

    sa, sb = resolve(commit_a), resolve(commit_b)
    if sa is None:
        return (
            f"[BUER] No snapshot found for commit '{commit_a}'. "
            "Use list_snapshots to see tracked commits."
        )
    if sb is None:
        return (
            f"[BUER] No snapshot found for commit '{commit_b}'. "
            "Use list_snapshots to see tracked commits."
        )

    fa = store.define_fingerprints_at_seq(pid, sa["snapshot_at_seq"])
    fb = store.define_fingerprints_at_seq(pid, sb["snapshot_at_seq"])
    keys_a, keys_b = set(fa), set(fb)
    added = keys_b - keys_a
    removed = keys_a - keys_b
    changed = {k for k in (keys_a & keys_b) if fa[k] != fb[k]}

    if not (added or removed or changed):
        return (
            f"[BUER] No define-level structural changes between "
            f"{commit_a[:8]} and {commit_b[:8]}."
        )

    def fmt(k: tuple) -> str:
        return f"{k[0]}::{k[1]}"

    lines = [f"[BUER] Structural diff {commit_a[:8]} → {commit_b[:8]} (define-level):"]
    if changed:
        lines.append(f"\n  Changed structure ({len(changed)}):")
        for k in sorted(changed):
            lines.append(f"    ~ {fmt(k)}")
    if added:
        lines.append(f"\n  Added ({len(added)}):")
        for k in sorted(added):
            lines.append(f"    + {fmt(k)}")
    if removed:
        lines.append(f"\n  Removed ({len(removed)}):")
        for k in sorted(removed):
            lines.append(f"    - {fmt(k)}")
    lines.append("\n  (Edge-topology diff not included — see tool docs.)")
    return "\n".join(lines)


def _hook_json(event: str, text: str) -> Response:
    """Claude Code http-hook 合规响应。
    空 text → {}(无操作/无注入);非空 → hookSpecificOutput.additionalContext。
    event 取 'PostToolUse' 或 'SessionStart'。"""
    if not text:
        return Response("{}", media_type="application/json", status_code=200)
    payload = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    return Response(json.dumps(payload, ensure_ascii=False),
                    media_type="application/json", status_code=200)


# ── Hook endpoint ─────────────────────────────────────────────────────────────

@mcp.custom_route("/buer/post-edit", methods=["POST"])
async def post_edit_handler(request: Request) -> Response:
    """PostToolUse hook endpoint (§4.4).

    Receives JSON from Claude Code PostToolUse hook (Edit|Write|MultiEdit).
    Expected payload:
      {
        "tool_name":   "Edit",
        "tool_input":  {"file_path": "/abs/path/to/file.py", ...},
        "cwd":         "/abs/project/root"   # optional, used for project lookup fallback
      }

    Steps:
      1. Extract file_path from tool_input.
      2. Resolve project_id: find_project_for_file(file_path) first (longest-match,
         so nested sub-projects take priority over parent cwd).  If not found and
         cwd is present, auto-register cwd as a new project (idempotent).
         If neither yields a project → silent empty (no root to anchor on).
      3. reconcile(store, project_id, [file_path]) — derives everything from
         changed files; never reads edit_type/reasoning from the payload (R2).
      4. Collect newly-enqueued agent-channel deliveries.
      5. Return plain-text injection (empty string = no incidents this round).

    The response body (plain text) is what curl outputs to stdout, which
    Claude Code injects into the agent context.
    """
    try:
        body = await request.json()
    except Exception:
        return _hook_json("PostToolUse", "")

    tool_input = body.get("tool_input") or {}
    file_path = tool_input.get("file_path", "")
    cwd = body.get("cwd", "")

    if not file_path:
        return _hook_json("PostToolUse", "")

    store = _get_store()

    # Resolve project — longest-match first (nested projects resolve to deepest).
    # If not found and cwd available, auto-register cwd as a new project.
    # Never use file_path as root (one file ≠ one project).
    pid = store.find_project_for_file(file_path)
    if pid is None and cwd:
        pid = store.get_or_create_project(cwd)
    if pid is None:
        return _hook_json("PostToolUse", "")

    project = store.get_project(pid)
    root = project["root_path"] if project else cwd

    _check_gitignore_protection(root, store, pid)

    # Pre-edit reconciliation: catch external mutations (rm, git checkout, IDE edits)
    # that BUER was never notified about. Exclude file_path — it is handled by the
    # dedicated reconcile([file_path]) call below; excluding it prevents double-reconcile.
    # exclude is applied in both Stage 1 (stat) and Stage 2 (dir shallow-scan) so that
    # a Write-created file (which advances its dir's mtime) is not re-found by Stage 2.
    reconcile_against_disk(store, pid, root, exclude={file_path})

    # reconcile already calls advance_incidents internally (§4.6)
    # exclude_tests=False: allow test-file defines into affected so detect_test_tampering
    # can see them. Noise guard: stuck/debug/define_loop each have _is_excluded_path gates
    # that drop test-path defines before firing (added alongside this change).
    reconcile(store, pid, [file_path], exclude_tests=False)

    # Enqueue file for async structural recomputation (§4.0 v2.1)
    store.enqueue_recompute(pid, file_path)

    # Take newly-enqueued agent-channel deliveries (incident state machine)
    deliveries = store.take_agent_deliveries(pid)

    # High-impact caller warnings (neutral notices, not incidents, session-deduped)
    session_id = body.get("session_id", "")
    hi_warnings = signals.check_high_impact_defines(
        store, pid, file_path, session_id, _high_impact_warned
    )

    parts = [d["message"] for d in deliveries] + hi_warnings
    text = "\n\n".join(parts) if parts else ""
    return _hook_json("PostToolUse", text)


@mcp.custom_route("/buer/post-read", methods=["POST"])
async def post_read_handler(request: Request) -> Response:
    """PostToolUse Read|Grep|Glob hook — enqueue + optional form-C structural hint (§4.0 v2.1).

    Phase 1 (always): enqueue file for background recomputation.  Extremely
    lightweight (<5s constraint is tightest here — fires on every read).

    Phase 2 (form C, §2 形态 C): after enqueue, check clarity threshold and inject
    a one-time structural hint if the graph is clear enough.  All clarity checks are
    fast read-only queries (has_gd_edges = LIMIT 1; gd_node_count = COUNT DISTINCT;
    hub lookup = ORDER BY indegree LIMIT 5).  No graph computation in post-read.

    Clarity gate (all three must be true):
      (a) has_gd_edges — graph exists
      (b) gd_node_count >= THETA_CLARITY_NODES — graph is meaningful (not trivial)
      (c) (project_id, session_id) not in _structure_guide_given — given once per session

    Project resolution: same auto-register logic as post_edit_handler.
    find_project_for_file first (longest-match); if not found and cwd present,
    auto-register cwd.  cwd-only path (Grep/Glob) also auto-registers if needed.

    Payload: {"tool_name": "Read", "tool_input": {"file_path": "..."}, "cwd": "...", "session_id": "..."}
    Grep/Glob: tool_input may lack file_path — enqueues cwd as scan-activity marker.
    No cwd and no matching project → silent empty 200.
    """
    try:
        body = await request.json()
    except Exception:
        return _hook_json("PostToolUse", "")

    tool_input = body.get("tool_input") or {}
    file_path = tool_input.get("file_path", "")
    cwd = body.get("cwd", "")
    session_id = body.get("session_id", "")

    store = _get_store()
    pid: int | None = None

    if file_path:
        pid = store.find_project_for_file(file_path)
        if pid is None and cwd:
            pid = store.get_or_create_project(cwd)
        if pid is not None:
            store.enqueue_recompute(pid, file_path)
    elif cwd:
        # Grep/Glob: no single file_path — record cwd as scan-activity signal
        pid = store.find_project_for_file(cwd)
        if pid is None:
            pid = store.get_or_create_project(cwd)
        if pid is not None:
            store.enqueue_recompute(pid, cwd)

    # Form C: structural clarity check — inject guide once per (project, session).
    # Fast queries only; no graph computation here.
    if pid is not None:
        guide_key = (pid, session_id)
        if guide_key not in _structure_guide_given:
            if store.has_gd_edges(pid) and store.gd_node_count(pid) >= THETA_CLARITY_NODES:
                hint = navigator.structure_guide_hint(store, pid)
                if hint:
                    _structure_guide_given.add(guide_key)
                    return _hook_json("PostToolUse", hint)

    return _hook_json("PostToolUse", "")


@mcp.custom_route("/buer/session-start", methods=["POST"])
async def session_start_handler(request: Request) -> Response:
    """SessionStart hook — inject coarse structural overview into agent context (§4.0 v2.1).

    Payload: {"source": "startup"|"resume"|..., "cwd": "...", "session_id": "..."}

    source=resume is handled identically to startup — SessionStart re-fires on resume
    to refresh context, which is the correct behaviour (refresh the structural overview).

    Returns project_overview summary if 𝒢_D has been built (gd_edges non-empty).
    Returns empty if no graph yet — nothing accurate to show (宁漏不误报).

    Full connected-component global partition is batch 3a (§4.1).  This batch
    provides the project_overview-level summary as the placeholder until then.
    """
    try:
        body = await request.json()
    except Exception:
        return _hook_json("SessionStart", "")

    cwd = body.get("cwd", "")
    session_id = body.get("session_id", "")
    if not cwd:
        return _hook_json("SessionStart", "")

    store = _get_store()
    # Git-aware project resolution + missed-event fallback (git integration batch 4)
    git_branch = None
    git_head = None
    if git_utils.is_git_repo(cwd):
        git_branch = git_utils.get_current_branch(cwd)
        git_head = git_utils.get_head_commit(cwd)

    pid: int | None = None
    project_root = cwd
    if git_branch is not None:
        existing_pid = store.find_project_for_file(cwd, branch=git_branch)
        if existing_pid is not None:
            pid = existing_pid
            project = store.get_project(pid)
            project_root = project["root_path"] if project else cwd
            # Fallback: HEAD not seen before → missed commit/rollback/branch-switch
            if git_head and not store.has_snapshot_for_commit(pid, git_head):
                await _full_ingest_sync(pid, project_root, reason="session_start")
        else:
            # First time seeing (root, branch) — create project + full ingest
            pid = store.get_or_create_project(cwd, git_branch, created_at_commit=git_head)
            project_root = cwd
            await _full_ingest_sync(pid, project_root, reason="initial")
    else:
        pid = store.find_project_for_file(cwd)
        if pid is None:
            pid = store.get_or_create_project(cwd)  # branch=None for non-git
            project_root = cwd
            await _full_ingest_sync(pid, project_root, reason="initial")

    if pid is None:
        return _hook_json("SessionStart", "")

    _check_gitignore_protection(project_root, store, pid)

    # Record session boundary (best-effort — errors must not disrupt hook response).
    if session_id:
        try:
            store.open_session(pid, session_id)
        except Exception:
            pass

    # Carry-over delivery: if Stop hook didn't fire last session (abnormal exit),
    # pending user-channel deliveries surface here.  take() is destructive — delivered once.
    # alert-kind: were not blocked last session (abnormal exit) — prepend as urgent carry-over.
    # suggestion-kind: commit/blast hints — not urgent, append softly after overview.
    _pending_alerts = store.take_user_deliveries(pid, kinds=("alert",))
    _pending_suggestions = store.take_user_deliveries(pid, kinds=("suggestion",))
    if _pending_alerts:
        alert_prefix = (
            "[BUER] 上一轮结束时有未处理的检测告警，现在补送：\n\n"
            + "\n\n".join(d["message"] for d in _pending_alerts)
            + "\n\n"
        )
    else:
        alert_prefix = ""
    if _pending_suggestions:
        suggestion_suffix = (
            "\n\n[BUER] 上一轮遗留建议：\n\n"
            + "\n\n".join(d["message"] for d in _pending_suggestions)
        )
    else:
        suggestion_suffix = ""

    triggered = _maybe_trigger_full_ingest(store, pid, cwd)
    if not store.has_gd_edges(pid):
        if triggered:
            return _hook_json("SessionStart", alert_prefix + "buer: completing project structure coverage in the background; influence analysis will be more complete shortly." + suggestion_suffix)
        return _hook_json("SessionStart", alert_prefix + suggestion_suffix)

    overview = health.project_overview(store, pid, cwd)
    coarse_map = health.coarse_structure_map(store, pid, cwd)
    content = overview + coarse_map if coarse_map else overview
    if triggered:
        content += "\n(incomplete structural coverage detected; completing in background — next session will be more accurate.)"
    if git_branch is not None and not _buer_in_gitignore(project_root):
        content += "\n(tip: add .buer/ to .gitignore to prevent buer data from being affected by git operations.)"
    from buer.session_report import build_teaser
    teaser = build_teaser(store, pid)
    if teaser:
        content = content + "\n" + teaser if content else teaser
    return _hook_json("SessionStart", alert_prefix + content + suggestion_suffix)


def _scan_boundary_files(root: str) -> list[str]:
    root_real = os.path.realpath(root)
    out = []
    for pattern in _SRC_PATTERNS:
        for f in Path(root).rglob(pattern):
            fr = os.path.realpath(str(f))
            if boundary.should_ingest(fr, root_real) and not parse.is_test_file(str(f)):
                out.append(str(f))
    return out


def _coverage_ratio(store: Store, project_id: int, root: str) -> tuple[float, int, int]:
    boundary_files = _scan_boundary_files(root)
    boundary_real = {os.path.realpath(f) for f in boundary_files}
    no_def = store.get_no_define_count(project_id)
    denom = len(boundary_files) - no_def
    if denom <= 0:
        return (1.0, 0, 0)
    rows = store.con.execute(
        "SELECT DISTINCT file_path FROM determinations WHERE project_id=?",
        (project_id,),
    ).fetchall()
    covered = sum(
        1 for r in rows
        if os.path.exists(r["file_path"])
        and os.path.realpath(r["file_path"]) in boundary_real
    )
    return (covered / denom, covered, denom)


def _full_ingest_in_background(project_id: int, root: str, snapshot_reason: str | None = None) -> None:
    try:
        store = Store(_db_path)
        files = _scan_boundary_files(root)
        _BATCH = 50
        for i in range(0, len(files), _BATCH):
            batch = files[i : i + _BATCH]
            try:
                reconcile(store, project_id, batch)
            except Exception:
                pass
        # Second pass: rebuild call_edges with complete reexport table.
        # Batch reconcile writes reexport_edges incrementally; early batches
        # miss barrel-penetrated edges from barrels in later batches.
        rebuild_call_edges_full(store, project_id)

        no_def = 0
        for f in files:
            try:
                if not parse.extract_defines(f):
                    no_def += 1
            except Exception:
                no_def += 1
        store.set_no_define_count(project_id, no_def)
        # git integration: snapshot after reconcile (rollback / branch_switch)
        if snapshot_reason and git_utils.is_git_repo(root):
            try:
                head = git_utils.get_head_commit(root)
                if head:
                    branch = git_utils.get_current_branch(root)
                    parent = git_utils.get_parent_commit(root, head)
                    seq = store.max_seq(project_id)
                    store.create_snapshot(project_id, head, branch, seq, parent, reason=snapshot_reason)
            except Exception:
                pass
        store.close()
    except Exception:
        pass
    finally:
        with _full_ingest_lock:
            _full_ingest_in_progress.discard(project_id)


def _maybe_trigger_full_ingest(store: Store, project_id: int, root: str) -> bool:
    with _full_ingest_lock:
        if project_id in _full_ingest_in_progress:
            return False
    try:
        ratio, covered, denom = _coverage_ratio(store, project_id, root)
    except Exception:
        return False
    if denom > 0 and ratio < COVERAGE_THRESHOLD:
        with _full_ingest_lock:
            if project_id in _full_ingest_in_progress:
                return False
            _full_ingest_in_progress.add(project_id)
        threading.Thread(
            target=_full_ingest_in_background,
            args=(project_id, root),
            daemon=True,
        ).start()
        return True
    return False


def _trigger_full_ingest_with_snapshot(project_id: int, root: str, reason: str) -> None:
    """Schedule a full ingest in background, then create a snapshot with reason."""
    with _full_ingest_lock:
        if project_id in _full_ingest_in_progress:
            return
        _full_ingest_in_progress.add(project_id)
    threading.Thread(
        target=_full_ingest_in_background,
        args=(project_id, root, reason),
        daemon=True,
    ).start()


async def _full_ingest_sync(project_id: int, root: str, reason: str) -> None:
    """Session-start: block until baseline is complete before returning.

    Eliminates the race where the agent's first edit arrives before
    determinations exist and is absorbed as create instead of modify.
    """
    with _full_ingest_lock:
        if project_id in _full_ingest_in_progress:
            return
        _full_ingest_in_progress.add(project_id)
    await asyncio.to_thread(_full_ingest_in_background, project_id, root, reason)
    # _full_ingest_in_background's finally block handles discard from _full_ingest_in_progress


def _recompute_in_background(project_id: int, files: list[str]) -> None:
    """Background thread: reconcile queued files. Best-effort — errors suppressed."""
    try:
        store = Store(_db_path)
        for file_path in files:
            try:
                reconcile(store, project_id, [file_path])
            except Exception:
                pass
        store.close()
    except Exception:
        pass


@mcp.custom_route("/buer/stop", methods=["POST"])
async def stop_handler(request: Request) -> Response:
    """Stop/SubagentStop hook — async recompute trigger + decision:block alert (§4.0 v2.1).

    Payload: {"stop_hook_active": true|false, "cwd": "...", "session_id": "..."}

    CRITICAL: check stop_hook_active FIRST — if true, return {} immediately (allow).
    The Stop hook itself triggers another Stop when it produces output; stop_hook_active
    flags this re-entry to prevent infinite loop.

    stop_hook_active=false:
      (a) Drain pending_recompute queue → kick off background recompute thread.
          Background thread is non-blocking; endpoint returns before recompute finishes.
      (b) take_user_deliveries → if any pending, return {"decision":"block","reason":...}
          so Claude Code injects the alerts into agent context before it stops.
          Empty deliveries → return {} (allow stop).

    HTTP hook response semantics: {} = allow stop; {"decision":"block","reason":...} = block.
    All non-delivery paths return {} (standard allow) as application/json.
    """
    _ALLOW = Response("{}", media_type="application/json", status_code=200)

    try:
        body = await request.json()
    except Exception:
        return _ALLOW

    # Hard anti-loop guard (§0 §4.0 查证硬要求): second Stop must be allowed through.
    if body.get("stop_hook_active"):
        return _ALLOW

    cwd = body.get("cwd", "")
    session_id = body.get("session_id", "")
    if not cwd:
        return _ALLOW

    store = _get_store()
    pid = store.find_project_for_file(cwd)
    if pid is None:
        return _ALLOW

    # Close session boundary (best-effort).
    if session_id:
        try:
            store.close_session(pid, session_id)
        except Exception:
            pass

    # (a) Drain queue and kick off background recompute (non-blocking)
    files = store.drain_recompute_queue(pid)
    if files:
        threading.Thread(
            target=_recompute_in_background,
            args=(pid, files),
            daemon=True,
        ).start()

    # (b) Block stop on alert-kind deliveries only.
    # suggestion-kind (commit/blast hints) stay in queue for SessionStart — never block.
    deliveries = store.take_user_deliveries(pid, kinds=("alert",))
    if not deliveries:
        return _ALLOW

    _PREFIX = (
        "[BUER] 本轮检测到以下未处理问题。这是 BUER 的自动检测，不是用户拒绝。"
        "请先向用户说明或修复，再结束本轮：\n\n"
    )
    reason = _PREFIX + "\n\n".join(d["message"] for d in deliveries)
    return Response(
        json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False),
        media_type="application/json",
        status_code=200,
    )


def _compute_crash_injection(
    store: "Store",
    pid: int,
    root: str,
    stack_fqns_set: set[str],
) -> str:
    """Compute crash-injection text to feed back to the agent.

    Seed-priority logic:
      a. seed_hits = changed_fqns ∩ stack_fqns → "头号嫌疑" (changed fn is on the crash path)
      b. seed_hits empty, cone ∩ stack non-empty → "受害者" (downstream callers in crash stack)
      c. both empty → cone top-3 by δ as fallback hint
      Case 3: no recent changes → silent ""

    No hard top-N cut: returns all candidates (typically 1-2 from intersection).
    _MAX_INJECT=12 prevents flood on extreme stacks; "...及其他X个" appended when hit.
    All exceptions are swallowed; returns "" on error.
    """
    import time
    from buer import callgraph, influence

    try:
        sessions = store.recent_sessions(pid, 1)
        if not sessions:
            return ""
        sess = sessions[0]
        start_seq = sess["start_seq"]
        end_seq = sess["end_seq"] if sess["end_seq"] is not None else store.max_seq(pid)
        changes = store.changes_in_range(pid, start_seq, end_seq)
        if not changes:
            return ""

        changed_fqns: set[str] = set()
        for r in changes:
            if r["define_name"]:
                try:
                    mod = callgraph.module_name_of(r["file_path"], root)
                    changed_fqns.add(callgraph._lang_fqn(r["file_path"], mod, r["define_name"]))
                except Exception:
                    pass

        if not changed_fqns:
            return ""

        cone_with_depth = influence.caller_cone_with_depth(store, pid, list(changed_fqns))
        cone_fqns_set = set(cone_with_depth.keys())

        # Dedup: fingerprint = pid + sorted stack FQNs
        fp = f"{pid}:{','.join(sorted(stack_fqns_set))}"
        global _crash_inject_seen
        now = time.monotonic()
        last = _crash_inject_seen.get(fp, 0.0)
        if now - last < _CRASH_DEDUP_WINDOW:
            return ""
        _crash_inject_seen[fp] = now

        def _rank_by_delta(candidates: set[str], depth_map: dict[str, int]) -> list[str]:
            ranked = influence.cone_priority_ranks(
                store, pid,
                {fqn: depth_map.get(fqn, 0) for fqn in candidates},
                top_n=_MAX_INJECT,
            )
            return [fqn for fqn, _ in ranked["delta"]]

        def _format(header: str, candidates: set[str], depth_map: dict[str, int]) -> str:
            total = len(candidates)
            ranked = _rank_by_delta(candidates, depth_map) if total > 1 else list(candidates)
            lines = [header]
            for i, fqn in enumerate(ranked, 1):
                lines.append(f"  {i}. {fqn}")
            if total > _MAX_INJECT:
                lines.append(f"  ... and {total - _MAX_INJECT} more")
            return "\n".join(lines)

        # Branch a: seed_hits — changed functions that are directly on the crash stack
        seed_hits = changed_fqns & stack_fqns_set
        if seed_hits:
            return _format(
                "[BUER] ⚡ top crash suspect (function you just edited and on the crash stack):",
                seed_hits,
                {fqn: 0 for fqn in seed_hits},
            )

        # Branch b: victims — downstream callers of changed functions that are in crash stack
        victims = influence.intersect_cone_with_stack(cone_fqns_set, stack_fqns_set)
        if victims:
            return _format(
                "[BUER] ⚡ crash-path suspects (on the crash stack and calling functions you just edited):",
                victims,
                cone_with_depth,
            )

        # Branch c: no intersection — show cone top-3 as hint
        ranked = influence.cone_priority_ranks(store, pid, cone_with_depth, top_n=3)
        top3 = [fqn for fqn, _ in ranked["delta"]]
        if not top3:
            return "[BUER] crash stack does not directly intersect the changed region."
        lines = ["[BUER] crash stack does not directly intersect the changed region. Top 3 by priority within the influence cone (for reference):"]
        for i, fqn in enumerate(top3, 1):
            lines.append(f"  {i}. {fqn}")
        return "\n".join(lines)
    except Exception:
        return ""


def _is_git_commit(command: str) -> bool:
    """True if command is a git commit invocation (various forms). Conservative.

    Matches: git commit, git -C /p commit, git commit -m '...', git commit --amend
    Excludes: git log, git show, git status, git diff (contain 'commit' only in args/output)
    """
    parts = command.strip().split()
    if not parts or parts[0] != "git":
        return False
    i = 1
    while i < len(parts) and parts[i].startswith("-"):
        if parts[i] in ("-C", "-c"):
            i += 2
        else:
            i += 1
    return i < len(parts) and parts[i] == "commit"


def _buer_in_gitignore(root: str) -> bool:
    """True if .buer/ appears to be gitignored in root/.gitignore."""
    gi = os.path.join(root, ".gitignore")
    if not os.path.isfile(gi):
        return False
    try:
        with open(gi, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            # Explicit opt-out "!.buer/" — respect user intent, treat as handled.
            if line.startswith("!") and line.lstrip("!").strip().rstrip("*").rstrip("/") == ".buer":
                return True
            # Normalise: strip leading / and **/, then strip trailing /* /** /
            core = line.lstrip("/")
            if core.startswith("**/"):
                core = core[3:]
            core = core.rstrip("*").rstrip("/")
            if core == ".buer":
                return True
    except Exception:
        pass
    return False


def _gitignore_seen_mtime_path(root: str) -> str:
    return os.path.join(root, ".buer", ".gitignore_seen_mtime")


def _gitignore_changed(root: str) -> bool:
    """Return True (and persist new mtime) when .gitignore changed since last check."""
    gi = os.path.join(root, ".gitignore")
    try:
        cur = str(os.stat(gi).st_mtime) if os.path.isfile(gi) else "ABSENT"
    except OSError:
        cur = "ABSENT"
    seen_path = _gitignore_seen_mtime_path(root)
    try:
        with open(seen_path, "r") as f:
            seen = f.read().strip()
    except Exception:
        seen = None
    if cur == seen:
        return False
    try:
        os.makedirs(os.path.dirname(seen_path), exist_ok=True)
        with open(seen_path, "w") as f:
            f.write(cur)
    except Exception:
        pass
    return True


def _ensure_buer_gitignored(root: str) -> bool:
    """Ensure .buer/ is gitignored in a git project. Idempotent, append-only,
    never rewrites user content. No-op for non-git projects. Degrades silently.
    Returns True if .gitignore was written, False otherwise."""
    try:
        if not os.path.isdir(os.path.join(root, ".git")):
            return False
        if _buer_in_gitignore(root):
            return False
        gi = os.path.join(root, ".gitignore")
        block = "# BUER monitoring data (auto-added by BUER)\n.buer/\n"
        if not os.path.isfile(gi):
            with open(gi, "w", encoding="utf-8") as f:
                f.write(block)
        else:
            with open(gi, "r", encoding="utf-8", errors="ignore") as f:
                existing = f.read()
            prefix = "" if existing.endswith("\n") or existing == "" else "\n"
            with open(gi, "a", encoding="utf-8") as f:
                f.write(prefix + "\n" + block)
        return True
    except Exception:
        return False


def _check_gitignore_protection(root: str, store: "BuerStore", project_id: int | None) -> None:
    """mtime-gated .gitignore protection. Skips the check when .gitignore is unchanged.
    After writing, syncs dir mtime to prevent a spurious reconcile_against_disk hit."""
    try:
        if not _gitignore_changed(root):
            return
        wrote = _ensure_buer_gitignored(root)
        if wrote and project_id is not None:
            try:
                store.set_dir_mtime(project_id, os.path.realpath(root), os.stat(root).st_mtime)
            except Exception:
                pass
    except Exception:
        pass


def _git_subcommand(command: str) -> tuple[str, list[str]] | None:
    """Return (subcommand, rest_args) if command is a git invocation, else None.

    Skips global options (-C path, -c key=val) before the subcommand.
    """
    parts = command.strip().split()
    if not parts or parts[0] != "git":
        return None
    i = 1
    while i < len(parts) and parts[i].startswith("-"):
        if parts[i] in ("-C", "-c"):
            i += 2
        else:
            i += 1
    if i >= len(parts):
        return None
    return (parts[i], parts[i + 1 :])


def _is_git_rollback(command: str) -> bool:
    """True if command changes the working tree in a rollback-like way.

    Covers: reset (non-soft), revert, rebase, merge, pull, stash pop/apply,
    restore, clean, checkout -- <file>.
    Excludes: reset --soft (index only, no file changes).
    """
    result = _git_subcommand(command)
    if result is None:
        return False
    cmd, rest = result
    if cmd == "reset":
        return "--soft" not in rest
    if cmd in ("revert", "rebase", "merge", "pull"):
        return True
    if cmd == "stash":
        return bool(rest) and rest[0] in ("pop", "apply")
    if cmd in ("restore", "clean"):
        return True
    if cmd == "checkout":
        return "--" in rest
    return False


def _is_git_branch_switch(command: str) -> str | None:
    """Return a non-None marker if command switches branches, else None.

    Covers: git checkout <branch>, git checkout -b <new>, git switch <branch>.
    Does NOT match checkout -- <file> (that's a rollback).
    Note: the returned value is a best-effort parse; callers should use
    git_utils.get_current_branch() after execution for the authoritative branch.
    """
    result = _git_subcommand(command)
    if result is None:
        return None
    cmd, rest = result
    if cmd == "switch":
        args = [a for a in rest if not a.startswith("-")]
        return args[0] if args else None
    if cmd == "checkout":
        if "--" in rest:
            return None  # file restore, not branch switch
        args = [a for a in rest if not a.startswith("-")]
        return args[0] if args else None
    return None


def _missing_xml_should_warn(store, pid: int) -> bool:
    """One-shot warning: True only the first time a test ran without JUnit XML.
    Permanently silent after warning once (configuring XML stops the warning;
    no reset — avoids nagging on a stray no-XML scan window)."""
    state = store.get_assist_state(pid)
    if state and state["xml_missing_warned"]:
        return False
    store.update_assist_state(pid, xml_missing_warned=1)
    return True


@mcp.custom_route("/buer/post-bash", methods=["POST"])
async def post_bash_handler(request: Request) -> Response:
    """PostToolUse Bash hook endpoint — crash detection + test capture (§4.4 opt-in).

    Receives JSON from Claude Code PostToolUse hook for Bash tool events.

    Real Claude Code payload (verified empirically, v2.1.145):
      {
        "session_id":      "...",
        "transcript_path": "...",
        "cwd":             "/abs/project/root",
        "hook_event_name": "PostToolUse",
        "tool_name":       "Bash",
        "tool_input":      {"command": "...", "description": "..."},
        "tool_response":   {           ← dict, NOT string
          "stdout":        "... merged stdout+stderr ...",
          "stderr":        "",         ← always empty; CC merges into stdout
          "interrupted":   false,
          "isImage":       false,
          "noOutputExpected": false
        },
        "tool_use_id":     "...",
        "duration_ms":     ...
      }

    Key finding: CC merges stderr into tool_response.stdout — crash stack traces
    written to stderr always appear in tool_response.stdout, not stderr.
    Code handles both dict (real) and string (legacy/test) tool_response formats.

    Two independent features:
      1. Crash stack detection (all commands): if output contains a stack trace,
         parse source FQNs, store them in crash_stacks (best-effort), and compute
         cone ∩ stack suspects to inject back into agent context.
      2. Test run recording (test commands only): run-level totals, dedup-guarded.

    Response body: crash injection text (if any), otherwise empty.
    Claude Code PostToolUse stdout injection delivers it into agent context.
    """
    try:
        body = await request.json()
    except Exception:
        return _hook_json("PostToolUse", "")

    tool_input = body.get("tool_input") or {}
    command = tool_input.get("command", "")
    cwd = body.get("cwd", "")

    output = body.get("tool_response", "") or body.get("output", "")
    if isinstance(output, dict):
        output = output.get("output", "") or output.get("stdout", "")
    output = str(output) if output else ""

    if not output:
        return _hook_json("PostToolUse", "")

    store = _get_store()
    pid = store.find_project_for_file(cwd) if cwd else None
    if pid is None:
        return _hook_json("PostToolUse", "")

    inject_text = ""

    # Feature 1: crash stack detection (any command, best-effort)
    from buer import stacktrace as _st
    if _st.has_stack_trace(output):
        project = store.get_project(pid)
        root = project["root_path"] if project else (cwd or "")
        fqns = _st.stack_fqns(output, root)
        error_sig = _st.normalize_error_signature(output)
        if fqns:
            try:
                store.insert_crash_stack(
                    project_id=pid,
                    seq=store.max_seq(pid) or None,
                    stack_fqns_json=json.dumps(sorted(fqns)),
                    command=command[:500] if command else None,
                    error_signature=error_sig,
                )
            except Exception:
                pass
            inject_text = _compute_crash_injection(store, pid, root, fqns)

    # Feature 2: test-run detection (command string only — reliable, never truncated).
    # Test RESULTS come exclusively from JUnit XML (testscan), never from stdout:
    # stdout is a truncated payload field (CC head-truncates at 10K with a green-biased
    # sample on failure). Here we only DETECT that a test command ran (is_test_command
    # reads the short command field). If no JUnit XML was scanned recently, warn ONCE
    # that test-aware signals (regression/debug_loop/test-crash) are inactive without XML.
    if command and is_test_command(command):
        if not store.recent_xml_run_exists(pid, within_seconds=120):
            if _missing_xml_should_warn(store, pid):
                xml_warn = (
                    "[BUER] a test run was detected, but no JUnit XML was found. "
                    "BUER reads test results only from JUnit XML (command output is unreliable/truncated), "
                    "so regression / debug_loop / test-crash detection are NOT active. "
                    "Install the pytest-buer plugin (auto-emits XML) or configure your test runner "
                    "to write junit.xml (e.g. pytest --junitxml=.pytest_cache/junit.xml)."
                )
                if inject_text:
                    inject_text += "\n" + xml_warn
                else:
                    inject_text = xml_warn

    # Feature 3: git commit → create snapshot (git integration batch 2)
    if command and _is_git_commit(command):
        try:
            project = store.get_project(pid)
            root = project["root_path"] if project else (cwd or "")
            if root and git_utils.is_git_repo(root):
                head = git_utils.get_head_commit(root)
                if head:
                    branch = git_utils.get_current_branch(root)
                    parent = git_utils.get_parent_commit(root, head)
                    seq = store.max_seq(pid)
                    store.create_snapshot(pid, head, branch, seq, parent, reason="commit")
                    store.update_assist_state(pid, last_commit_seq=seq, last_commit_suggest_defines="")
        except Exception:
            pass  # snapshot failure must not affect post_bash main flow

    # Feature 4: rollback / branch-switch → full reconcile + snapshot (git batch 3)
    if command:
        try:
            project = store.get_project(pid)
            root = project["root_path"] if project else (cwd or "")
            if root and git_utils.is_git_repo(root):
                if _is_git_rollback(command):
                    cur_branch = git_utils.get_current_branch(root)
                    rb_pid = store.get_or_create_project(root, cur_branch)
                    _trigger_full_ingest_with_snapshot(rb_pid, root, reason="rollback")
                elif _is_git_branch_switch(command) is not None:
                    new_branch = git_utils.get_current_branch(root)
                    head = git_utils.get_head_commit(root)
                    bs_pid = store.get_or_create_project(root, new_branch, created_at_commit=head)
                    _trigger_full_ingest_with_snapshot(bs_pid, root, reason="branch_switch")
        except Exception:
            pass  # batch 3 failure must not affect post_bash main flow

    return _hook_json("PostToolUse", inject_text)


# ── OTLP metrics receiver ─────────────────────────────────────────────────────

@mcp.custom_route("/v1/metrics", methods=["POST"])
async def otlp_metrics_handler(request: Request) -> Response:
    """OTLP/http/json metrics receiver for Claude Code telemetry.

    Accepts ExportMetricsServiceRequest (OTLP JSON encoding).
    Extracts claude_code.cost.usage and claude_code.token.usage datapoints.
    Bridges session_id → project via sessions table (session.id OTel attribute
    value == sessions.session_id stored by hook handlers — same Claude Code UUID).
    Privacy: numeric metrics only — no conversation content, no file paths.
    Returns 200 with empty body; OTLP clients retry on non-2xx.
    """
    try:
        body = await request.json()
    except Exception:
        return _hook_json("PostToolUse", "")
    try:
        _parse_otlp_metrics(_get_store(), body)
    except Exception:
        pass  # telemetry ingestion must never disrupt other endpoints
    return _hook_json("PostToolUse", "")


def _otel_attrs(attr_list: list) -> dict[str, str]:
    """Convert OTLP attribute list → flat dict (string values only)."""
    result: dict[str, str] = {}
    for attr in attr_list:
        key = attr.get("key", "")
        val = attr.get("value", {})
        # OTLP JSON uses camelCase value wrappers
        if "stringValue" in val:
            result[key] = val["stringValue"]
        elif "intValue" in val:
            result[key] = str(val["intValue"])
        elif "doubleValue" in val:
            result[key] = str(val["doubleValue"])
    return result


def _parse_otlp_metrics(store: Store, body: dict) -> None:
    """Parse OTLP/json body and persist cost/token samples, one row per (session, model)."""
    # Accumulate per (session_id, model) so each OTLP batch produces one row.
    acc: dict[tuple[str, str], dict] = {}

    for rm in body.get("resourceMetrics", []):
        res_attrs = _otel_attrs(rm.get("resource", {}).get("attributes", []))

        for sm in rm.get("scopeMetrics", []):
            for metric in sm.get("metrics", []):
                name = metric.get("name", "")
                if name not in ("claude_code.cost.usage", "claude_code.token.usage"):
                    continue

                # Claude Code uses sum (monotonic counter) for both metrics
                dps = (
                    metric.get("sum", {}).get("dataPoints", [])
                    or metric.get("gauge", {}).get("dataPoints", [])
                )
                for dp in dps:
                    dp_attrs = _otel_attrs(dp.get("attributes", []))
                    # session.id follows OTel semconv (dot notation).
                    # It may live in dataPoint attrs or resource attrs depending on SDK version.
                    session_id = dp_attrs.get("session.id") or res_attrs.get("session.id", "")
                    model = dp_attrs.get("model", "")
                    if not session_id:
                        continue

                    key = (session_id, model)
                    if key not in acc:
                        acc[key] = {
                            "session_id": session_id,
                            "model": model,
                            "cost_usd": 0.0,
                            "input": 0,
                            "output": 0,
                            "cache_read": 0,
                            "cache_creation": 0,
                        }

                    raw = dp.get("asDouble") or dp.get("asInt") or 0
                    value = float(raw)

                    if name == "claude_code.cost.usage":
                        acc[key]["cost_usd"] += value
                    elif name == "claude_code.token.usage":
                        token_type = dp_attrs.get("type", "")
                        if token_type == "input":
                            acc[key]["input"] += int(value)
                        elif token_type == "output":
                            acc[key]["output"] += int(value)
                        elif token_type == "cacheRead":
                            acc[key]["cache_read"] += int(value)
                        elif token_type == "cacheCreation":
                            acc[key]["cache_creation"] += int(value)

    for entry in acc.values():
        if entry["cost_usd"] > 0 or entry["input"] > 0 or entry["output"] > 0:
            store.record_cost_sample(
                session_id=entry["session_id"],
                model=entry["model"],
                cost_usd=entry["cost_usd"],
                input_tokens=entry["input"],
                output_tokens=entry["output"],
                cache_read_tokens=entry["cache_read"],
                cache_creation_tokens=entry["cache_creation"],
            )


# ── savings_report MCP tool ───────────────────────────────────────────────────

_SUBSCRIPTION_NOTE = (
    "Note: for subscription plans (Max/Pro), cost reflects API-equivalent value,\n"
    "  not direct billing. See https://anthropic.com/pricing"
)
_TELEMETRY_HINT = (
    "To track real cost, point CLAUDE_CODE_ENABLE_TELEMETRY to BUER:\n"
    "  settings.json env: CLAUDE_CODE_ENABLE_TELEMETRY=1, OTEL_METRICS_EXPORTER=otlp,\n"
    "  OTEL_EXPORTER_OTLP_PROTOCOL=http/json, OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:7777"
)
_DIAG_CHECKLIST = (
    "No cost data recorded. Checklist:\n"
    "  1. Is BUER server running?  →  systemctl status buer\n"
    "                             or:  nc -z 127.0.0.1 7777 && echo up\n"
    "  2. Telemetry enabled?  →  CLAUDE_CODE_ENABLE_TELEMETRY=1 in ~/.claude/settings.json\n"
    "  3. Protocol http/json, endpoint 7777 (NOT grpc/4317)?\n"
    "  4. Did the CC session start AFTER the BUER server?\n"
    "       (OTLP SDK initialises at session start; server must be up first)\n"
    "\n"
) + _TELEMETRY_HINT
_AVOIDED_ROUNDS_CONSERVATIVE = signals.THETA_WASTE // 3


@mcp.tool()
def savings_report(project_root: str, model: str = "") -> str:
    """Show BUER's cost/savings report for the project.

    Three-tier fallback based on available data:
    - Layer 1 (OTel telemetry present): real measured cost + estimated intervention savings
    - Layer 2 (no OTel, model provided): all-estimated from edit rounds × list price
    - Layer 3 (no OTel, no model): engineering proxy metrics only, no USD

    model: optional Claude model name for layer-2 estimation
      (e.g. 'claude-sonnet-4-6'). Ignored when OTel data is available.
    """
    store = _get_store()
    pid = store.find_project_for_file(project_root)
    if pid is None:
        return f"[BUER] No project registered for: {project_root}"

    project_name = Path(project_root).name
    samples = store.cost_summary_for_project(pid)

    if samples:
        return _savings_layer1(store, pid, project_name, samples)
    elif model and pricing.lookup(model):
        return _savings_layer2(store, pid, project_name, model)
    else:
        return _savings_layer3(store, pid, project_name, model)


def _savings_layer1(store: Store, pid: int, project_name: str, samples: list[dict]) -> str:
    """Layer 1: real measured cost (OTel) + estimated intervention savings."""
    total_cost = sum(s["total_cost"] for s in samples)
    lines = [
        f"BUER Savings Report — {project_name}",
        f"Actual cost this project (measured via telemetry): ${total_cost:.4f}",
        "  by model:",
    ]
    for s in samples:
        lines.append(f"    {s['model'] or 'unknown'}: ${s['total_cost']:.4f}"
                     f" ({s['sample_count']} sample(s))")
        if s["total_cache_read"]:
            lines.append(f"      cache read: {s['total_cache_read']:,} tokens"
                         " (reuse already counted in cost above)")

    savings_lines, total_est = _estimate_intervention_savings(store, pid, total_cost)
    if savings_lines:
        lines.append(f"\nEstimated savings from BUER interventions: ~${total_est:.4f} (estimate)")
        lines.extend(savings_lines)
        lines.append("  (estimate assumes loops would have continued; actual may differ)")

    lines.append(f"\n{_SUBSCRIPTION_NOTE}")
    return "\n".join(lines)


def _savings_layer2(store: Store, pid: int, project_name: str, model: str) -> str:
    """Layer 2: all-estimated from edit rounds × list price. No OTel data."""
    total_det = store.max_seq(pid)
    cost_per_round = pricing.estimate_round_cost(model)
    est_total = total_det * cost_per_round

    lines = [
        f"BUER Savings Report — {project_name} (ESTIMATED — no telemetry)",
        f"Based on edit activity × {model} list prices",
        f"  ({pricing.PRICING_NOTE})",
        f"Estimated cost: ~${est_total:.4f}"
        f" ({total_det} edit rounds × ~${cost_per_round:.5f}/round; rough)",
    ]

    incs = store.resolved_intervention_incidents(pid)
    if incs and total_det > 0:
        lines.append("\nEstimated savings from BUER interventions (rough):")
        total_est = 0.0
        for inc in incs:
            det = json.loads(inc["details"]) if inc["details"] else {}
            rounds = det.get("rounds", 0)
            est_saved = _AVOIDED_ROUNDS_CONSERVATIVE * cost_per_round
            total_est += est_saved
            lines.append(
                f"  {inc['signal']} on {inc['target_node'] or 'unknown'}:"
                f" flagged at round {rounds}"
                f" → est. ~${est_saved:.4f} avoided"
            )
        lines.append(f"  Total estimated savings: ~${total_est:.4f}")
        lines.append("  (estimate assumes loops would have continued; actual may differ)")

    lines.append(
        "\nThis is a rough estimate from engineering proxies (edit rounds),"
        "\nNOT real token billing."
    )
    lines.append(f"\n{_TELEMETRY_HINT}")
    return "\n".join(lines)


def _savings_layer3(store: Store, pid: int, project_name: str, model: str) -> str:
    """Layer 3: engineering proxy metrics only. No USD, no model pricing."""
    total_det = store.max_seq(pid)

    busy = store.con.execute(
        """SELECT define_name, file_path, COUNT(*) as edits
           FROM determinations WHERE project_id = ? AND define_name IS NOT NULL
           GROUP BY define_name, file_path ORDER BY edits DESC LIMIT 5""",
        (pid,),
    ).fetchall()

    incs = store.resolved_intervention_incidents(pid)

    lines = [
        f"BUER Savings Report — {project_name} (proxy metrics only — no telemetry)",
        f"Edit activity: {total_det} total edit rounds recorded",
    ]
    if busy:
        lines.append("Most-revised defines:")
        for r in busy:
            lines.append(f"  {r['define_name']} ({r['file_path']}): {r['edits']} rounds")
    if incs:
        lines.append(f"BUER interventions (resolved): {len(incs)}"
                     " (define_loop / token_waste)")

    if model:
        lines.append(
            f"\nModel '{model}' not found in pricing table."
            " Try a known model (e.g. claude-sonnet-4-6) for cost estimates."
        )
    else:
        lines.append(
            "\nProvide model= (e.g. savings_report(model='claude-sonnet-4-6'))"
            " for a cost estimate."
        )
    lines.append(_DIAG_CHECKLIST)
    return "\n".join(lines)


def _estimate_intervention_savings(
    store: Store, pid: int, total_cost: float
) -> tuple[list[str], float]:
    """Estimate USD avoided by BUER interventions (conservative lower bound)."""
    incs = store.resolved_intervention_incidents(pid)
    if not incs:
        return [], 0.0

    total_det = store.max_seq(pid)
    if total_det == 0 or total_cost == 0:
        return [], 0.0

    cost_per_round = total_cost / total_det
    lines = []
    total_est = 0.0

    for inc in incs:
        det = json.loads(inc["details"]) if inc["details"] else {}
        rounds = det.get("rounds", 0)
        est_saved = _AVOIDED_ROUNDS_CONSERVATIVE * cost_per_round
        total_est += est_saved
        lines.append(
            f"  {inc['signal']} on {inc['target_node'] or 'unknown'}:"
            f" flagged at round {rounds}"
            f" → est. ~${est_saved:.4f} avoided"
            f" ({_AVOIDED_ROUNDS_CONSERVATIVE} rounds × ${cost_per_round:.5f}/round)"
        )

    return lines, total_est


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """buer-server entry point.  Runs the MCP + hook server."""
    import argparse

    parser = argparse.ArgumentParser(description="BUER MCP server (§4.4)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7777)
    parser.add_argument("--db", default=os.environ.get("BUER_DB", ".buer/store.sqlite"),
                        help="Path to BUER SQLite store")
    parser.add_argument(
        "--transport",
        default="streamable-http",
        choices=["stdio", "sse", "streamable-http"],
        help="MCP transport (hook path is HTTP-only)",
    )
    args = parser.parse_args()

    global _db_path
    _db_path = args.db

    # Pre-warm store so schema is created before first request
    _get_store()

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()

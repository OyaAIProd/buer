"""BUER assistive functions — §4.9 / §4.10 / §3.8 / §4.11.

Assistive functions are high-frequency advisory tools, NOT signals.
无 SDT 对位、纯工程辅助 (§5: SDT 地位已定 — 辅助功能无 SDT 对位).

Three independent channels (§4.11):
  - Alerts (signals §2.x):        always fire, no arbitration
  - Health hints (safety net §2.9): own low-frequency rhythm, not here
  - Inline assists (§4.10/§3.8):  high-frequency, arbitrated by §4.11

Public API
----------
assist_add_tests(store, project_id, root, target=None) -> str
run_inline_assists(store, project_id, affected, root) -> None
acknowledge_commit(store, project_id) -> str
arbitrate_inline_assists(candidates) -> Optional[InlineAssist]

Inline assist priority (PRIORITY, §4.11):
  commit     — have a save-point first (highest priority)
  run_tests  — confirm nothing is broken after the edit
  blast_radius — structural preview (lowest priority)
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from buer import callgraph, health
from buer.boundary import EXCLUDED_DIR_NAMES
from buer.store import Store

# ── thresholds (无 SDT 对位、纯工程辅助) ──────────────────────────────────────

HUB_THRESHOLD = 3             # caller_count >= this → tag "影响大"
CHANGE_FREQ_THRESHOLD = 3     # version_count >= this → tag "常改"
BLAST_RADIUS_THRESHOLD = 3    # caller_count >= this → trigger blast_radius assist
STABLE_WINDOW = 5             # no active-define edits in last N seqs → stable


# ── InlineAssist ───────────────────────────────────────────────────────────────

@dataclass
class InlineAssist:
    """One candidate inline assist. 无 SDT 对位、纯工程辅助 (§4.11)."""

    kind: str         # 'commit' | 'blast_radius'
    should_fire: bool
    message: str
    suggested_defines: set = field(default_factory=set)


# ── assist_add_tests (§4.9) ────────────────────────────────────────────────────

def _detect_shell_tests(root: str) -> list[str]:
    """Return 'file_path::fn_name' for test functions with no assert statements.

    无 SDT 对位、纯工程辅助 — AST scan for obvious empty tests (no assert).
    '断言有没有意义' is semantics; BUER only flags 'no assert at all'.
    """
    results: list[str] = []
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in EXCLUDED_DIR_NAMES]
            for fn in filenames:
                if not (fn.startswith("test_") or fn.endswith("_test.py")):
                    continue
                file_path = os.path.join(dirpath, fn)
                try:
                    source = Path(file_path).read_text(encoding="utf-8", errors="ignore")
                    tree = ast.parse(source)
                except Exception:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.FunctionDef):
                        continue
                    if not node.name.startswith("test_"):
                        continue
                    has_assert = any(
                        isinstance(child, ast.Assert) for child in ast.walk(node)
                    )
                    if not has_assert:
                        results.append(f"{file_path}::{node.name}")
    except OSError:
        pass
    return results


def assist_add_tests(
    store: Store,
    project_id: int,
    root: str,
    target: Optional[str] = None,
) -> str:
    """List uncovered defines ranked by structural importance (§4.9).

    Ranking: caller_count (blast_radius) > version_count (change_frequency).
    BUER gives list + priority; agent writes the tests.
    用户触发（非 BUER 自作主张）. 可拒绝/调整清单.
    无 SDT 对位、纯工程辅助 — ranking reuses call_edges and version chain data.
    """
    all_defs = store.all_current_defines(project_id)
    covered = store.covered_define_names(project_id)

    uncovered = [
        d for d in all_defs
        if d["define_name"] and d["define_name"] not in covered
    ]

    if not uncovered:
        return "[BUER] all known defines have test coverage; no tests to add."

    def _importance_key(d: object) -> tuple:
        fp = d["file_path"] or ""
        dn = d["define_name"] or ""
        try:
            mod = callgraph.module_name_of(fp, root)
            fqn = callgraph._lang_fqn(fp, mod, dn)
            caller_n = store.caller_count_for_fqn(project_id, fqn)
        except Exception:
            caller_n = 0
        freq = d["version_count"] or 0
        return (-caller_n, -freq)

    ranked = sorted(uncovered, key=_importance_key)
    shell_tests = _detect_shell_tests(root)

    lines = [
        "[BUER] add tests — structured list of uncovered defines (§4.9)",
        "⚠ recommended: git commit a rollback point first (before adding a large batch of tests, ensure you have a safe fallback).",
        "",
        f"uncovered defines: {len(ranked)} (ranked by structural importance — high blast-radius and high-churn first)",
        "",
    ]

    for i, d in enumerate(ranked[:20], 1):
        fp = d["file_path"] or ""
        dn = d["define_name"] or ""
        freq = d["version_count"] or 0
        try:
            mod = callgraph.module_name_of(fp, root)
            fqn = callgraph._lang_fqn(fp, mod, dn)
            caller_n = store.caller_count_for_fqn(project_id, fqn)
        except Exception:
            caller_n = 0

        tags = []
        if freq >= CHANGE_FREQ_THRESHOLD:
            tags.append(f"edited {freq}x")
        if caller_n >= HUB_THRESHOLD:
            tags.append(f"{caller_n} callers")
        tag_str = f" ({', '.join(tags)})" if tags else ""

        try:
            rel = os.path.relpath(fp, root) if fp else fp
        except ValueError:
            rel = fp
        lines.append(f"  {i}. {rel} :: {dn}{tag_str}")

    if len(ranked) > 20:
        lines.append(f"  … {len(ranked) - 20} more")

    if shell_tests:
        lines.append("")
        lines.append(f"possible shell tests (no assertions — verify manually): {len(shell_tests)}")
        for item in shell_tests[:5]:
            try:
                fp_part = item.split("::")[0]
                fn_part = item.split("::", 1)[1] if "::" in item else ""
                rel = os.path.relpath(fp_part, root)
                lines.append(f"  - {rel}::{fn_part}")
            except Exception:
                lines.append(f"  - {item}")
        lines.append("  (BUER only flags 'no assert'; whether the test is meaningful is a semantic question BUER does not judge.)")

    goal_str = target or "cover all core defines (≥ 3 callers or version chain ≥ 3)"
    lines.append("")
    lines.append(f"goal: {goal_str}")
    lines.append("BUER provides the list and priority; the agent writes the tests. You may reject or adjust the list.")
    lines.append("stuck_region / regression signals remain active during test-writing (no exemption).")

    return "\n".join(lines)


# ── B-mechanism set helpers ────────────────────────────────────────────────────

def _set_has_new_member(now: set, last: set) -> bool:
    """B mechanism: True iff `now` contains a define not in `last` (new risk surfaced)."""
    return bool(now - last)

def _serialize_defines(s: set) -> str:
    """Serialize a define-name set for assist_state storage (sorted, comma-joined)."""
    return ",".join(sorted(s))

def _deserialize_defines(text: str) -> set:
    """Parse assist_state define-set text back to a set ('' → empty set)."""
    return set(text.split(",")) if text else set()


# ── inline assist: commit timing (§4.10) ──────────────────────────────────────

def _build_commit_assist(
    store: Store,
    project_id: int,
    root: str,
    affected: list | None = None,
) -> InlineAssist:
    """Check whether this is a good commit point (§4.10).

    Good-commit-point criteria (all must pass):
      1. Structural stability (Gate 3): the commit cluster (changed defines excluding
         currently-edited files) has not been touched in the last STABLE_WINDOW seqs
      2. New define in cluster (Gate 2 B): the cluster contains at least one define
         not present at the last commit suggestion (new risk surfaced since last prompt)
      3. No open regression incidents (Gate 4): tests are green or absent

    'affected' is the list of (file_path, define_name, det_id) from the current
    reconcile call. Files in 'affected' are excluded from the stability cluster so
    we can detect when editing has moved away from the original cluster.

    无 SDT 对位、纯工程辅助.
    「结构稳定」≠「逻辑完整」: 措辞用「看起来告一段落，要提交吗」(候选+疑问).
    """
    state = store.get_assist_state(project_id)
    last_commit_seq = state["last_commit_seq"] if state else 0
    cur_max = store.max_seq(project_id)

    # Gate 3: structural stability
    # Cluster = defines changed since last commit, excluding currently-edited files.
    # If the current edit is to a DIFFERENT file from the cluster, the cluster may
    # have settled.  cluster_max_seq >= STABLE_WINDOW ago → cluster is stable.
    affected_files: set[str] = {fp for fp, _, _ in (affected or [])}
    changed_defs = store.defs_changed_since(project_id, last_commit_seq)
    cluster_defs = [row for row in changed_defs
                    if row["file_path"] not in affected_files]

    if not cluster_defs:
        # Currently editing ALL files in the cluster — not stable
        return InlineAssist(kind="commit", should_fire=False, message="")

    cluster_max_seq = max(row["last_seq"] for row in cluster_defs)
    if cur_max - cluster_max_seq < STABLE_WINDOW:
        return InlineAssist(kind="commit", should_fire=False, message="")

    # Gate 2 (B): only re-suggest if the uncommitted cluster gained a define
    # not present at the last commit suggestion (new risk surfaced).
    cluster_def_names = {row["define_name"] for row in cluster_defs if row["define_name"]}
    last_set = _deserialize_defines(state["last_commit_suggest_defines"] if state else "")
    if not _set_has_new_member(cluster_def_names, last_set):
        return InlineAssist(kind="commit", should_fire=False, message="")

    # Gate 4: no open regression
    if store.has_open_regression(project_id):
        return InlineAssist(kind="commit", should_fire=False, message="")

    # Build message
    touched_dirs: set[str] = set()
    touched_defines: list[str] = []
    for row in cluster_defs[:8]:
        fp = row["file_path"] or ""
        dn = row["define_name"] or ""
        try:
            rel = os.path.relpath(fp, root) if fp else fp
        except ValueError:
            rel = fp
        d = os.path.dirname(rel)
        touched_dirs.add(d if d and d != "." else rel)
        if dn:
            touched_defines.append(dn)

    dir_str = ", ".join(sorted(touched_dirs)[:3]) or "edited files"
    def_str = ", ".join(touched_defines[:4]) or "several defines"

    msg = "\n".join([
        "[BUER] 💡 commit timing suggestion (§4.10)",
        "this set of changes looks like a natural stopping point — ready to commit?",
        "('looks like a stopping point' is a structural stability judgment, not a semantic completeness check — verify logic yourself.)",
        "",
        f"suggested commit scope: {dir_str}",
        f"structural description (add semantics as needed): touched {def_str}",
        "",
        "after committing, call acknowledge_commit to let BUER reset its commit-timing tracking.",
    ])

    return InlineAssist(kind="commit", should_fire=True, message=msg,
                        suggested_defines=cluster_def_names)


# ── inline assist: blast radius preview (§3.8) ────────────────────────────────

def _build_blast_radius_assist(
    store: Store,
    project_id: int,
    affected: list,
    root: str,
) -> InlineAssist:
    """Preview impact zone for high-influence defines being edited (§3.8).

    Fires when a changed define has ≥ BLAST_RADIUS_THRESHOLD callers.
    Picks the define with the most callers among affected defines.
    Honest about static analysis limits: "至少 N 处，动态调用可能还有未检测到的".
    无 SDT 对位、纯工程辅助 — reuses call_edges data from callgraph layer.
    """
    best_fqn: Optional[str] = None
    best_callers: list[str] = []
    best_count = 0

    for file_path, define_name, _det_id in affected:
        if not define_name:
            continue
        try:
            mod = callgraph.module_name_of(file_path, root)
        except Exception:
            continue
        fqn = callgraph._lang_fqn(file_path, mod, define_name)
        callers = store.callers_of(project_id, fqn)
        if len(callers) >= BLAST_RADIUS_THRESHOLD and len(callers) > best_count:
            best_fqn = fqn
            best_callers = callers
            best_count = len(callers)

    if best_fqn is None:
        return InlineAssist(kind="blast_radius", should_fire=False, message="")

    shown = best_callers[:5]
    more = best_count - len(shown)
    caller_lines = "\n".join(f"  - {c}" for c in shown)
    if more > 0:
        caller_lines += f"\n  … {more} more"

    msg = "\n".join([
        "[BUER] 💡 blast-radius preview (§3.8)",
        f"you are editing {best_fqn}, which affects at least {best_count} locations",
        "(dynamic dispatch may add more not captured here):",
        caller_lines,
        "",
        "whether and how to change this is your decision; this is a structural fact, not a 'will cause bugs' assertion.",
    ])

    return InlineAssist(kind="blast_radius", should_fire=True, message=msg)


# ── inline assist: run tests (§4.11 extension, §4.5 extension) ───────────────
#
# Testing lifecycle context (三阶段，不重叠):
#   safety_net (§2.9)        — project has NO tests at all
#   assist_add_tests (§4.9)  — define has NO test coverage
#   THIS assist              — define HAS coverage but tests not run since last edit
#
# 受众: agent channel (agent can run tests directly in vibe-coding session).
# 无 SDT 对位、纯工程辅助.


def _stale_covered_defines(
    store: Store,
    project_id: int,
    affected: list,
) -> list[tuple[str, list[str]]]:
    """Precise tier: defines whose ALL covering tests have run_seq < det_seq.

    A define is "stale" when every covering test case has its most recent test
    run's seq strictly before the define's determination seq — meaning no test
    ran after the edit.  Empty history also counts as stale (never run).
    """
    stale: list[tuple[str, list[str]]] = []
    for _file_path, define_name, det_id in affected:
        if not define_name:
            continue
        covering = store.test_cases_covering(project_id, define_name)
        if not covering:
            continue
        det = store.get_determination(det_id)
        if det is None:
            continue
        det_seq = det["seq"]

        all_stale = True
        for tc in covering:
            parts = tc.split("::", 1)
            if len(parts) != 2:
                continue
            classname, name = parts
            history = store.test_case_history(project_id, classname, name)
            if history:
                last_run_seq = history[-1]["run_seq"]
                if last_run_seq is not None and last_run_seq >= det_seq:
                    all_stale = False
                    break

        if all_stale:
            stale.append((define_name, covering))
    return stale


def _build_run_tests_assist(
    store: Store,
    project_id: int,
    affected: list,
    root: str,
) -> InlineAssist:
    """Suggest running tests when covered defines were edited but tests haven't run.

    Two tiers:
    - Precise (coverage_map populated): check test_cases_covering each affected
      define; fire when all covering tests have run_seq < det_seq.
    - Heuristic (no coverage_map): fire when project has test files but no
      test_run seq >= the minimum edit seq in this batch.

    Does NOT fire in incidents — advisory only.  无 SDT 对位、纯工程辅助.
    """
    if not affected:
        return InlineAssist(kind="run_tests", should_fire=False, message="")

    state = store.get_assist_state(project_id)
    last_set = _deserialize_defines(state["last_run_tests_suggest_defines"] if state else "")

    has_coverage = bool(store.covered_define_names(project_id))

    if has_coverage:
        # ── Precise tier ──────────────────────────────────────────────────────
        stale = _stale_covered_defines(store, project_id, affected)
        if not stale:
            # All covering tests have run — reset so next stale define fires fresh.
            store.update_assist_state(project_id, last_run_tests_suggest_defines="")
            return InlineAssist(kind="run_tests", should_fire=False, message="")

        # Deduplicate test cases; collect bare define names for message
        seen_cases: set[str] = set()
        all_cases: list[str] = []
        define_names: list[str] = []
        for dn, cases in stale:
            bare = dn.split(".")[-1] if "." in dn else dn
            define_names.append(bare)
            for tc in cases:
                if tc not in seen_cases:
                    seen_cases.add(tc)
                    all_cases.append(tc)

        # Gate B: only re-suggest if the stale set gained a define not in last suggestion.
        stale_def_names = set(define_names)
        if not _set_has_new_member(stale_def_names, last_set):
            return InlineAssist(kind="run_tests", should_fire=False, message="")

        shown = all_cases[:5]
        more = len(all_cases) - len(shown)

        dn_str = ", ".join(define_names[:3])
        if len(define_names) > 3:
            dn_str += f" and {len(define_names)} defines total"
        case_lines = "\n".join(f"  · {tc}" for tc in shown)
        if more > 0:
            case_lines += f"\n  … {more} more"

        msg = "\n".join([
            "[BUER] 💡 run tests (§4.11 inline assist)",
            f"you edited {dn_str}; {len(all_cases)} tests cover it and none have run since the edit — run them to confirm nothing is broken?",
            "(precise tier: the following tests directly cover this edit, confirmed via coverage_map.)",
            "",
            case_lines,
            "",
            "BUER does not run tests or guess commands — which tests to run and how is your call.",
        ])
        return InlineAssist(kind="run_tests", should_fire=True, message=msg,
                            suggested_defines=stale_def_names)

    else:
        # ── Heuristic tier ────────────────────────────────────────────────────
        if not health._has_test_files(root):
            return InlineAssist(kind="run_tests", should_fire=False, message="")

        # Minimum determination seq in this edit batch
        min_edit_seq: Optional[int] = None
        for _fp, _dn, det_id in affected:
            det = store.get_determination(det_id)
            if det:
                s = det["seq"]
                if min_edit_seq is None or s < min_edit_seq:
                    min_edit_seq = s

        if min_edit_seq is None:
            return InlineAssist(kind="run_tests", should_fire=False, message="")

        # Any test run at or after the start of this edit batch?
        latest_tr_seq = store.latest_test_run_seq(project_id)
        if latest_tr_seq is not None and latest_tr_seq >= min_edit_seq:
            # Tests have run — reset so next uncovered edit fires fresh.
            store.update_assist_state(project_id, last_run_tests_suggest_defines="")
            return InlineAssist(kind="run_tests", should_fire=False, message="")

        define_names_all = [dn for _, dn, _ in affected if dn]

        # Gate B: only re-suggest if the affected set gained a define not in last suggestion.
        heuristic_def_names = set(define_names_all)
        if not _set_has_new_member(heuristic_def_names, last_set):
            return InlineAssist(kind="run_tests", should_fire=False, message="")

        shown_dn = define_names_all[:3]
        dn_str = ", ".join(shown_dn)
        if len(define_names_all) > 3:
            dn_str += f" and {len(define_names_all)} defines total"

        msg = "\n".join([
            "[BUER] 💡 run tests (§4.11 inline assist)",
            f"you edited {dn_str}; the project has tests but none ran in this edit window — run them to confirm nothing is broken?",
            "(heuristic tier: coverage mapping unavailable; run the full test suite or whichever tests you consider relevant.)",
            "",
            "BUER does not run tests or guess commands — which tests to run and how is your call.",
        ])
        return InlineAssist(kind="run_tests", should_fire=True, message=msg,
                            suggested_defines=heuristic_def_names)


# ── arbitrate_inline_assists (§4.11) ──────────────────────────────────────────

PRIORITY = ["commit", "run_tests", "blast_radius"]


def arbitrate_inline_assists(
    candidates: list[InlineAssist],
) -> Optional[InlineAssist]:
    """Return at most one inline assist per priority order (§4.11).

    事中辅助仲裁：同一时刻多个想出现 → 只出优先级最高的一个.
    三通道独立：报警（信号）和健康提示（安全网）不进此仲裁.
    无 SDT 对位、纯工程辅助.
    """
    active = [a for a in candidates if a.should_fire]
    if not active:
        return None
    return min(active, key=lambda a: PRIORITY.index(a.kind))


# ── run_inline_assists (reconcile hook, §4.11) ────────────────────────────────

def run_inline_assists(
    store: Store,
    project_id: int,
    affected: list,
    root: str,
) -> None:
    """Build inline assist candidates, arbitrate, and enqueue winner.

    Called at end of each reconcile. Inline-assist channel only —
    signals and safety-net health hints are in separate channels, untouched.
    无 SDT 对位、纯工程辅助 (§4.11).
    """
    commit_assist = _build_commit_assist(store, project_id, root, affected)
    run_tests_assist = _build_run_tests_assist(store, project_id, affected, root)
    blast_assist = _build_blast_radius_assist(store, project_id, affected, root)

    winner = arbitrate_inline_assists([commit_assist, run_tests_assist, blast_assist])
    if winner is None:
        return

    # run_tests goes to agent channel (agent is primary audience for test suggestions);
    # commit and blast_radius go to user channel.
    channel = "agent" if winner.kind == "run_tests" else "user"
    store.enqueue_delivery(project_id, None, channel, winner.message, kind="suggestion")

    # B-mechanism: record the define set this suggestion covered, so we only
    # re-suggest when a new member appears (not on every repeat edit).
    if winner.kind == "commit":
        store.update_assist_state(
            project_id,
            last_commit_suggest_defines=_serialize_defines(winner.suggested_defines),
        )
    elif winner.kind == "run_tests":
        store.update_assist_state(
            project_id,
            last_run_tests_suggest_defines=_serialize_defines(winner.suggested_defines),
        )


# ── acknowledge_commit (§4.10) ────────────────────────────────────────────────

def acknowledge_commit(store: Store, project_id: int) -> str:
    """Record current seq as a known commit point (§4.10).

    User calls this after git commit so BUER resets its commit-timing tracking.
    无 SDT 对位、纯工程辅助.
    """
    seq = store.max_seq(project_id)
    store.update_assist_state(
        project_id,
        last_commit_seq=seq,
        last_commit_suggest_defines="",
    )
    return f"[BUER] commit point recorded (seq={seq}). BUER has reset commit-timing tracking."

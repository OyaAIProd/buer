"""BUER signal detection + two-step state machine — §2.2 (stuck_region / debug_loop) / §4.3.

Architecture: detect_*() writes incidents for one signal; advance_incidents()
is the generic state machine.  Subsequent signals need only their own
detect_*() + _recurred_*() helpers.

Public API (called from reconcile.py after each reconcile pass)
---------------------------------------------------------------
  detect_debug_loop(store, project_id, affected, root, idx)   # must run before stuck_region
  detect_stuck_region(store, project_id, affected, root, idx)
  detect_define_loop(store, project_id, affected, root, idx)
  detect_boundary_breach(store, project_id, boundary_violations)
  detect_task_scope_breach(store, project_id, affected, root)  # opt-in, §2.3
  detect_regression(store, project_id, affected, root, idx)   # after testscan ingestion
  detect_test_tampering(store, project_id, affected, root, idx)  # after detect_regression
  advance_incidents(store, project_id, affected)
"""
from __future__ import annotations

import fnmatch
import json
import os
from datetime import datetime, timezone
from typing import Any

from buer import callgraph, delivery, influence, metrics, parse, testscan, ts_dataflow
from buer.sensitivity import effective_escalation_theta2
from buer.store import Store

# ── thresholds (§4.3 table) ───────────────────────────────────────────────────

THETA_1: int = 5        # version chain length before stuck_region fires (§4.3)

THETA_2: dict[str, int] = {    # post-notify recurrence limit before escalating user
    "stuck_region":       3,
    "debug_loop":         3,    # same as stuck_region — pending dogfooding calibration
    "regression":         2,    # pending dogfooding calibration
    "define_loop":        2,
    "task_scope_breach":  1,
    "boundary_breach":    1,
    "test_tampering":     1,    # cheating class — fires once, escalates immediately (§2.7)
    "dangling_reference": 2,    # pending dogfooding calibration
}

# dangling_reference persistence threshold — 初值待 dogfooding 校准，非本体推导.
#
# THETA_1_DANGLING = 2: the same (caller_define, callee_text) pair must be seen
# unresolved in ≥2 separate determinations of the caller define before triggering.
# Single-occurrence suppression: "先写调用后写定义"的正常中间态 (§2.8).
THETA_1_DANGLING: int = 2
THETA_2_DEFAULT: int = 2

# High-impact caller threshold — 初值待 dogfooding 校准，非本体推导.
#
# HIGH_IMPACT_CALLERS = 5: defines with at least this many direct callers in
# gd_edges receive a proactive neutral notice in the agent channel (not an
# incident; never escalates to user; independent of notification_level).
HIGH_IMPACT_CALLERS: int = 5

# token_waste upgrade threshold — 初值待 dogfooding 校准，非本体推导.
#
# THETA_WASTE = 15: rounds of edits on one define before triggering the investment
# waste alert.  Higher than stuck_region θ₁=5 by design: stuck fires early so
# the agent has a chance to fix; waste fires only when the region has remained
# un-converged through many more rounds and multiple stuck/debug cycles.
THETA_WASTE: int = 15

# stuck_region sub-thresholds — 初值待 dogfooding 校准，非本体推导.
#
# THETA_DJ = 0.2: in a pure 5-node version chain the minimum adjacent
# d_J is d_J(v4,v5) = 1/5 = 0.2.  Using 0.3 (user's first suggestion)
# would NEVER trigger on pure chains: max adjacent d_J at position 5
# in any monotone-ancestry topology is ≤ 1/(5-1) = 0.25.
# Cross-define callee changes raise adjacent d_J; 0.2 is the safe floor
# for θ₁=5 pure chains.
THETA_DJ: float = 0.2   # minimum adjacent d_J  ("持续实质改动")
K_CONSEC: int   = 3     # consecutive recent adjacent pairs to check
N_STABLE: int   = 2     # reconcile rounds without trigger before resolving

# Signals whose resolve criterion is a state confirmation, not a behavioural
# stability countdown.  _signal_recurred returning False means the underlying
# state is gone (duplicate removed / real fix confirmed) — resolve immediately,
# no N_STABLE countdown.  Add new state-based signals here; do NOT scatter
# individual `if signal == "..."` checks in advance_incidents.
STATE_BASED_SIGNALS: frozenset[str] = frozenset({
    "test_tampering",   # resolve = prod code fixed + test genuinely green (§2.7)
})

# define_loop sub-threshold — 待 dogfooding 校准.
#
# N_LOOP_GAP = 2: minimum index distance in version chain for two equal-fp
# versions to count as a loop.  Gap=1 (adjacent) = "no change" — reconcile
# never creates a new determination for an unchanged fingerprint, so gap=1
# is already prevented upstream.  Gap=2 is the canonical §2.1 example:
# v1→v2→v3=v1 (one edit between the two equivalent versions).
N_LOOP_GAP: int = 2     # min chain-index distance for non-adjacent equiv ("绕回")

# define_loop consecutive-edit thresholds (§2.1 全版):
# Agent notification fires at ≥ N_DEFINE_LOOP_CONSEC_AGENT consecutive edits.
# User escalation fires at ≥ N_DEFINE_LOOP_CONSEC_USER consecutive edits.
N_DEFINE_LOOP_CONSEC_AGENT: int = 5
N_DEFINE_LOOP_CONSEC_USER: int  = 8

# Path exclusion patterns — shared by regression and test_tampering detectors.
#
# Test code (arrange-act-assert naturally repetitive), generated code (protobuf/
# ORM/migrations), and boilerplate are excluded so that edits to these files do
# not trigger regression or test_tampering signals.  These globs are defaults.
#
# Next.js App Router convention files are matched via _NEXTJS_CONVENTION_BASENAMES
# (basename check, not glob) so they work at any path depth — including bare
# filenames and short relative paths that "*/X" globs would miss.
#
# a real Next.js project's i18n legal content: two globs cover all path depths:
#   "*/legal/*.tsx" — paths with at least one prefix dir (absolute + most relative)
#   "legal/*.tsx"   — bare paths starting directly with legal/ (no leading dir)
DUPLICATE_EXCLUDE_GLOBS: tuple[str, ...] = (
    "*/tests/*", "*_test.py",                             # test code (test_* → basename in _is_excluded_path)
    "*/migrations/*", "*_pb2.py", "*/generated/*",        # generated / protobuf
    # example: i18n legal content variants (French/Chinese/English per document)
    "*/legal/*.tsx",  "legal/*.tsx",
)

# Next.js App Router convention filenames — matched by basename in _is_excluded_path,
# independent of path depth.  Covers bare filenames ("loading.tsx"), short relative
# paths ("app/loading.tsx"), and absolute paths equally.
#
# Validated on a real-world project: suppressed 34 incidents, zero true-positive
# collateral.  Template/default included defensively (0 current incidents).
_NEXTJS_CONVENTION_BASENAMES: frozenset[str] = frozenset({
    "loading.tsx",     "loading.jsx",
    "error.tsx",       "error.jsx",
    "not-found.tsx",   "not-found.jsx",
    "global-error.tsx",
    "template.tsx",    "default.tsx",
    "page.tsx",        "page.jsx",
    "layout.tsx",      "layout.jsx",
})


# ── stuck_region criteria helpers (§2.2) ─────────────────────────────────────

def _no_equiv_loop(chain) -> bool:
    """All fingerprints in chain are distinct (no non-adjacent repetition = define_loop)."""
    fps = [r["node_fingerprint"] for r in chain if r["node_fingerprint"]]
    return len(set(fps)) == len(fps)


def _adjacent_dj_large(store: Store, project_id: int, chain) -> bool:
    """Last K_CONSEC adjacent pairs in chain all have d_J >= THETA_DJ (§2.2 "持续实质改动").

    Initial threshold 0.2; pure-chain minimum at θ₁=5 is d_J(v4,v5)=0.2.
    """
    pairs = list(zip(chain, chain[1:]))
    check = pairs[-K_CONSEC:]
    if not check:
        return False
    return all(
        metrics.d_J(store, project_id, a["id"], b["id"]) >= THETA_DJ
        for a, b in check
    )


def _is_stuck(store: Store, project_id: int, file_path: str, define_name: str) -> bool:
    """True iff all three stuck_region criteria are satisfied (§2.2):
      (1) version_chain length >= θ₁
      (2) no fingerprint repetition (not a define_loop)
      (3) adjacent d_J consistently large (substantial change, not small oscillation)
    """
    chain = store.version_chain(project_id, file_path, define_name)
    return (
        len(chain) >= THETA_1
        and _no_equiv_loop(chain)
        and _adjacent_dj_large(store, project_id, chain)
    )


def _open_incident_for(store: Store, project_id: int, signal: str, target_node: str):
    """Find existing open/notified incident for this signal+target, or None."""
    return next(
        (inc for inc in store.open_incidents(project_id)
         if inc["signal"] == signal and inc["target_node"] == target_node),
        None,
    )


# ── define_loop criteria helpers (§2.1) ──────────────────────────────────────

def _loop_key(det) -> tuple | None:
    """(coarse, fine) pair used as loop-identity key.

    A true loop requires BOTH coarse AND fine to match, preventing false-equiv
    defines (same last-segment, different receiver) from triggering a spurious loop.
    fine may be None for pre-migration records; in that case only coarse is available
    for backward compat — old records only match other old records with the same coarse.
    """
    coarse = det["node_fingerprint"]
    if not coarse:
        return None
    try:
        fine = det["fine_fingerprint"]
    except (IndexError, KeyError):
        fine = None
    return (coarse, fine)


def _find_loop(chain) -> tuple | None:
    """First non-adjacent (coarse, fine) pair match with gap >= N_LOOP_GAP.

    Returns (earlier_seq, later_seq, coarse) or None.
    Keeps the earliest occurrence of each key so the returned span is maximal.
    """
    key_first: dict = {}  # (coarse, fine) -> earliest chain index
    for idx, det in enumerate(chain):
        key = _loop_key(det)
        if key is None:
            continue
        if key in key_first:
            if idx - key_first[key] >= N_LOOP_GAP:
                first = key_first[key]
                return (chain[first]["seq"], det["seq"], key[0])  # key[0]=coarse for display
        else:
            key_first[key] = idx
    return None


def _latest_loops(chain) -> bool:
    """True if the latest version's (coarse, fine) key matches any earlier at distance >= N_LOOP_GAP.

    Used for recurrence check: did the most recent edit loop back to a prior state?
    Requires BOTH coarse AND fine to match — prevents false-equiv from triggering recurrence.
    """
    if len(chain) < N_LOOP_GAP + 1:
        return False
    latest_key = _loop_key(chain[-1])
    if latest_key is None:
        return False
    cutoff = len(chain) - 1 - N_LOOP_GAP  # highest index far enough from latest
    return any(_loop_key(chain[i]) == latest_key for i in range(cutoff + 1))


# ── lateral context (§3.5, §3.2) ─────────────────────────────────────────────

def _lateral_context(
    store: Store,
    project_id: int,
    file_path: str,
    define_name: str,
    root: str,
    idx: callgraph.SymbolIndex,
) -> dict:
    """Three-dimensional lateral context for root-cause direction (§3.5 / §3.2).

    Dimensions returned:
      shared_ancestry  — ω(target, callee) > 0 for each callee  [Math Ext §11.1b]
      gamma_r          — Γ_R direct neighbors of target's determination  [GD Def 7.1]
      component_size   — size of target's undirected 𝒢_D component  [GD Prop 9.2]

    gamma_r items include a recently_modified flag: True when the neighbor
    determination's seq falls within the target's version-chain edit window,
    flagging the strongest root-cause direction (structural + temporal convergence).

    component_size = 1 means the define is causally isolated — no shared history
    or shared downstream production with any other node.  This is negative evidence:
    unrelated nodes cannot be the root cause (§3.2 causal independence filtering).

    Runtime note: Γ_R and ω are structurally rigorous.  Input is lossy (Python
    call-graph analysis misses dynamic dispatch, §3.7).  Absence of a Γ_R edge
    means no static evidence found, not structural independence.
    """
    target_det = store.current_version_determination(project_id, file_path, define_name)
    if target_det is None:
        return {}

    # ── Dimension 1: ω-based shared ancestry with callees ────────────────────
    mod = callgraph.module_name_of(file_path, root)
    caller_fqn = callgraph._lang_fqn(file_path, mod, define_name)
    shared = []
    for callee_fqn in store.callees_of(project_id, caller_fqn):
        if callee_fqn not in idx.loc:
            continue
        callee_file, callee_define = idx.loc[callee_fqn]
        callee_det = store.current_version_determination(project_id, callee_file, callee_define)
        if callee_det is None:
            continue
        w = metrics.omega(store, project_id, target_det["id"], callee_det["id"])
        if w > 0:
            shared.append({"define": callee_fqn, "omega": w})
    shared.sort(key=lambda x: x["omega"], reverse=True)

    # ── Dimension 2: Γ_R direct neighbors (shared direct merger)  [GD Def 7.1] ─
    chain = store.version_chain(project_id, file_path, define_name)
    edit_window_start = chain[0]["seq"] if chain else None

    gr_items = []
    for nb_det_id in metrics.gamma_r_neighbors(store, project_id, target_det["id"]):
        nb_det = store.get_determination(nb_det_id)
        if nb_det is None or not nb_det["define_name"]:
            continue
        nb_fp = nb_det["file_path"] or ""
        nb_mod = callgraph.module_name_of(nb_fp, root) if nb_fp else ""
        nb_fqn = f"{nb_mod}.{nb_det['define_name']}" if nb_mod else nb_det["define_name"]
        recently_modified = (
            edit_window_start is not None
            and nb_det["seq"] is not None
            and nb_det["seq"] >= edit_window_start
        )
        gr_items.append({"define": nb_fqn, "recently_modified": recently_modified})

    # ── Dimension 3: connected component size  [GD Prop 9.2] ─────────────────
    component = metrics.connected_component(store, project_id, target_det["id"])

    result: dict = {"shared_ancestry": shared[:5], "component_size": len(component)}
    if gr_items:
        result["gamma_r"] = gr_items[:5]
    return result


# ── debug_loop helpers (§2.2, §4.5, §5.3) ────────────────────────────────────

def _find_test_cases_for_define(
    store: Store,
    project_id: int,
    file_path: str,
    define_name: str,
    root: str,
) -> tuple[list[str], str]:
    """Find test cases associated with a define.  Returns (tc_list, tier).

    tier = 'precise' when coverage_map has matching entries that also exist
    in test_cases rows (§4.5 exact tier).  Falls back to 'heuristic' (§5.3
    degraded: name-pattern matching).  Returns ([], 'none') when no tests found.
    """
    mod = callgraph.module_name_of(file_path, root)
    fqn = f"{mod}.{define_name}"

    # Precise tier — try FQN first, then bare define_name
    for lookup_key in (fqn, define_name):
        candidates = store.test_cases_covering(project_id, lookup_key)
        if candidates:
            # Only keep tc_ids that actually appear in test_case rows so that
            # synthetic Cobertura ids ("coverage::pkg.Cls") silently drop out.
            valid = [tc for tc in candidates if store.test_case_history(
                project_id, *tc.split("::", 1)
            )] if candidates else []
            if valid:
                return valid, "precise"

    # Heuristic tier — scan known test-case names for naming-convention matches
    local_name = define_name.split(".")[-1]
    matched = []
    for classname, name in store.distinct_test_case_pairs(project_id):
        cands = testscan.heuristic_defines_for_testcase(classname, name)
        if define_name in cands or local_name in cands:
            matched.append(f"{classname}::{name}")
    if matched:
        return matched, "heuristic"

    # File-level heuristic: match test/source file stems (strict equality after stripping test_ prefix).
    # Falls back here only when both precise and function-name heuristic miss.
    matched_file: list[str] = []
    for classname, name, tc_file_path in store.distinct_test_case_triples(project_id):
        identifier = tc_file_path if tc_file_path else classname
        if testscan.test_stem_matches_source(identifier, file_path):
            matched_file.append(f"{classname}::{name}")
    if matched_file:
        return matched_file, "heuristic"

    return [], "none"


def _tests_persistently_failing(
    store: Store,
    project_id: int,
    chain: list,
    test_cases: list[str],
) -> bool:
    """True iff every associated test case that appeared in the edit window was failing.

    Edit window = [chain[0].seq, chain[-1].seq].  At least one test run must
    exist in the window (otherwise we have no data → cannot confirm → False).
    A test case status of 'error' is treated as failed (§2.2 持续失败判定).
    """
    if not test_cases or not chain:
        return False
    first_seq = chain[0]["seq"]
    last_seq = chain[-1]["seq"]
    if first_seq is None or last_seq is None:
        return False

    found_any = False
    for tc in test_cases:
        parts = tc.split("::", 1)
        if len(parts) != 2:
            continue
        classname, name = parts
        history = store.test_case_history(project_id, classname, name)
        in_window = [
            h for h in history
            if h["run_seq"] is not None and first_seq <= h["run_seq"] <= last_seq
        ]
        if not in_window:
            continue
        found_any = True
        if any(h["status"] not in ("failed", "error") for h in in_window):
            return False  # at least one non-failure — condition not met

    return found_any


def _recently_modified_callees(
    store: Store,
    project_id: int,
    file_path: str,
    define_name: str,
    chain: list,
    root: str,
    idx: callgraph.SymbolIndex,
) -> list[str]:
    """Callees of define_name that were also modified during its edit window.

    Used to build the root-cause direction note (§2.2 压测反思#1: "往哪看").
    """
    if not chain:
        return []
    first_seq = chain[0]["seq"]
    if first_seq is None:
        return []
    mod = callgraph.module_name_of(file_path, root)
    caller_fqn = callgraph._lang_fqn(file_path, mod, define_name)
    result = []
    for callee_fqn in store.callees_of(project_id, caller_fqn):
        callee_loc = idx.loc.get(callee_fqn)
        if callee_loc is None:
            continue
        callee_file, callee_def = callee_loc
        c_chain = store.version_chain(project_id, callee_file, callee_def)
        if c_chain and c_chain[-1]["seq"] is not None and c_chain[-1]["seq"] >= first_seq:
            result.append(callee_fqn)
    return result


def _direction_note(
    define_name: str,
    recently_modified: list[str],
    lateral: dict,
) -> str:
    """Structural direction hint using three structural dimensions (§3.2 / §3.5).

    Priority:
      1. Recently-modified callees + Γ_R recent neighbors — two lines of evidence
      2. Recently-modified callees only — call-graph + time
      3. Γ_R neighbors that were recently modified — downstream convergence + time
      4. Γ_R neighbors (no time signal) — downstream convergence only
      5. ω shared ancestry — upstream history
      6. Fallback — generic direction
    """
    gamma_r = lateral.get("gamma_r") or []
    gr_recent = [g["define"] for g in gamma_r if g.get("recently_modified")]
    gr_all = [g["define"] for g in gamma_r]

    if recently_modified and gr_recent:
        deps = ", ".join(recently_modified[:2])
        gr_str = ", ".join(gr_recent[:2])
        return (
            f"upstream {deps} and shared-merge-direction {gr_str} "
            "were both modified in the same change window — two converging signals, check these first"
        )
    if recently_modified:
        deps = ", ".join(recently_modified[:3])
        return (
            f"{define_name} depends on {deps}, "
            "which was also modified in the same change window — worth checking upstream first"
        )
    if gr_recent:
        gr_str = ", ".join(gr_recent[:3])
        return (
            f"{define_name} and {gr_str} both feed the same merge point (Γ_R neighbors); "
            f"{gr_str} was recently modified — check whether the shared downstream is affected"
        )
    if gr_all:
        gr_str = ", ".join(gr_all[:3])
        return (
            f"{define_name} and {gr_str} both feed the same merge point (Γ_R neighbors) "
            "— changes here should be coordinated with these defines"
        )
    if lateral.get("shared_ancestry"):
        shared = lateral["shared_ancestry"][0]["define"]
        return (
            f"{define_name} and {shared} share a structural ancestor (ω>0) "
            "— potentially affected by the same upstream change; check common dependencies"
        )
    return (
        f"{define_name} keeps changing without converging; associated tests keep failing "
        "— check whether its upstream dependencies are the true root cause"
    )


# ── detect_debug_loop (§2.2 debug_loop — stuck_region 增强) ──────────────────

def detect_debug_loop(
    store: Store,
    project_id: int,
    affected: list,   # [(file_path, define_name, det_id), ...]
    root: str,
    idx: callgraph.SymbolIndex,
) -> None:
    """Upgrade stuck_region to debug_loop when tests are persistently failing (§2.2).

    Must run BEFORE detect_stuck_region.  Defines claimed by debug_loop are
    excluded from stuck_region (prevent double-reporting on same define).

    Gates:
      (1) structural: same _is_stuck() condition as stuck_region
      (2) test association: find test cases via precise (coverage_map) or
          heuristic (name-pattern) tier — no tests → fall through to stuck_region
      (3) persistent failure: every associated test case was failing in ALL
          test_runs within the define's edit window — tests passed → not debug_loop

    Structural direction hint (§2.2 压测反思#1) is always attached to details so
    the agent gets "往哪看" not just "你卡住了".
    """
    for file_path, define_name, _det_id in affected:
        if _is_excluded_path(file_path):
            continue
        target_node = f"{file_path}::{define_name}"
        if _open_incident_for(store, project_id, "debug_loop", target_node) is not None:
            continue

        # Gate 1: structural stuck condition (same as stuck_region)
        if not _is_stuck(store, project_id, file_path, define_name):
            continue

        # Gate 2: test case association
        test_cases, tier = _find_test_cases_for_define(
            store, project_id, file_path, define_name, root
        )
        if not test_cases:
            continue  # no tests → stuck_region handles this define

        # Gate 3: persistent failure in edit window
        chain = store.version_chain(project_id, file_path, define_name)
        if not _tests_persistently_failing(store, project_id, chain, test_cases):
            continue  # tests not consistently failing → not debug_loop

        lateral = _lateral_context(store, project_id, file_path, define_name, root, idx)
        recently_modified = _recently_modified_callees(
            store, project_id, file_path, define_name, chain, root, idx
        )
        note = _direction_note(define_name, recently_modified, lateral)

        store.write_incident(
            project_id,
            signal="debug_loop",
            target_node=target_node,
            details=json.dumps({
                "file_path": file_path,
                "define_name": define_name,
                "chain_length": len(chain),
                "test_cases": test_cases,
                "test_tier": tier,
                "recently_modified_callees": recently_modified,
                "lateral": lateral,
                "direction_note": note,
                "consecutive_stable": 0,
            }),
        )


# ── regression helpers (§2.6) ────────────────────────────────────────────────

def _is_green_to_red(history: list) -> bool:
    """True iff the latest run is failed/error AND any prior run was passing.

    "一直红" (always failing) → False.  First-ever run → False (needs prior pass).
    """
    if len(history) < 2:
        return False
    if history[-1]["status"] not in ("failed", "error"):
        return False
    return any(h["status"] == "passed" for h in history[:-1])


# ── detect_regression (§2.6) ─────────────────────────────────────────────────

def detect_regression(
    store: Store,
    project_id: int,
    affected: list,   # [(file_path, define_name, det_id), ...]
    root: str,
    idx: callgraph.SymbolIndex,
) -> None:
    """Detect testcase green→red caused by production code changes (§2.6).

    Both conditions required:
      (1) testcase history shows passed→failed flip (_is_green_to_red)
      (2) an associated PRODUCTION define was modified this round (in affected)
          — this is what distinguishes regression (code broke the test) from
            tampering (test was edited to mask a failure, §2.7)

    Production-code gate: defines in test/generated paths (DUPLICATE_EXCLUDE_GLOBS)
    are skipped so that changes to the test file itself don't trigger regression.

    Wording is always a question (§2.6 原文):
      "test_login 之前通过，你这次改动后失败了，可能改坏了相关功能，是预期的吗？"
    Never asserts "you broke it" — the change may be an intentional update.

    θ₁=1: single green→red triggers immediately.
    Idempotent: skips testcases that already have an open regression incident.
    """
    for file_path, define_name, _det_id in affected:
        # Gate: production code only — test/generated file edits are excluded
        # (their green→red is §2.7 tampering territory, not regression)
        if _is_excluded_path(file_path):
            continue

        test_cases, tier = _find_test_cases_for_define(
            store, project_id, file_path, define_name, root
        )
        for tc in test_cases:
            parts = tc.split("::", 1)
            if len(parts) != 2:
                continue
            classname, name = parts

            target_node = tc  # classname::name — testcase is the direct target
            if _open_incident_for(store, project_id, "regression", target_node) is not None:
                continue

            history = store.test_case_history(project_id, classname, name)
            if not _is_green_to_red(history):
                continue

            store.write_incident(
                project_id,
                signal="regression",
                target_node=target_node,
                details=json.dumps({
                    "testcase": tc,
                    "classname": classname,
                    "name": name,
                    "affected_define": f"{file_path}::{define_name}",
                    "test_tier": tier,
                    "question": (
                        f"{name} was passing before; it failed after your latest edit "
                        f"— did this break related functionality, or is this expected?"
                    ),
                    "consecutive_stable": 0,
                }),
            )


# ── test_tampering helpers (§2.7) ────────────────────────────────────────────

def _is_red_to_green(history: list) -> bool:
    """True iff the latest run is passing AND any prior run was failed/error.

    "一直绿" (always passing) → False.  First-ever run → False (needs prior failure).
    Complement to _is_green_to_red (§2.6 regression vs §2.7 tampering).
    """
    if len(history) < 2:
        return False
    if history[-1]["status"] != "passed":
        return False
    return any(h["status"] in ("failed", "error") for h in history[:-1])


def _test_define_matches_testcase(define_name: str, classname: str, name: str) -> bool:
    """True if a define in a test file plausibly corresponds to this testcase.

    Matches on:
      - exact name match (define "test_fn" ↔ testcase name "test_fn")
      - class method: BOTH class segment and method segment match
        (define "TestJWT.test_x" ↔ classname "tests.TestJWT", name "test_x").
        Requires both parts to match to prevent cross-class collisions
        (TestA.test_init must not match TestB::test_init).
      - define is the test class itself (define "TestF" or "tests.TestF")
    """
    cls_simple = classname.rsplit(".", 1)[-1]
    if define_name == name:
        return True
    parts = define_name.split(".")
    if len(parts) >= 2 and parts[-1] == name and parts[-2] == cls_simple:
        return True
    return define_name in (classname, cls_simple)


# ── detect_test_tampering (§2.7) ─────────────────────────────────────────────

def _norm_test_path(path: str, root: str) -> str:
    """Normalize path to root-relative posix string for cross-source comparison.

    Handles absolute/relative mismatch between junit-reported file_path and
    reconcile-provided edited_files.

    Absolute path: resolved via realpath then made relative to root.
    Relative path: assumed already root-relative (JUnit convention) — normalise
                   separators only, no CWD-based resolution (which would be wrong).
    Fallback to basename when the path escapes root or ValueError on Windows drives.
    """
    try:
        if os.path.isabs(path):
            rel = os.path.relpath(os.path.realpath(path), os.path.realpath(root))
            if rel.startswith(".."):
                return os.path.basename(path)
            return rel.replace(os.sep, "/")
        # Relative path — trust it as root-relative, just normalise separators.
        norm = os.path.normpath(path).replace(os.sep, "/")
        return norm if not norm.startswith("..") else os.path.basename(path)
    except ValueError:
        return os.path.basename(path)


def detect_test_tampering(
    store: Store,
    project_id: int,
    affected: list,   # [(file_path, define_name, det_id), ...]
    root: str,
    idx: callgraph.SymbolIndex,
    edited_files: list[str] = (),
) -> None:
    """Detect testcase red→green caused by test code change, not production fix (§2.7).

    Three conditions required:
      (1) testcase history shows failed→passed flip (_is_red_to_green)
      (2) a TEST define was modified this round (excluded-path file in affected)
          whose name matches this testcase  [define-level, exact — pytest]
          OR the testcase's file_path was among the edited test files  [file-level fallback — vitest/jest]
      (3) no PRODUCTION define associated with this testcase was also modified
          (if production was also changed, the fix might be real — skip)

    File-level fallback rationale: vitest/jest tests use anonymous callbacks
    (describe/it) that yield 0 defines; the affected list is therefore always empty
    for those test files.  Passing edited_files (= changed_files from reconcile)
    lets the signal reach condition-2 via the test file itself instead of a define.
    Safety: condition 3 still guards against false positives — only "test file
    changed + no related production change + red→green" fires.

    Uses escalate_user_directly=True in details: advance_incidents routes
    directly to escalated_user, bypassing agent self-correction (§2.7 作弊类).

    θ₁=1: single red→green triggers immediately.
    Idempotent: skips testcases that already have an open test_tampering incident.

    Wording is always a question (§2.7):
      "改的是测试本身，不是被测代码，确认测试改对了吗？"
    """
    # Quick exit: no test-path defines in affected AND no test files edited
    test_affected = [
        (fp, dn, did) for fp, dn, did in affected if _is_excluded_path(fp)
    ]
    # File-level fallback: test files edited this round (vitest/jest have 0 defines).
    # parse.is_test_file covers .test.ts/.spec.ts and Python test_*.py equally.
    test_files_edited: frozenset[str] = frozenset(
        _norm_test_path(f, root) for f in edited_files if parse.is_test_file(f)
    )
    if not test_affected and not test_files_edited:
        return

    prod_affected = [
        (fp, dn, did) for fp, dn, did in affected if not _is_excluded_path(fp)
    ]

    for classname, name, tc_file_path in store.distinct_test_case_triples(project_id):
        tc = f"{classname}::{name}"
        if _open_incident_for(store, project_id, "test_tampering", tc) is not None:
            continue

        history = store.test_case_history(project_id, classname, name)
        if not _is_red_to_green(history):
            continue

        # Condition 2: define-level (pytest, precise) OR file-level (vitest, fallback)
        matching_test_def = next(
            (f"{fp}::{dn}" for fp, dn, _did in test_affected
             if _test_define_matches_testcase(dn, classname, name)),
            None,
        )
        if matching_test_def is None and test_files_edited:
            # File-level path resolution: prefer explicit tc_file_path, fall back to
            # classname when it looks like a file path (vitest JUnit classname=filename).
            tc_path_candidate = tc_file_path
            if tc_path_candidate is None and ("/" in classname or os.sep in classname
                                              or classname.endswith((".ts", ".tsx", ".js",
                                                                      ".jsx", ".py"))):
                tc_path_candidate = classname
            if tc_path_candidate is not None:
                tc_rel = _norm_test_path(tc_path_candidate, root)
                if tc_rel in test_files_edited:
                    matching_test_def = tc_path_candidate
        if matching_test_def is None:
            continue

        # Condition 3: window-based production analysis
        # Window = (red_seq, green_seq]: all production changes between last failure and current pass.
        green_seq = history[-1]["run_seq"]
        red_seq = next(
            h["run_seq"] for h in reversed(history[:-1])
            if h["status"] in ("failed", "error")
        )
        win = store.production_defines_in_seq_window(project_id, red_seq, green_seq)
        prod_win = [(fp, dn) for fp, dn in win if not _is_excluded_path(fp)]

        if prod_win:
            # Check whether any production define reliably explains the green.
            # Default: don't fire unless all tier==precise and none cover tc.
            should_fire = True
            for fp, dn in prod_win:
                tcs, tier = _find_test_cases_for_define(store, project_id, fp, dn, root)
                if tc in tcs:
                    should_fire = False  # possible real fix
                    break
                if tier in ("heuristic", "none"):
                    should_fire = False  # can't reliably exclude coverage
                    break
            if not should_fire:
                continue
        # prod_win empty → fire (no production changes can explain the green)

        store.write_incident(
            project_id,
            signal="test_tampering",
            target_node=tc,
            details=json.dumps({
                "testcase": tc,
                "classname": classname,
                "name": name,
                "test_define": matching_test_def,
                "question": "the change is to the test itself, not the code under test — confirm the test change is correct?",
                "escalate_user_directly": True,
                "consecutive_stable": 0,
            }),
        )


# ── detect_stuck_region (§2.2) ────────────────────────────────────────────────

def detect_stuck_region(
    store: Store,
    project_id: int,
    affected: list,   # [(file_path, define_name, det_id), ...]
    root: str,
    idx: callgraph.SymbolIndex,
) -> None:
    """Check each affected define; write a new incident if stuck_region criteria met.

    Idempotent: skips defines that already have an open/notified incident.
    Does NOT update existing incidents — advance_incidents handles recurrences.
    """
    for file_path, define_name, _det_id in affected:
        if _is_excluded_path(file_path):
            continue
        target_node = f"{file_path}::{define_name}"
        # Upgrade relationship (§2.2): debug_loop has claimed this define → skip
        if _open_incident_for(store, project_id, "debug_loop", target_node) is not None:
            continue
        if not _is_stuck(store, project_id, file_path, define_name):
            continue
        if _open_incident_for(store, project_id, "stuck_region", target_node) is not None:
            continue
        chain_len = len(store.version_chain(project_id, file_path, define_name))
        lateral = _lateral_context(store, project_id, file_path, define_name, root, idx)
        store.write_incident(
            project_id,
            signal="stuck_region",
            target_node=target_node,
            details=json.dumps({
                "file_path": file_path,
                "define_name": define_name,
                "chain_length": chain_len,
                "lateral": lateral,
                "consecutive_stable": 0,
            }),
        )


# ── detect_token_waste (v2.1 §1) ─────────────────────────────────────────────

def _parse_created_at(s: str) -> datetime | None:
    """Parse SQLite datetime string ("YYYY-MM-DD HH:MM:SS" or ISO with T)."""
    if not s:
        return None
    try:
        normalized = s.strip()
        if "T" not in normalized:
            normalized = normalized.replace(" ", "T", 1)
        return datetime.fromisoformat(normalized)
    except Exception:
        return None


def detect_token_waste(
    store: Store,
    project_id: int,
    affected: list,
) -> None:
    """Upgrade stuck_region/debug_loop to token_waste user alert (v2.1 §1).

    Not a new detector — reads version_chain + accumulated stuck/debug incidents.
    Three conditions (ALL must hold):
      (a) rounds >= THETA_WASTE (high investment)
      (b) open stuck_region or debug_loop incident (still not converged)
      (c) count_incidents_for_target >= 2 (pattern, not one-off)

    Delivers immediately to BOTH agent channel (relay to user) and user channel
    (stop-hook summary).  Does NOT enter the two-step state machine — the token_waste
    incident row serves only as a dedup anchor.

    SDT 地位: 投入度量是工程代理（轮数/时间/incident 数），无 SDT 对位.
    R2: 全部从 version_chain（编辑事件）+ incidents（BUER 自生成）派生，非 agent 声明.
    """
    for file_path, define_name, _det_id in affected:
        target_node = f"{file_path}::{define_name}"

        # Dedup: already warned → skip (token_waste incident is the dedup anchor)
        if _open_incident_for(store, project_id, "token_waste", target_node) is not None:
            continue

        # Condition (a): rounds >= THETA_WASTE
        chain = store.version_chain(project_id, file_path, define_name)
        rounds = len(chain)
        if rounds < THETA_WASTE:
            continue

        # Condition (b): still not converged (open stuck_region or debug_loop)
        has_open = (
            _open_incident_for(store, project_id, "stuck_region", target_node) is not None
            or _open_incident_for(store, project_id, "debug_loop", target_node) is not None
        )
        if not has_open:
            continue

        # Condition (c): pattern not one-off (>=2 historical stuck/debug incidents)
        incident_count = store.count_incidents_for_target(
            project_id, target_node, ["stuck_region", "debug_loop"]
        )
        if incident_count < 2:
            continue

        # Investment proxy: time span from first to last version
        times = [r["created_at"] for r in chain if r["created_at"]]
        time_span_hours = 0.0
        if len(times) >= 2:
            t0 = _parse_created_at(times[0])
            t1 = _parse_created_at(times[-1])
            if t0 and t1:
                time_span_hours = round(abs((t1 - t0).total_seconds()) / 3600.0, 1)

        inc_id = store.write_incident(
            project_id,
            signal="token_waste",
            target_node=target_node,
            details=json.dumps({
                "file_path": file_path,
                "define_name": define_name,
                "rounds": rounds,
                "time_span_hours": time_span_hours,
                "incident_count": incident_count,
                "proxy_note": "investment metric is an engineering proxy (rounds/time/incident count), not actual token billing",
            }),
        )

        # Immediate two-channel delivery (token_waste bypasses two-step state machine)
        inc_row = store.get_incident(inc_id)
        if inc_row is not None:
            delivery.queue_agent_injection(store, project_id, inc_row)
            delivery.queue_user_notification(store, project_id, inc_row)


# ── define_loop helpers (§2.1 全版) ──────────────────────────────────────────

def _error_sig_in_range(
    store: Store, project_id: int, seq_start: int, seq_end: int
) -> str | None:
    """Latest error_signature from crash_stacks with seq in (seq_start, seq_end]."""
    stacks = store.crash_stacks_in_seq_range(project_id, seq_start, seq_end)
    for s in reversed(stacks):
        try:
            sig = s["error_signature"]
        except (IndexError, KeyError):
            sig = None
        if sig:
            return sig
    return None


def _classify_error_type(
    sigs: list[str | None],
) -> tuple[str, str | None, str | None]:
    """Classify error evolution: ('type1'|'type2'|'resolved'|'no_data', current, prev).

    type1 = latest sig == all prior sigs (同一错没碰根源).
    type2 = latest sig != some prior sig (报错在变化).
    resolved = latest sig is None (最新轮无报错).
    no_data = all sigs None (没有任何崩溃记录).
    """
    if not sigs:
        return "no_data", None, None
    current = sigs[-1]
    prior = [s for s in sigs[:-1] if s is not None]
    if current is None:
        if not prior:
            return "no_data", None, None   # never any crash data — not "resolved"
        return "resolved", None, prior[-1]
    if not prior:
        return "no_data", current, None
    if all(s == current for s in prior):
        return "type1", current, prior[-1]
    return "type2", current, prior[-1]


def _cone_suspects(
    store: Store, project_id: int, file_path: str, define_name: str, root: str
) -> tuple[list[str], set[str]]:
    """Compute influence cone + crash-stack intersection suspects.

    Returns (suspects_list, intersection_set).
    suspects_list: up to 3 FQNs, intersection-preferred then delta-ranked.
    intersection_set: cone ∩ recent crash-stack FQNs.
    """
    mod = callgraph.module_name_of(file_path, root)
    fqn = callgraph._lang_fqn(file_path, mod, define_name)
    cone = influence.caller_cone_with_depth(store, project_id, [fqn])
    if not cone:
        return [], set()

    # Union of recent crash-stack FQNs
    recent = store.recent_crash_stacks(project_id, n=5)
    stack_union: set[str] = set()
    for cs in recent:
        try:
            stack_union |= set(json.loads(cs["stack_fqns"]))
        except Exception:
            pass

    intersection = influence.intersect_cone_with_stack(set(cone.keys()), stack_union)

    if intersection:
        suspects = sorted(intersection)[:3]
    else:
        ranks = influence.cone_priority_ranks(store, project_id, cone, top_n=3)
        suspects = [f for f, _ in ranks["delta"][:3]]

    return suspects, intersection


def _build_define_loop_message(
    define_name: str,
    consec: int,
    trigger_type: str,       # "loop" | "consec" | "both"
    error_class: str,        # "type1" | "type2" | "resolved" | "no_data"
    sig_current: str | None,
    sig_prev: str | None,
    suspects: list[str],
) -> str:
    """Build the agent-facing define_loop message based on trigger type and error class."""
    cone_str = ", ".join(suspects) if suspects else "(no data yet)"

    if trigger_type in ("loop", "both"):
        # Precise loop-back: early signal
        base = f"[BUER] {define_name} has reverted to a structural state it held earlier — this region is cycling rather than converging."
    elif error_class == "type1":
        base = (
            f"[BUER] {define_name} has been modified {consec} consecutive times; "
            f"the error signature is unchanged across these {consec} edits ({sig_current}). "
            f"Repeated edits here have not changed the error — consider whether the cause lies elsewhere."
        )
    elif error_class == "type2":
        sig_change = f"{sig_prev}→{sig_current}" if sig_prev else sig_current
        base = (
            f"[BUER] {define_name} has been modified {consec} consecutive times; "
            f"the error is shifting ({sig_change}) — possibly progressing, or possibly introducing new problems. "
            f"If stuck, consider reverting to a clean state."
        )
    else:
        # no_data or resolved — soft wording
        base = (
            f"[BUER] {define_name} has been modified {consec} consecutive times. "
            f"If the issue is unresolved, consider reverting and inspecting related locations."
        )

    return f"{base}\nrelated dependencies/callers: {cone_str}"


# ── detect_define_loop (§2.1 全版) ───────────────────────────────────────────

def detect_define_loop(
    store: Store,
    project_id: int,
    affected: list,   # [(file_path, define_name, det_id), ...]
    root: str,
    idx: callgraph.SymbolIndex,
) -> None:
    """Detect define_loop via two complementary triggers (§2.1 全版):

    Trigger A — 精确绕回: current fp matches any earlier chain version at
      distance >= N_LOOP_GAP.  Early signal — may fire with < 5 edits.

    Trigger B — 连续修改: consecutive_edit_count >= N_DEFINE_LOOP_CONSEC_AGENT (5).
      Catchall for iterative-but-not-identical thrashing.

    Error signature context (from crash_stacks.error_signature):
      type1 — same error throughout  → "未触及根源，建议恢复原状"
      type2 — error is changing      → "可能在推进，若卡住建议恢复"
      no_data / resolved             → soft wording

    Influence cone (caller_cone_with_depth + cone_priority_ranks) is computed
    and included in suspects, preferring the crash-stack intersection when available.

    Delivery: ≥5 consecutive → agent notification; ≥8 → also escalate user directly.
    Idempotent: skips defines that already have an open define_loop incident.
    """
    for file_path, define_name, _det_id in affected:
        if _is_excluded_path(file_path):
            continue
        target_node = f"{file_path}::{define_name}"
        if _open_incident_for(store, project_id, "define_loop", target_node) is not None:
            continue

        chain = store.version_chain(project_id, file_path, define_name)
        loop = _find_loop(chain)
        consec = store.consecutive_edit_count(project_id, file_path, define_name)

        trigger_loop = loop is not None
        trigger_consec = consec >= N_DEFINE_LOOP_CONSEC_AGENT

        if not (trigger_loop or trigger_consec):
            continue

        # Upgrade relationship: if the define qualifies for stuck_region (large d_J,
        # no loop, chain >= θ₁), Trigger B (consec-only) defers — stuck_region is
        # more informative.  Check _is_stuck() directly (not open-incident state) so
        # the gate works regardless of detector call order.  Trigger A (fingerprint
        # loop-back) fires regardless — it's a precise early signal.
        if trigger_consec and not trigger_loop:
            if (
                _is_stuck(store, project_id, file_path, define_name)
                or _open_incident_for(store, project_id, "debug_loop", target_node) is not None
            ):
                continue

        # Determine trigger type label
        if trigger_loop and trigger_consec:
            trigger_type = "both"
        elif trigger_loop:
            trigger_type = "loop"
        else:
            trigger_type = "consec"

        # Error signature analysis: last N rounds of the chain
        sigs: list[str | None] = []
        if len(chain) >= 2:
            max_seq = store.max_seq(project_id) or chain[-1]["seq"]
            # Last up to 5 rounds
            check_chain = chain[-5:]
            for i, det in enumerate(check_chain):
                seq_start = check_chain[i - 1]["seq"] if i > 0 else 0
                seq_end = det["seq"]
                sigs.append(_error_sig_in_range(store, project_id, seq_start, seq_end))
            # Plus crashes after the last edit (current state)
            if check_chain:
                sigs.append(_error_sig_in_range(store, project_id, check_chain[-1]["seq"], max_seq))

        error_class, sig_current, sig_prev = _classify_error_type(sigs)

        # Skip if latest round clearly resolved (no crash, not a loop-back)
        if error_class == "resolved" and not trigger_loop:
            continue

        # Influence cone + suspects
        suspects, intersection = _cone_suspects(store, project_id, file_path, define_name, root)

        # Loop-back metadata
        earlier_seq, later_seq, fp = loop if loop else (None, None, None)

        # Build message
        loop_question = _build_define_loop_message(
            define_name=define_name,
            consec=consec,
            trigger_type=trigger_type,
            error_class=error_class,
            sig_current=sig_current,
            sig_prev=sig_prev,
            suspects=suspects,
        )

        lateral = _lateral_context(store, project_id, file_path, define_name, root, idx)

        escalate_user = consec >= N_DEFINE_LOOP_CONSEC_USER

        inc_id = store.write_incident(
            project_id,
            signal="define_loop",
            target_node=target_node,
            details=json.dumps({
                "file_path": file_path,
                "define_name": define_name,
                "trigger_type": trigger_type,
                "consec": consec,
                "earlier_seq": earlier_seq,
                "later_seq": later_seq,
                "fingerprint": fp,
                "error_class": error_class,
                "error_sig_current": sig_current,
                "error_sig_prev": sig_prev,
                "suspects": suspects,
                "loop_question": loop_question,
                "lateral": lateral,
                "escalate_user_directly": escalate_user,
                "consecutive_stable": 0,
            }),
        )


# ── path exclusion helper (shared: regression / test_tampering) ──────────────

def _is_excluded_path(file_path: str) -> bool:
    """True if file_path is test/generated/boilerplate — excluded from detector scope.

    Three-stage check:
    1. Basename check for Next.js convention files (_NEXTJS_CONVENTION_BASENAMES).
    2. Basename starts with "test_" — matches test files at any path depth without
       accidentally excluding production code inside directories named test_*.
    3. Glob match against DUPLICATE_EXCLUDE_GLOBS (tests/ dir, generated, i18n-legal).
    """
    basename = os.path.basename(file_path)
    if basename in _NEXTJS_CONVENTION_BASENAMES:
        return True
    if fnmatch.fnmatch(basename, "test_*"):
        return True
    return any(fnmatch.fnmatch(file_path, pat) for pat in DUPLICATE_EXCLUDE_GLOBS)


# ── detect_boundary_breach (§2.4) ────────────────────────────────────────────

def detect_boundary_breach(
    store: Store,
    project_id: int,
    boundary_violations: list[str],
) -> None:
    """Write a boundary_breach incident for each out-of-project file path (§2.4).

    θ₁=1: a single out-of-project edit triggers immediately.
    target_node is the raw file path (not file::define — the file may never parse).
    Idempotent: skips paths that already have an open incident.
    Boundary resolution (realpath / multi-root / whitelist) is handled upstream
    in reconcile.py before boundary_violations is built.
    """
    for file_path in boundary_violations:
        if _open_incident_for(store, project_id, "boundary_breach", file_path) is not None:
            continue
        store.write_incident(
            project_id,
            signal="boundary_breach",
            target_node=file_path,
            details=json.dumps({
                "file_path": file_path,
                "note": f"you edited {file_path}, which is outside the project directory — boundary exceeded",
                "consecutive_stable": 0,
            }),
        )


# ── detect_task_scope_breach (§2.3, opt-in) ──────────────────────────────────

def detect_task_scope_breach(
    store: Store,
    project_id: int,
    affected: list,   # [(file_path, define_name, det_id), ...]
    root: str,
) -> None:
    """Detect edits outside the declared task scope (§2.3).

    Opt-in: only active when set_task_scope has been called.  Without a declared
    scope this detector is a no-op — boundary_breach (§2.4) still handles any
    edits outside the project root.

    Engineering constraint note (§2.3 / §5.3): range constraint is a
    entity-theoretic engineering requirement, not an SDT theorem.  SDT supplies
    vocabulary (ρ / R-member) to express it; the constraint itself is external.
    This is distinct from use-exclusivity; see §5.3 table row 6-7.

    θ₁=1: single out-of-scope edit triggers immediately.
    Idempotent: skips file paths that already have an open incident.
    Affected files have already passed the project-boundary check (boundary_breach
    handles files outside project root), so all paths here are inside the project.
    """
    scope = store.get_active_task_scope(project_id)
    if scope is None:
        return  # No scope declared → opt-in gate: no check

    allowed = scope["allowed"]
    forbidden = scope["forbidden"]

    for file_path, _define_name, _det_id in affected:
        target_node = file_path  # file-level target, same convention as boundary_breach
        if _open_incident_for(store, project_id, "task_scope_breach", target_node) is not None:
            continue

        try:
            rel_path = os.path.relpath(file_path, root)
        except ValueError:
            rel_path = file_path

        is_forbidden = any(fnmatch.fnmatch(rel_path, g) for g in forbidden)
        is_allowed = any(fnmatch.fnmatch(rel_path, g) for g in allowed)

        if not is_forbidden and is_allowed:
            continue  # within scope and not blacklisted

        # Breach — distinguish reason; forbidden takes priority over allowed
        if is_forbidden:
            matched_forbidden = [g for g in forbidden if fnmatch.fnmatch(rel_path, g)]
            note = f"you edited {rel_path}, which is in a forbidden zone: {', '.join(matched_forbidden)}"
            breach_reason = "forbidden"
        else:
            note = f"you edited {rel_path}, which is outside the declared task scope: {', '.join(allowed)}"
            breach_reason = "not_in_allowed"

        store.write_incident(
            project_id,
            signal="task_scope_breach",
            target_node=target_node,
            details=json.dumps({
                "file_path": file_path,
                "allowed_globs": allowed,
                "forbidden_globs": forbidden,
                "breach_reason": breach_reason,
                "note": note,
                "consecutive_stable": 0,
            }),
        )


# ── detect_dangling_reference (§2.8) ─────────────────────────────────────────

def detect_dangling_reference(
    store: Store,
    project_id: int,
    affected: list,           # [(file_path, define_name, det_id), ...]
    root: str,
    unresolved_calls: list,   # [{caller_define, callee_text, file, line}, ...]
) -> None:
    """Detect calls to project-undefined symbols in TS/TSX code (§2.8).

    Design constraints (焊死):
    - Language gate: only typescript/tsx with TS toolchain available. Dynamic
      languages (Python/JS) and toolchain-missing TS are no-ops (honest boundary
      §2.8: dynamic language legitimate dynamic refs would overwhelm; no language
      service → unreliable).  unresolved_calls is empty for those cases (gd.py).
    - Persistence gate (θ₁=2): same (caller_define, callee_text) must appear
      unresolved across ≥THETA_1_DANGLING determinations before firing.  Suppresses
      top-down "write call before writing definition" normal intermediate state.
    - Resolve: callee_text appears in project define set (exact short-name match),
      OR caller define deleted.  Call removal resolves via N_STABLE countdown.
    - Standard two-step (not direct-escalate): agent self-correction is expected.
    - Details include similar define suggestions (Levenshtein ≤2 or substring).
    """
    if not unresolved_calls:
        return

    # Current project defines — needed for _similar_defines suggestions only.
    # TS analysis is authoritative for unresolved detection; no string-matching filter here.
    all_defines = store.all_current_defines(project_id)

    # Index affected by (abs_file_path, define_name) → det_id
    affected_map: dict[tuple[str, str], int] = {}
    for fp, dn, did in affected:
        affected_map[(os.path.abspath(fp), dn)] = did

    for call in unresolved_calls:
        abs_file = call.get("file", "")
        caller_define = call.get("caller_define", "")
        callee_text = call.get("callee_text", "")
        if not abs_file or not caller_define or not callee_text:
            continue

        # Find canonical file_path and det_id from affected
        det_id = affected_map.get((abs_file, caller_define))
        if det_id is None:
            continue  # caller not changed this round — no observation to record

        # Reverse-map to the original (non-abs) file_path as stored in determinations
        canonical_fp = next(
            (fp for fp, dn, did in affected
             if dn == caller_define and os.path.abspath(fp) == abs_file),
            abs_file,
        )

        # Record observation; check persistence threshold
        obs_count = store.record_dangling_observation(
            project_id, canonical_fp, caller_define, callee_text, det_id
        )
        if obs_count < THETA_1_DANGLING:
            continue  # not yet persistent — suppress (§2.8 single-occurrence control)

        # target_node includes callee_text so each (caller_define, callee_text) pair
        # gets its own incident — one define may have multiple distinct unresolved calls.
        target_node = f"{canonical_fp}::{caller_define}::{callee_text}"
        if _open_incident_for(store, project_id, "dangling_reference", target_node) is not None:
            continue  # already open — advance_incidents handles recurrence

        # Build "是不是想调它" suggestion
        similar = _similar_defines(callee_text, all_defines)
        if similar:
            suggestion = ", ".join(s.split(".")[-1] if "." in s else s for s in similar)
            question = (
                f"{callee_text} (called repeatedly in {caller_define}) has no definition in the project; "
                f"the closest match is {suggestion} — did you mean to call that?"
            )
        else:
            question = (
                f"{callee_text} (called repeatedly in {caller_define}) has no definition in the project "
                "— possible typo or missing definition?"
            )

        store.write_incident(
            project_id,
            signal="dangling_reference",
            target_node=target_node,
            details=json.dumps({
                "file_path": canonical_fp,
                "caller_define": caller_define,
                "callee_text": callee_text,
                "similar_defines": similar,
                "question": question,
                "consecutive_stable": 0,
            }),
        )


# ── high-impact caller warning (agent channel, neutral, not an incident) ─────

def check_high_impact_defines(
    store: Store,
    project_id: int,
    file_path: str,
    session_id: str,
    warned_set: set[tuple[int, str, str]],
) -> list[str]:
    """Return neutral agent-facing notices for high-impact defines in file_path.

    A define is high-impact when its current determination has >= HIGH_IMPACT_CALLERS
    direct callers in gd_edges (COUNT of rows where from_det = det_id).

    NOT an incident. Never escalated to user. Unaffected by notification_level.
    Session-deduped: each (project_id, session_id, define_name) warned at most once
    per server process — prevents same define flooding across many edits to the same file.
    Lower-bound language ("at least N") is honest: gd_edges may undercount callers.
    Lists up to 3 caller define_names so the agent knows the concrete blast radius.
    """
    if not store.has_gd_edges(project_id):
        return []

    current_defines = store.recorded_defines_for_file(project_id, file_path)
    if not current_defines:
        return []

    messages: list[str] = []
    for define_name, (_coarse, _fine, det_id, _ch) in current_defines.items():
        dedup_key = (project_id, session_id, define_name)
        if dedup_key in warned_set:
            continue

        n = store.gd_caller_count(project_id, det_id)
        if n < HIGH_IMPACT_CALLERS:
            continue

        warned_set.add(dedup_key)

        names = store.gd_caller_names(project_id, det_id, limit=3)
        callers_str = ", ".join(names) if names else "(names unavailable)"
        messages.append(
            f"[BUER] {define_name} has at least {n} direct callers "
            f"(e.g. {callers_str}) — changes here propagate to these call sites."
        )

    return messages


# ── two-step state machine (§4.3) ────────────────────────────────────────────

def _theta_2(signal: str) -> int:
    return THETA_2.get(signal, THETA_2_DEFAULT)


def _resolve_reason(signal: str) -> str:
    return {
        "stuck_region":       "stable_region",
        "debug_loop":         "test_passed",       # objective closure (§2.2)
        "regression":         "test_recovered",    # test back to passing (§2.6)
        "test_tampering":     "really_fixed",      # suspicion cleared (§2.7)
        "define_loop":        "no_more_equiv",
        "boundary_breach":    "back_in_scope",
        "task_scope_breach":  "back_in_scope",
        "dangling_reference": "defined_or_removed",  # callee got defined or call removed (§2.8)
    }.get(signal, "resolved")


# ── name similarity helpers (§2.8 dangling reference "是不是想调它") ────────────

def _levenshtein(a: str, b: str) -> int:
    """Standard Levenshtein edit distance."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (0 if ca == cb else 1)))
        prev = curr
    return prev[len(b)]


def _similar_defines(callee_text: str, define_rows) -> list[str]:
    """Project defines whose short name is similar to callee_text.

    Criteria (§2.8): Levenshtein(callee_text, bare_name) ≤ 2, OR callee_text is a
    substring/prefix of bare_name (or vice versa).  bare_name = last component of
    dotted define_name ("MyClass.method" → "method").  Returns ≤3 suggestions,
    closest first.  Pure structural comparison — no semantic reasoning.
    """
    callee_lower = callee_text.lower()
    results: list[tuple[int, str]] = []
    for row in define_rows:
        name = row["define_name"] or ""
        if not name:
            continue
        bare = name.split(".")[-1] if "." in name else name
        bare_lower = bare.lower()
        if bare_lower == callee_lower:
            continue  # exact match → already handled by caller
        dist = _levenshtein(callee_lower, bare_lower)
        if dist <= 2:
            results.append((dist, name))
            continue
        if callee_lower in bare_lower or bare_lower.startswith(callee_lower):
            results.append((3, name))
    results.sort(key=lambda x: x[0])
    return [r[1] for r in results[:3]]


def _signal_recurred(
    store: Store,
    project_id: int,
    inc,
    affected: list,
    boundary_violations: list[str] | None = None,
    _ts_cache: dict | None = None,
) -> bool:
    """True if the signal's condition is still active this reconcile round.

    For stuck_region: any further modification to the stuck define counts.
    (The initial detection established it's stuck; subsequent edits keep it open.)
    Subsequent signals: add dispatchers here.
    """
    if inc["signal"] == "debug_loop":
        # Recurrence = at least one associated test case is still failing in its
        # most recent run.  Not gated on _is_stuck() — the structural condition was
        # the OPEN gate; keeping the incident open depends on test status alone.
        # Test results may update independently of edits (not gated on `affected`).
        details_raw = json.loads(inc["details"] or "{}")
        test_cases = details_raw.get("test_cases", [])
        for tc in test_cases:
            parts = tc.split("::", 1)
            if len(parts) != 2:
                continue
            classname, name = parts
            history = store.test_case_history(project_id, classname, name)
            if not history:
                continue
            if history[-1]["status"] in ("failed", "error"):
                return True
        return False
    if inc["signal"] == "regression":
        # Recurrence = testcase still failing in its most recent run.
        details_raw = json.loads(inc["details"] or "{}")
        classname = details_raw.get("classname", "")
        name = details_raw.get("name", "")
        if not classname or not name:
            return False
        history = store.test_case_history(project_id, classname, name)
        if not history:
            return False
        return history[-1]["status"] in ("failed", "error")
    if inc["signal"] == "test_tampering":
        # Suspicion clears (really_fixed) only when production code was modified
        # this round AND the testcase is currently passing — confirming a real fix.
        #
        # Suspicion persists if:
        #   - testcase is failing/error  (bug still present, nothing was fixed)
        #   - testcase passing but no production define in affected  (test still
        #     masking the failure; even if it went red and back green via another
        #     test edit, we don't resolve)
        details_raw = json.loads(inc["details"] or "{}")
        classname = details_raw.get("classname", "")
        name = details_raw.get("name", "")
        if not classname or not name:
            return False
        history = store.test_case_history(project_id, classname, name)
        if not history:
            return False
        if history[-1]["status"] in ("failed", "error"):
            return True  # bug still present — tampering suspicion active
        # Test is passing: real fix only if production code was also modified
        prod_was_modified = any(not _is_excluded_path(fp) for fp, _dn, _did in affected)
        return not prod_was_modified  # True = still suspicious; False = real fix
    if inc["signal"] == "stuck_region":
        target_node = inc["target_node"]
        file_path, define_name = target_node.split("::", 1)
        return any(fp == file_path and dn == define_name for fp, dn, _ in affected)
    if inc["signal"] == "define_loop":
        file_path, define_name = inc["target_node"].split("::", 1)
        if not any(fp == file_path and dn == define_name for fp, dn, _ in affected):
            return False
        chain = store.version_chain(project_id, file_path, define_name)
        if _latest_loops(chain):
            return True
        return store.consecutive_edit_count(project_id, file_path, define_name) >= N_DEFINE_LOOP_CONSEC_AGENT
    if inc["signal"] == "boundary_breach":
        # target_node is the violated file path (not file::define)
        return inc["target_node"] in (boundary_violations or [])
    if inc["signal"] == "task_scope_breach":
        # Recurs if the out-of-scope file is still being edited AND still breaches scope.
        # If scope was cleared, _signal_recurred returns False → stable → auto-resolve.
        target_file = inc["target_node"]
        if not any(fp == target_file for fp, _, _ in affected):
            return False  # file not touched this round
        scope = store.get_active_task_scope(project_id)
        if scope is None:
            return False  # scope cleared
        project = store.get_project(project_id)
        proj_root = project["root_path"] if project else ""
        try:
            rel_path = os.path.relpath(target_file, proj_root)
        except ValueError:
            rel_path = target_file
        is_forbidden = any(fnmatch.fnmatch(rel_path, g) for g in scope["forbidden"])
        is_allowed = any(fnmatch.fnmatch(rel_path, g) for g in scope["allowed"])
        return is_forbidden or not is_allowed
    if inc["signal"] == "dangling_reference":
        # Resolve condition: TS no longer reports callee_text as unresolved (callee
        # got defined, call removed, or caller deleted).  Resolution is authoritative:
        # re-run run_ts_dataflow_analysis rather than string-matching project defines
        # (different modules may share a bare name — string matching would false-resolve).
        details_raw = json.loads(inc["details"] or "{}")
        file_path = details_raw.get("file_path", "")
        caller_define = details_raw.get("caller_define", "")
        callee_text = details_raw.get("callee_text", "")
        if not file_path or not caller_define or not callee_text:
            return False
        # Caller define deleted → resolve immediately (before Node call)
        if store.current_version_determination(project_id, file_path, caller_define) is None:
            return False
        # Re-run TS analysis; resolve iff callee_text no longer in unresolved_calls.
        project = store.get_project(project_id)
        if project is None:
            return False
        root = project["root_path"]
        cache_key = (root, file_path, caller_define)
        if _ts_cache is not None and cache_key in _ts_cache:
            cached = _ts_cache[cache_key]
        else:
            toolchain = ts_dataflow.detect_ts_toolchain(root)
            if not toolchain["available"]:
                if _ts_cache is not None:
                    _ts_cache[cache_key] = None
                return True  # conservative: no toolchain → assume still active
            result = ts_dataflow.run_ts_dataflow_analysis(root, file_path, caller_define)
            cached = result if not result["degraded"] else None
            if _ts_cache is not None:
                _ts_cache[cache_key] = cached
        if cached is None:
            return True  # degraded → conservative
        current_unresolved = {u.get("callee_text") for u in cached.get("unresolved_calls", [])}
        return callee_text in current_unresolved
    if inc["signal"] == "parse_skipped":
        # Remains active until the file is explicitly resolved (parse_ok) by reconcile.
        # Always return True so advance_incidents never auto-resolves via stability
        # countdown; closure is driven by resolve_parse_skipped_incident only.
        return True
    return False


def advance_incidents(
    store: Store,
    project_id: int,
    affected: list,                           # [(file_path, define_name, det_id), ...]
    boundary_violations: list[str] | None = None,  # out-of-project paths from reconcile
) -> None:
    """Advance the two-step state machine for all open/notified incidents (§4.3).

    Delivery (§4.4): each state transition that produces a notification calls
    delivery.queue_agent_injection (agent channel) or
    delivery.queue_user_notification (user channel).  Messages are rendered from
    incident details and stored in pending_deliveries for the MCP server to take.
    """
    now = _utcnow()
    level = store.get_notification_level(project_id)
    # Cache TS analysis results per (root, file_path, caller_define) within this
    # advance pass — avoids N×Node subprocess calls when multiple dangling_reference
    # incidents share the same caller define.
    _ts_cache: dict = {}
    for inc in store.open_incidents(project_id):
        details: dict[str, Any] = json.loads(inc["details"] or "{}")
        state = inc["state"]

        if state == "open":
            if details.get("escalate_user_directly"):
                # Integrity signals (test_tampering, boundary_breach, task_scope_breach)
                # always escalate regardless of notification level.
                # Efficiency signals (define_loop) respect silent mode.
                eff = effective_escalation_theta2(level, inc["signal"], 1)
                if eff is not None:
                    store.update_incident(inc["id"], state="escalated_user", escalated_at=now)
                    delivery.queue_user_notification(store, project_id, inc)
                else:
                    # Silent mode, efficiency signal: notify agent only
                    store.update_incident(inc["id"], state="notified_agent", agent_notified_at=now)
                    delivery.queue_agent_injection(store, project_id, inc)
            else:
                store.update_incident(inc["id"], state="notified_agent", agent_notified_at=now)
                delivery.queue_agent_injection(store, project_id, inc)

        elif state == "notified_agent":
            if _signal_recurred(store, project_id, inc, affected, boundary_violations, _ts_cache=_ts_cache):
                new_count = (inc["post_notify_count"] or 0) + 1
                new_details = {**details, "consecutive_stable": 0}
                eff = effective_escalation_theta2(level, inc["signal"], _theta_2(inc["signal"]))
                if eff is None:
                    # Silent mode for efficiency signal: agent channel only, never escalate user
                    store.update_incident(
                        inc["id"],
                        post_notify_count=new_count,
                        details=json.dumps(new_details),
                    )
                    delivery.queue_agent_injection(store, project_id, inc)  # reminder
                elif new_count >= eff:
                    store.update_incident(
                        inc["id"],
                        state="escalated_user",
                        post_notify_count=new_count,
                        escalated_at=now,
                        details=json.dumps(new_details),
                    )
                    delivery.queue_user_notification(store, project_id, inc)
                else:
                    store.update_incident(
                        inc["id"],
                        post_notify_count=new_count,
                        details=json.dumps(new_details),
                    )
                    delivery.queue_agent_injection(store, project_id, inc)  # reminder
            else:
                # Not recurred: state-based signals resolve immediately; behaviour
                # signals use N_STABLE countdown (§4.3).
                if inc["signal"] in STATE_BASED_SIGNALS:
                    store.update_incident(
                        inc["id"],
                        state="resolved",
                        resolved_by=_resolve_reason(inc["signal"]),
                        details=json.dumps(details),
                    )
                else:
                    new_stable = details.get("consecutive_stable", 0) + 1
                    new_details = {**details, "consecutive_stable": new_stable}
                    if new_stable >= N_STABLE:
                        store.update_incident(
                            inc["id"],
                            state="resolved",
                            resolved_by=_resolve_reason(inc["signal"]),
                            details=json.dumps(new_details),
                        )
                    else:
                        store.update_incident(inc["id"], details=json.dumps(new_details))

        elif state == "escalated_user":
            if _signal_recurred(store, project_id, inc, affected, boundary_violations, _ts_cache=_ts_cache):
                # Still active; reset stable counter (user has been notified)
                new_details = {**details, "consecutive_stable": 0}
                store.update_incident(inc["id"], details=json.dumps(new_details))
            else:
                if inc["signal"] in STATE_BASED_SIGNALS:
                    store.update_incident(
                        inc["id"],
                        state="resolved",
                        resolved_by=_resolve_reason(inc["signal"]),
                        details=json.dumps(details),
                    )
                else:
                    new_stable = details.get("consecutive_stable", 0) + 1
                    new_details = {**details, "consecutive_stable": new_stable}
                    if new_stable >= N_STABLE:
                        store.update_incident(
                            inc["id"],
                            state="resolved",
                            resolved_by=_resolve_reason(inc["signal"]),
                            details=json.dumps(new_details),
                        )
                    else:
                        store.update_incident(inc["id"], details=json.dumps(new_details))


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

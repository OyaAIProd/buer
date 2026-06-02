"""Tests for analyze.mjs value-alias resolution.

Level 1: const fn = target; fn(x) → edge to target (not to fn).
Level 2: readonly property aliases from as-const object literals.

False-edge guards (3):
  - let/var alias: must NOT follow to the aliased target
  - conditional/ternary initializer: must NOT follow to either branch
  - mutable (non-readonly) property: must NOT follow to the initializer

Integration tests require node + typescript; skipped automatically when unavailable.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from buer import ts_dataflow


def _has_node():
    try:
        r = subprocess.run(["node", "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


_NEED_NODE = pytest.mark.skipif(not _has_node(), reason="node not available")


@pytest.fixture(scope="module")
def alias_project(tmp_path_factory):
    """Minimal TS project with alias scenarios + tsconfig + symlinked typescript."""
    root = tmp_path_factory.mktemp("alias_project")

    (root / "tsconfig.json").write_text(json.dumps({
        "compilerOptions": {"target": "ES2020", "module": "commonjs", "strict": True},
        "include": [str(root) + "/**/*"],
    }))

    nm = root / "node_modules"
    nm.mkdir()
    global_ts = subprocess.run(
        ["node", "-e",
         "console.log(require.resolve('typescript').replace('/typescript.js','').replace('/lib',''))"],
        capture_output=True, cwd=str(root), timeout=10,
    ).stdout.decode().strip()
    if not global_ts or not os.path.isdir(global_ts):
        npm_root = subprocess.run(
            ["npm", "root", "-g"], capture_output=True, timeout=10,
        ).stdout.decode().strip()
        global_ts = os.path.join(npm_root, "typescript")
    if os.path.isdir(global_ts):
        os.symlink(global_ts, str(nm / "typescript"))

    # producer.ts — real implementations
    (root / "producer.ts").write_text("""\
export function realFn(x: number): number { return x * 2; }
export function otherFn(x: number): number { return x + 1; }
""")

    # const_alias.ts — Level 1 positive: const fn = realFn
    (root / "const_alias.ts").write_text("""\
import { realFn } from './producer';
const fn = realFn;
export function consumer(x: number): number {
    const data = fn(x);
    return data;
}
""")

    # let_alias.ts — false-edge guard 1: let alias must not follow
    (root / "let_alias.ts").write_text("""\
import { realFn } from './producer';
let fn = realFn;
export function consumer(x: number): number {
    const data = fn(x);
    return data;
}
""")

    # cond_alias.ts — false-edge guard 2: conditional initializer must not follow
    (root / "cond_alias.ts").write_text("""\
import { realFn, otherFn } from './producer';
export function consumer(flag: boolean, x: number): number {
    const fn = flag ? realFn : otherFn;
    const data = fn(x);
    return data;
}
""")

    # chain_alias.ts — chained const aliases resolve to real fn
    (root / "chain_alias.ts").write_text("""\
import { realFn } from './producer';
const step1 = realFn;
const step2 = step1;
export function consumer(x: number): number {
    const data = step2(x);
    return data;
}
""")

    # readonly_alias.ts — Level 2 positive: as-const object property
    (root / "readonly_alias.ts").write_text("""\
import { realFn } from './producer';
const config = { process: realFn } as const;
export function consumer(x: number): number {
    const data = config.process(x);
    return data;
}
""")

    # mutable_alias.ts — false-edge guard 3: mutable property must not follow
    (root / "mutable_alias.ts").write_text("""\
import { realFn } from './producer';
const config = { process: realFn };
export function consumer(x: number): number {
    const data = config.process(x);
    return data;
}
""")

    # type_annotation_readonly.ts — Level 2 positive: explicit type-annotation readonly
    (root / "type_annotation_readonly.ts").write_text("""\
import { realFn } from './producer';
const cfg: { readonly process: (x: number) => number } = { process: realFn };
export function consumer(x: number): number {
    const data = cfg.process(x);
    return data;
}
""")

    return str(root)


@_NEED_NODE
class TestValueAlias:
    def test_const_alias_resolves_to_real_fn(self, alias_project):
        """const fn = realFn; fn(x) → edge to realFn, not to the alias variable fn."""
        result = ts_dataflow.run_ts_dataflow_analysis(
            alias_project, f"{alias_project}/const_alias.ts", "consumer",
        )
        assert result["degraded"] is False
        producers = {e["producer_define"] for e in result["edges"]}
        assert "realFn" in producers, f"must resolve through const alias; producers={producers}"
        assert "fn" not in producers, f"must not emit bare alias name; producers={producers}"

    def test_let_alias_does_not_follow(self, alias_project):
        """False-edge guard 1: let fn = realFn must NOT produce an edge to realFn."""
        result = ts_dataflow.run_ts_dataflow_analysis(
            alias_project, f"{alias_project}/let_alias.ts", "consumer",
        )
        assert result["degraded"] is False
        producers = {e["producer_define"] for e in result["edges"]}
        assert "realFn" not in producers, \
            f"let alias must not follow to realFn; producers={producers}"

    def test_conditional_alias_does_not_follow(self, alias_project):
        """False-edge guard 2: const fn = flag ? a : b must NOT resolve to either branch."""
        result = ts_dataflow.run_ts_dataflow_analysis(
            alias_project, f"{alias_project}/cond_alias.ts", "consumer",
        )
        assert result["degraded"] is False
        producers = {e["producer_define"] for e in result["edges"]}
        assert "realFn" not in producers, \
            f"conditional alias must not resolve realFn; producers={producers}"
        assert "otherFn" not in producers, \
            f"conditional alias must not resolve otherFn; producers={producers}"

    def test_chained_const_alias_resolves(self, alias_project):
        """step2 = step1 = realFn → recursive alias following reaches realFn."""
        result = ts_dataflow.run_ts_dataflow_analysis(
            alias_project, f"{alias_project}/chain_alias.ts", "consumer",
        )
        assert result["degraded"] is False
        producers = {e["producer_define"] for e in result["edges"]}
        assert "realFn" in producers, \
            f"chained const alias must resolve to realFn; producers={producers}"

    def test_readonly_property_alias_resolves(self, alias_project):
        """as-const property config.process = realFn → readonly alias resolves to realFn."""
        result = ts_dataflow.run_ts_dataflow_analysis(
            alias_project, f"{alias_project}/readonly_alias.ts", "consumer",
        )
        assert result["degraded"] is False
        producers = {e["producer_define"] for e in result["edges"]}
        assert "realFn" in producers, \
            f"readonly property alias must resolve to realFn; producers={producers}"

    def test_mutable_property_does_not_follow(self, alias_project):
        """False-edge guard 3: mutable config.process = realFn must NOT follow to realFn."""
        result = ts_dataflow.run_ts_dataflow_analysis(
            alias_project, f"{alias_project}/mutable_alias.ts", "consumer",
        )
        assert result["degraded"] is False
        producers = {e["producer_define"] for e in result["edges"]}
        assert "realFn" not in producers, \
            f"mutable property alias must not produce edge to realFn; producers={producers}"

    def test_const_alias_produces_single_edge(self, alias_project):
        """Following const alias must not duplicate edges (one edge to realFn, not two)."""
        result = ts_dataflow.run_ts_dataflow_analysis(
            alias_project, f"{alias_project}/const_alias.ts", "consumer",
        )
        assert result["degraded"] is False
        assert len(result["edges"]) == 1, \
            f"exactly one edge expected after alias resolution; edges={result['edges']}"
        assert result["edges"][0]["producer_define"] == "realFn"

    def test_type_annotation_readonly_resolves(self, alias_project):
        """Type-annotation readonly: const x: {readonly p: T} = {p: realFn}; x.p(n) → realFn."""
        result = ts_dataflow.run_ts_dataflow_analysis(
            alias_project, f"{alias_project}/type_annotation_readonly.ts", "consumer",
        )
        assert result["degraded"] is False
        producers = {e["producer_define"] for e in result["edges"]}
        assert "realFn" in producers, \
            f"type-annotation readonly must resolve to realFn; producers={producers}"

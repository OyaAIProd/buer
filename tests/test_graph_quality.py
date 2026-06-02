"""Tests for graph-quality improvements:
  1. is_test_file — convention-based test file detection
  2. @/ alias import resolution via tsconfig.json paths
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from buer.parse import (
    _load_alias_map,
    _ALIAS_MAP_CACHE,
    _js_resolve_import_path,
    _parse_tsconfig_json,
    extract_module_imports,
    is_test_file,
)


# ── is_test_file ──────────────────────────────────────────────────────────────

class TestIsTestFileJsTs:
    # ── files that MUST be detected as test ──
    def test_dot_test_ts(self):
        assert is_test_file("/proj/src/auth.test.ts")

    def test_dot_test_tsx(self):
        assert is_test_file("/proj/components/Button.test.tsx")

    def test_dot_test_js(self):
        assert is_test_file("/proj/utils.test.js")

    def test_dot_test_jsx(self):
        assert is_test_file("/proj/App.test.jsx")

    def test_dot_spec_ts(self):
        assert is_test_file("/proj/lib/parser.spec.ts")

    def test_dot_spec_tsx(self):
        assert is_test_file("/proj/components/Modal.spec.tsx")

    def test_dot_e2e_ts(self):
        assert is_test_file("/proj/flows/login.e2e.ts")

    def test_in_tests_dir(self):
        assert is_test_file("/proj/tests/api/auth.ts")

    def test_in_tests_dir_helper(self):
        assert is_test_file("/proj/tests/helpers/mock-factories.ts")

    def test_in_tests_dir_setup(self):
        assert is_test_file("/proj/tests/setup.ts")

    def test_in_dunder_tests_dir(self):
        assert is_test_file("/proj/src/__tests__/auth.ts")

    def test_in_e2e_dir(self):
        assert is_test_file("/proj/e2e/flows/login.ts")

    def test_in_e2e_dir_nested(self):
        assert is_test_file("/proj/tests/e2e/helpers/wait.ts")

    def test_in_cypress_dir(self):
        assert is_test_file("/proj/cypress/integration/login.ts")


class TestIsTestFileJsTsNegative:
    """Production files that must NOT be excluded."""

    def test_vitest_config(self):
        assert not is_test_file("/proj/vitest.config.ts")

    def test_vitest_config_integration(self):
        assert not is_test_file("/proj/vitest.config.integration.ts")

    def test_script_test_smtp(self):
        # scripts/test-smtp.ts has "test" in filename but no .test. suffix
        assert not is_test_file("/proj/scripts/test-smtp.ts")

    def test_regular_ts_file(self):
        assert not is_test_file("/proj/lib/auth/helpers.ts")

    def test_route_file(self):
        assert not is_test_file("/proj/app/api/v3/projects/route.ts")

    def test_component_file(self):
        assert not is_test_file("/proj/components/Button.tsx")

    def test_lib_utils(self):
        assert not is_test_file("/proj/lib/utils.ts")

    def test_no_test_in_name_at_all(self):
        assert not is_test_file("/proj/src/pipeline.ts")

    def test_test_substring_in_dir_name_non_exact(self):
        # "contest" contains "test" but is not an exact match of "tests"
        assert not is_test_file("/proj/contest/entry.ts")

    def test_testing_dir_not_excluded(self):
        # "testing" is not in _TEST_DIR_NAMES (only exact "tests")
        assert not is_test_file("/proj/testing-utils/helpers.ts")


class TestIsTestFilePython:
    def test_test_prefix(self):
        assert is_test_file("/proj/tests/test_auth.py")

    def test_test_suffix(self):
        assert is_test_file("/proj/src/auth_test.py")

    def test_conftest(self):
        assert is_test_file("/proj/conftest.py")

    def test_conftest_in_subdir(self):
        assert is_test_file("/proj/tests/conftest.py")

    def test_in_tests_dir(self):
        assert is_test_file("/proj/tests/helpers.py")

    def test_regular_module(self):
        assert not is_test_file("/proj/lib/auth.py")

    def test_testing_module(self):
        # "testing.py" has "test" as prefix? No — "testing" != "test_"
        assert not is_test_file("/proj/lib/testing.py")

    def test_attest_module(self):
        assert not is_test_file("/proj/lib/attest.py")


# ── @/ alias import resolution ────────────────────────────────────────────────

class TestLoadAliasMap:
    def test_reads_at_slash_from_tsconfig(self, tmp_path):
        _ALIAS_MAP_CACHE.clear()
        tsconfig = {
            "compilerOptions": {
                "baseUrl": ".",
                "paths": {"@/*": ["./*"]},
            }
        }
        (tmp_path / "tsconfig.json").write_text(json.dumps(tsconfig))
        result = _load_alias_map(str(tmp_path))
        assert "@/" in result
        assert result["@/"] == ""     # maps to project root

    def test_baseurl_respected(self, tmp_path):
        _ALIAS_MAP_CACHE.clear()
        tsconfig = {
            "compilerOptions": {
                "baseUrl": "src",
                "paths": {"~/*": ["src/*"]},
            }
        }
        (tmp_path / "tsconfig.json").write_text(json.dumps(tsconfig))
        result = _load_alias_map(str(tmp_path))
        assert "~/" in result

    def test_no_tsconfig_returns_empty(self, tmp_path):
        _ALIAS_MAP_CACHE.clear()
        result = _load_alias_map(str(tmp_path))
        assert result == {}

    def test_invalid_json_returns_empty(self, tmp_path):
        _ALIAS_MAP_CACHE.clear()
        (tmp_path / "tsconfig.json").write_text("{ invalid json }")
        result = _load_alias_map(str(tmp_path))
        assert result == {}

    def test_line_comments_stripped(self, tmp_path):
        _ALIAS_MAP_CACHE.clear()
        tsconfig_text = """{
  // project tsconfig
  "compilerOptions": {
    "baseUrl": ".",
    "paths": { "@/*": ["./*"] } // alias
  }
}"""
        (tmp_path / "tsconfig.json").write_text(tsconfig_text)
        result = _load_alias_map(str(tmp_path))
        assert "@/" in result

    def test_glob_patterns_not_eaten_as_block_comments(self, tmp_path):
        # Regression: "**/*.ts" contains /* which must NOT be treated as a block comment.
        _ALIAS_MAP_CACHE.clear()
        tsconfig_text = json.dumps({
            "compilerOptions": {"paths": {"@/*": ["./*"]}},
            "include": ["**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
        })
        (tmp_path / "tsconfig.json").write_text(tsconfig_text)
        result = _load_alias_map(str(tmp_path))
        assert "@/" in result

    def test_caching(self, tmp_path):
        _ALIAS_MAP_CACHE.clear()
        (tmp_path / "tsconfig.json").write_text(
            json.dumps({"compilerOptions": {"paths": {"@/*": ["./*"]}}})
        )
        r1 = _load_alias_map(str(tmp_path))
        r2 = _load_alias_map(str(tmp_path))
        assert r1 is r2   # same object — came from cache

    def test_no_paths_key_returns_empty(self, tmp_path):
        _ALIAS_MAP_CACHE.clear()
        (tmp_path / "tsconfig.json").write_text(
            json.dumps({"compilerOptions": {"baseUrl": "."}})
        )
        result = _load_alias_map(str(tmp_path))
        assert result == {}


class TestJsResolveImportPathAlias:
    def _setup_tsconfig(self, tmp_path):
        _ALIAS_MAP_CACHE.clear()
        tsconfig = {"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./*"]}}}
        (tmp_path / "tsconfig.json").write_text(json.dumps(tsconfig))

    def test_at_slash_resolves_to_root_relative(self, tmp_path):
        self._setup_tsconfig(tmp_path)
        from_file = str(tmp_path / "app" / "route.ts")
        result = _js_resolve_import_path("@/lib/utils", from_file, str(tmp_path))
        assert result == "lib/utils"

    def test_at_slash_with_extension_stripped(self, tmp_path):
        self._setup_tsconfig(tmp_path)
        from_file = str(tmp_path / "app" / "route.ts")
        result = _js_resolve_import_path("@/lib/auth.ts", from_file, str(tmp_path))
        assert result == "lib/auth"

    def test_at_slash_nested_path(self, tmp_path):
        self._setup_tsconfig(tmp_path)
        from_file = str(tmp_path / "app" / "api" / "route.ts")
        result = _js_resolve_import_path("@/lib/db/postgres", from_file, str(tmp_path))
        assert result == "lib/db/postgres"

    def test_relative_still_works(self, tmp_path):
        self._setup_tsconfig(tmp_path)
        from_file = str(tmp_path / "lib" / "auth.ts")
        result = _js_resolve_import_path("./helpers", from_file, str(tmp_path))
        assert result == "lib/helpers"

    def test_external_package_still_none(self, tmp_path):
        self._setup_tsconfig(tmp_path)
        from_file = str(tmp_path / "app" / "route.ts")
        result = _js_resolve_import_path("react", from_file, str(tmp_path))
        assert result is None

    def test_at_slash_no_tsconfig_returns_none(self, tmp_path):
        _ALIAS_MAP_CACHE.clear()
        from_file = str(tmp_path / "app" / "route.ts")
        result = _js_resolve_import_path("@/lib/utils", from_file, str(tmp_path))
        assert result is None

    def test_at_slash_no_root_returns_none(self):
        result = _js_resolve_import_path("@/lib/utils", "/some/file.ts", "")
        assert result is None


class TestExtractModuleImportsWithAlias:
    """Integration: extract_module_imports correctly maps @/ imports."""

    def test_named_import_via_alias(self, tmp_path):
        _ALIAS_MAP_CACHE.clear()
        tsconfig = {"compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["./*"]}}}
        (tmp_path / "tsconfig.json").write_text(json.dumps(tsconfig))

        # Create the target module so idx would pick it up
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "utils.ts").write_text("export function cn() {}")

        # Caller imports via @/
        caller = tmp_path / "app" / "page.tsx"
        caller.parent.mkdir()
        caller.write_text("import { cn } from '@/lib/utils';\n")

        imap = extract_module_imports(str(caller), root=str(tmp_path))
        assert "cn" in imap
        assert imap["cn"] == "lib/utils.cn"

    def test_default_import_via_alias(self, tmp_path):
        _ALIAS_MAP_CACHE.clear()
        (tmp_path / "tsconfig.json").write_text(
            json.dumps({"compilerOptions": {"paths": {"@/*": ["./*"]}}})
        )
        caller = tmp_path / "app" / "page.tsx"
        caller.parent.mkdir()
        caller.write_text("import Auth from '@/lib/auth';\n")

        imap = extract_module_imports(str(caller), root=str(tmp_path))
        assert "Auth" in imap
        assert imap["Auth"] == "lib/auth"

    def test_no_tsconfig_alias_not_resolved(self, tmp_path):
        _ALIAS_MAP_CACHE.clear()
        caller = tmp_path / "app" / "page.tsx"
        caller.parent.mkdir()
        caller.write_text("import { cn } from '@/lib/utils';\n")

        imap = extract_module_imports(str(caller), root=str(tmp_path))
        # Without tsconfig, @/ is unresolvable → not in map
        assert "cn" not in imap

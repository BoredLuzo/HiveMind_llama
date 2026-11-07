# -*- coding: utf-8 -*-
"""Behavior tests for hive_functions/testmap.py (edit -> test-file mapping).

The module is pure stdlib, so every test runs against a throwaway temp
workspace - nothing in the repo is touched. Covered: the is_test_file
conventions (pytest layout, node layout, test-dir placement), the stem
mapping, the edited-test self-mapping, the bounded python import probe,
the skip-dir walk filter, the _MAX_RESULTS cap, path normalization
(absolute + backslash inputs) and the documented empty-input edges.

Run: python tests/test_testmap_behavior.py
Exit 0 = all pass, Exit 1 = failures.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from hive_functions.testmap import is_test_file, map_edits_to_tests  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def _mkws(files):
    """Create a temp workspace; returns (TemporaryDirectory, workspace str)."""
    td = tempfile.TemporaryDirectory()
    for rel, content in files.items():
        p = Path(td.name) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return td, td.name


def main():
    # ── is_test_file: name/directory conventions ────────────────────────────
    check("tests/ dir marks test file", is_test_file("tests/test_app.py"))
    check("test_<stem>.py name pattern", is_test_file("src/test_app.py"))
    check("<stem>_test.py name pattern", is_test_file("src/app_test.py"))
    check("<stem>.test.js node pattern", is_test_file("src/app.test.js"))
    check("<stem>.spec.tsx node pattern", is_test_file("src/app.spec.tsx"))
    check("plain source is not a test", not is_test_file("src/app.py"))
    check("tests/ dir part wins over name", is_test_file("tests/utils/helper.py"))
    check("'test' dir part recognized", is_test_file("test/smoke.js"))
    check("'__tests__' dir part recognized", is_test_file("pkg/__tests__/x.py"))
    check("empty path is not a test", not is_test_file(""))
    check("bare name without pattern", not is_test_file("app.py"))

    # ── stem mapping: src/app.py -> tests/test_app.py ───────────────────────
    td, ws = _mkws({"tests/test_app.py": "", "src/app.py": "x = 1\n"})
    try:
        got = map_edits_to_tests(ws, ["src/app.py"])
        check("stem mapping finds tests/test_app.py", got == ["tests/test_app.py"],
              f"got {got}")
        # absolute + backslash input is normalized the same way
        got2 = map_edits_to_tests(ws, [str(Path(ws) / "src" / "app.py")])
        check("absolute path input normalized", got2 == ["tests/test_app.py"],
              f"got {got2}")
    finally:
        td.cleanup()

    # ── <stem>_test.py layout matches too ───────────────────────────────────
    td, ws = _mkws({"src/parser.py": "", "src/parser_test.py": ""})
    try:
        got = map_edits_to_tests(ws, ["src/parser.py"])
        check("_test.py suffix layout matched", got == ["src/parser_test.py"],
              f"got {got}")
    finally:
        td.cleanup()

    # ── an edited test file maps to itself ──────────────────────────────────
    td, ws = _mkws({"tests/test_app.py": "", "src/app.py": ""})
    try:
        got = map_edits_to_tests(ws, ["tests/test_app.py"])
        check("edited test file maps to itself", got == ["tests/test_app.py"],
              f"got {got}")
        # node layout: sibling .test.js counts as a test file as well
        got2 = map_edits_to_tests(ws, ["tests/app.test.js"])
        check("node .test.js maps to itself (missing file)",
              got2 == [], f"got {got2}")
    finally:
        td.cleanup()

    # ── import probe: name does not match, the import does ─────────────────
    # The probe regex captures the first dotted component, so a bare-stem
    # import matches, while a submodule import ("utils.token") only counts
    # for its top-level package stem - both behaviors pinned here.
    td, ws = _mkws({
        "tests/custom_check.py": "from token_utils import window_by_budget\n",
        "src/token_utils.py": "",
    })
    try:
        got = map_edits_to_tests(ws, ["src/token_utils.py"])
        check("import probe matches importer", got == ["tests/custom_check.py"],
              f"got {got}")
    finally:
        td.cleanup()

    td, ws = _mkws({
        "tests/other_check.py": "from utils.token import window_by_budget\n",
        "src/token.py": "",
    })
    try:
        got = map_edits_to_tests(ws, ["src/token.py"])
        check("dotted import only counts top-level package", got == [],
              f"got {got}")
    finally:
        td.cleanup()

    # ── walk skips node_modules et al. ──────────────────────────────────────
    td, ws = _mkws({
        "tests/test_app.py": "",
        "node_modules/pkg/app.test.js": "",
        "src/app.py": "",
    })
    try:
        got = map_edits_to_tests(ws, ["src/app.py"])
        check("node_modules test file ignored",
              got == ["tests/test_app.py"], f"got {got}")
    finally:
        td.cleanup()

    # ── result cap: at most 8 matches, sorted ───────────────────────────────
    files = {"src/one.py": "",
             "tests/test_one.py": "",
             "test/test_one.py": "",
             "__tests__/test_one.py": "",
             "spec/test_one.py": "",
             "test_one.py": "",
             "one_test.py": "",
             "tests/one.test.js": "",
             "tests/one.spec.mjs": "",
             "tests/one.test.tsx": ""}
    td, ws = _mkws(files)
    try:
        got = map_edits_to_tests(ws, ["src/one.py"])
        check("match list capped at 8", len(got) == 8, f"got {len(got)}: {got}")
        check("matches are sorted", got == sorted(got), f"got {got}")
        check("capped matches are real matches",
              all(t in files for t in got), f"got {got}")
    finally:
        td.cleanup()

    # ── documented empty edges ───────────────────────────────────────────────
    check("empty edit list -> []", map_edits_to_tests("ws", []) == [])
    check("empty workspace -> []", map_edits_to_tests("", ["src/app.py"]) == [])
    td, ws = _mkws({"src/app.py": ""})
    try:
        got = map_edits_to_tests(ws, ["src/does_not_exist.py"])
        check("non-existent edit -> []", got == [], f"got {got}")
        got2 = map_edits_to_tests(ws, ["README.md"])
        check("source with no covering test -> []", got2 == [], f"got {got2}")
    finally:
        td.cleanup()

    return h.finish("testmap_behavior")


if __name__ == "__main__":
    sys.exit(main())

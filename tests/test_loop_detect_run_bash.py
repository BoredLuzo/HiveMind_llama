"""Behavioral test: Problem 2 - run_bash differentiation for loop detection.

Verifies:
  A) signature_changed() (hive_functions/chunking.py) in isolation - all cases from
     the spec: True on ANY uncertainty/change, False ONLY on an explicit match.
  B) hook semantics (duo_runner, explore/write branch): run_bash-only round
     without file changes -> neutral (counter stays); with changes -> reset;
     run_bash + a real write tool -> unconditional reset.
  C) diff-null on build_file_signature/file_signature_matches (cache path not
     touched) - source reference comparison.
  D) source structure: snapshot closure + hook present in duo_runner.

Run: python tests/test_loop_detect_run_bash.py
Exit 0 = all pass, Exit 1 = failures.
"""
from __future__ import annotations
import inspect
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hive_functions.chunking import (  # noqa: E402
    signature_changed,
    resolve_explore_reset,
    compute_bash_changed,
)

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


# ── A: signature_changed isoliert ────────────────────────────────────────────

def test_signature_changed():
    s1 = {"mtime": 1000.0, "size": 10}
    s2 = {"mtime": 1000.05, "size": 10}
    s3 = {"mtime": 1000.2, "size": 10}
    s4 = {"mtime": 1000.0, "size": 11}
    h1 = {"mtime": 1000.0, "size": 10, "sha1": "abc123"}
    h2 = {"mtime": 1000.0, "size": 10, "sha1": "abc124"}
    if signature_changed(s1, s2) is False:
        ok("A1: mtime within tolerance (0.05s) + same size -> False (unchanged)")
    else:
        fail("A1: mtime tolerance match", signature_changed(s1, s2))
    if signature_changed(s1, s3) is True:
        ok("A2: mtime diff > 0.1s -> True")
    else:
        fail("A2: mtime diff")
    if signature_changed(s1, s4) is True:
        ok("A3: size diff -> True")
    else:
        fail("A3: size diff")
    if signature_changed(h1, h2) is True:
        ok("A4: sha1 diff (same mtime/size) -> True")
    else:
        fail("A4: sha1 diff")
    if signature_changed(h1, h1) is False:
        ok("A5: sha1 identical on both sides -> False")
    else:
        fail("A5: sha1 identical")
    if signature_changed(None, s1) is True:
        ok("A6: before=None (new) -> True")
    else:
        fail("A6: before=None")
    if signature_changed(s1, None) is True:
        ok("A7: after=None (deleted) -> True")
    else:
        fail("A7: after=None")
    if signature_changed(None, None) is True:
        ok("A8: both None -> True (conservative: persistent errors not invisible)")
    else:
        fail("A8: both None")
    if signature_changed({}, {}) is True:
        ok("A9: no criteria -> True (conservative)")
    else:
        fail("A9: no criteria")
    partial = {"mtime": 1000.0}
    if signature_changed(partial, s1) is True:
        ok("A10: size missing on one side, no sha1 -> True (insufficient)")
    else:
        fail("A10: insufficient criteria")


# ── B: resolve_explore_reset + compute_bash_changed - real functions ──────

def _build_snap(paths):
    from hive_functions.chunking import build_file_signature
    out = {}
    for p in paths:
        s = build_file_signature(p)
        if s:
            out[str(p)] = s
    return out


def test_compute_bash_changed_direct():
    """compute_bash_changed() imported+called directly - all spec cases."""
    s = {"mtime": 1000.0, "size": 10}
    if compute_bash_changed(None, {"a.py": s}) is None:
        ok("CBC1: before=None -> None (no comparison)")
    else:
        fail("CBC1: before=None", compute_bash_changed(None, {"a.py": s}))
    if compute_bash_changed({}, {"a.py": s}) is None:
        ok("CBC2: before={} (no candidates) -> None")
    else:
        fail("CBC2: before empty")
    if compute_bash_changed({"a.py": s}, None) is None:
        ok("CBC3: after=None (snapshot error) -> None")
    else:
        fail("CBC3: after=None")
    if compute_bash_changed({"a.py": s}, {"a.py": s}) is False:
        ok("CBC4: before/after identical -> False (all unchanged)")
    else:
        fail("CBC4: identical")
    if compute_bash_changed({"a.py": s}, {"a.py": {"mtime": 1001.0, "size": 10}}) is True:
        ok("CBC5: one change -> True")
    else:
        fail("CBC5: change")


def test_hook_snapshot_failure_path():
    """The untested path from the last round: the SECOND snapshot (after the
    tool execution) fails -> _snap_after=None -> compute_bash_changed
    returns None -> resolve_explore_reset falls back to 0 (reset), not neutral.
    Mocks the closure semantics: the closure never throws, it returns None on error."""
    calls = {"n": 0}

    def _fake_snapshot():
        calls["n"] += 1
        if calls["n"] == 1:
            return {"a.py": {"mtime": 1000.0, "size": 10}}
        return None  # 2nd call: snapshot failed (closure error semantics)

    # hook sequence exactly like in production code (duo_runner, explore/write branch)
    _snap_before = _fake_snapshot()
    counter = 3
    _bash_changed = None
    if {"run_bash"} == {"run_bash"} and _snap_before is not None:
        _snap_after = _fake_snapshot()
        _bash_changed = compute_bash_changed(_snap_before, _snap_after)
    _result = resolve_explore_reset({"run_bash"}, _bash_changed, counter)
    if calls["n"] == 2:
        ok("SF1: snapshot called 2x (before + after tool execution)")
    else:
        fail("SF1: call count", calls["n"])
    if _bash_changed is None:
        ok("SF2: failed after-snapshot -> bash_changed=None (not False)")
    else:
        fail("SF2: bash_changed wrong", _bash_changed)
    if _result == 0:
        ok("SF3: snapshot error -> counter to 0 (reset, not neutral)")
    else:
        fail("SF3: counter wrong", _result)


def test_resolve_explore_reset_direct():
    """All four spec cases: resolve_explore_reset() imported+called directly."""
    if resolve_explore_reset({"run_bash"}, True, 5) == 0:
        ok("B0: run_bash-only + changed=True -> 0 (reset)")
    else:
        fail("B0: run_bash-only + True")
    if resolve_explore_reset({"run_bash"}, False, 5) == 5:
        ok("B0b: run_bash-only + changed=False -> current_value UNCHANGED (5)")
    else:
        fail("B0b: run_bash-only + False", resolve_explore_reset({"run_bash"}, False, 5))
    if resolve_explore_reset({"run_bash"}, None, 5) == 0:
        ok("B0c: run_bash-only + changed=None (snapshot failed) -> 0 (reset)")
    else:
        fail("B0c: run_bash-only + None")
    for bc in (True, False, None):
        if resolve_explore_reset({"run_bash", "edit_file"}, bc, 5) == 0:
            ok(f"B0d: run_bash + edit_file, bash_changed={bc} -> 0 (unconditional)")
        else:
            fail(f"B0d: run_bash + edit_file, bash_changed={bc}")
    if resolve_explore_reset({"run_tests"}, None, 5) == 0:
        ok("B0e: run_tests (write without run_bash) -> 0 (unconditional)")
    else:
        fail("B0e: run_tests")


def test_hook_semantics():
    """B1-B4 with real temp files - but the decision is made in the REAL
    function resolve_explore_reset(), not in a replica."""
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "a.py"
        f.write_text("x = 1\n", encoding="utf-8")
        paths = [f]
        snap1 = _build_snap(paths)
        counter = 3
        # B1: run_bash-only, no change -> snapshot comparison returns False ->
        # resolve_explore_reset returns current_value (3) - the value stays.
        changed = any(signature_changed(b, snap1.get(p)) for p, b in snap1.items())
        if resolve_explore_reset({"run_bash"}, changed, counter) == counter:
            ok("B1: run_bash-only without change -> counter stays (3)")
        else:
            fail("B1: neutral case", resolve_explore_reset({"run_bash"}, changed, counter))
        # B2: run_bash-only, content + mtime changed -> reset to 0
        f.write_text("x = 2\n", encoding="utf-8")
        os.utime(f, (1000.0, 1000.0))
        snap2 = _build_snap(paths)
        changed2 = any(signature_changed(b, snap2.get(p)) for p, b in snap1.items())
        if changed2 is True and resolve_explore_reset({"run_bash"}, changed2, counter) == 0:
            ok("B2: run_bash-only WITH file change -> reset to 0")
        else:
            fail("B2: reset case", changed2)
        # B3: run_bash + a real write tool -> unconditional reset
        if resolve_explore_reset({"run_bash", "edit_file"}, None, counter) == 0:
            ok("B3: run_bash + edit_file -> Reset unconditional")
        else:
            fail("B3: unconditional branch")
        # B4: write tool without run_bash -> unconditional reset
        if resolve_explore_reset({"run_tests"}, None, counter) == 0:
            ok("B4: write tool without run_bash (run_tests) -> unconditional reset")
        else:
            fail("B4: write-tool-without-run_bash branch")


# ── C: diff-null on the cache-path functions ──────────────────────────────────

_REF_BUILD_FILE_SIGNATURE = '''def build_file_signature(path_like: str | Path) -> dict | None:
    """Build a lightweight file signature (mtime, size, sha1) for change detection."""
    p = Path(path_like)
    try:
        st = p.stat()
    except OSError:
        return None
    if not p.is_file():
        return None
    sig: dict[str, Any] = {
        "mtime": float(st.st_mtime),
        "size": int(st.st_size),
    }
    try:
        sig["sha1"] = quick_file_sha1(p)
    except Exception:
        pass
    return sig'''

_REF_FILE_SIGNATURE_MATCHES = '''def file_signature_matches(path_like: str | Path, cached, *, allow_missing: bool = True) -> bool:
    """Compatibility matcher for old mtime-only and new signature dict formats."""
    sig = _normalize_cached_signature(cached)
    if not sig:
        return True
    p = Path(path_like)
    try:
        st = p.stat()
    except OSError:
        return bool(allow_missing)

    cached_mtime = sig.get("mtime")
    cached_size = sig.get("size")

    if cached_mtime is not None and abs(float(st.st_mtime) - float(cached_mtime)) <= 0.1:
        if cached_size is None or int(st.st_size) == int(cached_size):
            return True

    if cached_size is not None and int(st.st_size) != int(cached_size):
        return False

    cached_sha = sig.get("sha1")
    if cached_sha:
        try:
            return quick_file_sha1(p).lower() == cached_sha
        except Exception:
            return False

    if cached_mtime is not None:
        return abs(float(st.st_mtime) - float(cached_mtime)) <= 0.1

    # No matching criteria - assume match
    return True'''


def test_diff_null_on_cache_fns():
    import hive_functions.chunking as chk
    got_build = inspect.getsource(chk.build_file_signature).rstrip("\n")
    got_match = inspect.getsource(chk.file_signature_matches).rstrip("\n")
    if got_build == _REF_BUILD_FILE_SIGNATURE:
        ok("C1: build_file_signature unchanged (diff-null)")
    else:
        fail("C1: build_file_signature changed", got_build[:80])
    if got_match == _REF_FILE_SIGNATURE_MATCHES:
        ok("C2: file_signature_matches unchanged (diff-null)")
    else:
        fail("C2: file_signature_matches changed", got_match[:80])
    if "signature_changed" not in inspect.getsource(chk.file_signature_matches):
        ok("C3: file_signature_matches does NOT call signature_changed")
    else:
        fail("C3: cache function references signature_changed")


# ── D: duo_runner source structure ────────────────────────────────────────

def test_source_structure():
    src = Path(__file__).parent.parent / "core" / "duo_runner.py"
    text = src.read_text(encoding="utf-8")
    if "def _loop_detect_file_snapshot():" in text:
        ok("D1: snapshot closure present in duo_runner")
    else:
        fail("D1: snapshot closure missing")
    if "_round_bash_only" in text and '== {"run_bash"}' in text:
        ok("D2: run_bash-only detection before the tool execution")
    else:
        fail("D2: run_bash-only detection missing")
    if "_explore_only_rounds = resolve_explore_reset(" in text:
        ok("D3: hook calls resolve_explore_reset() (pure function)")
    else:
        fail("D3: resolve_explore_reset call missing")
    seg = text[text.find("_round_tool_names & _WRITE_TOOLS:"):]
    seg = seg[:seg.find("ctx.exec_ctrl.sync_tool_rounds")]
    if "_explore_only_rounds = 0" not in seg and "if _bash_changed:" not in seg:
        ok("D4: old duplicate '_explore_only_rounds = 0' cascade is gone")
    else:
        fail("D4: old cascade still present", seg[:200])
    if "resolve_explore_reset(" in seg:
        ok("D5: hook segment delegates to resolve_explore_reset")
    else:
        fail("D5: no resolve_explore_reset delegation in the hook segment")
    if "compute_bash_changed(_snap_before, _snap_after)" in seg:
        ok("D6: snapshot comparison delegates to compute_bash_changed")
    else:
        fail("D6: compute_bash_changed call missing in the hook segment")
    if "_bash_changed = False" not in seg:
        ok("D7: old 'else: _bash_changed = False' fallback logic is gone")
    else:
        fail("D7: _bash_changed=False fallback still present")


def main():
    print("\n=== run_bash differentiation (Problem 2) ===\n")
    print("-- A: signature_changed in isolation --")
    test_signature_changed()
    print("\n-- B: hook semantics (real function) --")
    test_compute_bash_changed_direct()
    print()
    test_hook_snapshot_failure_path()
    print()
    test_resolve_explore_reset_direct()
    print()
    test_hook_semantics()
    print("\n-- C: diff-null cache path --")
    test_diff_null_on_cache_fns()
    print("\n-- D: source structure --")
    test_source_structure()
    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)

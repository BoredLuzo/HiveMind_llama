# -*- coding: utf-8 -*-
"""Auto-test-on-edit (G#2 lever, 2026-11-11): run the testmap-mapped tests
of an edited file IN THE SAME TOOL TURN as the edit.

The note is appended to the edit tool's result, so the model sees the test
outcome without spending a round on run_tests. Hard bounds keep this safe:

- OPT-IN / DEFAULT-SAFE: settings["duo_auto_test_on_edit"] defaults to False.
  Nothing runs unless the user (or a preset) turns it on.
- MAPPED TESTS ONLY: hive_functions.testmap maps the edited path to at most
  8 concrete test files; when nothing maps, NOTHING runs (no full-suite
  fallback — an implicit full suite is the opposite of bounded).
- BOUNDED: explicit timeout (duo_auto_test_timeout, 10..180 s, default 60)
  and a per-run cap (duo_auto_test_max_per_run, default 2 auto-tests per
  run).
- LOOP-SAFE: per-run counter + a (path -> mtime/size) signature — an
  unchanged file is never auto-tested twice, and the cap bounds pathological
  edit/test cycles. run_tests itself never triggers this (only write tools
  call the helper).
- SILENT DEGRADATION: any wiring problem (no test runner injected, bench
  container workspace, import error) returns "" — an edit result is never
  turned into a failure by the auto-test machinery.
"""
from __future__ import annotations

import logging
from pathlib import Path

_logger = logging.getLogger("tools.auto_test")

# run_id -> {"count": int, "sigs": {path: (mtime_ns, size)}}
_state: dict[str, dict] = {}
# bounded: drop the oldest run ids beyond this many entries
_MAX_TRACKED_RUNS = 64

_FAIL_SNIPPET_CHARS = 600


def _settings() -> dict:
    try:
        from core.state import settings as _s
        return _s or {}
    except (ImportError, AttributeError):
        return {}


def _enabled() -> bool:
    return bool(_settings().get("duo_auto_test_on_edit", False))


def _cap() -> int:
    try:
        return max(0, min(10, int(_settings().get("duo_auto_test_max_per_run", 2))))
    except (TypeError, ValueError):
        return 2


def _timeout() -> int:
    try:
        return max(10, min(180, int(_settings().get("duo_auto_test_timeout", 60))))
    except (TypeError, ValueError):
        return 60


def _sig_of(p: Path) -> tuple[int, int] | None:
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def reset_run_state(run_id: str) -> None:
    """Test/helper hook: forget the auto-test bookkeeping for one run."""
    _state.pop(run_id, None)


def _note_for(result, mapped: list[str]) -> str:
    """Render the compact [AUTO-TEST] block from a TestResult."""
    label = ", ".join(mapped[:3]) + (f" (+{len(mapped) - 3} more)" if len(mapped) > 3 else "")
    try:
        if result.is_clean():
            return (f"[AUTO-TEST] ✅ passed ({result.language}, "
                    f"cmd: {result.command[:80]}) — {label}")
        if result.failure_count == 0 and not result.success:
            # "no tests found" scoped run — treat as neutral, do not nag
            return ""
        snippet = ""
        inject = str(result.inject_msg or "") or "\n".join(result.error_lines[:5])
        if inject.strip():
            snippet = "\n" + inject.strip()[:_FAIL_SNIPPET_CHARS]
        return (f"[AUTO-TEST] ❌ {result.failure_count} failure(s) "
                f"({result.language}, cmd: {result.command[:80]}) — {label}"
                f"{snippet}\n"
                "[AUTO-TEST] Fix the failures before task_complete; do NOT "
                "re-edit blindly.")
    except (AttributeError, TypeError):
        return ""


async def auto_test_note(paths: list[str], workspace: Path,
                         workspace_lock: str | None) -> str:
    """Return the [AUTO-TEST] note for the just-edited paths, or "".

    Called from the write-tool post-processing (tools/runner) and from the
    apply_patch handler with the patch's written files. Never raises."""
    if not paths or not _enabled():
        return ""
    ws = Path(workspace_lock) if workspace_lock else Path(workspace)
    ws_str = str(ws)
    # BENCH-CONTAINER: run_tests uses the host runner — wrong environment for
    # a benchmark task container (same guard the run_tests tool applies).
    try:
        from tools.bench_container import container_info as _ci
        if _ci(ws) is not None:
            return ""
    except (ImportError, OSError, ValueError):
        pass
    # Only source-code-ish edits map to tests; static assets never trigger.
    try:
        from core.duo.preflight import needs_test_verification
        if not needs_test_verification([str(p) for p in paths]):
            return ""
    except (ImportError, AttributeError, TypeError):
        pass
    try:
        from hive_functions.testmap import map_edits_to_tests
        mapped = map_edits_to_tests(ws_str, [str(p) for p in paths])
    except (ImportError, OSError, ValueError, TypeError):
        mapped = []
    if not mapped:
        return ""

    run_id = ""
    try:
        from tools.runner import _current_run_id
        run_id = str(_current_run_id.get(None) or "")
    except (ImportError, AttributeError):
        run_id = ""
    st = _state.setdefault(run_id, {"count": 0, "sigs": {}})
    if len(_state) > _MAX_TRACKED_RUNS:
        for k in list(_state.keys())[:-_MAX_TRACKED_RUNS]:
            _state.pop(k, None)
    if st["count"] >= _cap():
        return ""
    # signature gate: unchanged files are not re-tested (loop protection)
    fresh: dict[str, tuple[int, int]] = {}
    for rel in mapped:
        sig = _sig_of(ws / rel)
        if sig is not None:
            fresh[rel] = sig
    if fresh and all(fresh.get(rel) == st["sigs"].get(rel) for rel in fresh) \
            and set(fresh) <= set(st["sigs"]):
        return ""

    try:
        from tools.handlers._shared import _run_test_suite
    except ImportError:
        return ""
    if _run_test_suite is None:
        return ""
    try:
        result = await _run_test_suite(
            workspace=str(ws.resolve()),
            timeout=_timeout(),
            chat_id=run_id,
            paths=mapped,
        )
    except (RuntimeError, OSError, ValueError, TypeError, AttributeError,
            ImportError) as e:
        # LOOP-SAFE: the suite runner is injected and normally returns a
        # TestResult even on red; only infra problems surface as exceptions.
        _logger.info("[AUTO-TEST] suite run failed (%s: %s) — skipping note",
                     type(e).__name__, str(e)[:120])
        return ""
    st["count"] += 1
    st["sigs"].update(fresh)
    return _note_for(result, mapped)

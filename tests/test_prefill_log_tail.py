# -*- coding: utf-8 -*-
"""F4 (deep audit 2026-11-07): sync I/O out of the asyncio loop.

Three behaviors, all hermetic (temp dirs only, no servers):

1. Prefill meter (core/duo/preflight._llama_prefill_progress):
   - slot logs are timestamped `llama_server_{port}_{YYYYMMDD-HHMMSS}.log`
     (SLOT-LOG-ROTATION 2026-10-09) — the meter must read the NEWEST one by
     mtime, never fall back to an older rotated log, and never match the old
     fixed-name format;
   - missing log dir / no matching files -> None (no exception);
   - discovery is cached ~30 s (like agentic_tool_loop._diag_newest_slot_log)
     and the glob + tail read run via asyncio.to_thread, not on the loop.

2. Walk helpers (core.duo._pre_explore._walk_code_partitions,
   core.duo_compress_cycle._walk_workspace_inventory):
   - same relative file set (and same partition structure) as the old inline
     os.walk logic for a temp tree with nested dirs;
   - caps/ignore lists unchanged, result order deterministic (sorted);
   - they are plain sync helpers, thread-safe via asyncio.to_thread (the
     call sites await asyncio.to_thread — verified by behavior for the
     meter via a to_thread spy; the helpers are checked for sync identity +
     to_thread equivalence, not by source-text matching).
"""
from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness

from core.duo import preflight as pf
from core.duo._pre_explore import _walk_code_partitions
from core.duo_compress_cycle import _walk_workspace_inventory

h = Harness()


# ── helpers ──────────────────────────────────────────────────────────────────

def _progress_line(n: int, prog: float, tps: float) -> str:
    t = n / prog / tps
    return (f"prompt processing, n_tokens = {n}, progress = {prog:.2f}, "
            f"t = {t:.2f} s / {tps:.2f} tokens per second")


def _mk_log(logs_dir: Path, port: int, stamp: str, text: str, age_s: float) -> Path:
    p = logs_dir / f"llama_server_{port}_{stamp}.log"
    p.write_text(text, encoding="utf-8")
    ts = time.time() - age_s
    os.utime(p, (ts, ts))
    return p


def _fresh_logs_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="_tmp_prefill_logs_"))


def _meter(port: int, logs_dir) -> dict | None:
    pf._PREFILL_LOG_CACHE.clear()
    return asyncio.run(pf._llama_prefill_progress(port, logs_dir=logs_dir))


# ── 1. prefill meter ─────────────────────────────────────────────────────────

def test_meter_picks_newest_log():
    """Newest-by-mtime log wins; the older (still fresh) log is ignored.

    The old log's mtime is within the 45 s activity window, so if the meter
    wrongly picked it the total would be 4000, not 12000."""
    port = 47191
    logs = _fresh_logs_dir()
    try:
        _mk_log(logs, port, "20261001-000000",
                _progress_line(2000, 0.5, 100.0) + "\n", age_s=30.0)
        _mk_log(logs, port, "20261108-120000",
                _progress_line(6000, 0.5, 200.0) + "\n", age_s=2.0)
        res = _meter(port, logs)
        h.check("meter returns progress dict", isinstance(res, dict), str(res))
        assert isinstance(res, dict)
        h.check("total from NEWEST log (12000)", res.get("total") == 12000,
                f"got {res.get('total')}")
        h.check("n interpolated from newest n+age*tps",
                6000 <= res.get("n", 0) <= 12000, f"n={res.get('n')}")
        h.check("tps from newest log", res.get("tps") == 200.0, str(res.get("tps")))
    finally:
        shutil.rmtree(logs, ignore_errors=True)


def test_meter_does_not_fall_back_to_older_log():
    """Newest log says 'prefill done' -> None, even though an older rotated
    log still contains a progress line."""
    port = 47192
    logs = _fresh_logs_dir()
    try:
        _mk_log(logs, port, "20261001-000000",
                _progress_line(6000, 0.5, 200.0) + "\n", age_s=5.0)
        _mk_log(logs, port, "20261108-120000",
                "prompt eval time =    12.34 s\n", age_s=1.0)
        h.check("newest 'eval time' log -> None (no older fallback)",
                _meter(port, logs) is None)
    finally:
        shutil.rmtree(logs, ignore_errors=True)


def test_meter_ignores_old_fixed_name_format():
    """The pre-rotation fixed name `llama_server_{port}.log` must NOT match."""
    port = 47193
    logs = _fresh_logs_dir()
    try:
        fixed = logs / f"llama_server_{port}.log"
        fixed.write_text(_progress_line(6000, 0.5, 200.0) + "\n", encoding="utf-8")
        ts = time.time() - 2.0
        os.utime(fixed, (ts, ts))
        h.check("fixed-name log not matched", _meter(port, logs) is None)
    finally:
        shutil.rmtree(logs, ignore_errors=True)


def test_meter_missing_dir_or_files_returns_none():
    for name, logs, port in [
        ("missing log dir", Path(tempfile.mkdtemp(prefix="_tmp_prefill_gone_")) / "nope", 47194),
        ("empty log dir", _fresh_logs_dir(), 47195),
        ("only other port", _fresh_logs_dir(), 47196),
    ]:
        try:
            if name == "only other port":
                _mk_log(logs, 47111, "20261108-120000",
                        _progress_line(6000, 0.5, 200.0) + "\n", age_s=1.0)
            h.check(f"missing -> None: {name}",
                    _meter(port, logs) is None)
        finally:
            shutil.rmtree(logs, ignore_errors=True)
    h.check("port falsy -> None", asyncio.run(pf._llama_prefill_progress(0)) is None)


def test_meter_stale_log_returns_none():
    """Log silent for 45 s+ -> not actively prefilling -> None."""
    port = 47197
    logs = _fresh_logs_dir()
    try:
        _mk_log(logs, port, "20261108-120000",
                _progress_line(6000, 0.5, 200.0) + "\n", age_s=120.0)
        h.check("stale log -> None", _meter(port, logs) is None)
    finally:
        shutil.rmtree(logs, ignore_errors=True)


def test_meter_small_prompt_returns_none():
    port = 47198
    logs = _fresh_logs_dir()
    try:
        # n=1000, prog=0.5 -> total=2000 < 3000 -> plain elapsed counter wins
        _mk_log(logs, port, "20261108-120000",
                _progress_line(1000, 0.5, 200.0) + "\n", age_s=2.0)
        h.check("total < 3000 -> None", _meter(port, logs) is None)
    finally:
        shutil.rmtree(logs, ignore_errors=True)


def test_meter_io_runs_via_to_thread():
    """Behavior (not source text): the meter's glob and tail read are
    dispatched through asyncio.to_thread, so they run off the event loop."""
    port = 47199
    logs = _fresh_logs_dir()
    real_to_thread = asyncio.to_thread
    seen: list[str] = []

    async def _spy(fn, *a, **kw):
        seen.append(getattr(fn, "__name__", str(fn)))
        return await real_to_thread(fn, *a, **kw)

    try:
        _mk_log(logs, port, "20261108-120000",
                _progress_line(6000, 0.5, 200.0) + "\n", age_s=2.0)
        pf._PREFILL_LOG_CACHE.clear()
        asyncio.to_thread = _spy
        try:
            res = asyncio.run(pf._llama_prefill_progress(port, logs_dir=logs))
        finally:
            asyncio.to_thread = real_to_thread
        h.check("meter got a result via to_thread", isinstance(res, dict), str(res))
        h.check("discovery ran in to_thread", "_newest_slot_log" in seen, str(seen))
        h.check("tail read ran in to_thread", "_read_slot_log_tail" in seen, str(seen))
    finally:
        shutil.rmtree(logs, ignore_errors=True)


def test_meter_caches_discovery_30s():
    """Second call within 30 s reuses the discovered path (one glob only);
    after the TTL the discovery re-runs."""
    port = 47201
    logs = _fresh_logs_dir()
    real_to_thread = asyncio.to_thread
    globs: list[int] = []

    async def _spy(fn, *a, **kw):
        if getattr(fn, "__name__", "") == "_newest_slot_log":
            globs.append(1)
        return await real_to_thread(fn, *a, **kw)

    try:
        _mk_log(logs, port, "20261108-120000",
                _progress_line(6000, 0.5, 200.0) + "\n", age_s=2.0)
        pf._PREFILL_LOG_CACHE.clear()
        asyncio.to_thread = _spy
        try:
            r1 = asyncio.run(pf._llama_prefill_progress(port, logs_dir=logs))
            r2 = asyncio.run(pf._llama_prefill_progress(port, logs_dir=logs))
            h.check("two calls, one discovery", len(globs) == 1, f"globs={len(globs)}")
            h.check("cached call still meters", isinstance(r2, dict) and isinstance(r1, dict))
            # force-expire the cache entry -> re-glob
            _key = (str(logs), int(port))
            ts, path = pf._PREFILL_LOG_CACHE[_key]
            pf._PREFILL_LOG_CACHE[_key] = (ts - pf._PREFILL_LOG_TTL - 1.0, path)
            asyncio.run(pf._llama_prefill_progress(port, logs_dir=logs))
            h.check("expired cache re-discovers", len(globs) == 2, f"globs={len(globs)}")
        finally:
            asyncio.to_thread = real_to_thread
    finally:
        shutil.rmtree(logs, ignore_errors=True)


# ── 2. walk helpers ──────────────────────────────────────────────────────────

# The OLD inline logic, verbatim from core/duo/_pre_explore.py before the F4
# extraction — reference implementation for the equivalence check.
def _old_inline_walk(ws_str: str, effective_max_files: int) -> list[dict]:
    import os as _os
    _walk_parts: list[dict] = []
    _IGNORE_DIRS = {".git", "__pycache__", "node_modules", ".venv",
                    "venv", ".mypy_cache", ".pytest_cache", "dist", "build"}
    for _root, _dirs, _files in _os.walk(ws_str):
        _dirs[:] = [d for d in sorted(_dirs) if d not in _IGNORE_DIRS]
        _rel = _os.path.relpath(_root, ws_str).replace("\\", "/")
        _rel = "" if _rel == "." else _rel
        _code_exts = {".py", ".js", ".ts", ".jsx", ".tsx", ".go",
                      ".rs", ".cpp", ".c", ".h", ".java", ".cs",
                      ".rb", ".php", ".swift", ".kt", ".md", ".json",
                      ".yaml", ".yml", ".toml", ".cfg", ".ini",
                      ".html", ".css", ".scss", ".less", ".sh", ".bat",
                      ".ps1", ".sql", ".r", ".lua", ".vim", ".el",
                      ".dockerfile", ".makefile", ".cmake",
                      ".txt", ".rst", ".tex", ".csv"}
        _fps = [
            _os.path.join(_rel, f).replace("\\", "/").lstrip("/")
            for f in sorted(_files)
            if _os.path.splitext(f)[1].lower() in _code_exts
        ]
        if _fps:
            for _ci in range(0, max(1, len(_fps)), max(1, effective_max_files)):
                _chunk = _fps[_ci:_ci + effective_max_files]
                _lbl = (_rel or "__root__") + (f":sz{_ci//effective_max_files+1}" if len(_fps) > effective_max_files else "")
                _walk_parts.append({"paths": _chunk, "label": _lbl})
    return _walk_parts


# The OLD compress-inventory loop, verbatim from core/duo_compress_cycle.py
# before the F4 extraction (minus its result-order nondeterminism).
def _old_inline_inventory(ws_str: str, cap: int = 80) -> list[str]:
    import os as _os
    _inv: list[str] = []
    _inv_n = 0
    for _iroot, _idirs, _ifnames in _os.walk(ws_str):
        _idirs[:] = [x for x in _idirs if x not in
                     ("node_modules", ".git", "dist",
                      ".hive_uploads", "__pycache__")]
        for _ifn in _ifnames:
            if _inv_n >= cap:
                break
            _irel = _os.path.relpath(
                _os.path.join(_iroot, _ifn), ws_str)
            _inv.append(_irel.replace("\\", "/"))
            _inv_n += 1
        if _inv_n >= cap:
            break
    return _inv


def _mk_tree(ws: Path) -> None:
    (ws / "sub" / "deep").mkdir(parents=True)
    (ws / "empty").mkdir()
    (ws / "node_modules").mkdir()
    (ws / ".git").mkdir()
    (ws / "venv").mkdir()
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    (ws / "readme.md").write_text("doc\n", encoding="utf-8")
    (ws / "data.bin").write_bytes(b"\x00\x01")          # ext not code -> excluded
    (ws / "sub" / "b.js").write_text("let x;\n", encoding="utf-8")
    (ws / "sub" / "deep" / "c.ts").write_text("let y;\n", encoding="utf-8")
    (ws / "node_modules" / "x.py").write_text("pass\n", encoding="utf-8")
    (ws / ".git" / "y.py").write_text("pass\n", encoding="utf-8")
    (ws / "venv" / "z.py").write_text("pass\n", encoding="utf-8")


def test_walk_partitions_same_as_old_inline():
    ws = Path(tempfile.mkdtemp(prefix="_tmp_prefill_ws_"))
    try:
        _mk_tree(ws)
        new = _walk_code_partitions(str(ws), 30)
        old = _old_inline_walk(str(ws), 30)
        new_set = {p for part in new for p in part["paths"]}
        old_set = {p for part in old for p in part["paths"]}
        h.check("walk: same relative file set as old inline logic",
                new_set == old_set,
                f"only_new={sorted(new_set - old_set)} only_old={sorted(old_set - new_set)}")
        h.check("walk: identical partition list (paths+labels+order)",
                new == old, f"new={new} old={old}")
        h.check("walk: ignored dirs stay excluded",
                not any(p.startswith(("node_modules/", ".git/", "venv/"))
                        for p in new_set))
        h.check("walk: non-code ext excluded", "data.bin" not in new_set)
        h.check("walk: forward slashes / no leading slash",
                all(not p.startswith("/") and "\\" not in p for p in new_set))
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_walk_partitions_chunk_cap_and_determinism():
    ws = Path(tempfile.mkdtemp(prefix="_tmp_prefill_ws2_"))
    try:
        for i in range(5):
            (ws / f"f{i}.py").write_text("x\n", encoding="utf-8")
        parts = _walk_code_partitions(str(ws), 2)
        h.check("cap=2 -> 3 partitions", len(parts) == 3, str([p["label"] for p in parts]))
        h.check("chunk labels szN", [p["label"] for p in parts] ==
                ["__root__:sz1", "__root__:sz2", "__root__:sz3"],
                str([p["label"] for p in parts]))
        h.check("chunk sizes 2/2/1", [len(p["paths"]) for p in parts] == [2, 2, 1])
        h.check("all 5 files across chunks",
                sorted(p for part in parts for p in part["paths"]) ==
                [f"f{i}.py" for i in range(5)])
        h.check("deterministic across calls", parts == _walk_code_partitions(str(ws), 2))
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_inventory_same_as_old_inline():
    ws = Path(tempfile.mkdtemp(prefix="_tmp_prefill_ws3_"))
    try:
        _mk_tree(ws)
        new = _walk_workspace_inventory(str(ws))
        old = _old_inline_inventory(str(ws))
        h.check("inventory: same relative file set as old inline logic",
                set(new) == set(old),
                f"only_new={sorted(set(new) - set(old))} only_old={sorted(set(old) - set(new))}")
        h.check("inventory: cap 80 intact (small tree -> all files)", len(new) == len(old))
        h.check("inventory: ignored dirs excluded",
                not any(p.startswith(("node_modules/", ".git/", "dist/",
                                      ".hive_uploads/", "__pycache__/")) for p in new))
        h.check("inventory: deterministic sorted output", new == sorted(new))
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_inventory_caps_at_80():
    ws = Path(tempfile.mkdtemp(prefix="_tmp_prefill_ws4_"))
    try:
        for i in range(85):
            (ws / f"m{i:03d}.py").write_text("x\n", encoding="utf-8")
        inv = _walk_workspace_inventory(str(ws))
        h.check("inventory capped at 80", len(inv) == 80, f"len={len(inv)}")
        h.check("capped entries are real files",
                all((ws / p).exists() for p in inv) and len(set(inv)) == 80)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_helpers_are_sync_and_thread_safe():
    """F4 identity + behavior: the walk helpers are PLAIN sync functions
    (never coroutines), and awaiting them through asyncio.to_thread (exactly
    what the call sites do) returns the same result as a direct call."""
    import core.duo._pre_explore as _pe
    import core.duo_compress_cycle as _cc
    h.check("meter is a coroutine function",
            inspect.iscoroutinefunction(pf._llama_prefill_progress))
    h.check("_walk_code_partitions is sync (not a coroutine fn)",
            not inspect.iscoroutinefunction(_walk_code_partitions))
    h.check("_walk_workspace_inventory is sync (not a coroutine fn)",
            not inspect.iscoroutinefunction(_walk_workspace_inventory))
    h.check("pre_explore module has the to_thread-dispatched helper",
            callable(getattr(_pe, "_walk_code_partitions", None)))
    h.check("compress module has the to_thread-dispatched helper",
            callable(getattr(_cc, "_walk_workspace_inventory", None)))

    ws = Path(tempfile.mkdtemp(prefix="_tmp_prefill_ws5_"))
    try:
        _mk_tree(ws)
        direct = _walk_workspace_inventory(str(ws))
        via_thread = asyncio.run(asyncio.to_thread(_walk_workspace_inventory, str(ws)))
        h.check("inventory: to_thread result == direct call", via_thread == direct)
        direct_p = _walk_code_partitions(str(ws), 2)
        via_thread_p = asyncio.run(asyncio.to_thread(_walk_code_partitions, str(ws), 2))
        h.check("walk: to_thread result == direct call", via_thread_p == direct_p)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    test_meter_picks_newest_log()
    test_meter_does_not_fall_back_to_older_log()
    test_meter_ignores_old_fixed_name_format()
    test_meter_missing_dir_or_files_returns_none()
    test_meter_stale_log_returns_none()
    test_meter_small_prompt_returns_none()
    test_meter_io_runs_via_to_thread()
    test_meter_caches_discovery_30s()
    test_walk_partitions_same_as_old_inline()
    test_walk_partitions_chunk_cap_and_determinism()
    test_inventory_same_as_old_inline()
    test_inventory_caps_at_80()
    test_helpers_are_sync_and_thread_safe()
    sys.exit(h.finish("prefill_log_tail"))

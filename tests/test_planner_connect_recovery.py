"""ConnectError-Recovery des Planners (2026-08-31).

Live-Befund: Planner-POST bekam httpx.ConnectError auf einem "geladenen" Port
(Phantom-Slot) - der Planner hatte KEINE Recovery (kein evict+reload+retry),
the agentic tool loop did. This test verifies the new
_llm_stream_retry-Wrapper in hive_functions/planner.py.

Run: python tests/test_planner_connect_recovery.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

from hive_functions import planner as _planner_mod

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


_orig_llm_stream = _planner_mod._llm_stream
_orig_refresh = _planner_mod._planner_refresh_port


# ── Verhalten: ConnectError einmal -> evict+reload+retry -> Erfolg ─────────
async def _run_recovery_test():
    calls = {"n": 0, "refresh": 0}
    ports_used = []

    async def _fake_llm_stream(model, port, messages, **kwargs):
        calls["n"] += 1
        ports_used.append(port)
        if calls["n"] == 1:
            raise httpx.ConnectError("All connection attempts failed")
        return ("thinking", "content", False)

    async def _fake_refresh(model, port, num_ctx):
        calls["refresh"] += 1
        return port + 1

    _planner_mod._llm_stream = _fake_llm_stream
    _planner_mod._planner_refresh_port = _fake_refresh
    try:
        t, c, to = await _planner_mod._llm_stream_retry(
            "m", 8101, [{"role": "user", "content": "x"}], num_ctx=8192
        )
    finally:
        _planner_mod._llm_stream = _orig_llm_stream
        _planner_mod._planner_refresh_port = _orig_refresh
    return t, c, to, calls, ports_used


def test_connect_error_recovers():
    t, c, to, calls, ports = asyncio.run(_run_recovery_test())
    if c == "content" and calls["n"] == 2 and calls["refresh"] == 1 and ports == [8101, 8102]:
        ok("connect_error_recovers (ConnectError -> evict+reload+retry -> Erfolg)")
    else:
        fail("connect_error_recovers", f"calls={calls} ports={ports} t={t!r} c={c!r} timeout={to}")


# ── Verhalten: Retries erschoepft -> ConnectError wird weitergeworfen ───────
async def _run_exhausted_test():
    async def _fake_llm_stream(model, port, messages, **kwargs):
        raise httpx.ConnectError("dead")

    async def _fake_refresh(model, port, num_ctx):
        return port + 1

    _planner_mod._llm_stream = _fake_llm_stream
    _planner_mod._planner_refresh_port = _fake_refresh
    try:
        await _planner_mod._llm_stream_retry(
            "m", 8101, [{"role": "user", "content": "x"}], max_retries=1, num_ctx=8192
        )
        return False
    except httpx.ConnectError:
        return True
    finally:
        _planner_mod._llm_stream = _orig_llm_stream
        _planner_mod._planner_refresh_port = _orig_refresh


def test_connect_error_exhausts_raises():
    if asyncio.run(_run_exhausted_test()):
        ok("connect_error_exhausts_raises (Retries begrenzt, kein Endlos-Loop)")
    else:
        fail("connect_error_exhausts_raises", "kein ConnectError trotz totem Port")


# ── Verhalten: abort -> kein Stream-Call mehr ──────────────────────────────
async def _run_aborted_test():
    called = {"n": 0}

    async def _fake_llm_stream(model, port, messages, **kwargs):
        called["n"] += 1
        return ("t", "c", False)

    _planner_mod._llm_stream = _fake_llm_stream
    try:
        t, c, to = await _planner_mod._llm_stream_retry(
            "m", 8101, [{"role": "user", "content": "x"}], aborted_fn=lambda: True
        )
    finally:
        _planner_mod._llm_stream = _orig_llm_stream
    return called["n"], t, c


def test_aborted_no_call():
    n, t, c = asyncio.run(_run_aborted_test())
    if n == 0 and c == "":
        ok("aborted_no_call (abort stops the retry before the stream call)")
    else:
        fail("aborted_no_call", f"n={n} t={t!r} c={c!r}")


# ── Verhalten: _planner_refresh_port nutzt manager.evict + ensure_loaded ────
def test_refresh_port_uses_manager():
    import backend.llama_server_manager as _lsm_mod

    calls = {"evict": 0, "ensure": 0, "ensure_port": 8701}

    class _FakeManager:
        async def evict(self, model):
            calls["evict"] += 1

        async def ensure_loaded(self, model, num_ctx=0, n_parallel=1):
            calls["ensure"] += 1
            return calls["ensure_port"]

    _orig_manager = _lsm_mod.manager
    _lsm_mod.manager = _FakeManager()
    try:
        new_port = asyncio.run(_planner_mod._planner_refresh_port("m", 8101, 8192))
    finally:
        _lsm_mod.manager = _orig_manager

    if new_port == 8701 and calls["evict"] == 1 and calls["ensure"] == 1:
        ok("refresh_port_uses_manager (evict + ensure_loaded -> frischer Port)")
    else:
        fail("refresh_port_uses_manager", f"new_port={new_port} calls={calls}")


# ── Source-Guards ───────────────────────────────────────────────────────────
def test_source_guard_retry_helper():
    src = (Path(__file__).parent.parent / "hive_functions" / "planner.py").read_text(encoding="utf-8")
    _helper = "_llm_stream_retry" in src
    _catches = "httpx.ConnectError" in src
    _refresh = "_planner_refresh_port" in src
    if _helper and _catches and _refresh:
        ok("source_guard_retry_helper (Helper + ConnectError + Port-Refresh)")
    else:
        fail("source_guard_retry_helper", f"helper={_helper} catches={_catches} refresh={_refresh}")


def test_source_guard_call_sites():
    src = (Path(__file__).parent.parent / "hive_functions" / "planner.py").read_text(encoding="utf-8")
    _calls = src.count("await _llm_stream_retry(")
    if _calls >= 3:
        ok(f"source_guard_call_sites ({_calls} Retry-Call-Sites in run_planner)")
    else:
        fail("source_guard_call_sites", f"nur {_calls} Retry-Call-Sites (erwartet >=3)")


if __name__ == "__main__":
    test_connect_error_recovers()
    test_connect_error_exhausts_raises()
    test_aborted_no_call()
    test_refresh_port_uses_manager()
    test_source_guard_retry_helper()
    test_source_guard_call_sites()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)

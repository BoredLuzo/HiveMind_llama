# -*- coding: utf-8 -*-
"""Behavior tests for infra/keep_awake.py (was at 0% coverage).

Covers the ref-counted keep-awake lifecycle: enabled() against faked
settings (off / default-on / on / settings error / non-Windows), acquire and
release counting with and without a worker thread, the single-thread
guarantee, and the SetThreadExecutionState call sequence - with _es_set
monkeypatched, so no real kernel32 power call and no real settings load.
All module globals are saved/restored around the suite.
"""
import sys
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import infra.keep_awake as ka  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

_ES_BOTH = ka._ES_CONTINUOUS | ka._ES_SYSTEM_REQUIRED

_saved_globals = (ka._refs, ka._thread, ka._stop_event)
_saved_es_set = ka._es_set
_saved_platform = sys.platform


def _install_settings(return_value=None, raise_exc=None):
    """Point the module's `from settings import load_settings` at a fake."""
    mod = types.ModuleType("settings")
    calls = []

    def load_settings():
        calls.append(1)
        if raise_exc is not None:
            raise raise_exc
        return return_value

    mod.load_settings = load_settings
    sys.modules["settings"] = mod
    return calls


def _reset_module_state():
    ka._refs = 0
    ka._thread = None
    ka._stop_event = None


try:
    # --- enabled() ----------------------------------------------------------
    calls = _install_settings({"keep_awake_during_run": False})
    check("setting off -> disabled", ka.enabled() is False)
    check("settings were consulted", len(calls) == 1)

    _install_settings({})
    check("missing setting defaults to enabled", ka.enabled() is True)

    _install_settings({"keep_awake_during_run": True})
    check("setting on -> enabled", ka.enabled() is True)

    _install_settings(raise_exc=ValueError("settings broken"))
    check("settings error falls back to enabled", ka.enabled() is True)

    sys.platform = "linux"
    calls = _install_settings({"keep_awake_during_run": True})
    check("non-Windows -> disabled without touching settings",
          ka.enabled() is False and calls == [])
    sys.platform = _saved_platform

    # --- acquire/release counting without a thread --------------------------
    _reset_module_state()
    _install_settings({"keep_awake_during_run": False})
    check("refs() starts at zero", ka.refs() == 0)
    check("acquire counts up", ka.acquire() == 1 and ka.refs() == 1)
    check("disabled -> no worker thread started",
          ka._thread is None and ka._stop_event is None)
    check("second acquire counts up", ka.acquire() == 2)
    check("release counts down", ka.release() == 1)
    check("release to zero", ka.release() == 0)
    check("release below zero clamps at 0", ka.release() == 0)
    check("thread stays None after disabled lifecycle", ka._thread is None)

    # --- full threaded lifecycle with faked SetThreadExecutionState ---------
    _reset_module_state()
    es_calls = []
    first_call = threading.Event()

    def fake_es(flags):
        es_calls.append(flags)
        if len(es_calls) == 1:
            first_call.set()
        return True

    ka._es_set = fake_es
    _install_settings({"keep_awake_during_run": True})

    check("first acquire returns refs=1", ka.acquire() == 1)
    check("worker thread raised the execution-state request",
          first_call.wait(2.0) and es_calls[0] == _ES_BOTH)
    thread1 = ka._thread
    check("a named worker thread is running",
          thread1 is not None and thread1.name == "hivemind-keep-awake"
          and thread1.is_alive() and thread1.daemon)
    check("a stop event is armed", ka._stop_event is not None)

    check("second acquire returns refs=2 and keeps the same thread",
          ka.acquire() == 2 and ka._thread is thread1
          and len(es_calls) == 1)

    _install_settings({"keep_awake_during_run": False})
    check("acquire with live thread skips enabled() and counts up",
          ka.acquire() == 3 and ka._thread is thread1
          and len(es_calls) == 1)

    check("release above zero keeps the thread alive",
          ka.release() == 2 and ka._thread is thread1 and thread1.is_alive())

    check("release to 1 keeps the thread alive",
          ka.release() == 1 and ka._thread is thread1)

    _install_settings({"keep_awake_during_run": True})
    last_stop_event = ka._stop_event
    check("final release returns 0 and clears the thread handle",
          ka.release() == 0 and ka._thread is None
          and ka._stop_event is None and last_stop_event is not None)
    check("worker thread finished", not thread1.is_alive())
    check("stop path cleared the execution state",
          len(es_calls) == 2 and es_calls[1] == ka._ES_CONTINUOUS)
    check("the armed stop event is set", last_stop_event.is_set())

    check("extra release below zero is safe", ka.release() == 0)
finally:
    if ka._thread is not None and ka._stop_event is not None:
        ka._stop_event.set()
    ka._refs, ka._thread, ka._stop_event = _saved_globals
    ka._es_set = _saved_es_set
    sys.platform = _saved_platform
    sys.modules.pop("settings", None)

sys.exit(h.finish("keep_awake_behavior"))

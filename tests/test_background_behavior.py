# -*- coding: utf-8 -*-
"""Behavior tests for tools/background.py (was at 0% coverage).

Exercises the registry/reader/terminate logic fully in-process with faked
seams: subprocess.Popen is replaced (no real shell), and the in-function
imports (tools.sandbox, tools.handlers.exec_tools, infra.child_env) are
swapped for fake modules in sys.modules, so no real process, job object or
environment scrub runs. Covers start (validation, happy path, spawn failure,
8-process eviction), the output reader (buffering, 20k cap, vanished entry,
iteration errors), the status/list views and stop/terminate behavior.
"""
import subprocess
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import tools.background as bg  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

sandbox_calls = {"kill_tree": [], "confine": []}
popen_calls = []
popen_instances = []


class FakeJob:
    def __init__(self, proc):
        self.proc = proc
        self.terminated = False
        self.closed = False

    def terminate(self):
        self.terminated = True

    def close(self):
        self.closed = True

    @staticmethod
    def confine(proc):
        sandbox_calls["confine"].append(proc)
        return FakeJob(proc)


class MeanJob:
    """job.terminate() raises - close() must still run (finally branch)."""

    def __init__(self):
        self.closed = False

    def terminate(self):
        raise ValueError("job already gone")

    def close(self):
        self.closed = True


class FakeProc:
    def __init__(self, pid=777, running=True, code=7):
        self.pid = pid
        self.running = running
        self.returncode = None if running else code
        self.stdout = []
        self.calls = []

    def poll(self):
        return None if self.running else (
            self.returncode if self.returncode is not None else 0)

    def terminate(self):
        self.calls.append("terminate")
        self.running = False
        self.returncode = 0

    def kill(self):
        self.calls.append("kill")
        self.running = False
        self.returncode = 0

    def wait(self, timeout=None):
        self.calls.append(f"wait({timeout})")
        return 0


class FakePopen(FakeProc):
    def __init__(self, cmd, **kwargs):
        super().__init__(pid=4000 + len(popen_instances) + 1)
        self.stdout = ["hello from child\n"]  # consumed by the reader thread
        popen_calls.append({"cmd": cmd, **kwargs})
        popen_instances.append(self)


def _fake_sandbox():
    mod = types.ModuleType("tools.sandbox")
    mod.spawn_kwargs = lambda: {}
    mod.kill_tree = lambda proc, **kw: sandbox_calls["kill_tree"].append(proc)
    mod.ToolJob = FakeJob
    return mod


def _fake_handlers():
    pkg = types.ModuleType("tools.handlers")
    mod = types.ModuleType("tools.handlers.exec_tools")
    mod._BASH_BIN = "/bin/bash"
    return pkg, mod


def _fake_child_env():
    mod = types.ModuleType("infra.child_env")
    mod.tool_env = lambda extra=None: {"FAKE_ENV": "1"}
    return mod


def _wait_for_buf(handle, needle, timeout=5.0):
    deadline = time.time() + timeout
    buf = ""
    while time.time() < deadline:
        with bg._LOCK:
            entry = bg._REGISTRY.get(handle)
            buf = entry["buf"] if entry else ""
        if needle in buf:
            break
        time.sleep(0.02)
    return buf


_FAKES = {
    "tools.sandbox": _fake_sandbox(),
    "tools.handlers": _fake_handlers()[0],
    "tools.handlers.exec_tools": _fake_handlers()[1],
    "infra.child_env": _fake_child_env(),
}
_saved_modules = {n: sys.modules.get(n) for n in _FAKES}
_saved_popen = subprocess.Popen


def _install_fakes():
    for name, mod in _FAKES.items():
        sys.modules[name] = mod
    subprocess.Popen = FakePopen


def _restore():
    subprocess.Popen = _saved_popen
    for name, mod in _saved_modules.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod


def _clear_registry():
    with bg._LOCK:
        bg._REGISTRY.clear()


def _add_entry(handle, proc, started, cmd="old cmd", job=None):
    with bg._LOCK:
        bg._REGISTRY[handle] = {"proc": proc, "buf": "", "started": started,
                                "cmd": cmd, "job": job}


_install_fakes()
try:
    # --- start_background: input validation ---------------------------------
    _clear_registry()
    check("blank cmd -> clean error, no spawn",
          bg.start_background("   ") == {
              "ok": False,
              "error": "start_background requires a non-empty 'cmd'."}
          and popen_calls == [] and bg._REGISTRY == {})

    # --- start_background: happy path ---------------------------------------
    r = bg.start_background("  echo hi  ")
    handle = r.get("handle", "")
    entry = bg._REGISTRY.get(handle)
    proc1 = popen_instances[-1]
    check("ok=True with a 12-hex handle",
          r["ok"] is True and len(handle) == 12
          and all(c in "0123456789abcdef" for c in handle))
    check("cmd is stripped and passed to Popen with shell=True",
          popen_calls[-1]["cmd"] == "echo hi"
          and popen_calls[-1]["shell"] is True)
    check("child env comes from the scrubbed tool_env seam",
          popen_calls[-1]["env"] == {"FAKE_ENV": "1"})
    check("output is merged stdout+stderr, utf-8 replace",
          popen_calls[-1]["stderr"] == subprocess.STDOUT
          and popen_calls[-1]["encoding"] == "utf-8"
          and popen_calls[-1]["errors"] == "replace")
    check("pid forwarded and proc stored in the registry entry",
          r["pid"] == proc1.pid and entry is not None
          and entry["proc"] is proc1 and entry["cmd"] == "echo hi"
          and isinstance(entry["started"], float)
          and set(entry) == {"proc", "buf", "started", "cmd", "job"})
    check("proc is confined via the sandbox ToolJob seam",
          isinstance(entry["job"], FakeJob)
          and sandbox_calls["confine"][-1] is proc1)

    buf = _wait_for_buf(handle, "hello from child")
    check("reader thread collects the child output asynchronously",
          "hello from child\n" in buf)

    out = bg.get_background_output(handle)
    check("running status header while the process is alive",
          out.startswith(f"[background {handle} running]")
          and "hello from child" in out)

    # --- stop_background: registered + unknown ------------------------------
    check("stop_background returns True for a live handle",
          bg.stop_background(handle) is True)
    check("stopped handle is removed from the registry",
          handle not in bg._REGISTRY)
    check("job was terminated and closed",
          entry["job"].terminated and entry["job"].closed)
    check("running proc went through kill_tree + terminate + wait",
          sandbox_calls["kill_tree"] == [proc1]
          and proc1.calls == ["terminate", "wait(3)"])
    check("wait() succeeded so kill() was not needed",
          "kill" not in proc1.calls)
    check("stop_background of unknown handle -> False",
          bg.stop_background(handle) is False)

    # --- _terminate: kill fallback when wait() fails, job errors swallowed --
    sandbox_calls["kill_tree"].clear()
    exited_mean = FakeProc(running=False)
    mean_job = MeanJob()
    bg._terminate({"proc": exited_mean, "job": mean_job})
    check("job.terminate() error does not prevent job.close()",
          mean_job.closed and exited_mean.calls == [])

    stubborn = FakeProc()
    stubborn.wait = lambda timeout=None: (_ for _ in ()).throw(
        RuntimeError("hang"))
    bg._terminate({"proc": stubborn, "job": None})
    check("failed wait() escalates to proc.kill()",
          stubborn.calls == ["terminate", "kill"])

    exited = FakeProc(running=False)
    bg._terminate({"proc": exited, "job": None})
    check("already-exited proc is left alone", exited.calls == [])

    # --- get_background_output ----------------------------------------------
    check("unknown handle explains itself",
          bg.get_background_output("nope") ==
          "[background: no process 'nope' — stopped or never started]")
    _add_entry("h_exit", FakeProc(running=False, code=7), time.time())
    check("exited proc reports the exit code",
          bg.get_background_output("h_exit") ==
          "[background h_exit exited (code 7)]\n(no output yet)")
    _add_entry("h_buf", FakeProc(running=False), time.time())
    with bg._LOCK:
        bg._REGISTRY["h_buf"]["buf"] = "some output\n"
    check("buffered output is returned under the status header",
          bg.get_background_output("h_buf") ==
          "[background h_buf exited (code 7)]\nsome output\n")

    # --- list_background ----------------------------------------------------
    _clear_registry()
    long_cmd = "y" * 300
    _add_entry("h1", FakeProc(pid=11, running=True), 5.0, cmd=long_cmd)
    _add_entry("h2", FakeProc(pid=22, running=False), 6.0)
    rows = bg.list_background()
    check("list_background reports pid/running per entry",
          rows == [{"handle": "h1", "pid": 11, "running": True,
                    "cmd": long_cmd[:120]},
                   {"handle": "h2", "pid": 22, "running": False,
                    "cmd": "old cmd"}])

    # --- _reader: buffering, cap, vanished entry, iteration errors ----------
    _clear_registry()
    proc_read = FakeProc()
    proc_read.stdout = iter(["one\n", "", "two\n"])  # empty lines skipped
    _add_entry("h_read", proc_read, 1.0)
    bg._reader("h_read", proc_read)
    check("reader accumulates non-empty lines in order",
          bg._REGISTRY["h_read"]["buf"] == "one\ntwo\n")

    big_line = "x" * 25000 + "END\n"
    with bg._LOCK:
        bg._REGISTRY["h_read"]["buf"] = ""
    proc_big = FakeProc()
    proc_big.stdout = iter([big_line])
    bg._reader("h_read", proc_big)
    buf = bg._REGISTRY["h_read"]["buf"]
    check("buffer is capped at the last 20000 chars",
          len(buf) == bg._MAX_OUTPUT_CHARS
          and buf == big_line[-bg._MAX_OUTPUT_CHARS:])

    def vanishing_stdout():
        yield "first\n"
        with bg._LOCK:
            bg._REGISTRY.pop("h_read", None)
        yield "second\n"

    proc_van = FakeProc()
    proc_van.stdout = vanishing_stdout()
    bg._reader("h_read", proc_van)
    check("reader stops (without error) once the entry vanished",
          "h_read" not in bg._REGISTRY)

    class BoomIter:
        def __iter__(self):
            raise RuntimeError("stdout exploded")

    proc_boom = FakeProc()
    proc_boom.stdout = BoomIter()
    bg._reader("h_boom", proc_boom)  # must swallow and return
    check("reader survives a broken stdout iterator", True)

    # --- eviction of the oldest process at the registry cap -----------------
    _clear_registry()
    sandbox_calls["kill_tree"].clear()
    handles = []
    procs = {}
    for i in range(bg._MAX_PROCESSES):
        hd = f"h{i:02d}aaaa".ljust(12, "b")
        procs[hd] = FakeProc(pid=100 + i, running=(i == 0))
        _add_entry(hd, procs[hd], 100.0 + i, cmd=f"old{i}")
        handles.append(hd)
    r9 = bg.start_background("new cmd")
    check("9th start evicts the oldest entry",
          r9["ok"] is True and r9["evicted"]["handle"] == handles[0])
    check("evicted payload reports pid and truncated cmd",
          r9["evicted"]["pid"] == 100 and r9["evicted"]["cmd"] == "old0")
    check("registry stays capped and the new handle is registered",
          len(bg._REGISTRY) == bg._MAX_PROCESSES
          and handles[0] not in bg._REGISTRY
          and r9["handle"] in bg._REGISTRY)
    check("eviction terminated the oldest RUNNING proc via kill_tree",
          sandbox_calls["kill_tree"] == [procs[handles[0]]]
          and procs[handles[0]].calls == ["terminate", "wait(3)"])
    check("already-exited entries are untouched by eviction",
          all(bg._REGISTRY[h]["proc"].calls == [] for h in handles[1:]))
finally:
    _restore()
    _clear_registry()

sys.exit(h.finish("background_behavior"))

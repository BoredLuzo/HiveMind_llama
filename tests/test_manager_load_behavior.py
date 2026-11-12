# -*- coding: utf-8 -*-
"""Behavior checks: backend/manager_load.py module-level helpers (B8, 2026-11-09).

The mixin methods run real servers; the module-level helpers are the pure
seams. The other helpers are already behavior-covered (_pre_flight_evict_
candidates/_slot_evictable, _resolve_load_mode, _archive_old_slot_logs,
_append_chat_template). Covered here - with subprocess.run patched, so no
binary is ever executed:

  * _binary_help: one cached probe per path; failed probes are NOT cached
    (audit fix) and retry on the next load
  * _probe_all: --version/--help parsing, >= b8278 auto-true, filename
    build fallback when the binary cannot be executed
  * _omit_mmproj_for: the vision-gate negative-test switch
"""
import asyncio
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import backend.manager_load as ML  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def _proc(out):
    return types.SimpleNamespace(stdout=out, stderr="")


# ── _binary_help / _help_supports ──────────────────────────────────────────
run_calls = []


def _run_ok(args, **k):
    run_calls.append(list(args))
    return _proc("usage: llama-server --n-cpu-moe N --jinja")


async def _help_scenario():
    key = "fake-llama-server"  # str(LLAMA_BIN) after the patch - same cache key
    with mock.patch.object(ML, "_BIN_HELP_CACHE", {}), \
            mock.patch.object(ML, "LLAMA_BIN", key), \
            mock.patch.object(subprocess, "run", side_effect=_run_ok):
        txt1 = await ML._binary_help(key)
        sup = await ML._help_supports("--n-cpu-moe")      # same cache key
        sup_missing = await ML._help_supports("--fit")
        txt2 = await ML._binary_help(key)                 # cached
    return txt1, sup, sup_missing, txt2


txt1, sup, sup_missing, txt2 = asyncio.run(_help_scenario())
check("binary help: one probe per path, flag capability from the help text",
      txt1 == txt2 and run_calls == [["fake-llama-server", "--help"]]
      and sup is True and sup_missing is False,
      f" (runs={len(run_calls)}/sup={sup}/{sup_missing})")

fail_calls = []


def _run_fail(args, **k):
    fail_calls.append(args)
    raise OSError("binary vanished")


async def _help_fail_scenario():
    with mock.patch.object(ML, "_BIN_HELP_CACHE", {}), \
            mock.patch.object(subprocess, "run", side_effect=_run_fail):
        t1 = await ML._binary_help("C:/gone/llama-server.exe")
        t2 = await ML._binary_help("C:/gone/llama-server.exe")
    return t1, t2


t1, t2 = asyncio.run(_help_fail_scenario())
check("binary help: failed probe returns empty and is NOT cached (retries)",
      t1 == "" and t2 == "" and len(fail_calls) == 2,
      f" (t1={t1!r}/runs={len(fail_calls)})")

# ── _probe_all ─────────────────────────────────────────────────────────────
def _run_probe(args, **k):
    if "--version" in args or "-v" in args:
        return _proc("llama.cpp b9000 server")
    return _proc("usage: --cache-type-k --n-cpu-moe")


with mock.patch.object(subprocess, "run", side_effect=_run_probe):
    probe = ML._probe_all("C:/fake/llama-server.exe")
check("probe_all: help parsing, >= b8278 auto-true, missing flags stay false",
      probe == (9000, True, True, True, False, False),
      f" ({probe})")

with mock.patch.object(subprocess, "run", side_effect=OSError("cannot exec")):
    probe_fn = ML._probe_all("C:/fake/llama-b9999-bin-win-vulkan-x64.exe")
check("probe_all: filename b-number fallback when the binary cannot run",
      probe_fn == (9999, True, True, True, False, False),
      f" ({probe_fn})")

# ── _omit_mmproj_for (vision-gate negative-test switch) ────────────────────
# TEXT-ONLY MMPROJ SKIP (2026-11-12): a text-only load of a projector family
# no longer counts as "needs mmproj" (the projector is omitted anyway), so
# the switch cases run on vision=True requests.
omit_off = ML._omit_mmproj_for("llava:7b", vision=True)
with mock.patch.dict(os.environ, {"HIVEMIND_TEST_OMIT_MMPROJ": "*"}):
    omit_all = ML._omit_mmproj_for("llava:7b", vision=True)
with mock.patch.dict(os.environ, {"HIVEMIND_TEST_OMIT_MMPROJ": "9b-ud"}):
    omit_hit = ML._omit_mmproj_for("qwen3.5:9b-ud-instruct", vision=True)
    omit_miss = ML._omit_mmproj_for("qwen3.5:2b", vision=True)
    omit_none = ML._omit_mmproj_for("qwen3.5:9b-ud-instruct", vision=False)
check("omit_mmproj: unset does nothing, '*' all, scoped substring exact",
      omit_off is False and omit_all is True and omit_hit is True
      and omit_miss is False and omit_none is False,
      f" (off={omit_off}/all={omit_all}/hit={omit_hit}/"
      f"miss={omit_miss}/no-vision={omit_none})")

sys.exit(h.finish("manager_load_behavior"))

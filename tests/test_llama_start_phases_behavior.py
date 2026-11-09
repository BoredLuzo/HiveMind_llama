# -*- coding: utf-8 -*-
"""Behavior checks: backend/llama_start_phases.py phases 3+5 (B8, 2026-11-09).

test_llama_start_phases.py already covers _phase_flags and _phase_claim,
and test_split_audit_fixes.py probes _phase_moe; this suite drives the two
phases that had no behavior coverage. The binary probes are patched (no
subprocess, no GPU, no files on disk):

  * _phase_capabilities: probe results populate the class caps, --device
    appended only for a supported non-cpu backend, missing DLLs raise
  * _phase_model_flags: per-model reasoning override is consume-once,
    the MTP env off-switch removes the spec flags, qwen3.5 keeps
    reasoning off + --jinja
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

import backend.llama_start_phases as P4  # noqa: E402
import backend.llama_config as LC  # noqa: E402
import backend.manager_load as ML  # noqa: E402
import model_configs.models_registry as MR  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def _mgr(**caps):
    """Fresh manager double with the probed-capability class attrs the
    real LlamaServerManager carries (unset = probed-on-demand)."""
    defaults = dict(
        _binary_build_number=None, _device_flag_supported=None,
        _kv_flag_supported=None, _moe_flag_supported=None,
        _load_mode_supported=None, _dspark_flag_supported=None,
        _backend_dlls_ok=None, _backend_devices_ok=None,
        _reasoning_override=None,
    )
    defaults.update(caps)
    cls = type("Mgr", (), defaults)
    return cls()


def _patch_registry(**overrides):
    """Neutral model registry: every probe says 'unknown' unless told."""
    base = dict(
        is_jinja=lambda m: None, get_reasoning=lambda m: None,
        is_distilled=lambda m: False, get_chat_template=lambda m: None,
        is_mtp=lambda m: False, get_profile=lambda m: {},
        get_dspark_draft_n_max=lambda m: None,
    )
    base.update(overrides)
    return mock.patch.multiple(MR, **base)


async def _caps_run(self, cmd=None, *, uncached=True, probe_out=None,
                    backend="vulkan", dlls=True, devices=True):
    probe = probe_out or (12345, True, False, True, False, False)
    with mock.patch.object(ML, "_probe_all", return_value=probe), \
            mock.patch.object(P4, "_probe_backend_dlls", return_value=dlls), \
            mock.patch.object(P4, "_probe_backend_devices", return_value=devices), \
            mock.patch.object(P4, "GPU_BACKEND", backend):
        ph = await P4._phase_capabilities(self, list(cmd or ["llama"]),
                                          uncached)
        return ph, type(self)


# ── _phase_capabilities ────────────────────────────────────────────────────
caps_ph, cls_caps = asyncio.run(
    _caps_run(_mgr(), probe_out=(12345, True, True, True, True, True)))
check("capabilities: probe lands on the class once + device flag + load mode",
      caps_ph.cmd[-2:] == ["--device", f"VULKAN{P4.VULKAN_DEVICE}"]
      and caps_ph.load_mode == "none"
      and cls_caps._binary_build_number == 12345
      and cls_caps._device_flag_supported is True
      and cls_caps._kv_flag_supported is True
      and cls_caps._moe_flag_supported is True
      and cls_caps._load_mode_supported is True,
      f" ({caps_ph.cmd}/{cls_caps._binary_build_number})")

caps_nodev, _cls2 = asyncio.run(_caps_run(_mgr(), uncached=False))
try:
    asyncio.run(_caps_run(_mgr(_backend_dlls_ok=False,
                               _device_flag_supported=True)))
    _dll_raised = ""
except RuntimeError as e:
    _dll_raised = str(e)
check("capabilities: unsupported --device is omitted, missing DLLs refuse",
      "--device" not in caps_nodev.cmd
      and "DLL check failed" in _dll_raised
      and "fetch_llamacpp" in _dll_raised,
      f" ({caps_nodev.cmd}/{_dll_raised[:60]!r})")

# ── _phase_model_flags ─────────────────────────────────────────────────────
def _flags_run(self, model, model_base, *, env=None, help_ok=True):
    fake_help = "--spec-draft-n-max --n-cpu-moe" if help_ok else ""
    with mock.patch.dict(os.environ, env or {}), \
            mock.patch.object(LC, "_try_registry_mtp", return_value=None), \
            mock.patch.object(LC, "_try_registry_dspark_draft", return_value=None), \
            mock.patch.object(ML, "_help_supports",
                              new=mock.AsyncMock(return_value=help_ok)), \
            mock.patch.object(subprocess, "run",
                              return_value=types.SimpleNamespace(
                                  stdout=fake_help, stderr="")), \
            _patch_registry():
        return asyncio.run(P4._phase_model_flags(
            self, [], model, model_base, Path("x.gguf"), False))


def _pair_in(cmd, *pair):
    return any(cmd[i:i + len(pair)] == list(pair)
               for i in range(len(cmd) - len(pair) + 1))


ph = _flags_run(_mgr(_reasoning_override={"probe-m1": True}),
                "probe-m1", "probe")
check("model flags: reasoning override wins and is consumed per model",
      _pair_in(ph.cmd, "--reasoning", "on") and ph.mmproj_resolved is None,
      f" ({ph.cmd})")

mgr2 = _mgr(_reasoning_override={"probe-m2a": False, "probe-m2b": False})
ph_a = _flags_run(mgr2, "probe-m2a", "probe")
ph_b = _flags_run(mgr2, "probe-m2b", "probe")
check("model flags: override False -> --reasoning off, empty map auto-clears",
      _pair_in(ph_a.cmd, "--reasoning", "off")
      and _pair_in(ph_b.cmd, "--reasoning", "off")
      and mgr2._reasoning_override is None,
      f" ({ph_a.cmd}/{mgr2._reasoning_override})")

ph_mtp_off = _flags_run(_mgr(), "qwen3.5:4b-mtp", "qwen3.5",
                        env={"HIVEMIND_MTP_SPEC": "0"})
ph_mtp_on = _flags_run(_mgr(), "qwen3.5:4b-mtp", "qwen3.5")
check("model flags: MTP spec flags ride the env switch (off=none, on=clamped set)",
      "--spec-type" not in ph_mtp_off.cmd
      and ph_mtp_on.cmd[ph_mtp_on.cmd.index("--spec-type") + 1] == "draft-mtp"
      and ph_mtp_on.cmd[ph_mtp_on.cmd.index("--spec-draft-n-max") + 1] == "2"
      and ph_mtp_on.cmd[ph_mtp_on.cmd.index("--spec-draft-n-min") + 1] == "1",
      f" (off={ph_mtp_off.cmd}/on={ph_mtp_on.cmd})")

ph_q = _flags_run(_mgr(), "qwen3.5:4b", "qwen3.5",
                  env={"HIVEMIND_MTP_SPEC": "0"})
check("model flags: qwen3.5 base runs --jinja + reasoning off + one template",
      "--jinja" in ph_q.cmd and _pair_in(ph_q.cmd, "--reasoning", "off")
      and ph_q.cmd.count("--chat-template-file") == 1,
      f" ({ph_q.cmd})")

sys.exit(h.finish("llama_start_phases_behavior"))

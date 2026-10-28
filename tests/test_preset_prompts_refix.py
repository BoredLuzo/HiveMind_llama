# -*- coding: utf-8 -*-
"""Preset save/load behaviour after the 2026-10-28 audit fixes.

Covers the four findings of the release audit, all as BEHAVIOUR (real
endpoint/apply calls in-process, temp files, no engine):

1. PROMPT-CLOBBER: re-saving an existing preset used to copy the built-in
   base prompts over its saved custom prompts (data loss). A re-save must
   copy FROM the preset itself; only a new preset starts from the base.
2. PRESET-LOAD-SYNC: applying a preset must live-sync vram_preflight_gate
   into the backend modules the same way POST /settings does, and bump
   the settings revision (stale-tab protection).
3. BOOKKEEPING-KEYS: active_preset/settings_saved_at/preset_applied_at
   must not enter a snapshot.
4. DST-PROOF autoload guard: a save that is newer in real time must win
   even when its ISO stamp sorts behind the applied stamp's string.
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import routers.config as rc          # noqa: E402
import core.state as cs              # noqa: E402
import settings as S                 # noqa: E402
import backend.llama_config as LC    # noqa: E402
import backend.manager_load as ML    # noqa: E402
import backend.llama_server_manager as LSM  # noqa: E402
from hive_functions.prompts import PROMPTS  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


class _FakeReq:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


class _FakeAgent:
    def __init__(self, model="m"):
        self.model = model
        self.temperature = 0.7
        self.max_tokens = 512
        self.thinking = False
        self.thinking_budget = 0


class _FakePipeline:
    def __init__(self, roles):
        self.agents = {r: _FakeAgent() for r in roles}


_tmp = tempfile.TemporaryDirectory(prefix="hm_preset_refix_")
_tdp = Path(_tmp.name)
saved = (S.PRESETS_FILE, S.CUSTOM_PROMPTS_DIR, cs.REGISTRY_FILE,
         cs.pipeline, rc._settings_rev, LC.VRAM_PREFLIGHT_GATE,
         ML.VRAM_PREFLIGHT_GATE,
         getattr(LSM, "VRAM_PREFLIGHT_GATE", None))
try:
    S.PRESETS_FILE = _tdp / "presets.json"
    S.CUSTOM_PROMPTS_DIR = _tdp / "custom_prompts"
    cs.REGISTRY_FILE = None           # never touch the real registry.json
    cs.pipeline = _FakePipeline(["direct", "duo_coder"])
    rc.settings.clear()
    rc.settings.update({"duo_chunking": False, "vram_preflight_gate": True,
                        "agents": {"planner": {"model": "m"}}})
    S.PRESETS_FILE.write_text(json.dumps({
        "A": {"duo_chunking": True},
        "PF": {"vram_preflight_gate": False, "duo_chunking": True},
    }), encoding="utf-8")
    S.save_custom_prompt("A", "direct", "CUSTOM_DIRECT_TEXT")

    # 1a. re-save of an EXISTING preset keeps its custom prompt
    asyncio.run(rc.save_preset_ep("A", _FakeReq({})))
    got = S.get_custom_prompt("A", "direct")
    check("re-save keeps the preset's custom prompt", got == "CUSTOM_DIRECT_TEXT",
          f"({got!r})")

    # 1b. a NEW preset starts from the built-in base prompt
    base = (PROMPTS.get("direct") or "").strip()
    asyncio.run(rc.save_preset_ep("NEW", _FakeReq({})))
    got = S.get_custom_prompt("NEW", "direct")
    check("a new preset starts from the base prompt", got == base,
          f"({(got or '')[:40]!r} vs base {(base or '')[:40]!r})")

    # 2. applying a preset live-syncs vram_preflight_gate and bumps the rev
    rev_before = rc._settings_rev
    ok = asyncio.run(rc._apply_preset_internal("PF", persist=False))
    check("apply reports success", ok is True)
    check("apply live-syncs vram_preflight_gate into llama_config",
          LC.VRAM_PREFLIGHT_GATE is False)
    check("apply live-syncs vram_preflight_gate into manager_load",
          ML.VRAM_PREFLIGHT_GATE is False)
    check("apply syncs vram_preflight_gate into server_manager (dynamic attr)",
          getattr(LSM, "VRAM_PREFLIGHT_GATE", None) is False)
    check("apply bumps the settings revision (stale-tab guard)",
          rc._settings_rev > rev_before, f"({rev_before} -> {rc._settings_rev})")

    # 3. bookkeeping keys stay out of a snapshot
    snap = rc._preset_snapshot()
    leaked = {"active_preset", "settings_saved_at", "preset_applied_at"} & set(snap)
    check("bookkeeping keys never enter a snapshot", not leaked, f"({sorted(leaked)})")

    # 4. PRESET-LOAD-SYNC: llama startup flags apply immediately
    S.PRESETS_FILE.write_text(json.dumps({
        "PF2": {"llama_cache_ram_mib": 2048, "llama_ctx_checkpoints": 6},
    }), encoding="utf-8")
    _ram0, _cp0 = LC.CACHE_RAM_MIB, LC.CTX_CHECKPOINTS
    ok = asyncio.run(rc._apply_preset_internal("PF2", persist=False))
    check("apply syncs llama_cache_ram_mib into both modules",
          ok is True and LC.CACHE_RAM_MIB == 2048
          and LSM.CACHE_RAM_MIB == 2048,
          f"(config={LC.CACHE_RAM_MIB}, manager={LSM.CACHE_RAM_MIB})")
    check("apply syncs llama_ctx_checkpoints into both modules",
          LC.CTX_CHECKPOINTS == 6 and LSM.CTX_CHECKPOINTS == 6)
    LC.CACHE_RAM_MIB, LSM.CACHE_RAM_MIB, LC.CTX_CHECKPOINTS, LSM.CTX_CHECKPOINTS = (
        _ram0, _ram0 if isinstance(_ram0, int) else _cp0, _cp0, _cp0)

    # 5. PRESET-MODEL-CHECK: a missing model surfaces as a warning
    S.PRESETS_FILE.write_text(json.dumps({
        "PM": {"agents": {"direct": {"model": "not-a-model-xyz"}},
               "duo_coder_model": "also-missing-xyz"},
    }), encoding="utf-8")
    _warns: list = []
    asyncio.run(rc._apply_preset_internal("PM", persist=False, warnings=_warns))
    _joined = " | ".join(_warns)
    check("missing models surface as load warnings",
          "not found locally" in _joined and "not-a-model-xyz" in _joined
          and "also-missing-xyz" in _joined, f"({_joined[:100]})")

    # 6. DST-proof autoload guard: real chronology beats string order
    check("a newer save across the DST boundary wins (parsed chronology)",
          rc.preset_autoload_stale("2026-10-27T00:30:00+02:00",
                                   "2026-10-27T00:10:00+01:00") is True,
          "(string compare would say False)")
    check("same-moment stamps still apply (historical behaviour)",
          rc.preset_autoload_stale("2026-10-27T12:00:00+01:00",
                                   "2026-10-27T12:00:00+01:00") is False)
    check("garbage stamps fall back instead of raising",
          rc.preset_autoload_stale("not-a-date", "also-not") is False)
finally:
    (S.PRESETS_FILE, S.CUSTOM_PROMPTS_DIR, cs.REGISTRY_FILE,
     cs.pipeline, rc._settings_rev, LC.VRAM_PREFLIGHT_GATE,
     ML.VRAM_PREFLIGHT_GATE, LSM.VRAM_PREFLIGHT_GATE) = saved
    # llama_server_manager declares no VRAM_PREFLIGHT_GATE attribute (the
    # "triple sync" writes it dynamically, no reader) - only restore if the
    # apply created it
    if getattr(LSM, "VRAM_PREFLIGHT_GATE", "missing") is False:
        del LSM.VRAM_PREFLIGHT_GATE
    _tmp.cleanup()

print()
print(f"SUMMARY preset_prompts_refix: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

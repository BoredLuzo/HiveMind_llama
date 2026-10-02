# -*- coding: utf-8 -*-
"""C1 (2026-10-01): resolve_image_plan + Sentinel migration.

duo_image_mode None in DEFAULT_SETTINGS is DERIVED (active vision_model.json
-> "preprocess", else "direct") — tested over the REAL settings load path
(settings file without the key), with explicit values respected, post_settings
accepting null, and a preset load resetting an explicit mode to derived.
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings as settings_mod  # noqa: E402
from core.duo_helpers import resolve_image_plan  # noqa: E402
from core import state as core_state  # noqa: E402

_B64 = "iVBORw0KGgo"


def _ctx(images=None, desc="", vision_cfg=None):
    return SimpleNamespace(images=images or [], image_description=desc,
                           vision_cfg=vision_cfg or {})


def _vc(enabled, model="qwen3-vl-2b"):
    return {"enabled": enabled, "model": model}


class ResolveImagePlan(unittest.TestCase):
    def setUp(self):
        self._snap = dict(core_state.settings)
        core_state.settings["duo_image_to_planner"] = False
        core_state.settings["duo_image_to_coder"] = False

    def tearDown(self):
        core_state.settings.clear()
        core_state.settings.update(self._snap)

    def test_no_images_all_none_no_warnings(self):
        r = resolve_image_plan("p", "c", core_state.settings, _ctx())
        self.assertEqual((r["planner"], r["coder"], r["warnings"]), ("none", "none", []))

    def test_sentinel_derives_preprocess_from_active_vision_cfg(self):
        core_state.settings["duo_image_mode"] = None
        r = resolve_image_plan("p", "c", core_state.settings,
                               _ctx([_B64], desc="a red button", vision_cfg=_vc(True)))
        self.assertEqual(r["mode"], "preprocess")
        self.assertEqual((r["planner"], r["coder"]), ("description", "description"))
        self.assertTrue(any("derived" in w for w in r["warnings"]))

    def test_sentinel_derives_direct_without_vision_cfg(self):
        core_state.settings["duo_image_mode"] = None
        r = resolve_image_plan("p", "c", core_state.settings, _ctx([_B64], vision_cfg={}))
        self.assertEqual(r["mode"], "direct")
        self.assertEqual((r["planner"], r["coder"]), ("none", "none"))
        self.assertTrue(any("no target selected" in w for w in r["warnings"]))

    def test_explicit_direct_raw_to_capable_coder(self):
        core_state.settings["duo_image_mode"] = "direct"
        core_state.settings["duo_image_to_coder"] = True
        from unittest.mock import patch
        with patch("core.model_sampling._model_profile", return_value={"vision": True}):
            r = resolve_image_plan("p", "gemma-4:e4b", core_state.settings, _ctx([_B64]))
        self.assertEqual(r["coder"], "raw")
        self.assertEqual(r["planner"], "none")
        self.assertEqual(r["warnings"], [])

    def test_checked_nonmultimodal_role_warns_and_skips(self):
        core_state.settings["duo_image_mode"] = "direct"
        core_state.settings["duo_image_to_coder"] = True
        from unittest.mock import patch
        with patch("core.model_sampling._model_profile", return_value={"vision": False}):
            r = resolve_image_plan("p", "text-model", core_state.settings, _ctx([_B64]))
        self.assertEqual(r["coder"], "none")
        self.assertTrue(any("not multimodal" in w for w in r["warnings"]))

    def test_explicit_mode_wins_over_derivation(self):
        core_state.settings["duo_image_mode"] = "direct"
        r = resolve_image_plan("p", "c", core_state.settings,
                               _ctx([_B64], desc="d", vision_cfg=_vc(True)))
        self.assertEqual(r["mode"], "direct")  # explicit beats active vision cfg


class SentinelOverRealLoadPath(unittest.TestCase):
    """duo_image_mode absent from the settings FILE -> None (setdefault) ->
    derived. Explicit value in the file -> respected. Real load path."""

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(
            suffix=".json", delete=False, mode="w", encoding="utf-8")
        self._tmp.write(json.dumps({"server_port": 8011}))
        self._tmp.close()
        self._patcher = patch.object(settings_mod, "SETTINGS_FILE",
                                     Path(self._tmp.name))
        self._patcher.start()
        settings_mod._load_cache_key = None
        settings_mod._load_cache_data = None

    def tearDown(self):
        self._patcher.stop()
        settings_mod._load_cache_key = None
        settings_mod._load_cache_data = None
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_missing_key_loads_as_none_and_derives(self):
        loaded = settings_mod.load_settings()
        self.assertIsNone(loaded["duo_image_mode"])  # sentinel from defaults
        core_state.settings.clear()
        core_state.settings.update(loaded)
        r = resolve_image_plan("p", "c", core_state.settings,
                               _ctx([_B64], desc="d", vision_cfg=_vc(True)))
        self.assertEqual((r["mode"], r["planner"]), ("preprocess", "description"))

    def test_explicit_key_respected_over_real_load_path(self):
        Path(self._tmp.name).write_text(
            json.dumps({"server_port": 8011, "duo_image_mode": "direct"}),
            encoding="utf-8")
        settings_mod._load_cache_key = None
        loaded = settings_mod.load_settings()
        self.assertEqual(loaded["duo_image_mode"], "direct")
        core_state.settings.clear()
        core_state.settings.update(loaded)
        r = resolve_image_plan("p", "c", core_state.settings,
                               _ctx([_B64], desc="d", vision_cfg=_vc(True)))
        self.assertEqual(r["mode"], "direct")


class PostSettingsNullAndPresetLoad(unittest.TestCase):
    def setUp(self):
        self._snap = dict(core_state.settings)
        self._saved = []
        self._presets = {"tp": {"duo_image_mode": None, "duo_chunking": False}}
        self._patchers = [
            patch.object(cfg, "save_settings", lambda s: self._saved.append(dict(s))),
            patch.object(cfg, "load_presets", lambda: dict(self._presets)),
            patch.object(cfg, "save_presets", lambda p: None),
            patch.object(cfg, "apply_settings_to_pipeline", lambda s: None),
            patch.object(cfg, "registry_sync_from_pipeline", lambda: None),
            patch.object(cfg, "_refresh_safe_profile_policy", lambda: None),
        ]
        for _p in self._patchers:
            _p.start()
            self.addCleanup(_p.stop)
        core_state._WEBSEARCH_AVAILABLE = False

    def tearDown(self):
        core_state.settings.clear()
        core_state.settings.update(self._snap)

    def test_post_settings_accepts_null(self):
        core_state.settings["duo_image_mode"] = "direct"
        res = asyncio.run(cfg.post_settings(_Req({"duo_image_mode": None})))
        self.assertTrue(res.get("ok"))
        self.assertIsNone(core_state.settings["duo_image_mode"])  # null accepted

    def test_preset_with_null_resets_mode_to_derived(self):
        core_state.settings["duo_image_mode"] = "direct"
        core_state.settings["duo_image_to_coder"] = True
        asyncio.run(cfg._apply_preset_internal("tp", persist=False))
        self.assertIsNone(core_state.settings["duo_image_mode"])  # preset null back in
        r = resolve_image_plan("p", "c", core_state.settings,
                               _ctx([_B64], desc="d", vision_cfg=_vc(True)))
        self.assertEqual(r["mode"], "preprocess")  # derived again


# shared imports for the preset/post_settings tests
import routers.config as cfg  # noqa: E402

_B64 = _B64


class _Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


if __name__ == "__main__":
    unittest.main(verbosity=2)


class DuoGateStatus(unittest.TestCase):
    """C2: the chat_run gate status claims only what resolve_image_plan
    decided — the attach-time projector check has the final word."""

    def test_status_text_formats_plan(self):
        from core.duo_helpers import duo_gate_status_text as T
        self.assertEqual(
            T({"planner": "none", "coder": "raw", "warnings": []}),
            "[Image] duo plan — planner: none, coder: raw")
        self.assertIn("not multimodal", T({"planner": "none", "coder": "none",
                                           "warnings": ["coder 'x' is not multimodal — image skipped for it"]}))

    def test_gate_wrapper_uses_real_settings_and_vision_cfg(self):
        from core.duo_helpers import _build_duo_image_plan as G
        core_state.settings["duo_image_mode"] = None
        core_state.settings["duo_image_to_coder"] = True
        from unittest.mock import patch
        with patch("core.model_sampling._model_profile", return_value={"vision": True}):
            plan = G("p", "gemma-4:e4b", core_state.settings,
                     [_B64], "desc", {"enabled": True, "model": "vl"})
        self.assertEqual(plan["mode"], "preprocess")  # sentinel derives first
        self.assertEqual((plan["planner"], plan["coder"]), ("description", "description"))


class VisionReloadRule(unittest.TestCase):
    """C3: upgrade-only reload — never strip a projector."""

    def test_matrix(self):
        from backend.llama_manager_utils import needs_vision_reload as N
        self.assertFalse(N(True, False))   # plain load keeps vision slot
        self.assertFalse(N(True, True))    # same
        self.assertFalse(N(False, False))  # same
        self.assertTrue(N(False, True))    # upgrade: without -> with projector

    def test_unpin_clears_flag(self):
        from backend.manager_evict import LlamaEvictMixin
        m = LlamaEvictMixin()
        m._slots = [SimpleNamespace(model="coder", pinned=True),
                    SimpleNamespace(model="other", pinned=True)]
        n = m.unpin("coder")
        self.assertEqual(n, 1)
        self.assertFalse(m._slots[0].pinned)
        self.assertTrue(m._slots[1].pinned)  # other model untouched


class PinExpiryAndUnpinAll(unittest.TestCase):
    """C3-hardening (2026-10-02): pins expire after 30 min; run start
    resets all pins (stale pins from a crashed run block nothing)."""

    def _fake_manager(self, slots):
        from backend.manager_evict import LlamaEvictMixin
        m = LlamaEvictMixin()
        m._slots = slots
        m._metric_inc = lambda *a: None
        return m

    def test_evict_lru_ignores_expired_pins(self):
        import time
        from types import SimpleNamespace as NS
        from backend.manager_process import LlamaManagerProcessMixin
        m = LlamaManagerProcessMixin()
        old_pin = NS(model="old-coder", slot_id=0, is_running=True, pinned=True,
                     pinned_at=time.time() - 3600, last_used=1, kill=lambda: None)
        fresh_pin = NS(model="fresh-coder", slot_id=1, is_running=True, pinned=True,
                       pinned_at=time.time(), last_used=2, kill=lambda: None)
        m._slots = [old_pin, fresh_pin]
        victim = m._evict_lru()
        self.assertEqual(victim.model, "old-coder")  # fresh pin protected

    def test_unpin_all_resets_every_pin(self):
        from backend.manager_evict import LlamaEvictMixin
        m = LlamaEvictMixin()
        m._slots = [SimpleNamespace(model="a", pinned=True, pinned_at=1),
                    SimpleNamespace(model="b", pinned=True, pinned_at=2)]
        n = m.unpin_all()
        self.assertEqual(n, 2)
        self.assertTrue(all(not s.pinned for s in m._slots))


class EvictVisionGuard(unittest.TestCase):
    def test_evicts_only_in_code_duo(self):
        from core.duo_helpers import should_evict_vision_after_prepro as E
        self.assertTrue(E("code_duo", "qwen3-vl-2b", {"gemma-4:e4b"}))
        self.assertFalse(E("simple", "qwen-vl-2b", set()))      # normal chat: NEVER
        self.assertFalse(E("pipeline", "qwen-vl-2b", set()))    # pipeline keeps it
        self.assertFalse(E("code_duo", "gemma-4:e4b", {"gemma-4:e4b"}))  # it IS a duo model
        self.assertFalse(E("code_duo", "", {"x"}))              # no vision model: nothing to evict

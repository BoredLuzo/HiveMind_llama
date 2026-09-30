# -*- coding: utf-8 -*-
"""Preset housekeeping (2026-09-30): active_preset ownership.

- del_preset clears active_preset when the deleted preset was active (b)
- a preset workspace path that does not exist is NOT applied; the load
  response carries a warning for the UI (c)
- startup auto-load with a missing preset clears active_preset (d)
- post_settings never accepts an active_preset key (e) — the value is owned
  exclusively by preset load/delete/startup
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import routers.config as cfg  # noqa: E402
from core import state as core_state  # noqa: E402

_EXISTING_WS = tempfile.mkdtemp(prefix="preset_ws_test_")


class _Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


class PresetHousekeeping(unittest.TestCase):
    def setUp(self):
        self._orig_settings = dict(cfg.settings)
        self._saved_settings = []
        self._saved_presets = []
        self._patchers = [
            patch.object(cfg, "save_settings",
                         lambda s: self._saved_settings.append(dict(s))),
            patch.object(cfg, "load_presets", lambda: dict(self._presets)),
            patch.object(cfg, "save_presets",
                         lambda p: self._saved_presets.append(dict(p))),
            patch.object(cfg, "apply_settings_to_pipeline", lambda s: None),
            patch.object(cfg, "registry_sync_from_pipeline", lambda: None),
            patch.object(cfg, "_refresh_safe_profile_policy", lambda: None),
        ]
        for _p in self._patchers:
            _p.start()
            self.addCleanup(_p.stop)
        core_state._WEBSEARCH_AVAILABLE = False
        self._presets = {}

    def tearDown(self):
        cfg.settings.clear()
        cfg.settings.update(self._orig_settings)

    def test_del_active_preset_clears_and_persists(self):
        self._presets = {"tpA": {"duo_chunking": True}}
        cfg.settings["active_preset"] = "tpA"
        res = asyncio.run(cfg.del_preset("tpA"))
        self.assertTrue(res["ok"])
        self.assertIsNone(cfg.settings["active_preset"])
        self.assertEqual(len(self._saved_settings), 1)  # persisted
        self.assertNotIn("tpA", self._saved_presets[0])

    def test_del_other_preset_keeps_active(self):
        self._presets = {"tpA": {}, "tpB": {}}
        cfg.settings["active_preset"] = "tpB"
        asyncio.run(cfg.del_preset("tpA"))
        self.assertEqual(cfg.settings["active_preset"], "tpB")

    def test_load_skips_nonexistent_workspace_with_warning(self):
        self._presets = {"tpWS": {"workspace": "Z:/definitely/missing/dir",
                                  "duo_chunking": True}}
        cfg.settings["workspace"] = _EXISTING_WS
        warns: list = []
        ok = asyncio.run(cfg._apply_preset_internal(
            "tpWS", persist=False, warnings=warns))
        self.assertTrue(ok)
        self.assertEqual(len(warns), 1)
        self.assertIn("does not exist", warns[0])
        self.assertEqual(cfg.settings["workspace"], _EXISTING_WS)  # untouched
        self.assertTrue(cfg.settings["duo_chunking"])  # rest applied

    def test_load_applies_existing_workspace(self):
        self._presets = {"tpWS2": {"workspace": _EXISTING_WS}}
        cfg.settings["workspace"] = ""
        warns: list = []
        asyncio.run(cfg._apply_preset_internal(
            "tpWS2", persist=False, warnings=warns))
        self.assertEqual(warns, [])
        self.assertEqual(cfg.settings["workspace"], _EXISTING_WS)

    def test_startup_autoload_clears_missing_preset(self):
        cfg.settings["active_preset"] = "ghost_preset"
        asyncio.run(cfg.startup_preset_autoload())
        self.assertIsNone(cfg.settings["active_preset"])
        self.assertEqual(len(self._saved_settings), 1)  # cleared + persisted

    def test_startup_autoload_applies_existing(self):
        self._presets = {"tpLive": {"duo_chunking": False}}
        cfg.settings["active_preset"] = "tpLive"
        asyncio.run(cfg.startup_preset_autoload())
        self.assertEqual(cfg.settings["active_preset"], "tpLive")

    def test_post_settings_ignores_active_preset(self):
        cfg.settings["active_preset"] = "tpLive"
        self._presets = {"tpLive": {}}
        res = asyncio.run(cfg.post_settings(_Req({
            "active_preset": "ghost2", "duo_chunking": True})))
        self.assertTrue(res.get("ok"))
        self.assertEqual(cfg.settings["active_preset"], "tpLive")  # untouched
        self.assertTrue(cfg.settings["duo_chunking"])  # rest applied


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""C3a (2026-10-01): strict mmproj fallback + gemma-4 vision base.

The old directory fallback took the FIRST mmproj file — a 2B projector on
a 4B model (broken vision, wrong dimensions). Now: size-tag match or None.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.llama_manager_utils import (  # noqa: E402
    _VISION_CAPABLE_BASES,
    pick_mmproj_fallback,
)


class MmprojFallback(unittest.TestCase):
    def setUp(self):
        self._d = Path(tempfile.mkdtemp())

    def _mk(self, name):
        (self._d / name).write_bytes(b"\x00")

    def test_size_tag_match_wins(self):
        self._mk("mmproj-Qwen3.5-2B-F16.gguf")
        self._mk("mmproj-Qwen3.5-4B-F16.gguf")
        p = pick_mmproj_fallback(self._d, "qwen3.5:4b-ud")
        self.assertIsNotNone(p)
        self.assertIn("4B", p.name)

    def test_no_matching_tag_returns_none(self):
        self._mk("mmproj-Qwen3.5-2B-F16.gguf")
        self.assertIsNone(pick_mmproj_fallback(self._d, "qwen3.5:4b-ud"))  # strict: no 2B for 4B

    def test_no_files_returns_none(self):
        self.assertIsNone(pick_mmproj_fallback(self._d, "qwen3.5:4b-ud"))

    def test_missing_dir_returns_none(self):
        self.assertIsNone(pick_mmproj_fallback(self._d / "nope", "qwen3.5:4b-ud"))

    def test_gemma4_is_vision_capable_base(self):
        self.assertIn("gemma-4", _VISION_CAPABLE_BASES)


if __name__ == "__main__":
    unittest.main(verbosity=2)

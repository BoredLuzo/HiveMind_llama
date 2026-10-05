# -*- coding: utf-8 -*-
"""Feature-search core (static/feature_search_core.js) under node.

The alias expansion and dedupe rules of the UI feature search are pure JS;
this suite runs them with node when available and SKIPS (loudly) when node
is missing. Fails on any assertion failure or syntax error in the core.
"""
import shutil
import subprocess
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
JS = os.path.join(HERE, "js", "test_feature_search_core.mjs")


def test_feature_search_core_runs_under_node():
    if not shutil.which("node"):
        print("SKIP: node not installed")
        return
    out = subprocess.run(["node", JS], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"node core test failed:\n{out.stdout}\n{out.stderr}"


def test_core_file_present_and_pure():
    src = os.path.join(HERE, "..", "static", "feature_search_core.js")
    assert os.path.isfile(src)
    text = open(src, encoding="utf-8").read()
    assert "expandTerms" in text and "dedupe" in text
    # purity: no DOM/window access at module scope beyond the export guard
    assert "querySelector" not in text and "getElementById" not in text

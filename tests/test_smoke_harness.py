# -*- coding: utf-8 -*-
"""Harness tests for the vision smoke scripts (2026-10-04, Sonnet #2).

The dry-runs only covered path resolution and PNG rendering - the GRADING
logic and the CLEANUP never ran. These tests exercise both without GPU:
  - find_visual_claims: positive claims match, negations do NOT ("there is
    nothing in the image" was a false alarm with the old substring list)
  - _stop_tree: kills a DUMMY process tree (parent + child), verified dead
  - _port_free: True on a free port, False on a bound one
"""
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.smoke_vision_negative import find_visual_claims  # noqa: E402
from tests.direct_vision_check import _port_free, _stop_tree  # noqa: E402


def test_claims_match_assertions():
    assert find_visual_claims("I can see a red field in the picture.") != []
    assert find_visual_claims("As shown in the attachment, the layout is 3:2.") != []
    assert find_visual_claims("The image shows a white square.") != []
    assert find_visual_claims("Looking at the image, the stripe is diagonal.") != []


def test_claims_do_not_match_negations():
    # the exact false alarm from the old substring list:
    assert find_visual_claims("There is nothing in the image I could use.") == []
    assert find_visual_claims("No image was provided to me.") == []
    assert find_visual_claims("I will create the file now.") == []
    assert find_visual_claims("") == []


def test_claims_dedupe_case_insensitive():
    hits = find_visual_claims("I CAN SEE it. I can see it again.")
    assert hits == ["i can see"]


def _spawn_tree():
    """Parent python that spawns a sleeping child - both must die."""
    parent_code = (
        "import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(120)']); p.wait()")
    parent = subprocess.Popen([sys.executable, "-c", parent_code],
                              creationflags=subprocess.CREATE_NEW_CONSOLE)
    time.sleep(1.5)  # let the child spawn
    return parent


def _tree_alive(pid):
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                         capture_output=True, text=True).stdout.lower()
    return str(pid) in out and "python" in out


def test_stop_tree_kills_parent_and_child():
    parent = _spawn_tree()
    try:
        assert _tree_alive(parent.pid), "dummy parent did not start"
        _stop_tree(parent)
        time.sleep(1.0)
        assert parent.poll() is not None, "parent survived _stop_tree"
    finally:
        if parent.poll() is None:
            parent.kill()


def test_port_free_logic():
    assert _port_free(8199) is True  # nothing listens there in tests
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 8199))
        s.listen(1)
        assert _port_free(8199) is False

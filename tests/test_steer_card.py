"""Steer card visibility (2026-10-06).

Owner: steering a run was invisible — the drain sites emitted a 60-char
"🧭 steered:" STATUS line that drowned in the status stream, rendered as a
transient divider and was never persisted. Now every drained steer emits a
dedicated `{"type": "steer"}` event that:
  - the UI renders as a persistent card in the chat flow (and the DOM
    autosave persists it as an assistant part — same trick as the planner
    blocks),
  - the gateway relays to the phone as its own "Steer picked up" message
    (both the own-run stream loop and the takeover walker).

Run: python tests/test_steer_card.py
Exit 0 = all pass, Exit 1 = failures.
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


ROOT = Path(__file__).parent.parent


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_tool_loop_emits_steer():
    src = _src("core/tool_loop.py")
    if '"type": "steer"' in src and "drain_steer_messages" in src:
        ok("tool_loop: drained steers emit the dedicated steer event")
    else:
        fail("tool_loop", "steer event missing near the drain site")


def test_duo_sites_emit_steer():
    src = _src("core/duo_runner.py")
    hits = src.count('"type": "steer"')
    old = src.count('"🧭 steered: "')
    if hits >= 2 and old == 0:
        ok(f"duo_runner: both drain sites emit steer events ({hits}); old status line gone")
    else:
        fail("duo_sites", f"steer events={hits}, old status lines={old}")


def test_appjs_card_and_persistence():
    src = _src("static/app.js")
    handler = "d.type === 'steer'" in src
    card = "steer-note" in src
    save = src.count("steer-note") >= 2
    if handler and card and save:
        ok("app.js: steer event renders a card and the autosave persists it")
    else:
        fail("appjs", f"handler={handler} card={card} autosave={save}")


def test_bridge_relays_steer():
    src = _src("hivemind_gateway/bridge.py")
    hits = src.count('etype == "steer"') + src.count('ev.get("type") == "steer"')
    if hits >= 2 and "Steer picked up" in src:
        ok(f"bridge: own-run loop and takeover walker relay steer pickups ({hits})")
    else:
        fail("bridge", f"steer branches={hits}")


def test_event_shape_documented_consistency():
    # all three emitters carry content + images so UI and gateway can render
    for rel, needle in (("core/tool_loop.py", '"images": len('),
                        ("core/duo_runner.py", '"images": len(')):
        if needle not in _src(rel):
            fail("shape", f"{rel} steer event lacks the images count")
            return
    ok("event shape: every emitter carries content + images count")


if __name__ == "__main__":
    test_tool_loop_emits_steer()
    test_duo_sites_emit_steer()
    test_appjs_card_and_persistence()
    test_bridge_relays_steer()
    test_event_shape_documented_consistency()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

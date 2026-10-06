"""Re-audit round 2 (2026-10-06): the polish fixes audited again + image paths.

New findings fixed here:
  R1 - routed:"stale" (nonce mismatch: the card was replaced by a newer
       one) fell through to "delivered" on ALL THREE phone answer paths —
       same honesty class as the expired toast. Now answered honestly
       everywhere.
  R2 - the start route's PowerShell liveness check ran BLOCKING on the
       event loop AND widened the double-start window: a UI double-click
       could spawn two gateways. The route now serializes under an
       asyncio.Lock with the spawn INSIDE, and the PS child runs via
       asyncio.to_thread.
  R3 - /stream took ANY image list unbounded (no count, no size cap —
       unlike the steer route's 4 x 14 MB). Intake now clamps to 8 parts
       x 14 MB and drops garbage entries.

Image-path verdicts (no code change needed):
  - save path is server-generated (img_<ts>_<n>_<sha8>.<ext>, magic
    sniffed) — no client-controlled path components, no traversal
  - persistence is opt-in (image_uploads_persistent, default OFF)
  - phone photos get a readable "Text only in this build" note
  - run workspace resolution has NO body channel (the body "workspace"
    key only feeds dom import) — resolution order is force_ui ->
    task_path -> chat_ctx -> ui_setting -> last_used -> env -> cwd,
    every candidate existence-checked

Run: python tests/test_reaudit_polish.py
Exit 0 = all pass, Exit 1 = failures.
"""
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


def _src(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_stale_handled_everywhere():
    src = _src("hivemind_gateway/bridge.py")
    hits = src.count('routed == "stale"')
    exp = src.count('routed == "expired"')
    if hits >= 3 and hits == exp:
        ok(f"bridge: stale answered honestly on every path that knows expired ({hits})")
    else:
        fail("stale", f"stale={hits} expired={exp} (must match)")


def test_start_route_serialized():
    src = _src("server.py")
    seg = src.split("async def gateway_start")[1].split("@app.post")[0]
    lock = "_gateway_start_lock" in seg
    spawn_inside = "        subprocess.Popen" in seg
    thread = "asyncio.to_thread" in seg
    if lock and spawn_inside and thread:
        ok("start route: lock serialized, spawn inside the lock, PS off-loop")
    else:
        fail("start_lock", f"lock={lock} spawn_inside={spawn_inside} to_thread={thread}")


def test_image_intake_cap():
    src = _src("server.py")
    seg = src.split("IMAGE INTAKE CAP")[1].split("mode = body.get")[0]
    checks = [
        ("8]" in seg or "images[:8]" in seg, "count clamp to 8"),
        ("14_000_000" in seg, "per-image 14 MB ceiling"),
        ("len(b.strip()) >= 32" in seg, "garbage entries dropped"),
        ("not isinstance(images, list)" in seg, "non-list neutralized"),
    ]
    bad = [name for cond, name in checks if not cond]
    if not bad:
        ok("stream intake: image list clamped (count, size, type, garbage)")
    else:
        fail("intake", f"missing: {bad}")


def test_image_save_path_no_traversal():
    src = _src("core/chat_run.py")
    if 'f"img_{_stamp}_{_ii + 1}_{_h8}.{_ext}"' in src \
            and "write_bytes" in src:
        ok("image save: server-generated filename only, no client path parts")
    else:
        fail("save_path", "filename scheme changed — re-check traversal safety")


def test_phone_photos_get_note():
    src = _src("hivemind_gateway/main.py")
    if "Text only in this build" in src:
        ok("phone photos: readable rejection note (no silent drop)")
    else:
        fail("photos", "rejection note missing")


if __name__ == "__main__":
    test_stale_handled_everywhere()
    test_start_route_serialized()
    test_image_intake_cap()
    test_image_save_path_no_traversal()
    test_phone_photos_get_note()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

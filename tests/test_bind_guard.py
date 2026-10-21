"""External-bind guard (2026-10-21 audit finding).

HIVEMIND_HOST can bind the whole API to a LAN interface - and the API can
write files and execute commands. The guard requires non-local clients to
present HIVEMIND_INTERNAL_TOKEN while the bind is external; loopback
clients (the local UI) are unaffected. Pure-decision tests for
infra/security.external_bind_decision plus the loopback classifier.
update.bat / server.py are never executed here."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from infra.security import _is_loopback_client, external_bind_decision

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# Loopback classifier
check("loopback: 127.0.0.1", _is_loopback_client("127.0.0.1"))
check("loopback: ::1", _is_loopback_client("::1"))
check("loopback: ::1 with zone", _is_loopback_client("::1%0"))
check("loopback: localhost", _is_loopback_client("localhost"))
check("loopback: ipv4-mapped ::ffff:127.0.0.1",
      _is_loopback_client("::ffff:127.0.0.1"))
check("not loopback: LAN ip", not _is_loopback_client("192.168.1.50"))
check("not loopback: empty", not _is_loopback_client(""))

# Bind off: everything passes regardless
check("bind off: LAN client allowed",
      external_bind_decision("192.168.1.50", "", "secret", bind_external=False) == 0)

# Bind on: loopback unaffected, token enforced for remote clients
check("bind on: loopback still allowed",
      external_bind_decision("127.0.0.1", "", "secret", bind_external=True) == 0)
check("bind on: no token configured -> 503",
      external_bind_decision("192.168.1.50", "", "", bind_external=True) == 503)
check("bind on: missing header -> 403",
      external_bind_decision("192.168.1.50", "", "secret", bind_external=True) == 403)
check("bind on: wrong token -> 403",
      external_bind_decision("192.168.1.50", "wrong", "secret", bind_external=True) == 403)
check("bind on: correct token -> allowed",
      external_bind_decision("192.168.1.50", "secret", "secret",
                             bind_external=True) == 0)

# Source guards: middleware registered, flag flipped in run.py
_srv = (Path(__file__).parent.parent / "server.py").read_text(
    encoding="utf-8", errors="replace")
check("server.py registers the external-bind guard",
      "_external_bind_guard" in _srv and 'app.middleware("http")(_external_bind_guard)' in _srv)
_run = (Path(__file__).parent.parent / "run.py").read_text(
    encoding="utf-8", errors="replace")
check("run.py flips the flag on non-loopback binds",
      "EXTERNAL_BIND[\"on\"] = True" in _run
      and 'not in ("127.0.0.1", "::1", "localhost")' in _run)

print()
print(f"SUMMARY bind_guard: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

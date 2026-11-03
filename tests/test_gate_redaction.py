# -*- coding: utf-8 -*-
"""B8: the destructive gate must not log credential-shaped values."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.runner import _redact_secret_text as R, _approval_preview  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


out = R("curl -H 'Authorization: Bearer s3cr3t-token' https://example.test")
check("1 bearer tokens are masked", "s3cr3t-token" not in out, out)
check("2 the command stays visible", "curl" in out and "example.test" in out)

out = R("deploy --token=abc123 --api_key: xyz789")
check("3 token assignments are masked",
      "abc123" not in out and "xyz789" not in out, out)

out = R("python selftest.py")
check("4 a normal command is unchanged", out == "python selftest.py")

check("5 the gate applies it before logging",
      "_details = _redact_secret_text(_details)" in
      (ROOT / "tools" / "runner.py").read_text(encoding="utf-8"))

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)

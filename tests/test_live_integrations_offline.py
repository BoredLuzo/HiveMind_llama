# -*- coding: utf-8 -*-
"""tests/live_integrations.py offline contract (C5).

With no integration env set every check must SKIP with a reason, without a
single network call; --list must stay offline; secrets never reach stdout.
The real integrations are NOT exercised here (see docs/integrations.md).

Run: python tests/test_live_integrations_offline.py
"""
import contextlib
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402

from tests import live_integrations as li  # noqa: E402
from tests._harness import Harness  # noqa: E402

_h = Harness()
check = _h.check

for _k in list(os.environ):
    if _k.startswith("HIVEMIND_LIVE_") or _k in (li.ENV_TG_TOKEN, li.ENV_TG_OWNER):
        del os.environ[_k]

_net_calls = []


def _no_net(*a, **kw):
    _net_calls.append(a[:2])
    raise AssertionError("network call in an unconfigured check")


for _attr in ("get", "post", "stream", "request"):
    setattr(httpx, _attr, _no_net)


def _run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = li.main(list(argv))
    return rc, buf.getvalue()


rc, out = _run("--list")
check("L1 --list exits 0 and names all 7 checks",
      rc == 0 and all(c.name in out for c in li.CHECKS) and len(li.CHECKS) == 7, f" rc={rc}")
check("L2 --list shows every check as not configured",
      out.count("not configured") == 7)

rc, out = _run()
lines = [ln for ln in out.splitlines() if ln.startswith("[")]
check("L3 unconfigured run: 7 SKIP lines, each with a reason, exit 0",
      rc == 0 and len(lines) == 7 and all(ln.startswith("[SKIP]") and len(ln.split(None, 2)) == 3
                                          for ln in lines), f" rc={rc} lines={len(lines)}")
check("L4 no network call without configuration", not _net_calls, f" calls={_net_calls}")

rc, _out = _run("--strict")
check("L5 --strict turns skips into exit 1", rc == 1, f" rc={rc}")

rc, _out = _run("nope")
check("L6 unknown check name -> exit 2", rc == 2, f" rc={rc}")

# Opt-in flags alone are not enough: TTS without an engine URL still skips offline.
os.environ[li.ENV_TTS] = "1"
rc, out = _run("tts")
check("L7 opt-in flag without engine URL still skips, no network",
      rc == 0 and "[SKIP]" in out and li.ENV_BASE in out and not _net_calls)
del os.environ[li.ENV_TTS]

# Redaction: the configured token and any token-shaped string are masked.
_fake = "123456789:AAFakeTokenForRedactionTest_abcdefgh"
os.environ[li.ENV_TG_TOKEN] = _fake
red = li.redact(f"POST https://api.telegram.org/bot{_fake}/getMe failed; other 987654321:{'x' * 30}")
check("L8 redact() masks the token and token-shaped strings",
      _fake not in red and "x" * 30 not in red and red.count("***") == 2)
del os.environ[li.ENV_TG_TOKEN]

sys.exit(_h.finish("live_integrations_offline"))

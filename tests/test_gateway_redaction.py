"""Gateway WP1: log redaction — the token sits in the Telegram URLs.

Brief test requirement: "grep over all logs for the token pattern must be
empty." This suite emits messages through several loggers/handlers with a
realistic token embedded (incl. inside an API URL) and greps the captured
streams afterwards.
"""
import io
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hivemind_gateway.redaction import (
    TokenRedactionFilter,
    attach_redaction,
    scrub_text,
)

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


TOKEN = "7488112345:AAH9x_TEST_TOKEN_abcdEFGHijklMNOpqrs"


def _make_logger(secrets):
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    h.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    lg = logging.getLogger("gwtest.redaction.case")
    lg.handlers.clear()
    lg.setLevel(logging.DEBUG)
    lg.addHandler(h)
    filt = TokenRedactionFilter(secrets)
    h.addFilter(filt)
    return lg, stream, filt


# ── 1. literal token scrubbed in plain messages ─────────────────────────
lg, stream, filt = _make_logger([TOKEN])
lg.warning("getUpdates failed for url https://api.telegram.org/bot%s/getUpdates", TOKEN)
lg.info("token=%s embedded", TOKEN)
out = stream.getvalue()
check("token absent from logs", TOKEN not in out)
check("redaction marker present", "***REDACTED***" in out)
check("url shape kept greppable", "bot" in out and "getUpdates" in out)

# ── 2. unknown token still caught by the generic URL pattern ────────────
lg2, stream2, _ = _make_logger([])
lg2.warning("POST https://api.telegram.org/bot123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/sendMessage")
out2 = stream2.getvalue()
check("generic bot-url pattern redacted",
      "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in out2 and "bot123456789" in out2)

# ── 3. %-args cannot re-introduce the secret later ──────────────────────
lg3, stream3, _ = _make_logger([TOKEN])
lg3.info("token is %s today", TOKEN)
logging.shutdown()
out3 = stream3.getvalue()
check("lazy %-formatting scrubbed", TOKEN not in out3)

# ── 4. scrub_text direct ────────────────────────────────────────────────
s = scrub_text(f"bot999:{TOKEN.split(':', 1)[1]} tail", [])
check("scrub_text url", "AAH9x" not in s)
s2 = scrub_text("nothing to see", [])
check("scrub_text passthrough", s2 == "nothing to see")

# ── 5. the grep test: every log record the gateway could emit ───────────
big = io.StringIO()
root = logging.getLogger("gwtest.redaction.grep")
root.handlers.clear()
root.setLevel(logging.DEBUG)
gh = logging.StreamHandler(big)
gh.setFormatter(logging.Formatter("%(message)s"))
root.addHandler(gh)
attach_redaction(root, [TOKEN])
for name in ("a", "b", "c"):
    sub = logging.getLogger(f"gwtest.redaction.grep.{name}")
    sub.propagate = True
    sub.debug(f"call https://api.telegram.org/bot{TOKEN}/sendMessage")
    sub.info(f"echo {TOKEN}")
    sub.error("plain message")
gh.flush()
grep_hits = [ln for ln in big.getvalue().splitlines() if TOKEN in ln]
check("grep over all captured log lines is empty", grep_hits == [])

print()
print(f"passed={passed} failed={failed}")
sys.exit(0 if failed == 0 else 1)

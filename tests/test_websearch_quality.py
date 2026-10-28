"""web_search: near-duplicate stop + Wikipedia fallback (live findings 2026-10-17).

A live run asked for "Nintendo 64 technical specifications … site:wikipedia.org"
eight times with reshuffled words, burned its websearch budget and died as
loop_detected after 24 rounds with zero files written. Two things were wrong:

1. the dedup compared exact strings, so "RDP processor RAM memory" and "RDP
   processor graphics memory" were different searches;
2. the search itself returned junk: the configured instance only has a bing
   engine pinned to a German market (google/wikipedia engines return 0 rows), the
   ``site:`` operator is ignored, and every variant gave the same shop homepages.

This suite pins the fixes:
  (a) near-identical queries (Jaccard >= 0.72 on tokens, site: ignored) are
      recognised, and the third one stops the search with a clear tool error
      instead of consuming the budget;
  (b) a query that names Wikipedia goes straight to the MediaWiki API - no engine
      needed - with a descriptive User-Agent (Wikimedia answers 403 without one);
  (c) long stuffed queries are retried with shorter prefixes and ranked by title
      overlap, so "Nintendo 64 technical specifications RDP …" finds the console
      page instead of "List of file formats";
  (d) an empty SearXNG result falls back to Wikipedia once;
  (e) the "fetched today" header is a real f-string again (it printed the literal
      placeholder in every result).

Run: python tests/test_websearch_quality.py
"""
import asyncio
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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


# ── (a) near-duplicate detection ───────────────────────────────────────────
from core.tool_exec_helpers import (  # noqa: E402
    _WS_DUP_STOP, _ws_is_near_dup, _ws_tokens,
)

A = "Nintendo 64 technical specifications RDP processor RAM memory site:wikipedia.org"
B = "Nintendo 64 technical specifications RDP or RDRAM memory site:wikipedia.org N64 specs"
C = "python asyncio tutorial"


def jac(x, y):
    tx, ty = _ws_tokens(x), _ws_tokens(y)
    return len(tx & ty) / max(1, len(tx | ty))


check("site: does not count as a token", "site" not in _ws_tokens(A))
check("reshuffled variants count as the same search",
      _ws_is_near_dup(_ws_tokens(A), _ws_tokens(B)), f" (jaccard {jac(A, B):.2f})")
check("a different topic does not",
      not _ws_is_near_dup(_ws_tokens(A), _ws_tokens(C)), f" (jaccard {jac(A, C):.2f})")
check("the stop threshold is small but not zero", 2 <= _WS_DUP_STOP <= 5)
src_help = (ROOT / "core" / "tool_exec_helpers.py").read_text(encoding="utf-8", errors="replace")
check("the tool returns a hard stop", "[TOOL_ERROR:WEBSEARCH_LOOP]" in src_help)
check("the stop tells the model what to do",
      "STOP" in src_help and "task_complete" in src_help)
check("exact duplicates are still skipped instantly",
      "duplicate query skipped" in src_help)
check("near duplicates are logged for the next audit",
      "[WS-NEAR-DUP]" in src_help)

# ── (b)-(e) websearch tool ─────────────────────────────────────────────────
src_ws = (ROOT / "tools" / "websearch.py").read_text(encoding="utf-8", errors="replace")
check("Wikipedia has a direct path", "async def _wiki_direct" in src_ws)
check("a query naming Wikipedia triggers it",
      're.search(r"\\bwikipedia\\b"' in src_ws)
check("a descriptive User-Agent is sent",
      "_WIKI_UA" in src_ws and "User-Agent" in src_ws)
check("the site: operator is stripped for the API",
      "_WIKI_SITE_ANY.sub" in src_ws)
check("long queries are retried shorter", "for n in (len(toks), 4, 3, 2):" in src_ws)
check("hits are ranked by title overlap", "def _wiki_rank" in src_ws)
check("an empty engine result falls back to Wikipedia",
      src_ws.count("_wiki_direct(") >= 2)
check("the header is a real f-string again",
      'f"(fetched today: {_ws_today}' in src_ws)
check("the header line is an f-string",
      any('f"(fetched today: {_ws_today}' in line for line in src_ws.splitlines()))

# ── live behaviour (needs outbound network; SearXNG is optional) ───────────
# FLAKE-GUARD (2026-10-28): these checks went red once because the Wikipedia
# fallback answered with a hint instead of results - minutes later the same call
# returned both expected strings. They test the code, not Wikipedia's mood, so:
# retry once, and when the answer is a hint ("not reachable", "no results"),
# print why and skip instead of failing. Forcing is possible either way:
#   HIVEMIND_TEST_LIVE_WEBSEARCH=0  -> skip the live part entirely
#   HIVEMIND_TEST_LIVE_WEBSEARCH=1  -> assert even on a hint (old behaviour)
import os as _os  # noqa: E402
from tools import websearch  # noqa: E402

_LIVE_ENV = _os.environ.get("HIVEMIND_TEST_LIVE_WEBSEARCH", "")
_LIVE = _LIVE_ENV != "0"
_LIVE_FORCE = _LIVE_ENV == "1"
_HINT_MARKERS = ("not reachable", "no results", "rate limit", "429",
                 "no usable", "timed out")


def _looks_like_hint(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _HINT_MARKERS)


def _live_search(query: str) -> str:
    """One search, retried once; returns "" when the answer is a hint."""
    for attempt in (1, 2):
        try:
            out = asyncio.run(_live_one(query))
        except (OSError, RuntimeError, ValueError, KeyError,
                asyncio.TimeoutError) as exc:
            out = ""
            print(f"  .. live attempt {attempt} failed "
                  f"({type(exc).__name__}: {exc})")
        if out and (not _looks_like_hint(out) or _LIVE_FORCE):
            return out
        if out:
            print(f"  .. live answer is a hint, not a result: "
                  f"{out.strip().splitlines()[0][:90]}")
        elif attempt == 1:
            time.sleep(1.5)
    return ""


async def _live_one(query: str) -> str:
    websearch.configure(host="http://localhost:8888", enabled=True)
    return await websearch.web_search(query, 3)


out = _live_search(
    "Nintendo 64 technical specifications RDP processor RAM memory "
    "site:wikipedia.org") if _LIVE else ""

if out:
    check("the looping query now returns an encyclopedia page",
          "wikipedia.org/wiki/Nintendo_64" in out or "Nintendo 64" in out)
    check("the answer is marked as a direct API search",
          "direct API search" in out)

    async def _live_plain():
        websearch.configure(host="http://localhost:8888", enabled=True)
        return await websearch.web_search("python asyncio tutorial", 3)

    try:
        plain = asyncio.run(_live_plain())
    except (OSError, RuntimeError, ValueError, KeyError, asyncio.TimeoutError) as exc:
        plain = ""
        print(f"  .. plain live check skipped ({type(exc).__name__})")
    if plain:
        # A down SearXNG answers with a plain hint ("SearXNG not reachable ..."),
        # not with an error - that used to fail the suite on a machine without
        # the container. Only a real answer must carry the date header.
        if "SearXNG not reachable" in plain or "not reachable" in plain:
            print("  .. engine path skipped (SearXNG not reachable)")
        else:
            check("the engine path still works and shows a real date",
                  re.search(r"fetched today: \d{4}-\d{2}-\d{2}", plain) is not None)
else:
    print("  .. live check produced no output (SearXNG/network down) - source checks only")

print()
print(f"SUMMARY websearch_quality: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

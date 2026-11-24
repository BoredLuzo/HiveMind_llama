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
from tests.ui_sources import read_all_ui_js  # noqa: E402
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="summary")
check = h.check

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
check("the stop threshold leaves room for rephrasing", 6 <= _WS_DUP_STOP <= 12)
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

# ── (f) site:, relevance filtering, URL cleaning, migration & UI hit count ─
def _test_quality_filters():
    import json
    import tempfile

    import settings as settings_mod

    # URL cleaner strips tracking params while keeping functional query args
    dirty = "https://www.microsoft.com/en-us/download/details.aspx?id=35&msockid=0d4a75&utm_source=bing"
    cleaned = websearch._clean_result_url(dirty)
    check("url cleaner strips msockid and utm_*",
          cleaned == "https://www.microsoft.com/en-us/download/details.aspx?id=35", cleaned)

    # Deduplication merges duplicate URLs across engines cleanly
    deduped = websearch._dedup_results([
        {"title": "Docs", "url": "https://docs.python.org/3/library/asyncio.html?utm_source=x",
         "content": "Asyncio guide", "engine": "yahoo", "score": 1.0},
        {"title": "Docs — Python", "url": "https://docs.python.org/3/library/asyncio.html",
         "content": "Asyncio guide and API reference", "engine": "duckduckgo web", "score": 2.0},
    ])
    check("dedup merges multi-engine duplicates",
          len(deduped) == 1 and deduped[0]["_engines_set"] == {"yahoo", "duckduckgo web"},
          str(deduped))

    # Strict site: and -site: operator enforcement rejects off-site fallback spam
    junk_bing = [
        {"title": "DirectX Diagnostic Tool", "url": "https://en.wikipedia.org/wiki/DxDiag",
         "content": "Troubleshooting DirectX", "engine": "bing", "score": 1.0},
        {"title": "Matching Page", "url": "https://sub.wakuwaku.com/docs/guide",
         "content": "Guide details", "engine": "yahoo", "score": 0.5},
        {"title": "Blocked Pinterest", "url": "https://pinterest.com/pin/123",
         "content": "Pin", "engine": "yahoo", "score": 0.4},
    ]
    site_only, has_pos = websearch._filter_site_constraints(
        "guide site:wakuwaku.com -site:pinterest.com", junk_bing)
    check("site: filter keeps only requested domain and honors -site:",
          has_pos and [r["url"] for r in site_only] == ["https://sub.wakuwaku.com/docs/guide"],
          str(site_only))

    # Zero-overlap spam rejection + compound / word-boundary ranking
    mixed = [
        {"title": "Regular Puffer Jacket Men | GUESS", "url": "https://guess.com/puffer",
         "content": "Shop winter jackets", "engine": "bing", "score": 1.0},
        {"title": "Llama App", "url": "https://llama.app/",
         "content": "Chat on your computer", "engine": "bing", "score": 0.9},
        {"title": "llama.cpp --n-cpu-moe guide", "url": "https://aliteq.com/n-cpu-moe",
         "content": "Run Vulkan MoE models with llama.cpp and --n-cpu-moe",
         "engine": "yahoo", "score": 0.5},
    ]
    ranked = websearch._rank_and_filter_results("llama.cpp vulkan n-cpu-moe", mixed)
    check("0-match and fake partial compound rejected; exact guide ranks first",
          len(ranked) == 1 and ranked[0]["url"] == "https://aliteq.com/n-cpu-moe",
          str([r["url"] for r in ranked]))

    # Short token word boundary avoids matching "ai" inside "wait" or "go" inside "google"
    short_hits = websearch._rank_and_filter_results("go generics tutorial", [
        {"title": "Google Home", "url": "https://google.com", "content": "Search everything"},
        {"title": "Go Generics Tutorial", "url": "https://go.dev/doc/tutorial/generics",
         "content": "Introduction to generics in Go"},
    ])
    check("short token boundary rejects 'Google' for 'go'",
          [r["url"] for r in short_hits] == ["https://go.dev/doc/tutorial/generics"],
          str(short_hits))

    # configure(engines="") clears previously pinned engines and flushes cache
    orig_host = websearch._SEARXNG_HOST
    orig_eng = websearch._SEARXNG_ENGINES
    orig_lang = websearch._SEARXNG_LANGUAGE
    orig_en = websearch._SEARXNG_ENABLED
    orig_client = websearch._get_client_async
    try:
        websearch.configure(host="http://localhost:8888", enabled=True,
                            engines="bing,duckduckgo", language="all")
        check("configure accepts custom engines", websearch._SEARXNG_ENGINES == "bing,duckduckgo")
        websearch._SEARCH_CACHE["stale"] = (time.time(), "cached")
        websearch.configure(engines="", language="en")
        check("configure(engines='') clears engines and flushes cache",
              websearch._SEARXNG_ENGINES == "" and websearch._SEARXNG_LANGUAGE == "en"
              and not websearch._SEARCH_CACHE)

        # Rescue pass triggers once when default engines report CAPTCHA/429
        seen_params = []

        class _FakeResp:
            def __init__(self, payload):
                self._payload = payload
                self.status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return self._payload

        class _FakeClient:
            async def get(self, url, params=None, **kw):
                seen_params.append(dict(params or {}))
                if len(seen_params) == 1:
                    return _FakeResp({"results": [], "unresponsive_engines": [["duckduckgo", "CAPTCHA"]]})
                return _FakeResp({
                    "results": [
                        {"title": "Widgets Docs", "url": "https://example.org/widgets",
                         "content": "Widgets reference documentation", "engine": "yahoo", "score": 1.0}
                    ],
                    "unresponsive_engines": [],
                })

        async def _fake_client():
            return _FakeClient()

        websearch._get_client_async = _fake_client
        rescued = asyncio.run(websearch.web_search("widgets site:example.org", 3))
        check("rescue pass recovers results using yahoo,duckduckgo web",
              "https://example.org/widgets" in rescued and len(seen_params) == 2
              and seen_params[1].get("engines") == websearch._RESCUE_ENGINES,
              f"calls={seen_params} out={rescued[:120]}")

        # Off-site spam on strict site: returns No results (no Wikipedia substitution)
        seen_params.clear()

        class _OffsiteClient:
            async def get(self, url, params=None, **kw):
                return _FakeResp({
                    "results": [
                        {"title": "Random unrelated page", "url": "https://other.invalid/dxdiag",
                         "content": "DirectX diagnostic", "engine": "bing", "score": 1.0}
                    ],
                    "unresponsive_engines": [],
                })

        async def _offsite_client():
            return _OffsiteClient()

        websearch._get_client_async = _offsite_client
        websearch._SEARCH_CACHE.clear()
        offsite_out = asyncio.run(websearch.web_search("widgets site:example.org", 3))
        check("strict site: rejects off-site spam and never substitutes Wikipedia",
              offsite_out.startswith("[web_search: No results")
              and "wikipedia.org" not in offsite_out,
              offsite_out[:120])
    finally:
        websearch._get_client_async = orig_client
        websearch.configure(host=orig_host, enabled=orig_en,
                            engines=orig_eng, language=orig_lang)

    # Legacy settings.json migration clears "bing,duckduckgo" + "all" -> "" + "en"
    with tempfile.TemporaryDirectory(prefix="hm_ws_mig_") as tmp:
        sf = Path(tmp) / "settings.json"
        sf.write_text(json.dumps({
            "searxng_engines": "bing,duckduckgo",
            "searxng_language": "all",
        }), encoding="utf-8")
        orig_sf = settings_mod.SETTINGS_FILE
        try:
            settings_mod.SETTINGS_FILE = sf
            loaded = settings_mod.load_settings()
        finally:
            settings_mod.SETTINGS_FILE = orig_sf
        check("settings migration clears legacy bing,duckduckgo + all",
              loaded.get("searxng_engines") == "" and loaded.get("searxng_language") == "en",
              f"engines={loaded.get('searxng_engines')!r} lang={loaded.get('searxng_language')!r}")

    # UI badge counts numbered [N] results rather than line count
    app_src = read_all_ui_js()
    check("web_search UI pill counts [N] results instead of line count",
          "_wsHits = (_trFull.match(/^\\[\\d+\\]\\s/gm)" in app_src)


_test_quality_filters()

sys.exit(h.finish("websearch_quality"))

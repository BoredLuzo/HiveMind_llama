# -*- coding: utf-8 -*-
"""Websearch budget stop + engine-set tests (2026-09-17).

Live finding: a 9b coder spent 28 rounds retrying web_search after the
per-run budget was gone (every call -> WEBSEARCH_BUDGET_EXHAUSTED) before
the generic wedge detector finally stopped the run. Under test:

  - 2 consecutive budget-exhausted web_search results stop the run
    (result.loop_detected) via the run-persistent ws_budget_streak ref;
  - any other tool result resets the streak (model acted on feedback);
  - the streak survives ToolRoundState re-creation (list-ref passed in);
  - SearXNG default engines exclude dead engines (google = CAPTCHA-
    suspended on stock instances, wikipedia = 0 results under language
    "all") and a no-results answer carries a reformulation hint.

Run: python tests/test_websearch_budget_stop.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

passed = 0
failed = 0


def check(name, cond, msg=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {msg}")


def _seed_budget_exhausted():
    from tools import runner as _tr
    from tools import definitions as _defs
    _defs.init_websearch(True)   # server startup normally sets this
    _tr._web_search_count.set([10**9])


def _seed_budget_fresh():
    from tools import runner as _tr
    _tr._web_search_count.set([0])


# ── 1. Executor: 2 consecutive budget errors stop the run ──────────────────
def test_executor_budget_stop():
    try:
        from core.tool_executor import execute_tool_round, ToolExecHooks
        from core.tool_exec_helpers import ToolRoundState
        from core.agentic_duo_state import DuoRoundState
        from hive_functions.memory import ToolContextLRU
    except ImportError as e:
        print(f"  SKIP  executor-integration ({type(e).__name__}: {e})")
        return

    ws = Path(tempfile.mkdtemp(prefix="hvm_ws_budget_"))
    (ws / "note.txt").write_text("hello\n", encoding="utf-8")

    async def _noop_emit(ev):
        return ""

    hooks = ToolExecHooks(emit=_noop_emit, is_aborted=lambda cid: False, on_tool_result=None)
    streak_ref = [0]  # duo_runner passes the SAME ref into every round's trs

    async def one_round(name, args, streak=streak_ref):
        trs = ToolRoundState(
            tool_ctx_lru=ToolContextLRU(default_ttl=3),
            duo_deadline_at=time.time() + 600,
            ws_budget_streak=streak,
        )
        return await execute_tool_round(
            tool_calls=[{"id": "c1", "function": {"name": name, "arguments": args}}],
            dtool_msgs=[],
            round_state=DuoRoundState(),
            hooks=hooks,
            trs=trs,
            tool_mode="duo_full",
            duo_ws=True,
            workspace_lock=str(ws),
            exec_model="test-model",
            exec_has_thinking=False,
            tool_think_auto_mode="",
            run_id_global="r1",
            chat_id="chat1",
            subtask_index=0,
        )

    import json as _j
    ws_args = _j.dumps({"query": "best static site generator 2024"})

    _seed_budget_exhausted()
    r1 = asyncio.run(one_round("web_search", ws_args))
    check("stop: 1. Budget-Fail -> kein Abbruch", not r1.loop_detected)
    check("stop: Streak-Ref steht auf 1", streak_ref[0] == 1, f"streak={streak_ref[0]}")

    r2 = asyncio.run(one_round("web_search", ws_args))
    check("stop: 2. Budget-Fail -> loop_detected", r2.loop_detected)
    check("stop: Streak-Ref steht auf 2", streak_ref[0] == 2, f"streak={streak_ref[0]}")

    # Reset: ein anderer Tool-Erfolg dazwischen gibt dem Modell wieder Luft
    _seed_budget_exhausted()
    fresh_ref = [0]
    asyncio.run(one_round("web_search", ws_args, streak=fresh_ref))
    read_args = _j.dumps({"path": "note.txt"})
    asyncio.run(one_round("read_file", read_args, streak=fresh_ref))
    check("reset: anderer Tool-Erfolg setzt Streak auf 0", fresh_ref[0] == 0,
          f"streak={fresh_ref[0]}")
    r4 = asyncio.run(one_round("web_search", ws_args, streak=fresh_ref))
    check("reset: danach wieder nur 1 Fail -> kein Abbruch",
          not r4.loop_detected and fresh_ref[0] == 1, f"ld={r4.loop_detected} streak={fresh_ref[0]}")


# ── 2. No-results answer carries a reformulation hint ──────────────────────
def test_no_results_hint():
    import tools.websearch as wsm

    class _FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"results": []}

    class _FakeClient:
        async def get(self, *a, **k):
            return _FakeResp()

    orig_enabled, orig_client, orig_cache = (
        wsm._SEARXNG_ENABLED, wsm._get_client_async, dict(wsm._SEARCH_CACHE))
    wsm._SEARXNG_ENABLED = True
    wsm._SEARCH_CACHE.clear()

    async def _fake_client():
        return _FakeClient()

    wsm._get_client_async = _fake_client
    try:
        out = asyncio.run(wsm.web_search("hivemind_llama github boredluzo"))
    finally:
        wsm._SEARXNG_ENABLED = orig_enabled
        wsm._get_client_async = orig_client
        wsm._SEARCH_CACHE.clear()
        wsm._SEARCH_CACHE.update(orig_cache)

    check("no-results: erkennbar als leeres Ergebnis", out.startswith("[web_search: No results"),
          out[:80])
    check("no-results: Reformulierungs-Hint vorhanden", "Do NOT retry" in out, out[:200])


# ── 3. Default engine set: reliable engines, safe params, domain cap ───────
def test_default_engines():
    from settings import DEFAULT_SETTINGS
    import tools.websearch as wsm

    for label, engines in (("settings.default", str(DEFAULT_SETTINGS.get("searxng_engines", ""))),
                           ("websearch.fallback", wsm._SEARXNG_ENGINES)):
        parts = [e.strip().lower() for e in engines.split(",") if e.strip()]
        check(f"engines {label}: bing+duckduckgo aktiv",
              {"bing", "duckduckgo"} <= set(parts), str(parts))
        check(f"engines {label}: google entfernt (CAPTCHA-tot)", "google" not in parts, str(parts))
        check(f"engines {label}: wikipedia entfernt (0 Treffer unter fast jedem language-Tag)",
              "wikipedia" not in parts, str(parts))
        check(f"engines {label}: github entfernt (flutet Ranking mit ~30 Repos)",
              "github" not in parts, str(parts))

    # Language tags ("en", "en-US", ...) break every engine on stock instances
    # (region-fallback garbage, live-tested 2026-09-18).
    for label, lang in (("settings.default", str(DEFAULT_SETTINGS.get("searxng_language", ""))),
                        ("websearch.fallback", wsm._SEARXNG_LANGUAGE)):
        check(f"language {label}: all (kein Region-Tag -> keine Fallback-Muell-Results)",
              lang == "all", lang)

    params = wsm._build_search_params("test query")
    check("params: safesearch=1 gesetzt", str(params.get("safesearch")) == "1", str(params))
    check("params: format=json", params.get("format") == "json", str(params))

    # NO-CONTACT-URL: the 403-retry UA goes to third-party sites and must not
    # carry the operator's identity (repo URL, username, ...).
    for ua in wsm._FETCH_UA_FALLBACK:
        check(f"ua no-dox: '{ua[:40]}' ohne URL/Handle",
              "http" not in ua and "BoredLuzo" not in ua, ua)


# ── 4. Domain cap: one hostname must not fill the result list ──────────────
def test_cap_per_host():
    import tools.websearch as wsm

    results = [
        {"url": "https://mail.google.com/a"}, {"url": "https://mail.google.com/b"},
        {"url": "https://mail.google.com/c"}, {"url": "https://qwen.ai/home"},
        {"url": "https://github.com/QwenLM/Qwen3"}, {"url": "https://bad-url %%"},
    ]
    capped = wsm._cap_per_host(results, cap=2)
    urls = [r["url"] for r in capped]
    check("cap: google.com auf 2 begrenzt", sum("mail.google.com" in u for u in urls) == 2, str(urls))
    check("cap: andere Domains unangetastet",
          "https://qwen.ai/home" in urls and "https://github.com/QwenLM/Qwen3" in urls, str(urls))
    check("cap: kaputte URL faellt durch (kein Crash)", len(capped) == 5, str(len(capped)))
    check("cap: leer Input -> leer Output", wsm._cap_per_host([]) == [])


if __name__ == "__main__":
    test_executor_budget_stop()
    test_no_results_hint()
    test_default_engines()
    test_cap_per_host()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)

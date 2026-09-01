"""Thinking-Budget-Guard des Planners (2026-08-31).

Live-Befund: hermes3.6/MTP setzt thinking_budget nicht zuverlaessig durch -
der Thinking-Planner lief 600s in einen Thinking-Loop ohne Plan-Content
(Planner-LLM wall-timeout), dann "leerer Output" + NT-Retry. Der Guard in
hive_functions/planner.py _llm_stream schaetzt die gestreamten Thinking-Tokens
(chars/3) und bricht frueh ab, wenn das Budget (mit Headroom) ueberschritten
is present and no content has started yet -> run_planner falls back to NT.

Run: python tests/test_planner_thinking_cap.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hive_functions import planner as _planner_mod

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


# ── Fake httpx stream for _llm_stream ───────────────────────────────────────
class _FakeResp:
    def __init__(self, lines):
        self.status_code = 200
        self._lines = lines

    def aiter_lines(self):
        async def _gen():
            for _l in self._lines:
                yield _l

        return _gen()


class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, lines):
        self._stream = _FakeStream(_FakeResp(lines))

    def stream(self, *a, **k):
        return self._stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _sse(data) -> str:
    return "data: " + json.dumps(data)


def _delta_line(tok, content=None) -> str:
    _d = {"choices": [{"delta": {}}]}
    if tok is not None:
        _d["choices"][0]["delta"]["reasoning_content"] = tok
    if content is not None:
        _d["choices"][0]["delta"]["content"] = content
    return _sse(_d)


def _run_stream(lines, **kw):
    _orig_client = _planner_mod.httpx.AsyncClient
    _planner_mod.httpx.AsyncClient = lambda *a, **k: _FakeClient(lines)
    try:
        return asyncio.run(
            _planner_mod._llm_stream("m", 8101, [{"role": "user", "content": "task"}], **kw)
        )
    finally:
        _planner_mod.httpx.AsyncClient = _orig_client


# ── T1: thinking loop over budget -> guard breaks early (hit_timeout) ──────
def test_thinking_budget_cap_breaks_early():
    # budget=100 -> cap = 100 * 1.4 = 140 est tokens -> 420 chars.
    # 60 deltas of 12 chars = 720 chars (est 240) - without the guard the
    # whole stream would be consumed; with the guard it breaks at ~35 deltas.
    _lines = [_delta_line("t%012d" % i) for i in range(60)]
    t, c, to = _run_stream(
        _lines, use_thinking=True, thinking_budget=100, wall_timeout=600.0,
    )
    if to and c == "" and t:
        # est = chars//3; guard greift bei chars > 420. 35*12=420 -> est 140.
        ok("thinking_budget_cap_breaks_early (hit_timeout=True, kein Content)")
    else:
        fail("thinking_budget_cap_breaks_early",
             f"timeout={to} content_len={len(c)} thinking_len={len(t)}")


def test_thinking_budget_cap_respects_headroom():
    # 20 Deltas à 12 Zeichen = 240 chars -> est 80 < cap 140 -> kein Abbruch.
    _lines = [_delta_line("t%012d" % i) for i in range(20)]
    t, c, to = _run_stream(
        _lines, use_thinking=True, thinking_budget=100, wall_timeout=600.0,
    )
    if not to and t:
        ok("thinking_budget_cap_respects_headroom (unter Budget kein Abbruch)")
    else:
        fail("thinking_budget_cap_respects_headroom",
             f"timeout={to} thinking_len={len(t)}")


# ── T2: Content begonnen -> Guard darf nicht mehr greifen ──────────────────
def test_thinking_cap_ignored_after_content_started():
    # first thinking, then content, then much more thinking: once content
    # is there, it is no longer aborted (the plan may run until EOF).
    _lines = [_delta_line("t%06d" % i) for i in range(30)]
    _lines.append(_delta_line(None, "Plan-Content "))
    _lines.append(_delta_line(None, "weiterer Plan "))
    _lines.extend([_delta_line("tx" * 6) for _ in range(200)])
    t, c, to = _run_stream(
        _lines, use_thinking=True, thinking_budget=100, wall_timeout=600.0,
    )
    if not to and "Plan-Content" in c:
        ok("thinking_cap_ignored_after_content_started (content protects from abort)")
    else:
        fail("thinking_cap_ignored_after_content_started",
             f"timeout={to} content={c!r}")


# ── T3: NT-Modus (use_thinking=False) mit Thinking-Loop -> NT-Cap ──────────
def test_nt_mode_thinking_loop_capped():
    # NT-Cap = 2500 est tokens -> 7500 chars. 700 Deltas à 12 Zeichen.
    _lines = [_delta_line("n%011d" % i) for i in range(700)]
    t, c, to = _run_stream(
        _lines, use_thinking=False, thinking_budget=0, wall_timeout=600.0,
    )
    if to and c == "":
        ok("nt_mode_thinking_loop_capped (NT-Modus bricht Thinking-Loop ab)")
    else:
        fail("nt_mode_thinking_loop_capped",
             f"timeout={to} content_len={len(c)}")


# ── Source-Guards ───────────────────────────────────────────────────────────
def test_source_guard_thinking_cap():
    src = (Path(__file__).parent.parent / "hive_functions" / "planner.py").read_text(encoding="utf-8")
    _consts = "_THINK_EST_HEADROOM" in src and "_NT_THINK_CAP_EST" in src
    _guard = "Thinking-Budget-Guard" in src
    _chars = "_think_chars" in src
    if _consts and _guard and _chars:
        ok("source_guard_thinking_cap (Konstanten + Guard + _think_chars)")
    else:
        fail("source_guard_thinking_cap",
             f"consts={_consts} guard={_guard} chars={_chars}")


if __name__ == "__main__":
    test_thinking_budget_cap_breaks_early()
    test_thinking_budget_cap_respects_headroom()
    test_thinking_cap_ignored_after_content_started()
    test_nt_mode_thinking_loop_capped()
    test_source_guard_thinking_cap()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)

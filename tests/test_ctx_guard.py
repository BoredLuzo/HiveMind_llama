# -*- coding: utf-8 -*-
"""Tests: cache-freundlicher Kontext-Guard (context/ctx_guard.py) + rule-based
Kompression (context/compression._compress_rule_based).

Run: python tests/test_ctx_guard.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from context.ctx_guard import (
    resolve_compress_threshold as _thr,
    decide_context_action as _decide,
    should_use_partial as _should_partial,
    plan_partial_cut_index as _plan_cut,
)
from context.compression import _compress_rule_based

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


# ── Threshold: auto = min(P1 = floor*ctx, P2 = ctx - reserve) ──────────────
check("T1: 32k ctx -> P1 (0.70*ctx) dominant",
      _thr(ctx_tokens=32768, num_predict=2048) == int(0.70 * 32768),
      f" got={_thr(ctx_tokens=32768, num_predict=2048)}")
check("T2: 128k ctx -> P1 dominant",
      _thr(ctx_tokens=131072, num_predict=2048) == int(0.70 * 131072),
      f" got={_thr(ctx_tokens=131072, num_predict=2048)}")
# 4k ctx, num_predict 800 -> reserve 1824, P2 = 2176 < P1 2949 -> P2 wins
check("T3: small ctx keeps absolute reserve",
      _thr(ctx_tokens=4096, num_predict=800) == 4096 - (800 + 1024),
      f" got={_thr(ctx_tokens=4096, num_predict=800)}")
check("T4: ui override wins exactly",
      _thr(ctx_tokens=32768, num_predict=2048, ui_threshold=20000) == 20000)
check("T5: min_free_tokens binds when large",
      _thr(ctx_tokens=4096, num_predict=0, min_free_tokens=3000) == 4096 - 3000,
      f" got={_thr(ctx_tokens=4096, num_predict=0, min_free_tokens=3000)}")
check("T6: zero ctx -> 0", _thr(ctx_tokens=0) == 0)
check("T7: custom floor honoured",
      _thr(ctx_tokens=10000, num_predict=0, auto_floor=0.8) == int(0.8 * 10000),
      f" got={_thr(ctx_tokens=10000, num_predict=0, auto_floor=0.8)}")

# ── decide_context_action ───────────────────────────────────────────────────
d = _decide(guard_tokens=24000, ctx_tokens=32768, threshold=int(0.70 * 32768),
            can_compress=True)
check("D1: over threshold -> compress",
      d.action == "compress" and d.reason == "threshold", f" got={d.action}/{d.reason}")

d2 = _decide(guard_tokens=32000, ctx_tokens=32768, threshold=int(0.70 * 32768),
             can_compress=True)
check("D2: >90% -> compress (emergency-90)",
      d2.action == "compress" and d2.reason == "emergency-90", f" got={d2.action}/{d2.reason}")

d3 = _decide(guard_tokens=31000, ctx_tokens=32768, threshold=int(0.70 * 32768),
             can_compress=False)
check("D3: >90% + no compress -> emergency_evict",
      d3.action == "emergency_evict", f" got={d3.action}")

d4 = _decide(guard_tokens=20000, ctx_tokens=32768, threshold=int(0.70 * 32768),
             can_compress=False)
check("D4: below threshold + no compress -> none (append-only)",
      d4.action == "none", f" got={d4.action}")

d5 = _decide(guard_tokens=1000, ctx_tokens=32768, threshold=int(0.70 * 32768),
             can_compress=True, force_compress=True)
check("D5: force -> compress", d5.action == "compress" and d5.reason == "force",
      f" got={d5.action}/{d5.reason}")

d6 = _decide(guard_tokens=1000, ctx_tokens=32768, threshold=int(0.70 * 32768),
             can_compress=True, swa_warn=True)
check("D6: swa warn -> compress", d6.action == "compress" and d6.reason == "swa",
      f" got={d6.action}/{d6.reason}")

# ── partial helpers ─────────────────────────────────────────────────────────
check("P1: partial disabled -> False",
      _should_partial(partial_enabled=False, messages=[{"role": "x"}]) is False)
check("P2: partial needs enough history",
      _should_partial(partial_enabled=True, messages=[{"role": "x"}] * 3) is False,
      " got=" + str(_should_partial(partial_enabled=True, messages=[{"role": "x"}] * 3)))
check("P3: partial ok with big history",
      _should_partial(partial_enabled=True, messages=[{"role": "x"}] * 20) is True)

_short = [{"role": "system", "content": "s"}] * 6
check("P4: no valid cut on tiny list",
      _plan_cut(_short, ctx_tokens=10000, guard_tokens=9000) == -1,
      f" got={_plan_cut(_short, ctx_tokens=10000, guard_tokens=9000)}")

_big = [{"role": m, "content": "z" * 5000} for m in
        (["system"] + ["user"] + ["assistant", "tool"] * 7)]
# 16 msgs a 5000 Zeichen (1666 tok), min_tail 4 -> max cut 12.
cut = _plan_cut(_big, ctx_tokens=20000, guard_tokens=16000, min_tail_msgs=4)
check("P5: large old part -> cut index >=2 and tail within bounds",
      cut is not None and cut >= 2 and (16 - cut) >= 4, f" got={cut}")

# ── rule-based Kompression (deterministischer Fallback) ─────────────────────
def _tool_msg(i, content):
    return {"role": "tool", "content": content, "name": "read_file",
            "tool_call_id": f"c{i}"}

_rule_msgs = [
    {"role": "system", "content": "SYS"},
    {"role": "assistant", "content": "call 0"},
    _tool_msg(0, "[C:\\src\\a.py total lines: 5]\n" + "y" * 4000),
    {"role": "assistant", "content": "call 1"},
    _tool_msg(1, "[C:\\src\\b.py total lines: 5]\n" + "z" * 4000),
]
_before = sum(len(str(m.get("content") or "")) for m in _rule_msgs) // 3
_rule_out, _evicted_paths = _compress_rule_based(_rule_msgs, target_tokens=5,
                                                 keep_recent_msgs=2)
_after = sum(len(str(m.get("content") or "")) for m in _rule_out) // 3
check("R1: rule-based returns (list, paths) tuple", isinstance(_rule_out, list) and isinstance(_evicted_paths, list))
check("R2: old tool output replaced by marker (not in recent tail)",
      str(_rule_out[2].get("content")).startswith("[System: Content of"),
      f" got={str(_rule_out[2].get('content'))[:40]!r}")
check("R3: recent tail tool output untouched",
      str(_rule_out[4].get("content")).startswith("[C:\\src\\b.py"),
      f" got={str(_rule_out[4].get('content'))[:40]!r}")
check("R4: evicted paths collected (read-guard)",
      any("a.py" in p for p in _evicted_paths), f" got={_evicted_paths}")
check("R5: context shrunk", _after < _before, f" {_before} -> {_after}")
_rule_out2, _ = _compress_rule_based([], target_tokens=5)
check("R6: empty input -> empty", _rule_out2 == [])

# ── partial-Modus in _compress_tool_context (Tail bleibt byte-identisch) ───
import asyncio  # noqa: E402
from context.compression import _compress_tool_context  # noqa: E402


class _FailClient:
    """Client, der immer wirft -> LLM-Fallback-Zweig wird getestet."""
    async def post(self, *a, **k):
        raise RuntimeError("no model for test")


def _run_partial():
    _sys0 = {"role": "system", "content": "SYS"}
    _task = {"role": "user", "content": "TASK make it work"}
    _ass1 = {"role": "assistant", "content": "read call",
             "tool_calls": [{"id": "c0", "function": {"name": "read_file", "arguments": "{}"}}]}
    _tool1 = {"role": "tool", "name": "read_file", "tool_call_id": "c0",
              "content": "[C:\\x\\a.py total lines: 5]\n" + "x" * 3000}
    _tail_user = {"role": "user", "content": "[RUNTIME NOTICE] [CTX: ~85% full]"}
    _ass2 = {"role": "assistant", "content": "bash call",
             "tool_calls": [{"id": "c1", "function": {"name": "run_bash", "arguments": "{}"}}]}
    _tool2 = {"role": "tool", "name": "run_bash", "tool_call_id": "c1", "content": "echo hi"}
    msgs = [_sys0, _task, _ass1, _tool1, _tail_user, _ass2, _tool2]

    out = asyncio.run(_compress_tool_context(
        messages=msgs, model="m", port=1, client=_FailClient(),
        system_prompt="SYSTEM", original_task="TASK make it work",
        written_files=[], done_tasks=[], keep_recent_msgs=0,
        compression_mode="partial", cut_index=4,
    ))
    tail_raw = msgs[4:]
    check("M1: partial returns 3-tuple", isinstance(out, tuple) and len(out) == 3)
    result = out[0]
    check("M2: result = system + summary + raw tail",
          len(result) == 2 + len(tail_raw), f" len={len(result)} vs {2 + len(tail_raw)}")
    ok_tail = all(
        result[2 + i].get("content") == tail_raw[i].get("content")
        and result[2 + i].get("role") == tail_raw[i].get("role")
        for i in range(len(tail_raw))
    )
    check("M3: tail ab cut byte-identisch erhalten", ok_tail,
          f" result_tail={[m.get('content', '')[:20] for m in result[2:]]}")
    return out


_run_partial()

print()
print("=" * 50)
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)

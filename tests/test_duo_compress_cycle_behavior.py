# -*- coding: utf-8 -*-
"""Behavior tests for core/duo_compress_cycle.py (B3/B8 maintainability).

Covers:
  1. _walk_workspace_inventory: ignore dirs, forward-slash sorting, cap.
  2. _duo_compress_cycle happy path (local-only compression): shrinks history,
     resets round/loop counters, clears stale signatures, syncs state back.
  3. _duo_compress_cycle stop path 1 (LLM + rule-based fallback 3x failure):
     restores uncompressed messages, sets ps.stop=True and ps._loop_detected=True,
     syncs mutated counters via _sync_state().
  4. _duo_compress_cycle stop path 2 (valid summary but <10% shrinkage 3x at
     round > 0): sets ps.stop=True and ps._loop_detected=True, syncs state.
  5. _duo_compress_cycle round-0 incompressible guard: at _total_tool_rounds=0,
     3x non-shrinkage resets _compress_fail_streak=0 and continues (ps.stop=False).

Run: python tests/test_duo_compress_cycle_behavior.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.duo_compress_cycle as _CC  # noqa: E402
from core.duo_compress_cycle import (  # noqa: E402
    _DuoCompressCycleState,
    _duo_compress_cycle,
    _walk_workspace_inventory,
)
from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="summary")
check = h.check


# ── 1. _walk_workspace_inventory ────────────────────────────────────────────
with tempfile.TemporaryDirectory(prefix="hvm_cc_inv_") as _td:
    _root = Path(_td)
    (_root / "src").mkdir()
    (_root / "src" / "main.py").write_text("print(1)", encoding="utf-8")
    (_root / "README.md").write_text("# hi", encoding="utf-8")
    for _ign in ("node_modules", ".git", "dist", "__pycache__", ".hive_uploads"):
        (_root / _ign).mkdir()
        (_root / _ign / "ignored.txt").write_text("x", encoding="utf-8")

    _inv = _walk_workspace_inventory(str(_root), cap=80)
    check("1a inventory includes real files with forward slashes, sorted",
          _inv == ["README.md", "src/main.py"], f"got={_inv}")
    _inv_cap = _walk_workspace_inventory(str(_root), cap=1)
    check("1b inventory honors cap", len(_inv_cap) == 1, f"got={_inv_cap}")


# ── Fake ctx & state factory ────────────────────────────────────────────────
class _FakeLRU:
    def __init__(self) -> None:
        self.reset_calls = 0

    def reset(self) -> None:
        self.reset_calls += 1


class _FakeExecCtrl:
    def __init__(self) -> None:
        self.synced: list[int] = []

    def sync_tool_rounds(self, n: int) -> None:
        self.synced.append(n)


def _make_ctx(*, until_finished: bool = False):
    emitted: list[dict] = []

    async def _emit(ev: dict) -> dict:
        emitted.append(ev)
        return ev

    ctx = SimpleNamespace(
        emit=_emit,
        emitted=emitted,
        user_input="Refactor module A and add tests",
        settings={"duo_pin_static_map": False, "duo_compress_tail_tool_max_chars": 4000},
        duo_config=SimpleNamespace(until_finished=until_finished),
        exec_ctrl=_FakeExecCtrl(),
    )
    return ctx


def _big_msgs() -> list[dict]:
    msgs: list[dict] = [
        {"role": "system", "content": "SYS PROMPT"},
        {"role": "user", "content": "TASK: Refactor module A"},
    ]
    for i in range(8):
        msgs.append({
            "role": "assistant",
            "content": f"reading file {i}",
            "tool_calls": [{"id": f"tc_{i}", "function": {"name": "read_file", "arguments": "{}"}}],
        })
        msgs.append({
            "role": "tool",
            "name": "read_file",
            "tool_call_id": f"tc_{i}",
            "content": f"[C:/ws/file_{i}.py total lines: 100]\n" + ("x = 1\n" * 400),
        })
    return msgs


def _make_state(msgs: list[dict], **overrides) -> _DuoCompressCycleState:
    base = dict(
        _call_sigs=["sig1", "sig2"],
        _coder_est_at_real=[4200],
        _coder_real_prompt_tokens=[4500],
        _comp_llm_cfg="",
        _comp_llm_timeout_s=30,
        _compress_local_only=True,
        _compress_threshold=3000,
        _di=0,
        _done_tasks=["step 1"],
        _dport=8081,
        _dtc=None,
        _dtool_ctx=8192,
        _dtool_sys_eff="SYS PROMPT",
        _explore_ctx="",
        _goal_pin_msg=None,
        _guard_decision=SimpleNamespace(reason="threshold"),
        _guard_tokens=4800.0,
        _last_test_status="PASS",
        _lifetime_tool_rounds=5,
        _max_tool_rounds=25,
        _n_items=2,
        _partial_compression=False,
        _plan_result=None,
        _plan_tracker=None,
        _read_counts={"a.py": 2},
        _seen_explore_paths={"a.py"},
        _subtasks=["step 1", "step 2"],
        _tool_ctx_lru=_FakeLRU(),
        _written_files=["a.py"],
        _ws_str="",
        _MAX_COMPRESSIONS=5,
        _compress_fail_streak=1,
        _ctx_compressions=0,
        _ctx_critical_warned=True,
        _ctx_full_skips=2,
        _ctx_pressure_warned=True,
        _dtool_msgs=list(msgs),
        _est_tokens=5000,
        _explore_only_rounds=2,
        _force_compress_next=True,
        _llm_compress_fails=1,
        _loop_detected=False,
        _no_write_nudged=True,
        _no_write_rounds=3,
        _partial_escalated=False,
        _rule_compress_fails=1,
        _rule_compress_used=False,
        _total_tool_rounds=5,
        carry={"exec_mdl": "qwen3.5:4b"},
    )
    base.update(overrides)
    return _DuoCompressCycleState(**base)


async def _drain(gen) -> list:
    out = []
    async for ev in gen:
        out.append(ev)
    return out


# ── 2. Happy path (local-only compression shrinks and syncs state) ──────────
_ctx_ok = _make_ctx()
_msgs_ok = _big_msgs()
_ps_ok = _make_state(_msgs_ok)
_events_ok = asyncio.run(_drain(_duo_compress_cycle(_ctx_ok, _ps_ok)))

check("2a happy path does not stop the run",
      _ps_ok.stop is False and _ps_ok._loop_detected is False)
check("2b happy path increments _ctx_compressions and resets round counters",
      _ps_ok._ctx_compressions == 1
      and _ps_ok._total_tool_rounds == 0
      and _ps_ok._ctx_full_skips == 0
      and _ps_ok._force_compress_next is False
      and _ps_ok._compress_fail_streak == 0)
check("2c happy path shrinks _est_tokens and resets real-token calibration",
      _ps_ok._est_tokens < 5000
      and _ps_ok._coder_real_prompt_tokens[0] == 0
      and _ps_ok._coder_est_at_real[0] == 0,
      f"est={_ps_ok._est_tokens}")
check("2d happy path clears loop/explore state and resets LRU",
      _ps_ok._call_sigs == []
      and _ps_ok._read_counts == {}
      and len(_ps_ok._seen_explore_paths) == 0
      and _ps_ok._explore_only_rounds == 0
      and _ps_ok._no_write_rounds == 0
      and _ps_ok._no_write_nudged is False
      and _ps_ok._tool_ctx_lru.reset_calls >= 1)
check("2e happy path yields start/end status and ctx_meter events",
      any(e.get("type") == "ctx_meter" and e.get("compressing") is True for e in _events_ok)
      and any(e.get("type") == "ctx_meter" and e.get("compressing") is False for e in _events_ok)
      and any("✅ Context compressed" in str(e.get("content", "")) for e in _events_ok))


# ── 3. Stop path 1: invalid summary + rule-based fallback 3x failure ────────
async def _fake_bad_compress(**kwargs):
    return (
        [{"role": "system", "content": "SYS"},
         {"role": "user", "content": "COMPRESSION FAILED sentinel"}],
        [],
        {},
    )


_ctx_s1 = _make_ctx()
_orig_msgs_s1 = [{"role": "system", "content": "SYS"},
                 {"role": "user", "content": "x" * 900}]
_ps_s1 = _make_state(
    _orig_msgs_s1,
    _est_tokens=300,
    _rule_compress_fails=2,
    _compress_fail_streak=2,
    _llm_compress_fails=2,
    _force_compress_next=True,
)
with mock.patch.object(_CC, "_compress_tool_context", side_effect=_fake_bad_compress), \
     mock.patch.object(_CC, "_compress_rule_based", return_value=(list(_orig_msgs_s1), [])):
    _events_s1 = asyncio.run(_drain(_duo_compress_cycle(_ctx_s1, _ps_s1)))

check("3a rule-fallback 3x failure sets ps.stop and _loop_detected",
      _ps_s1.stop is True and _ps_s1._loop_detected is True)
check("3b rule-fallback 3x failure syncs mutated counters and restores messages",
      _ps_s1._llm_compress_fails == 3
      and _ps_s1._rule_compress_fails == 3
      and _ps_s1._compress_fail_streak == 3
      and _ps_s1._force_compress_next is False
      and _ps_s1._dtool_msgs == _orig_msgs_s1)
check("3c rule-fallback 3x failure emits the 3x stop status",
      any("repeatedly fails to shrink the context (3x)" in str(e.get("content", ""))
          for e in _events_s1))


# ── 4. Stop path 2: valid summary but <10% shrinkage 3x at round > 0 ────────
async def _fake_noshrink_compress(**kwargs):
    return (list(kwargs["messages"]), [], {})


_ctx_s2 = _make_ctx()
_msgs_s2 = [{"role": "system", "content": "SYS"},
            {"role": "user", "content": "y" * 900}]
_ps_s2 = _make_state(
    _msgs_s2,
    _est_tokens=300,
    _compress_fail_streak=2,
    _ctx_compressions=5,
    _MAX_COMPRESSIONS=5,
    _total_tool_rounds=4,
)
with mock.patch.object(_CC, "_compress_tool_context", side_effect=_fake_noshrink_compress), \
     mock.patch("context.compression._validate_compression_summary", return_value=True):
    _events_s2 = asyncio.run(_drain(_duo_compress_cycle(_ctx_s2, _ps_s2)))

check("4a non-shrinking valid summary 3x at round>0 sets ps.stop and _loop_detected",
      _ps_s2.stop is True and _ps_s2._loop_detected is True)
check("4b non-shrinking stop path syncs _ctx_compressions and _compress_fail_streak",
      _ps_s2._ctx_compressions == 6
      and _ps_s2._compress_fail_streak == 3
      and _ps_s2._MAX_COMPRESSIONS == -1
      and _ps_s2._force_compress_next is False)


# ── 5. Round-0 incompressible guard (_total_tool_rounds == 0) ───────────────
_ctx_r0 = _make_ctx()
_ps_r0 = _make_state(
    _msgs_s2,
    _est_tokens=300,
    _compress_fail_streak=2,
    _ctx_compressions=0,
    _MAX_COMPRESSIONS=5,
    _total_tool_rounds=0,
)
with mock.patch.object(_CC, "_compress_tool_context", side_effect=_fake_noshrink_compress), \
     mock.patch("context.compression._validate_compression_summary", return_value=True):
    _events_r0 = asyncio.run(_drain(_duo_compress_cycle(_ctx_r0, _ps_r0)))

check("5 round-0 incompressible resets fail streak and does NOT stop the run",
      _ps_r0.stop is False
      and _ps_r0._loop_detected is False
      and _ps_r0._compress_fail_streak == 0
      and _ps_r0._force_compress_next is False)

sys.exit(h.finish("duo_compress_cycle_behavior"))

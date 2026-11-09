# -*- coding: utf-8 -*-
"""Behavior tests for the _duo_coder_post_wait carve (F13, 2026-11-08).

The coder POST wait + result sync block was moved VERBATIM out of
run_code_duo into the module-level async generator `_duo_coder_post_wait`
(in-use slot guard, post_with_retry task, heartbeat/_coder_event_q drain
loop, task-death teardown, in-use clear, result unpack). This suite drives
the helper directly with a fake AgenticToolLoop whose post_with_retry
covers every exit the carved block has:

  1. loop_detected result   -> action="break", ld_setter(3022), the queued
                               events pass through raw, in-use slot cleared,
                               NO pause/governor cleanup
  2. force_compress result  -> action="continue"
  3. post task death        -> run_ended=True, status + done events relayed
                               in order, phase_timer skip, graceful-stop /
                               pause / governor cleanup, slot cleared
  4. normal result          -> action="proceed", full unpack into the result
                               object (dport, cached_port, dr_msg with
                               think/fence preprocessing, dr_tcs,
                               http_404_retries reset)
  5. stream-not-ok result   -> the verbatim RuntimeError propagates
  6. heartbeat              -> heartbeat events carry the live prefill meter
                               and the meter polls the PRE-POST port
  7. in-use guard swallow   -> a broken slot must not break the round

Run: python tests/test_duo_coder_post_wait.py
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import backend.llama_server_manager as LSM  # noqa: E402
import core.duo_runner as DR  # noqa: E402

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


# ── fakes ────────────────────────────────────────────────────────────────
class _Rec:
    """Records every call (args, kwargs) — stands in for the module-level
    cleanup helpers the carved task-death path must invoke."""
    def __init__(self):
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append((a, k))


class _PhaseTimer:
    def __init__(self):
        self.skipped = []

    def skip(self, name):
        self.skipped.append(name)


class _Slot:
    """In-use slot: records mark/clear order (the guard's real contract)."""
    def __init__(self):
        self.calls = []

    def mark_in_flight(self):
        self.calls.append("mark")

    def clear_in_flight(self):
        self.calls.append("clear")


class _FakeManager:
    def __init__(self, slot):
        self._slot = slot

    async def _find_loaded(self, model):
        return self._slot


class _FakeLoop:
    """AgenticToolLoop stand-in: post_with_retry returns the scripted
    result dict or raises — every carve exit is driven from here."""
    def __init__(self, behavior):
        self.behavior = behavior  # dict result or Exception instance
        self.calls = []

    async def post_with_retry(self, payload, *, dtool_msgs, ctx, _parts):
        self.calls.append({"payload": payload, "dtool_msgs": dtool_msgs,
                           "ctx": ctx, "_parts": _parts})
        if isinstance(self.behavior, Exception):
            raise self.behavior
        await asyncio.sleep(0)  # let the drain loop tick at least once
        return self.behavior


def _make_ctx(events):
    async def emit(ev):
        events.append(ev)
        return ev
    return NS(
        run_id="run-1", chat_id="chat-1", t_total=1000.0,
        duo_stop_reason="",
        phase_timer=_PhaseTimer(),
        emit=emit,
        done_event=lambda *a, **kw: {"type": "done",
                                     "elapsed": a[0] if a else 0,
                                     "stop_reason": a[1] if len(a) > 1 else "",
                                     **kw},
        collect_done_metrics=lambda: {"metrics": 1},
    )


def _drive(behavior, *, queue_events=(), hb_interval=10**9, chat_id="chat-1",
           prefill_reply=None):
    """Drive the carved helper once; return everything the seam touches."""
    async def _run():
        events = []  # what ctx.emit saw
        slot = _Slot()
        ctx = _make_ctx(events)
        if chat_id is None:
            ctx.chat_id = None
        q = asyncio.Queue(maxsize=20)
        for ev in queue_events:
            q.put_nowait(ev)
        loop = _FakeLoop(behavior)
        st = NS(cached_port=8102, http_404_retries=3)
        ld_codes = []
        rec = NS(graceful=_Rec(), pause_state=_Rec(), chat_pause=_Rec(),
                 governor=_Rec(), prefill_ports=[])

        async def _pf(port):
            rec.prefill_ports.append(port)
            return prefill_reply

        # patch the module seams the carved body resolves as globals
        orig = (DR.clear_graceful_stop, DR._cleanup_pause_state,
                DR._clear_pause_state, DR._cleanup_governor,
                DR._llama_prefill_progress, LSM.manager)
        DR.clear_graceful_stop = rec.graceful
        DR._cleanup_pause_state = rec.pause_state
        DR._clear_pause_state = rec.chat_pause
        DR._cleanup_governor = rec.governor
        DR._llama_prefill_progress = _pf
        LSM.manager = _FakeManager(slot)
        try:
            ps = DR._DuoCoderPostWaitState()
            payload = {"model": "m", "stream": True}
            msgs = [{"role": "user", "content": "hi"}]
            parts = ["p1"]
            yielded = []
            gen = DR._duo_coder_post_wait(
                ctx, ps,
                _duo_loop=loop, _tool_payload=payload,
                _dtool_msgs=msgs, _parts=parts,
                _coder_event_q=q, exec_mdl="m", _dport=8101,
                _hb_interval=hb_interval, _duo_state=st,
                _ld_setter=ld_codes.append,
            )
            async for ev in gen:
                yielded.append(ev)
            return NS(yielded=yielded, emitted=events, ps=ps, ctx=ctx,
                      slot=slot, st=st, ld_codes=ld_codes, loop=loop,
                      rec=rec, payload=payload, msgs=msgs, parts=parts)
        finally:
            (DR.clear_graceful_stop, DR._cleanup_pause_state,
             DR._clear_pause_state, DR._cleanup_governor,
             DR._llama_prefill_progress, LSM.manager) = orig

    return asyncio.run(_run())


def _no_cleanup(r, label):
    check(f"[{label}] no pause/governor/graceful cleanup on this exit",
          not (r.rec.graceful.calls or r.rec.pause_state.calls
               or r.rec.chat_pause.calls or r.rec.governor.calls))
    check(f"[{label}] phase_timer untouched", r.ctx.phase_timer.skipped == [])
    check(f"[{label}] duo_stop_reason unchanged",
          r.ctx.duo_stop_reason == "")


LD_RES = {"loop_detected": True, "dr_stream_ok": True, "dport": 8200,
          "dr_msg": {"role": "assistant", "content": "x"},
          "dr_thinking_parts": ["th"]}

FC_RES = {"force_compress": True, "dr_stream_ok": True, "dport": 8200,
          "dr_msg": {"role": "assistant", "content": "y"},
          "dr_thinking_parts": []}

_OK_TC = [{"id": "t1", "type": "function",
           "function": {"name": "edit_file", "arguments": "{}"}}]
NORMAL_RES = {"dr_stream_ok": True, "dport": 8200,
              "dr_msg": {"role": "assistant",
                         "content": '```json\n{"a": 1}\n```',
                         "tool_calls": _OK_TC},
              "dr_thinking_parts": ["t"]}
THINK_RES = {"dr_stream_ok": True, "dport": 8200,
             "dr_msg": {"role": "assistant",
                        "content": "<think>x</think>ok"},
             "dr_thinking_parts": []}


# ── 1. loop_detected ─────────────────────────────────────────────────────
r = _drive(dict(LD_RES), queue_events=[{"type": "token", "content": "a"}])
check("[ld] action == break (the block's loop_detected exit)",
      r.ps.action == "break")
check("[ld] run_ended stays False", r.ps.run_ended is False)
check("[ld] ld_setter ran with the 3022 loop code", r.ld_codes == [3022])
check("[ld] queue events relayed raw, in order",
      r.yielded == [{"type": "token", "content": "a"}])
check("[ld] raw result dict passes through by identity",
      r.ps.result is r.loop.behavior)
check("[ld] dport synced (old block assigned it before the break)",
      r.ps.dport == 8200)
check("[ld] cached_port synced from the shared state",
      r.ps.cached_port == 8102)
check("[ld] dr_msg synced", r.ps.dr_msg is LD_RES["dr_msg"])
check("[ld] dr_thinking_parts synced", r.ps.dr_thinking_parts == ["th"])
check("[ld] dr_tcs NOT packed on break (old code kept the previous "
      "round's value — the runner only reads it on proceed)",
      r.ps.dr_tcs is None)
check("[ld] in-use slot marked then cleared", r.slot.calls == ["mark", "clear"])
_no_cleanup(r, "ld")


# ── 2. force_compress ────────────────────────────────────────────────────
r = _drive(dict(FC_RES))
check("[fc] action == continue (the block's force_compress exit)",
      r.ps.action == "continue")
check("[fc] run_ended stays False", r.ps.run_ended is False)
check("[fc] no loop code set", r.ld_codes == [])
check("[fc] dport / cached_port synced before the continue",
      r.ps.dport == 8200 and r.ps.cached_port == 8102)
check("[fc] no events emitted by the helper on this exit", r.yielded == [])
check("[fc] in-use slot cleared", r.slot.calls == ["mark", "clear"])
_no_cleanup(r, "fc")


# ── 3. task death ────────────────────────────────────────────────────────
r = _drive(RuntimeError("boom — slot died"),
           queue_events=[{"type": "token", "content": "a"},
                         {"type": "tool_call", "name": "edit_file"}])
check("[death] run_ended=True (the block's bare return)", r.ps.run_ended is True)
check("[death] action stays proceed", r.ps.action == "proceed")
check("[death] no result unpacked (result() raised)", r.ps.result is None)
types = [e.get("type") for e in r.yielded]
check("[death] events relayed in order: queue, status, done",
      types == ["token", "tool_call", "status", "done"], f"({types})")
_status = r.yielded[-2]
check("[death] status names the exception",
      "Coder round failed (RuntimeError)" in str(_status.get("content"))
      and "boom — slot died" in str(_status.get("content")))
_done = r.yielded[-1]
check("[death] done event carries runtime_error + metrics",
      _done.get("stop_reason") == "runtime_error"
      and _done.get("metrics") == 1)
check("[death] ctx.duo_stop_reason set", r.ctx.duo_stop_reason == "runtime_error")
check("[death] phase_timer skipped coder_loop",
      r.ctx.phase_timer.skipped == ["coder_loop"])
check("[death] graceful stop cleared for the run",
      r.rec.graceful.calls == [(("run-1",), {})])
check("[death] run pause state cleaned",
      r.rec.pause_state.calls == [(("run-1",), {})])
check("[death] chat pause state cleaned",
      r.rec.chat_pause.calls == [(("chat-1",), {})])
check("[death] governor cleaned for the chat",
      r.rec.governor.calls == [(("chat-1",), {})])
check("[death] in-use slot cleared on the death path too",
      r.slot.calls == ["mark", "clear"])
# a None chat_id must skip the chat-pause call (old `if ctx.chat_id` guard)
r2 = _drive(RuntimeError("x"), chat_id=None)
check("[death] no chat pause cleanup when chat_id is None",
      r2.rec.chat_pause.calls == [] and r2.ps.run_ended is True)


# ── 4. normal result ─────────────────────────────────────────────────────
r = _drive(dict(NORMAL_RES))
check("[ok] action == proceed", r.ps.action == "proceed")
check("[ok] run_ended stays False", r.ps.run_ended is False)
check("[ok] no loop code set", r.ld_codes == [])
check("[ok] raw result passes through by identity",
      r.ps.result is r.loop.behavior)
check("[ok] dport synced from the result", r.ps.dport == 8200)
check("[ok] cached_port synced from the shared state", r.ps.cached_port == 8102)
check("[ok] http_404_retries reset on the shared state",
      r.st.http_404_retries == 0)
check("[ok] json fence stripped from dr_msg content",
      r.ps.dr_msg["content"] == '{"a": 1}')
check("[ok] dr_tcs unpacked from the message", r.ps.dr_tcs == _OK_TC)
check("[ok] dr_thinking_parts synced", r.ps.dr_thinking_parts == ["t"])
check("[ok] in-use slot cleared", r.slot.calls == ["mark", "clear"])
_no_cleanup(r, "ok")
r = _drive(dict(THINK_RES))
check("[ok] <think> block stripped from dr_msg content",
      r.ps.dr_msg["content"] == "ok")


# ── 5. exact post_with_retry wiring ──────────────────────────────────────
r = _drive(dict(NORMAL_RES))
call = r.loop.calls[0]
check("[wire] post_with_retry got the exact payload object",
      call["payload"] is r.payload)
check("[wire] post_with_retry got the exact dtool_msgs / ctx / _parts",
      call["dtool_msgs"] is r.msgs and call["ctx"] is r.ctx
      and call["_parts"] is r.parts)


# ── 6. stream-not-ok raise ───────────────────────────────────────────────
_exc = None
try:
    _r = _drive({"dr_stream_ok": False, "dport": 8200,
                 "dr_msg": {"role": "assistant", "content": "z"},
                 "dr_thinking_parts": []})
except RuntimeError as e:
    _exc = e
check("[5xx] stream-not-ok raises the verbatim RuntimeError",
      _exc is not None and "stream not received" in str(_exc),
      f"({type(_exc).__name__ if _exc else 'none'})")


# ── 7. heartbeat + prefill meter ─────────────────────────────────────────
r = _drive({"dr_stream_ok": True, "dport": 8200,
            "dr_msg": {"role": "assistant", "content": "ok"},
            "dr_thinking_parts": []},
           hb_interval=0, prefill_reply={"percent": 42},
           queue_events=[{"type": "token", "content": "a"}])
_hbs = [e for e in r.yielded if e.get("type") == "heartbeat"]
check("[hb] heartbeats emitted during the wait", len(_hbs) >= 1,
      f"(n={len(_hbs)})")
check("[hb] heartbeat carries the live prefill meter",
      all(e.get("prefill") == {"percent": 42} for e in _hbs))
check("[hb] first queued event precedes the first heartbeat",
      r.yielded[0].get("type") == "token")
check("[hb] meter polls the PRE-POST port (8101, not the result's 8200)",
      r.rec.prefill_ports and all(p == 8101 for p in r.rec.prefill_ports),
      f"({r.rec.prefill_ports})")


# ── 8. in-use guard swallow (the 2026-10-27 bug class) ───────────────────
class _BrokenSlot:
    """No mark_in_flight: the guard must swallow the AttributeError and
    run the round anyway (guard OFF, never a crash)."""


async def _broken_run():
    orig = LSM.manager
    LSM.manager = _FakeManager(_BrokenSlot())
    try:
        ctx = _make_ctx([])
        ps = DR._DuoCoderPostWaitState()
        gen = DR._duo_coder_post_wait(
            ctx, ps, _duo_loop=_FakeLoop(dict(FC_RES)),
            _tool_payload={}, _dtool_msgs=[], _parts=[],
            _coder_event_q=asyncio.Queue(maxsize=20), exec_mdl="m",
            _dport=8101, _hb_interval=10**9,
            _duo_state=NS(cached_port=8102, http_404_retries=1),
            _ld_setter=lambda code: None,
        )
        async for _ev in gen:
            pass
        return ps
    finally:
        LSM.manager = orig

_ps = asyncio.run(_broken_run())
check("[guard] broken slot does not crash the round (guard stays OFF)",
      _ps.action == "continue" and _ps.run_ended is False)


sys.exit(h.finish("duo_coder_post_wait"))

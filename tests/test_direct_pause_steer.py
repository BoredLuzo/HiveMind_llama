# -*- coding: utf-8 -*-
"""A1/A2 behavior: manual pause + steer boundaries on PURE direct runs.

Roadmap A1 (MAINTENANCE_TODO): `/pause/{run_id}` set the flag that only the
Duo chunk loop consumed — a direct stream ignored it (`/pause-state`
`{"active": false}`, `/resume` 409; live matrix 2026-11-07). The direct
runner now gates at every safe boundary (before a model round, at token
chunks — the upstream generator suspends at its yield, so llama-server
generation backpressures instead of running into the void).

Roadmap A2: the steer queue (`/api/run/{run_id}/steer`) stayed undrained on
a pure direct run — the text only reached the model via the frontend's
after-run queue. Direct runs now drain at the round boundary: queued
before a round it rides into that prompt; queued DURING an answer it gets
a bounded continuation round (assistant answer + [OWNER STEER] user
message) so the model actually sees and answers it.

Drives core.direct_runner.run_direct with a fake pipeline — no model, no
network, no server. Every run_direct event flows through ctx.emit, so the
recorder below sees the whole stream. Scenarios:
  A1  pause requested mid-stream -> suspends at the next token chunk,
      /pause-state (chat file) active, /resume continues, run completed
  A1' pause requested before the first round -> suspends before the POST
  A1" abort during pause -> run ends, stop_reason "aborted"
  A2  steer queued before the round -> "[OWNER STEER]" user message inside
      the model messages (pre-POST boundary), steer card event emitted
  A2' steer queued DURING the answer -> continuation round; call 2 sees
      assistant(answer) + steer message; usage stays ONE summed event
  A2" no steer -> exactly ONE model call (unchanged fast path)

Run: python tests/test_direct_pause_steer.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.direct_runner as DR  # noqa: E402
from context.pause_state import (  # noqa: E402
    init_pause_state, load_pause_state,
)
from infra import run_control as RC  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

_SESSIONS = Path(tempfile.mkdtemp(prefix="hvm_direct_pause_steer_"))
init_pause_state(_SESSIONS)

_MODEL = "fake-direct-model"


async def _async_noop(*a, **k):
    return None


# ── Fakes ─────────────────────────────────────────────────────────────────

class _FakeStream:
    """Scripted chat_stream: one async-gen handler per model call. Records
    every call as (messages snapshot, kwargs) and can inject usage blocks."""

    def __init__(self, handlers, usage_per_call=None):
        self.handlers = handlers
        self.usage_per_call = usage_per_call or {}
        self.calls = []

    def make_ollama(self):
        outer = self

        async def chat_stream(model, msgs, temp, max_tok, **kw):
            idx = len(outer.calls)
            outer.calls.append(([dict(m) for m in msgs], dict(kw)))
            u = outer.usage_per_call.get(idx)
            if u and kw.get("usage_out") is not None:
                kw["usage_out"].update(u)
            async for tok in outer.handlers[idx](msgs):
                yield tok
        return type("Ollama", (), {"chat_stream": staticmethod(chat_stream)})()

    @property
    def n_calls(self):
        return len(self.calls)


class _FakeMemory:
    def __init__(self):
        self.session = []

    def add_to_session(self, role, content):
        self.session.append((role, content))


class _Ctx:
    """Duck-typed RunContext with exactly what run_direct touches on the
    pure (tools off) path."""

    def __init__(self, rid, chat_id, stream, emit):
        self.complexity = "simple"
        self.complexity_source = "manual"
        self.effective_task_type = "general"
        self.active_preset = "default"
        self.use_learned = False
        self.pipeline_mem_ctx = ""
        self.pipeline_sess_msgs = []
        self.models_cache = [_MODEL]
        self.vision_cfg = {}
        self.prepro_success = False
        self.image_description = ""
        self.images = []
        self.effective_images = []
        self.registry_get = lambda role: _MODEL
        self.model_profile = lambda m: {}
        self.settings = {"direct_model": _MODEL, "direct_tools_enabled": False,
                         "direct_tools_tier": "off"}
        self.websearch_available = False
        self.pipeline_soul = {}
        self.user_input = "test question"
        self.workspace = ""
        self.run_id = rid
        self.chat_id = chat_id
        self.t_total = time.time()
        self.pipeline = type("P", (), {
            "agents": {"direct": type("A", (), {
                "model": _MODEL, "temperature": 0.4, "max_tokens": 300,
                "thinking": False, "thinking_budget": 0,
            })()},
            "ollama": stream.make_ollama(),
        })()
        self.get_num_ctx = lambda *a, **k: 8192
        self.aborted = lambda: False
        self.unregister_abort = lambda rid_: None
        self.unregister_step_skip = lambda rid_: None
        self.increment_run_counter = lambda: 0
        self.collect_done_metrics = lambda: {}
        self.done_event = lambda elapsed, stop_reason, **kw: {
            "type": "done", "elapsed": elapsed, "stop_reason": stop_reason,
            **kw}
        self.memory = _FakeMemory()

        async def _preload(model):
            return None
        self.maybe_preload = _preload

        async def _soul_evolve(n):
            return None
        self.maybe_trigger_soul_evolution = _soul_evolve

        self.get_effective_prompt_with_override = (
            lambda *a, **k: "SYS PROMPT")

        def _make_messages(pipeline, sys_p, inp, imgs, ta, tb,
                           cached_mem_ctx="", cached_sess_msgs=None):
            return [{"role": "system", "content": sys_p},
                    {"role": "user", "content": inp}]
        self.make_messages = _make_messages
        self.emit = emit


def _default_patches():
    return (
        mock.patch.object(DR, "_pick_direct_model", lambda *a, **k: _MODEL),
        mock.patch.object(DR, "_refresh_judge_keepalive",
                          lambda: _async_noop()),
        mock.patch.object(DR, "run_peer_ratings", lambda **kw: _async_noop()),
        mock.patch("context.chat._mutate_chat_context",
                   lambda cid, fn: None),
    )


def _recorder(events):
    """ctx.emit double: records every event (run_direct yields the emit
    results, so this sees the complete stream)."""
    async def _emit(ev):
        events.append(ev)
        return ev
    return _emit


async def _drive_run(ctx):
    async for _ in DR.run_direct(ctx):
        pass


async def _wait_type(events, etype):
    while not any(e.get("type") == etype for e in events):
        await asyncio.sleep(0.01)


def _cleanup_run(rid):
    RC._unregister_abort(rid)
    RC._unregister_step_skip(rid)
    RC.cleanup_pause_state(rid)
    RC._steer_queue.pop(rid, None)


async def main():
    # ── A1: pause mid-stream, /pause-state active, resume continues ──────
    rid, chat = "a1-mid", "chat_a1_mid"
    RC._register_abort(rid)
    hold_reached = asyncio.Event()
    release_hold = asyncio.Event()

    async def _stream_pause(msgs):
        yield ("Hello ", "")
        hold_reached.set()
        await release_hold.wait()
        yield ("world", "")

    stream = _FakeStream([_stream_pause])
    events = []
    ctx = _Ctx(rid, chat, stream, _recorder(events))
    with ExitStack() as stack:
        for p in _default_patches():
            stack.enter_context(p)
        task = asyncio.create_task(_drive_run(ctx))
        await asyncio.wait_for(hold_reached.wait(), 5)
        # the endpoint body after its registry check:
        await RC.request_pause_after_chunk(rid)
        release_hold.set()
        await asyncio.wait_for(_wait_type(events, "run_paused_manual"), 5)
        ps = load_pause_state(chat)
        check("A1 mid-stream pause: /pause-state becomes active with the run id",
              bool(ps) and ps.get("run_id") == rid, f" ({ps})")
        check("A1 mid-stream pause: no resumed divider before /resume",
              not any(e.get("type") == "run_resumed_manual" for e in events))
        ok = RC.signal_resume(rid)
        check("A1 mid-stream pause: /resume signal accepted", ok is True)
        await asyncio.wait_for(task, 10)
    types = [e.get("type") for e in events]
    check("A1 mid-stream pause: paused -> resumed divider order",
          types.index("run_paused_manual") < types.index("run_resumed_manual"),
          f" ({types})")
    toks = [e.get("content") for e in events if e.get("type") == "token"]
    check("A1 mid-stream pause: full answer streams after resume",
          toks == ["Hello ", "world"], f" ({toks})")
    done = next((e for e in events if e.get("type") == "done"), {})
    check("A1 mid-stream pause: run completes on resume",
          done.get("stop_reason") == "completed", f" ({done})")
    check("A1 mid-stream pause: pause-state file cleared on resume",
          load_pause_state(chat) is None, f" ({load_pause_state(chat)})")
    check("A1 mid-stream pause: events carry phase=direct for the UI divider",
          any(e.get("type") == "run_paused_manual" and e.get("phase") == "direct"
              for e in events))
    _cleanup_run(rid)

    # ── A1': pause requested before the first model round ────────────────
    rid, chat = "a1-pre", "chat_a1_pre"
    RC._register_abort(rid)
    await RC.request_pause_after_chunk(rid)  # arrives during prep

    async def _stream_plain(msgs):
        yield ("late answer", "")

    stream = _FakeStream([_stream_plain])
    events = []
    ctx = _Ctx(rid, chat, stream, _recorder(events))
    with ExitStack() as stack:
        for p in _default_patches():
            stack.enter_context(p)
        task = asyncio.create_task(_drive_run(ctx))
        await asyncio.wait_for(_wait_type(events, "run_paused_manual"), 5)
        ps = load_pause_state(chat)
        check("A1 pre-round pause: suspends BEFORE the model POST",
              stream.n_calls == 0 and bool(ps) and ps.get("run_id") == rid,
              f" (calls={stream.n_calls}, ps={ps})")
        RC.signal_resume(rid)
        await asyncio.wait_for(task, 10)
    check("A1 pre-round pause: round runs after resume",
          stream.n_calls == 1
          and any(e.get("type") == "token" and e.get("content") == "late answer"
                  for e in events)
          and any(e.get("type") == "done" and e.get("stop_reason") == "completed"
                  for e in events),
          f" (calls={stream.n_calls})")
    check("A1 pre-round pause: pause-state cleared",
          load_pause_state(chat) is None)
    _cleanup_run(rid)

    # ── A1": abort during the pause ends the run as aborted ──────────────
    rid, chat = "a1-abort", "chat_a1_abort"
    RC._register_abort(rid)
    hold2 = asyncio.Event()
    release2 = asyncio.Event()

    async def _stream_abort(msgs):
        yield ("Hello ", "")
        hold2.set()
        await release2.wait()
        yield ("world", "")

    stream = _FakeStream([_stream_abort])
    events = []
    ctx = _Ctx(rid, chat, stream, _recorder(events))
    with ExitStack() as stack:
        for p in _default_patches():
            stack.enter_context(p)
        task = asyncio.create_task(_drive_run(ctx))
        await asyncio.wait_for(hold2.wait(), 5)
        await RC.request_pause_after_chunk(rid)
        release2.set()
        await asyncio.wait_for(_wait_type(events, "run_paused_manual"), 5)
        # the /abort/{run_id} path while a pause waits:
        RC.signal_abort_during_pause(rid)
        await asyncio.wait_for(task, 10)
    done = next((e for e in events if e.get("type") == "done"), {})
    check("A1 abort during pause: stop_reason=aborted, no resumed divider",
          done.get("stop_reason") == "aborted"
          and not any(e.get("type") == "run_resumed_manual" for e in events),
          f" ({done.get('stop_reason')})")
    check("A1 abort during pause: pause-state cleared at run end",
          load_pause_state(chat) is None)
    check("A1 abort during pause: held-back token is NOT emitted",
          [e.get("content") for e in events if e.get("type") == "token"]
          == ["Hello "],
          f" ({[e.get('content') for e in events if e.get('type') == 'token']})")
    _cleanup_run(rid)

    # ── A2: steer queued before the round rides into the prompt ──────────
    rid, chat = "a2-pre", "chat_a2_pre"
    RC._register_abort(rid)
    check("A2 setup: queue_steer accepts a registered run",
          RC.queue_steer(rid, "Focus on the tuple unpacking") is True)

    async def _stream_steer_pre(msgs):
        yield ("answer ", "")

    stream = _FakeStream([_stream_steer_pre])
    events = []
    ctx = _Ctx(rid, chat, stream, _recorder(events))
    with ExitStack() as stack:
        for p in _default_patches():
            stack.enter_context(p)
        await asyncio.wait_for(_drive_run(ctx), 10)
    msgs_seen, _kw = stream.calls[0]
    steer_msgs = [m for m in msgs_seen
                  if m.get("role") == "user"
                  and "[OWNER STEER" in str(m.get("content", ""))]
    check("A2 pre-round steer: the model sees the steer text",
          len(steer_msgs) == 1 and "Focus on the tuple unpacking"
          in str(steer_msgs[0].get("content", "")),
          f" ({[str(m.get('content'))[:60] for m in steer_msgs]})")
    check("A2 pre-round steer: steer card event emitted",
          any(e.get("type") == "steer"
              and e.get("content") == "Focus on the tuple unpacking"
              for e in events))
    check("A2 pre-round steer: exactly one model call (no spurious rounds)",
          stream.n_calls == 1, f" (calls={stream.n_calls})")
    check("A2 pre-round steer: run completes",
          any(e.get("type") == "done" and e.get("stop_reason") == "completed"
              for e in events))
    _cleanup_run(rid)

    # ── A2': steer queued DURING the answer -> continuation round ────────
    rid, chat = "a2-cont", "chat_a2_cont"
    RC._register_abort(rid)
    round1_done = asyncio.Event()
    release1 = asyncio.Event()

    async def _round1(msgs):
        yield ("Part1 ", "")
        round1_done.set()
        await release1.wait()

    async def _round2(msgs):
        yield ("STEERED", "")

    stream = _FakeStream([_round1, _round2],
                         usage_per_call={0: {"completion_tokens": 5,
                                             "prompt_tokens": 10,
                                             "cached_tokens": 1},
                                         1: {"completion_tokens": 5,
                                             "prompt_tokens": 10,
                                             "cached_tokens": 1}})
    events = []
    ctx = _Ctx(rid, chat, stream, _recorder(events))
    with ExitStack() as stack:
        for p in _default_patches():
            stack.enter_context(p)
        task = asyncio.create_task(_drive_run(ctx))
        await asyncio.wait_for(round1_done.wait(), 5)
        RC.queue_steer(rid, "now with examples")
        release1.set()
        await asyncio.wait_for(task, 10)
    check("A2 mid-answer steer: continuation round runs (2 model calls)",
          stream.n_calls == 2, f" (calls={stream.n_calls})")
    msgs2 = stream.calls[1][0]
    roles2 = [m.get("role") for m in msgs2]
    asst = [m for m in msgs2 if m.get("role") == "assistant"]
    steer2 = [m for m in msgs2 if m.get("role") == "user"
              and "[OWNER STEER" in str(m.get("content", ""))]
    check("A2 mid-answer steer: model sees its answer + the steer message",
          roles2 == ["system", "user", "assistant", "user"]
          and asst and asst[0].get("content") == "Part1"
          and len(steer2) == 1
          and "now with examples" in str(steer2[0].get("content", "")),
          f" (roles={roles2}, asst={[m.get('content') for m in asst]})")
    toks = [e.get("content") for e in events if e.get("type") == "token"]
    check("A2 mid-answer steer: continuation streams into the same answer",
          toks == ["Part1 ", "STEERED"], f" ({toks})")
    check("A2 mid-answer steer: steer card event emitted",
          any(e.get("type") == "steer" and e.get("content") == "now with examples"
              for e in events))
    usage = next((e for e in events if e.get("type") == "usage_meta"), {})
    check("A2 mid-answer steer: usage stays ONE summed event",
          usage.get("completion_tokens") == 10
          and usage.get("prompt_tokens") == 20
          and usage.get("phase") == "direct", f" ({usage})")
    done = next((e for e in events if e.get("type") == "done"), {})
    check("A2 mid-answer steer: run completes after the continuation",
          done.get("stop_reason") == "completed", f" ({done})")
    check("A2 mid-answer steer: session memory holds the full answer",
          ctx.memory.session and ctx.memory.session[-1][0] == "assistant"
          and ctx.memory.session[-1][1] == "Part1 STEERED",
          f" ({ctx.memory.session})")
    _cleanup_run(rid)

    # ── A2": no steer -> unchanged single-round fast path ────────────────
    rid, chat = "a2-none", "chat_a2_none"
    RC._register_abort(rid)

    async def _stream_plain2(msgs):
        yield ("plain", "")

    stream = _FakeStream([_stream_plain2])
    events = []
    ctx = _Ctx(rid, chat, stream, _recorder(events))
    with ExitStack() as stack:
        for p in _default_patches():
            stack.enter_context(p)
        await asyncio.wait_for(_drive_run(ctx), 10)
    check("A2 no steer: exactly one model call, no steer events",
          stream.n_calls == 1
          and not any(e.get("type") == "steer" for e in events)
          and any(e.get("type") == "done" and e.get("stop_reason") == "completed"
                  for e in events),
          f" (calls={stream.n_calls})")
    _cleanup_run(rid)


asyncio.run(main())
sys.exit(h.finish("direct_pause_steer"))

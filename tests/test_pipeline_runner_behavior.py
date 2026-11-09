# -*- coding: utf-8 -*-
"""Behavior checks: core/pipeline_runner.run_pipeline orchestration (B8, 2026-11-09).

The helpers (_role_vision / pin / unpin) are covered by
tests/test_pipeline_runner_helpers.py; this suite drives the 800-line
run_pipeline generator itself with a fake ctx and mocked VRAM loader
calls - no model, no server, no subprocess:

  * judge pre-check: evicted only when it does not fit beside the pipeline
  * full run: agent sequence, done event, synthesizer prompt composition
  * constraint mode: critic raw tokens suppressed, TUNE lines kept
  * abort mid-run: status event + finally cleanup (unpin/keep-alive)
  * synthesizer skipped: fallback answer + session memory write
"""
import asyncio
import sys
import time
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.pipeline_runner as P  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def _agent(key, model):
    return types.SimpleNamespace(key=key, model=model, temperature=0.2,
                                 max_tokens=512, thinking=False,
                                 thinking_budget=0)


class _Mem:
    def __init__(self):
        self.session = []

    def add_to_session(self, role, content):
        self.session.append((role, content))


class _ExecCtrl:
    stop_reason = None

    def get_summary(self):
        return ""


class _FakeCtx:
    """Duck-typed RunContext for run_pipeline (only what the generator touches)."""

    def __init__(self, *, iterations=1, skip_agents=(), constraint_mode=True,
                 budget=7.5, registry=None, vram=None, aborted_from=10**9):
        self.user_input = "explain the bug"
        self.images = []
        self.mode = "pipeline"
        self.iterations = iterations
        self.skip_agents = set(skip_agents)
        self.constraint_mode = constraint_mode
        self.settings = {"smart_preload_keep_alive": "10m"}
        self.registry = dict(registry or {
            "analyst": "m_a", "refiner": "m_r", "critic": "m_c",
            "synthesizer": "m_s", "judge": "judge_m",
        })
        self.vram_lookup_gb = dict(vram or {"judge_m": 6.0, "m_a": 3.0, "m_r": 3.0})
        self.vram_budget = budget
        self.vram_cache = set()
        self.pipeline = types.SimpleNamespace(agents={
            k: _agent(k, self.registry.get(k, "")) for k in
            ("analyst", "refiner", "critic", "synthesizer", "vision")
        })
        self.pipeline_soul = "soul"
        self.pipeline_mem_ctx = None
        self.pipeline_sess_msgs = None
        self.effective_task_type = "code"
        self.active_preset = "default"
        self.use_learned = False
        self.this_file = "server.py"
        self.run_id = "r1"
        self.t_total = time.time()
        self.image_description = None
        self.websearch_available = False
        self.chat_id = None
        self.model_profile = None
        self.agent_elapsed = {}
        self.prefetch_state = {"prefetch_lead_seconds": 8.0}
        self.prefetch_run_active = True
        self.memory = _Mem()
        self.exec_ctrl = _ExecCtrl()
        self.events = []
        self.msgs = []
        self.prefetch_scheduled = []
        self.unregistered = []
        self._aborted_from = aborted_from
        self._tick = [0]

    # -- emitters / helpers -------------------------------------------------
    async def emit(self, ev):
        self.events.append(ev)
        return ev

    def registry_get(self, key):
        return self.registry.get(key, "")

    def get_num_ctx(self, model, agent_role=None):
        return 2048

    def model_profile(self, _m):
        return None

    def build_soul_prompt_layer(self, soul, style_only=True):
        return ""

    def get_effective_prompt_with_override(self, role, preset, use_learned):
        return f"sys-{role}"

    def make_messages(self, pipeline, sys_p, user_in, imgs, *a, **k):
        self.msgs.append((sys_p, user_in))
        return [{"role": "user", "content": user_in}]

    def aborted(self):
        self._tick[0] += 1
        return self._tick[0] >= self._aborted_from

    def step_skipped(self):
        return False

    def clear_step_skip(self, run_id):
        self.unregistered.append(("step_skip_clear", run_id))

    def is_truncated(self, out):
        return False

    def schedule_prefetch(self, model, agent, t):
        self.prefetch_scheduled.append((model, agent))

    def update_prefetch_lead(self, agent, elapsed, state=None):
        pass

    def append_learning_log(self, file, model, payload):
        pass

    def increment_run_counter(self):
        return 1

    async def maybe_trigger_soul_evolution(self, run_count):
        return None

    def done_event(self, elapsed, stop_reason, **extra):
        ev = {"type": "done", "elapsed": elapsed, "stop_reason": stop_reason}
        ev.update(extra)
        return ev

    def collect_done_metrics(self):
        return {}

    def unregister_abort(self, run_id):
        self.unregistered.append(("abort", run_id))

    def unregister_step_skip(self, run_id):
        self.unregistered.append(("step_skip", run_id))

    def flush_prefetch_settings(self, state):
        return None


_STREAMS = {
    "analyst": ["A1 ", "A2"],
    "refiner": ["R1"],
    "critic": ["JUNK preamble\n", "ERR: logic flaw\n", "MISS: no tests\n",
               "trailing"],
    "synthesizer": ["S1"],
}


class _StreamCtx(_FakeCtx):
    """FakeCtx whose pipeline_chat_stream replays per-role token scripts."""

    def __init__(self, *a, streams=None, **k):
        super().__init__(*a, **k)
        self._streams = dict(streams or _STREAMS)
        self.stream_calls = []

    async def pipeline_chat_stream(self, model, msgs, temp, max_tok, **kw):
        role = kw.get("agent_role", "analyst")
        self.stream_calls.append((role, model, max_tok))
        for tok in self._streams.get(role, []):
            await asyncio.sleep(0)
            yield tok


def _fake_spawn(target, *a, **k):
    if asyncio.iscoroutine(target):
        return asyncio.get_running_loop().create_task(target)
    return None


def _patches():
    evict = mock.AsyncMock()
    load = mock.AsyncMock()
    return (
        mock.patch.object(P, "_bk_evict", new=evict),
        mock.patch.object(P, "_bk_load", new=load),
        mock.patch.object(P, "_get_loaded_models_set",
                          new=mock.AsyncMock(return_value=set())),
        mock.patch.object(P, "_refresh_judge_keepalive",
                          new=mock.AsyncMock()),
        mock.patch.object(P, "spawn", new=_fake_spawn),
        mock.patch.object(P, "run_peer_ratings", new=mock.AsyncMock()),
    )


async def _run(ctx):
    patchers = _patches()
    with patchers[0] as evict, patchers[1] as load, \
            patchers[2], patchers[3], patchers[4], patchers[5]:
        events = []
        async for ev in P.run_pipeline(ctx):
            events.append(ev)
        for _ in range(10):
            await asyncio.sleep(0)
        pending = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=5)
    return events, evict, load


# ── 1. judge pre-check ─────────────────────────────────────────────────────
ctx = _StreamCtx(budget=7.5)
events, evict, load = asyncio.run(_run(ctx))
ctx_fit = _StreamCtx(budget=50.0)
events_fit, evict_fit, _ = asyncio.run(_run(ctx_fit))
evicted_s = [c.args[0] for c in evict.await_args_list if c.args]
evicted_f = [c.args[0] for c in evict_fit.await_args_list if c.args]
check("judge is evicted only when it does not fit beside the pipeline",
      "judge_m" in evicted_s and "judge_m" not in evicted_f,
      f" (tight={evicted_s}/fits={evicted_f})")

# ── 2. full run shape ──────────────────────────────────────────────────────
agents = [e.get("content") for e in events if e.get("type") == "agent"]
agent_done_n = sum(1 for e in events if e.get("type") == "agent_done")
done_ev = next((e for e in events if e.get("type") == "done"), {})
check("pipeline streams analyst>refiner>critic>synthesizer + one done",
      agents == ["Analyst", "Refiner", "Critic", "Synthesizer"]
      and agent_done_n == 4
      and done_ev.get("stop_reason") == "completed",
      f" ({agents}/{agent_done_n}/{done_ev.get('stop_reason')})")

synth_msg = next((u for s, u in ctx.msgs
                  if "[INTERNAL INTERMEDIATE ANALYSES" in u), "")
check("synthesizer prompt carries all three stage outputs",
      "[Analysis]\nA1 A2" in synth_msg
      and "[Refinement]\nR1" in synth_msg
      and "[Critical points]\nERR: logic flaw" in synth_msg,
      f" ({synth_msg[:120]!r})")

# ── 3. constraint mode: critic tokens suppressed, TUNE lines kept ─────────
tokens = [e.get("content", "") for e in events if e.get("type") == "token"]
critic_done = next((e for e in events if e.get("type") == "agent_done"
                    and e.get("critic_tune")), {})
check("constraint mode streams no critic raw tokens and keeps TUNE lines",
      not any("ERR:" in t or "JUNK" in t for t in tokens)
      and critic_done.get("critic_tune") == "ERR: logic flaw\nMISS: no tests",
      f" ({tokens}/{critic_done.get('critic_tune')!r})")

# ── 4. abort mid-run + finally cleanup ─────────────────────────────────────
# ABORT-BEFORE-ROUND (2026-11-09): the synthesizer reads _pipeline_ws_ctx
# after the loop; an abort before round 1 used to raise UnboundLocalError.
# The fix initializes it before the loop, so both abort scenarios are pinned.
ctx_ab = _StreamCtx(iterations=2, skip_agents=("synthesizer",),
                    constraint_mode=False, aborted_from=1)
events_ab, evict_ab, load_ab = asyncio.run(_run(ctx_ab))
statuses = [e.get("content") for e in events_ab if e.get("type") == "status"]
done_ab = next((e for e in events_ab if e.get("type") == "done"), {})
unpin_calls = [c for c in load_ab.await_args_list
               if c.kwargs.get("keep_alive") == "10m"]
check("abort: status, done(reason=aborted) and full finally-cleanup",
      "Aborted." in statuses
      and done_ab.get("stop_reason") == "aborted"
      and ctx_ab.prefetch_run_active is False
      and ("abort", "r1") in ctx_ab.unregistered
      and ("step_skip", "r1") in ctx_ab.unregistered
      and all(c.args and c.args[0] == "m_a" for c in unpin_calls)
      and len(unpin_calls) == 1,
      f" ({statuses}/{done_ab.get('stop_reason')}/{unpin_calls})")

ctx_ab0 = _StreamCtx(iterations=1, constraint_mode=False, aborted_from=0)
events_ab0, _e_ab0, _l_ab0 = asyncio.run(_run(ctx_ab0))
done_ab0 = next((e for e in events_ab0 if e.get("type") == "done"), {})
check("abort before round 1 with synthesizer enabled ends cleanly",
      done_ab0.get("stop_reason") == "aborted", f" ({done_ab0})")

# ── 5. synthesizer skipped → fallback answer ───────────────────────────────
ctx_ns = _StreamCtx(skip_agents=("synthesizer",), constraint_mode=False)
events_ns, _e2, _l2 = asyncio.run(_run(ctx_ns))
agents_ns = [e.get("content") for e in events_ns if e.get("type") == "agent"]
tok_ns = [e.get("content") for e in events_ns if e.get("type") == "token"]
done_ns = next((e for e in events_ns if e.get("type") == "done"), {})
check("synthesizer off: registry fallback answer lands in the session",
      "Answer (synthesizer off)" in agents_ns
      and "R1" in tok_ns
      and done_ns.get("stop_reason") == "completed"
      and ("assistant", "R1") in ctx_ns.memory.session
      and ("user", "explain the bug") in ctx_ns.memory.session,
      f" ({agents_ns}/{tok_ns}/{ctx_ns.memory.session})")

sys.exit(h.finish("pipeline_runner_behavior"))

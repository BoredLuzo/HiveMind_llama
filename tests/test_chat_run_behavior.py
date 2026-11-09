# -*- coding: utf-8 -*-
"""Behavior checks: core/chat_run.run_stream wiring (B8, 2026-11-09).

test_chat_run_helpers.py covers the module-level helpers; this suite drives
the run_stream generator itself up to the runner dispatch. The heavy
`server` import is replaced by a stub module, `run_stream_orchestrated`
(the dispatch into the runners) is replaced by a capturing fake, and the
threaded run-record writer is patched - no model, no network, no subprocess:

  * run_id event first; orchestrator receives a fully wired RunContext
  * per-run ContextVar gates (approval force / phone restriction / snapshot)
  * ctx_meter events feed the peak metrics; emit() token accounting feeds
    the done event and the threaded run record (route "direct" for a
    forced-simple chat run)
  * workspace resolve raising WorkspaceForceInvalid -> blocked done event,
    orchestrator never called
"""
import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.chat_run as C  # noqa: E402
import core.state as S  # noqa: E402
from infra.token_stats import _estimate_tokens_from_content as _est_tok  # noqa: E402
import tools.runner as TR  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


class _FakePipeline:
    """Duck-typed pipeline: the four shortcut predicates are the seams
    run_stream touches before the dispatch (state.pipeline is None in a
    bare process - only the server boot builds it)."""

    def __init__(self):
        self.agents = {
            k: types.SimpleNamespace(model=f"m_{k}", temperature=0.2,
                                     max_tokens=512, thinking=False,
                                     thinking_budget=0, name=k)
            for k in ("analyst", "refiner", "critic", "synthesizer",
                      "vision", "direct", "planner", "coder", "judge")
        }
        # chat_history_seed reads pipeline.memory.as_context_string()
        self.memory = types.SimpleNamespace(as_context_string=lambda: "")

    def _is_list_memory_request(self, t):
        return False

    def _is_forget_request(self, t):
        return False

    def _is_memory_request(self, t):
        return False

    def _is_self_question(self, t):
        return False


class _FakeMemory:
    _session_chat_id = ""

    def get_session_messages(self, *a, **k):
        return []

    def add_to_session(self, *a, **k):
        pass

    def clear_session(self):
        pass

    def seed_session(self, msgs):
        pass

    def list_memories(self):
        return []

    def remember(self, *a):
        pass

    def forget(self, *a):
        pass

    def get_all(self):
        return {}


S.init_state(pipeline_obj=_FakePipeline(), memory_obj=_FakeMemory())


def _fake_server():
    srv = types.ModuleType("server")
    srv.S_models_cache = []
    srv.THIS_FILE = ROOT / "server.py"  # real server.py keeps it a Path
    srv._COMPLEX_DIRECT_MODELS = []
    srv._SIMPLE_DIRECT_MODELS = []
    srv._VRAM_LOOKUP_GB = {}
    srv._WEBSEARCH_AVAILABLE = False
    srv._flush_prefetch_settings = lambda state: None
    srv._get_avg_elapsed = lambda agent: 0.0
    srv._maybe_trigger_soul_evolution = lambda run_count: None
    srv._registry = {"judge": "", "direct": ""}
    srv._update_prefetch_lead = lambda *a, **k: None
    srv._vision_cfg = {}
    srv.apply_learned_configs_for_run = lambda: None
    srv.get_effective_prompt_with_override = lambda *a, **k: ""
    return srv


_SETTINGS_FORCE = {
    "smart_preload_enabled": False,
    "learning_preset_mode": False,
    "judge_prefetch_before_complexity": False,
    "intent_agent": {"enabled": False},
    "workspace": "",
}


def _ev(e):
    """run_stream yields either dicts (orchestrator passthrough) or the
    SSE strings emit() returns - normalise both to dicts."""
    if isinstance(e, dict):
        return e
    if isinstance(e, str) and e.startswith("data: "):
        try:
            return json.loads(e[6:].strip())
        except ValueError:
            return {"type": "sse_unparseable", "raw": e[:60]}
    return {"type": f"non_sse:{type(e).__name__}"}


async def _drive_run(overrides, request_ws, orchestrator, settings_extra=None):
    """Run run_stream with a stubbed server module + fake orchestrator."""
    from core import state as S
    from infra import run_control as RC
    from utils import workspace_resolve as WSR
    from infra import token_stats as TS
    from infra import notify as NTF

    forced = dict(_SETTINGS_FORCE)
    forced.update(settings_extra or {})
    calls = []
    saved_last = []
    records = []

    def _record(rec):
        records.append(rec)

    async def _orch(ctx):
        calls.append(ctx)
        yield {"type": "ctx_meter", "est_tokens": 100, "ctx_limit": 8000}
        yield {"type": "passthrough_probe", "n": 1}
        yield await ctx.emit({"type": "token", "content": "hello"})
        yield {"type": "metrics", "metrics": dict(ctx.collect_done_metrics())}
        yield ctx.done_event(0.5, "completed")

    events = []
    with mock.patch.dict(sys.modules, {"server": _fake_server()}), \
            mock.patch.dict(S.settings, forced), \
            mock.patch.object(C, "run_stream_orchestrated", new=_orch), \
            mock.patch.object(WSR, "save_last_workspace",
                              side_effect=lambda ws: saved_last.append(ws)), \
            mock.patch.object(WSR, "sync_env_workspace", new=lambda ws: None), \
            mock.patch.object(TS, "_record_run", new=_record), \
            mock.patch.object(NTF, "notify_run_completed", new=lambda *a: None), \
            mock.patch.object(NTF, "notify_run_stopped", new=lambda *a: None):
        gen = C.run_stream(
            user_input="hello world", images=[], mode="chat", iterations=1,
            active_preset="default", constraint_mode=True,
            force_complexity="simple", model_overrides=overrides,
            request_workspace=request_ws,
        )
        async for ev in gen:
            events.append(ev)
        await asyncio.sleep(0.3)  # the run record is written on a daemon thread
    events = [_ev(e) for e in events]
    # cleanup: abort/step-skip registrations of this run are torn down by the
    # runners in production - do it here for the fake path.
    rid = next((e.get("run_id") for e in events if e.get("type") == "run_id"),
               None)
    if rid:
        RC._unregister_abort(rid)
        RC._unregister_step_skip(rid)
    return events, calls, saved_last, records


async def main():
    ws = Path(tempfile.mkdtemp(prefix="hvm_chatrun_ws_"))
    overrides = {
        "duo_action_approval_enabled": True,
        "phone_restricted": True,
        "probe_override_marker": "present",
    }
    events, calls, saved_last, records = await _drive_run(
        overrides, str(ws), None)

    # ── 1. run_id event first ─────────────────────────────────────────────
    rid = events[0].get("run_id", "") if events else ""
    check("first event is the run_id with the <ts>-<hex8> shape",
          events[0].get("type") == "run_id" and len(rid.split("-")) == 2
          and len(rid.split("-")[1]) == 8,
          f" ({events[0]})")

    # ── 2. RunContext wiring ──────────────────────────────────────────────
    ctx = calls[0]
    check("orchestrator gets a wired RunContext (overrides, workspace, route)",
          calls and ctx.settings.get("probe_override_marker") == "present"
          and ctx.workspace == str(ws.resolve())
          and ctx.workspace_commit_allowed is True
          and ctx.complexity == "simple"
          and ctx.complexity_source == "manual"
          and isinstance(ctx.vram_budget, float)
          and ctx.mode == "chat"
          and saved_last == [str(ws.resolve())],
          f" ({ctx.workspace}/{ctx.complexity}/{ctx.complexity_source})")

    # ── 3. per-run ContextVar gates ride the overrides only ──────────────
    check("gate force + phone restriction + settings snapshot set for the run",
          TR._approval_gate_run_override.get() is True
          and TR._tools_restricted_run.get() is True
          and TR._run_settings_snapshot.get().get("probe_override_marker")
          == "present",
          f" (gate={TR._approval_gate_run_override.get()} "
          f"phone={TR._tools_restricted_run.get()})")

    # ── 4. ctx_meter -> peak metrics + event passthrough ─────────────────
    metrics = next((e.get("metrics") or {} for e in events
                    if e.get("type") == "metrics"), {})
    check("ctx_meter feeds the peak metrics and still streams through",
          metrics.get("metrics_ctx_peak_tokens") == 100
          and metrics.get("metrics_ctx_limit") == 8000
          and metrics.get("metrics_ctx_pressure_peak") == round(100 / 8000, 3)
          and any(e.get("type") == "ctx_meter" for e in events)
          and any(e.get("type") == "token" and e.get("content") == "hello"
                  for e in events),
          f" ({metrics.get('metrics_ctx_peak_tokens')}/"
          f"{metrics.get('metrics_ctx_limit')}/"
          f"{metrics.get('metrics_ctx_pressure_peak')})")

    # ── 5. token accounting + threaded run record ─────────────────────────
    done = next((e for e in events if e.get("type") == "done"), {})
    est = _est_tok("hello")
    rec = records[0] if records else {}
    check("done event + run record carry the heuristic token count and route",
          done.get("tokens_generated") == est
          and rec.get("tokens") == est
          and rec.get("mode") == "direct"
          and rec.get("ui_mode") == "chat"
          and rec.get("stop_reason") == "completed",
          f" ({done.get('tokens_generated')}/{rec.get('tokens')}/"
          f"{rec.get('mode')}/{rec.get('ui_mode')})")

    # ── 6. error path: workspace force-invalid blocks the run ────────────
    from utils.workspace_resolve import WorkspaceForceInvalid
    calls_before = len(calls)
    ws_broken = Path(tempfile.mkdtemp(prefix="hvm_chatrun_bad_")) / "nope"
    with mock.patch(
            "utils.workspace_resolve.resolve_workspace",
            side_effect=WorkspaceForceInvalid("bad workspace")):
        ev2, calls2, _s2, rec2 = await _drive_run({}, str(ws_broken), None)
    kinds = [e.get("type") for e in ev2]
    done2 = next((e for e in ev2 if e.get("type") == "done"), {})
    check("force-invalid workspace: blocked done event, no orchestrator run",
          kinds == ["status", "done"]
          and done2.get("stop_reason") == "blocked"
          and "Workspace invalid" in str(done2.get("summary", ""))
          and len(calls) == calls_before == 1,
          f" ({kinds}/{done2.get('stop_reason')}/{done2.get('summary')})")


asyncio.run(main())
sys.exit(h.finish("chat_run_behavior"))

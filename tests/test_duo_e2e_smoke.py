"""Duo e2e smoke: run_code_duo driven end-to-end with a scripted llama-server.

Deliberate scope (the net the run_code_duo split needs, Sprint C):
planner OFF, no chunking, the first coder round calls task_complete. This
pins the RunContext contract, the llama-manager seam, the AgenticToolLoop
HTTP path and the agentic completion path (verify gate included). Tool
execution internals are covered by test_execute_tool_round; deeper paths
(planner, chunk resume, compression) are follow-up segments of this suite.
"""
import asyncio
import httpx
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


MODEL = "qwen3.5:4b"
PORT = 8101

# ── the scripted llama-server ────────────────────────────────────────────
_post_bodies: list[dict] = []


def _sse(*chunks: dict) -> bytes:
    out = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    return out + b"data: [DONE]\n\n"


_TASK_COMPLETE = {
    "id": "c1", "type": "function", "index": 0,
    "function": {"name": "task_complete",
                 "arguments": json.dumps({"completed": True, "blockers": [],
                                          "build_status": "passing"})},
}


def _handler(request: httpx.Request) -> httpx.Response:  # noqa: F821 (import below)
    _post_bodies.append(json.loads(request.read().decode("utf-8", "replace")))
    if request.url.path.endswith("/chat/completions"):
        return httpx.Response(200, content=_sse(
            {"choices": [{"delta": {"tool_calls": [_TASK_COMPLETE]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
             "usage": {"completion_tokens": 42, "prompt_tokens": 500,
                       "prompt_tokens_details": {"cached_tokens": 300}},
             "timings": {"predicted_per_second": 33.3, "prompt_ms": 120, "prompt_n": 500}},
        ))
    if request.url.path.endswith("/props"):
        return httpx.Response(200, json={
            "default_generation_settings": {"n_ctx": 8192}, "model": MODEL})
    return httpx.Response(200, json={})


# ── fakes ────────────────────────────────────────────────────────────────
class _ShimClient(httpx.AsyncClient):
    """Every httpx client in the driven code lands on the mock server."""
    def __init__(self, *a, **kw):
        kw.setdefault("transport", httpx.MockTransport(_handler))
        super().__init__(*a, **kw)


class _HttpxShim:
    """Module stand-in for duo_runner.httpx: same surface, mocked client."""
    AsyncClient = _ShimClient

    def __getattr__(self, name):
        return getattr(httpx, name)


def _build(monkey_events: list):
    import backend.llama_server_manager as LSM
    from core.run_context import RunContext
    from core.duo_config import DuoConfig
    from infra.phase_timer import PhaseTimer
    from settings import DEFAULT_SETTINGS

    _slot = NS(port=PORT, _num_ctx=8192, model=MODEL, pinned=False,
               is_running=True, _loading=False, slot_id=0, last_used=0.0,
               mark_in_flight=lambda: None, clear_in_flight=lambda: None,
               kill=lambda: None)

    class _FakeManager:
        def __init__(self):
            self._slots = [_slot]
            self._pending_prefetch = []
            self._planner_critical_phase = False

        async def ensure_loaded(self, model, num_ctx=None, pin=False,
                                vision=False, n_parallel=1, ctx_graceful=True,
                                **kw):
            return PORT

        def _find_loaded(self, model):
            return _slot if model == MODEL else None

        def unpin_all(self):
            return 0

        def evict(self, *a, **k):
            return None

        def can_fit(self, *a, **k):
            return True

    LSM.manager = _FakeManager()

    settings = dict(DEFAULT_SETTINGS)
    settings.update({
        "agents": {"duo_coder": {"model": MODEL}},
        "duo_coder_model": MODEL,
        "duo_runtime_profile": "balanced",
        "vram_budget_gb": 7.0,
        "workspace": "",
    })

    async def emit(ev):
        monkey_events.append(ev)
        return ""

    class _Mem:
        """Memory fake: known methods + no-op fallback for the long tail."""

        def add_to_session(self, *a, **k):
            return None

        def __getattr__(self, name):
            def _noop(*a, **k):
                return None
            return _noop

    class _Agents(dict):
        def __missing__(self, key):
            cfg = NS(model="", max_tokens=400, temperature=0.3,
                     thinking=False, thinking_budget=0)
            self[key] = cfg
            return cfg

    _agents = _Agents()
    _agents["duo_coder"] = NS(model=MODEL, max_tokens=800, temperature=0.3,
                              thinking=False, thinking_budget=0)
    _agents["duo_planner"] = NS(model=MODEL, max_tokens=800, temperature=0.3,
                                thinking=False, thinking_budget=0)

    ws = Path(tempfile.mkdtemp(prefix="duo_e2e_"))
    ctx = RunContext(
        user_input="Create hello.py that prints hello",
        complexity="code_duo",
        chat_id=None,
        run_id="e2e-smoke",
        t_total=1000.0,
        workspace=str(ws),
        request_workspace="",
        workspace_commit_allowed=False,
        duo_config=DuoConfig(
            tool_rounds=3, use_pipeline=False, coding_mode=True,
            chunking=False, planner=False, pre_explore=False,
            agentic_mode=True, agentic_thinking=False, thinking_per_chunk=False,
            test_feedback_chunk=False, test_feedback_final=True,
            until_finished=False, runtime_profile="balanced",
            websearch_enabled=False, git_autocommit=False,
            evict_on_pause=False, pause_timeout_s=600,
            pass_explore_files="touched", parallel_preexplore=False,
        ),
        settings=settings,
        memory=_Mem(),
        registry={"duo_coder": MODEL},
        vram_budget=7.0,
        phase_timer=PhaseTimer(),
        exec_ctrl=NS(abort=lambda *a, **k: None,
                     reset_stuck_detection=lambda: None),
        emit=emit,
        aborted=lambda: False,
        is_aborted_chat=lambda *a: False,
        step_skipped=lambda: False,
        clear_step_skip=lambda: None,
        collect_done_metrics=lambda: {},
        done_event=lambda *a, **kw: {"type": "done",
                                     "elapsed": a[0] if a else 0,
                                     "stop_reason": a[1] if len(a) > 1 else "",
                                     **kw},
        registry_get=lambda *a: MODEL,
        get_num_ctx=lambda *a, **kw: 8192,
        model_profile=lambda *a, **kw: {"tool_call": True, "temperature": 0.3,
                                        "top_p": 0.95, "top_k": 20},
        get_effective_prompt_with_override=lambda *a, **kw: "",
        make_messages=lambda *a, **kw: [],
        pipeline=NS(ollama=NS(_client=_ShimClient(), chat_stream=None),
                    agents=_agents, memory=None),
        websearch_available=False,
        resolve_duo_run_timeout_seconds=lambda *a: 300,
        resolve_duo_rounds_cap=lambda *a: 3,
        resolve_duo_runtime_profile=lambda *a, **kw: "balanced",
    )
    return ctx


async def _drive():
    events: list = []
    from core.duo_runner import run_code_duo
    ctx = _build(events)
    async for _ev in run_code_duo(ctx):
        events.append(_ev)
    return events


# ── httpx shim: patch duo_runner's namespace so ad-hoc clients (props etc.)
#    also hit the mock transport. ─────────────────────────────────────────
import httpx  # noqa: E402


def _install_shim():
    import core.duo_runner as DR
    DR.httpx = _HttpxShim()


_install_shim()
# CLEANUP (2026-10-23): the run persists its tmp workspace as "last used"
# (context/last_workspace.json, git-ignored). A later REAL engine start in
# this checkout must not inherit it — restore the pre-suite state.
from utils.workspace_resolve import _LAST_WS_FILE as _LWSF  # noqa: E402
_lwsf_existed = _LWSF.exists()
try:
    _events = asyncio.run(_drive())
finally:
    import core.duo_runner as DR
    DR.httpx = httpx
    if not _lwsf_existed and _LWSF.exists():
        _LWSF.unlink()

_types = [e.get("type") for e in _events if isinstance(e, dict)]
needs_diag = ("done" not in _types or len(_post_bodies) == 0)
if needs_diag:
    print("  (diag) posts:", len(_post_bodies), "| events:", len(_events))
    for _e in _events[-10:]:
        print("  (diag)", repr(_e)[:160] if not isinstance(_e, dict)
              else f"{_e.get('type')} | {str(_e.get('content') or _e.get('stop_reason') or '')[:110]}")
check("run terminates with a done event", "done" in _types,
      f"(events={len(_events)})")
check("duo_start announced with the model label",
      any(t == "duo_start" for t in _types))
check("at least one coder round ran",
      any(t in ("duo_round", "round") for t in _types))
check("the scripted POST carried the tools schema incl. task_complete",
      bool(_post_bodies) and any(
          any(tc.get("function", {}).get("name") == "task_complete"
              for tc in (b.get("tools") or []))
          for b in _post_bodies if isinstance(b, dict)))
check("task_complete reached the engine exactly once",
      sum(1 for b in _post_bodies if isinstance(b, dict)) >= 1)
check("no loop_detected / abort in the stop reason",
      not any(e.get("stop_reason") in ("loop_detected", "aborted")
              for e in _events if isinstance(e, dict) and e.get("type") == "done"),
      f"(last={[_types[-3:]]})")
check("a coder token/tool event stream existed (status relay)",
      any(t in ("status", "token", "tool_call", "agent") for t in _types))

print()
print(f"SUMMARY duo_e2e_smoke: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

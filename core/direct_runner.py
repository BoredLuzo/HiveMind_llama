import asyncio
from utils.bg_tasks import spawn  # noqa: E402
import logging
import time
from datetime import datetime


from context.pause_state import (
    clear_pause_state as _clear_pause_state,
    persist_pause_state as _persist_pause_state,
)
from core.duo_helpers import build_steer_user_message as _build_steer_user_message
from infra.run_control import (
    _get_abort_event,
    cleanup_pause_state as _cleanup_pause_state,
    clear_pause_request as _clear_pause_request,
    drain_steer_messages as _drain_steer_messages,
    get_abort_during_pause_signal as _get_abort_during_pause_signal,
    get_resume_signal as _get_resume_signal,
    is_pause_requested as _pause_requested,
    steer_queue_view as _steer_queue_view,
)
from learning.peer_ratings import run_peer_ratings
from vram.loader import _refresh_judge_keepalive
from routing.model_picker import _pick_direct_model
from vram.loader import _bk_pin
from utils.messages import normalize_system_messages

_direct_log = logging.getLogger("hivemind.direct_runner")

# A2 (2026-11-11): a steer that arrives DURING a pure direct answer gets a
# bounded continuation round (1 initial answer + N continuations = the round
# cap). Anything queued later delivers after the run (frontend queue) as
# before — the cap only bounds the loop, it never loses a drained message.
_DIRECT_STEER_MAX_CONTINUATIONS = 2

_DIRECT_WS_NOTE = (
    "\n\nWEB SEARCH IS AVAILABLE: use the web_search / web_fetch tool calls "
    "for anything about recent events, external information or questions you "
    "cannot answer from your training data. Call the tools BEFORE answering."
)

def _direct_tools_note(tool_mode: str, ws_ok: bool = False) -> str:
    """TIER-AWARE (2026-09-02): only advertise tools the selected direct tier
    actually provides. The old note always listed read_file + web, which was
    wrong for the Websearch-only tier (models then called read_file and hit
    TOOL_NOT_ALLOWED)."""
    if tool_mode == "direct":
        _list = ("web_search / web_fetch" if ws_ok
                 else "no tools (web search is currently unavailable)")
    elif tool_mode == "direct_research":
        _list = ("read/search tools (read_file, list_dir, find_files, "
                 "search_code, get_signatures, find_references)"
                 + (", web_search / web_fetch" if ws_ok else ""))
    elif tool_mode == "direct_python":
        _list = ("read_file, list_dir, find_files, search_code, get_signatures, "
                 "find_references, run_python" + (", web_search / web_fetch" if ws_ok else ""))
    else:  # direct_full
        _list = ("read/write/edit tools (read_file, edit_file, write_file, ...), "
                 "run_python, run_bash" + (", web_search / web_fetch" if ws_ok else ""))
    return (
        "\n\nTOOLS ARE AVAILABLE: " + _list + ".\n"
        "Use tools ONLY when they actually help — for pure knowledge, conversation, "
        "math or general questions answer directly from your knowledge. "
        "When you have the answer or the change is complete, reply in plain text "
        "WITHOUT calling another tool. "
        "Never call a tool that is not listed above."
    )


class _DirectToolsResult:
    """Mutable outcome holder for a live direct-tools run.

    The generator streams events while it runs; after exhaustion `content` and
    `final_msgs` are filled for the fallback plain stream / persistence.
    """
    __slots__ = ("content", "final_msgs", "send_files", "written_files")

    def __init__(self) -> None:
        self.content = ""
        self.final_msgs = None
        self.send_files: list[str] = []
        self.written_files: list[str] = []


async def _run_direct_tools(ctx, model: str, msgs: list, tool_mode: str,
                            result: _DirectToolsResult, vision: bool = False):
    """Run the unified ToolLoop for the direct chat, STREAMING events live.

    Yields ToolLoop events (tokens, tool chips, results) as they happen.
    Previously the whole loop was awaited first and all events emitted only
    after the loop finished — the UI showed nothing until the run was done.

    After the generator is exhausted, result.content / result.final_msgs hold
    the loop outcome (for persistence and the abort/round-cap fallback).
    """
    try:
        from tools.definitions import _get_inline_tools as _tools_fn
        from backend.llama_client import manager as _mgr
        from core.tool_loop import ToolLoop, ToolLoopConfig
        from core.tool_guards import repeat_guard as _repeat_guard
        import httpx as _httpx
    except Exception as _e:
        _direct_log.warning("[Direct-Tools] unavailable: %s", _e)
        yield {"type": "status", "content": f"⚠ Tool loop unavailable: {str(_e)[:120]}"}
        return

    try:
        _ws_avail = bool(ctx.websearch_available)
        _num_ctx = ctx.get_num_ctx(model, "direct") or 8192
        # SCHEMA-FIT (2026-10-17): the direct tiers used to ship the FULL tool
        # schema regardless of the window (direct_full = 25 tools / ~15.6k
        # chars, i.e. ~2x the 8k budget). Fit it like the duo coder does.
        _schema_budget = 0
        try:
            from core.budgets import schema_budget_for
            _schema_budget = schema_budget_for(_num_ctx, model)
        except (ImportError, AttributeError, TypeError, ValueError):
            _schema_budget = 0
        _send_file_on = (tool_mode == "direct_full"
                         and str((getattr(ctx, "settings", {}) or {}).get("source") or "").startswith("telegram"))
        _tools = _tools_fn(include_websearch=_ws_avail, mode=tool_mode,
                           budget_chars=_schema_budget or None,
                           include_send_file=_send_file_on)
        if not _tools:
            return
        # IMAGES (2026-10-03): raw parts need the projector — the CALLER
        # decides the flag from the same expression that built the messages
        # (2026-10-06, live 11:45): ctx.effective_images was emptied by the
        # gate for the registry model while the msgs still carried raw parts
        # for the P9-override model → projector-less slot → POST 500.
        _port = await _mgr.ensure_loaded(model, num_ctx=_num_ctx,
                                         vision=vision)
        _agent = ctx.pipeline.agents["direct"]
        _think = bool(getattr(_agent, "thinking", False))
        _budget = int(getattr(_agent, "thinking_budget", 0) or 0)
        _max_tokens = max(int(getattr(_agent, "max_tokens", 600) or 600) + 400, 1000)
        try:
            _rounds_raw = int(ctx.settings.get("direct_tools_max_rounds", 12))
        except (TypeError, ValueError):
            _rounds_raw = 12
        _rounds = max(1, min(300, _rounds_raw))
        _read_timeout = float(ctx.settings.get("duo_llm_slow_timeout_s", 300) or 300)

        _final_msgs: list | None = None
        # SEND-FILE: the same run-written-file registry the shared executor
        # uses, so direct mode can queue a file it just wrote.
        _written_files: set = set()
        _written_token = None
        _epoch_token = None
        try:
            import time as _time_epoch
            from tools.runner import _files_written_in_run as _fw_ctx
            from tools.runner import _run_file_epoch as _epoch_ctx
            from tools.runner import _files_written_by_run as _fw_registry
            _fw_registry.pop(str(ctx.run_id), None)
            # A fresh set per direct run: reusing an outer context would let
            # send_file accept a file from an earlier run.
            _written_token = _fw_ctx.set(_written_files)
            _epoch_token = _epoch_ctx.set(_time_epoch.time())
        except (ImportError, AttributeError, TypeError):
            _fw_ctx = None
            _epoch_ctx = None
        _send_paths: list[str] = []

        async def _capture_tool_result(name, args, result, state):
            if name == "send_file" and str(result).startswith("[FILE_QUEUED]"):
                _p = str((args or {}).get("path") or "")
                if _p and _p not in _send_paths:
                    _send_paths.append(_p)
            return None

        async def _capture_msgs(messages, _round_num, state):
            nonlocal _final_msgs
            _final_msgs = list(messages)
            return messages, None

        _loop = ToolLoop(
            config=ToolLoopConfig(
                stream=True, max_rounds=_rounds, max_post_attempts=3,
                read_timeout_s=_read_timeout,
                model=model, temperature=getattr(_agent, "temperature", 0.4),
                max_tokens=_max_tokens, num_ctx=_num_ctx,
                thinking=_think, thinking_budget=_budget,
                tools=_tools, tool_mode=tool_mode,
                include_websearch=_ws_avail,
                # Direct chat: a plain answer without tools is a valid final answer.
                require_tool_call=False,
                retry_on_5xx=True, retry_on_grammar_crash=True,
                retry_on_context_overflow=True,
            ),
            http_client=_httpx.AsyncClient(timeout=_httpx.Timeout(
                connect=10.0, read=_read_timeout + 30.0, write=10.0, pool=5.0,
            )),
            port=_port,
            workspace=getattr(ctx, "workspace", "") or "",
            # RUN-ID PROPAGATION (2026-10-06, live): ToolLoop.run() sets the
            # _current_run_id ContextVar from self._run_id — without it every
            # inline tool saw run_id="" : ask_user could not register its
            # pause (gate fell open, instant auto-answer = "ask_user was
            # instantly aborted") and mid-run steering never drained in
            # direct mode (the drain site keys on the same id).
            run_id=str(getattr(ctx, "run_id", "") or ""),
            abort_check=ctx.aborted,
            on_tool_result=_capture_tool_result,
            on_before_post=_capture_msgs,
            # NO-PROGRESS (2026-11-08): identical repeated tool calls get one
            # nudge instead of silently burning rounds.
            on_after_round=_repeat_guard(),
        )
        try:
            async for _ev in _loop.run(msgs):
                yield _ev
        finally:
            try:
                await _loop._client.aclose()
            except Exception:
                pass
            if _written_token is not None and _fw_ctx is not None:
                _fw_ctx.reset(_written_token)
            if _epoch_token is not None and _epoch_ctx is not None:
                _epoch_ctx.reset(_epoch_token)
        _state = getattr(_loop, "state", None)
        result.content = "".join(_state.content_parts).strip() if _state is not None else ""
        result.final_msgs = _final_msgs
        result.send_files = list(_send_paths)
        result.written_files = sorted(str(p) for p in _written_files)
        # USAGE (2026-11-08): surface the loop's summed token counters so
        # run-stats and the benchmark see real numbers for tool runs.
        if _state is not None and (_state.prompt_tokens or _state.completion_tokens):
            yield {"type": "usage_meta", "phase": "direct_tools",
                   "completion_tokens": int(_state.completion_tokens),
                   "prompt_tokens": int(_state.prompt_tokens),
                   "cached_tokens": int(_state.cached_tokens)}
    except Exception as _e:
        _direct_log.warning("[Direct-Tools] tool loop failed: %s", _e)
        yield {"type": "status", "content": f"⚠ Tool loop error: {str(_e)[:120]}"}


async def _direct_websearch_tool_round(ctx, model: str, msgs: list, num_ctx: int,
                                       max_rounds: int = 2) -> tuple[list, list, bool]:


    events: list = []
    try:
        from tools.definitions import _get_inline_tools as _ws_tools_fn
        from tools.handlers import (
            _inline_tool_web_search as _ws_search_fn,
            _inline_tool_web_fetch as _ws_fetch_fn,
        )
        from backend.llama_client import manager as _ws_mgr
        import httpx as _ws_httpx
        import json as _ws_json
    except Exception as _e:
        _direct_log.warning("[Direct-WS] tool round unavailable: %s", _e)
        return events, msgs, False

    _tools = [
        t for t in _ws_tools_fn(include_websearch=True)
        if t.get("function", {}).get("name") in ("web_search", "web_fetch")
    ]
    if not _tools:
        return events, msgs, False

    try:
        _port = await _ws_mgr.ensure_loaded(model, num_ctx=num_ctx)
    except Exception as _e:
        _direct_log.warning("[Direct-WS] model load failed: %s", _e)
        return events, msgs, False

    # strict templates (MiniCPM-V 4.7) reject a system message after the
    # first turn - HiveMind appends no-think/nudge notes as system messages
    _msgs = normalize_system_messages(list(msgs))
    _any = False
    for _round in range(max_rounds):
        _payload = {
            "model": model,
            "messages": _msgs,
            "tools": _tools,
            "stream": False,
            "temperature": 0.2,
            "max_tokens": 500,
        }
        try:
            async with _ws_httpx.AsyncClient(
                timeout=_ws_httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)
            ) as _c:
                _r = await _c.post(f"http://127.0.0.1:{_port}/v1/chat/completions", json=_payload)
                _r.raise_for_status()
                _msg = (_r.json() or {}).get("choices", [{}])[0].get("message", {})
        except Exception as _e:
            _direct_log.debug("[Direct-WS] round %d failed: %s", _round, _e)
            break
        _tcs = _msg.get("tool_calls") or []
        if not _tcs:
            break
        _msgs.append({"role": "assistant", "content": _msg.get("content") or None,
                      "tool_calls": _tcs})
        _any = True
        for _tc in _tcs:
            _fn = ((_tc.get("function") or {}).get("name") or "").strip()
            try:
                _args = _ws_json.loads((_tc.get("function") or {}).get("arguments") or "{}")
                if not isinstance(_args, dict):
                    _args = {}
            except Exception:
                _args = {}
            _ws_what = str(_args.get("query") or _args.get("url") or "")[:120]
            events.append(await ctx.emit({
                "type": "status", "content": f"🔍 {_fn}: {_ws_what}",
            }))
            try:
                if _fn == "web_search":
                    _res = await _ws_search_fn(_args, None, None)
                elif _fn == "web_fetch":
                    _res = await _ws_fetch_fn(_args, None, None)
                else:
                    _res = f"[{_fn} is not available in direct chat]"
            except Exception as _e:
                _res = f"[{_fn} failed: {type(_e).__name__}: {str(_e)[:200]}]"
            _msgs.append({
                "role": "tool",
                "tool_call_id": _tc.get("id") or f"direct_tc_{_round}_{len(_msgs)}",
                "content": str(_res)[:8000],
            })
    return events, _msgs, _any


class _PauseGateResult:
    """Outcome holder for one _direct_manual_pause_gate call (the generator
    cannot return a value the async-for would see)."""

    __slots__ = ("continue_run",)

    def __init__(self) -> None:
        self.continue_run = True


async def _direct_manual_pause_gate(ctx, outcome: _PauseGateResult,
                                    phase: str = "direct"):
    """A1 (2026-11-11): manual pause for DIRECT runs at a safe boundary.

    `/pause/{run_id}` sets `request_pause_after_chunk`; until now only the
    Duo chunk loop consumed the flag, so a direct stream ignored it (live
    matrix 2026-11-07: `/pause` -> `pause_requested`, `/pause-state`
    `{"active": false}`, `/resume` 409). Direct runs call this gate at every
    safe boundary:
      * before a model round starts (websearch done, POST not yet sent)
      * at token-chunk boundaries of the live stream — the upstream
        generator suspends at its yield while the gate waits, so httpx
        stops reading and llama-server generation backpressures to a halt
        (no tokens are generated into the void while paused)

    Async generator so the `run_paused_manual` divider is yielded BEFORE
    the wait suspends the run — the UI flips to its Resume state while the
    gate blocks (the Duo chunk loop yields the same event before its wait).
    Mirrors the Duo gate otherwise: persist `/pause-state/{chat_id}`, wait
    resume | abort-during-pause | chat abort, then yield the resumed divider
    (or the abort status). `outcome.continue_run=False` = aborted during
    pause; the caller ends the run with stop_reason "aborted".
    """
    if not _pause_requested(ctx.run_id):
        return
    _clear_pause_request(ctx.run_id)
    # Direct runs have no chunks — the counters stay 0 (UI shows a plain
    # "Run paused" divider for the phase=="direct" variant of the event).
    if ctx.chat_id:
        _persist_pause_state(chat_id=ctx.chat_id, run_id=ctx.run_id,
                             chunks_done=0, chunks_remaining=0, written_files=[])
    yield await ctx.emit({
        "type": "run_paused_manual", "run_id": ctx.run_id, "phase": phase,
        "chunks_done": 0, "chunks_remaining": 0,
    })
    _resume_sig = _get_resume_signal(ctx.run_id)
    _abort_sig = _get_abort_during_pause_signal(ctx.run_id)
    _chat_ev = await _get_abort_event(ctx.chat_id) if ctx.chat_id else None
    _wait_tasks = [
        spawn(_resume_sig.wait()),
        spawn(_abort_sig.wait()),
    ]
    if _chat_ev is not None:
        _wait_tasks.append(asyncio.create_task(_chat_ev.wait()))
    _pending_wait = (await asyncio.wait(
        _wait_tasks, return_when=asyncio.FIRST_COMPLETED,
    ))[1]
    for _tw in _pending_wait:
        _tw.cancel()
    if _pending_wait:
        await asyncio.gather(*_pending_wait, return_exceptions=True)
    # Consume the resume signal — a second pause of the same run must not
    # wake up immediately (same fix as the Duo chunk loop).
    _resume_sig.clear()
    if ctx.chat_id:
        _clear_pause_state(ctx.chat_id)
    if _abort_sig.is_set() or (_chat_ev is not None and _chat_ev.is_set()):
        outcome.continue_run = False
        yield await ctx.emit({"type": "status",
                              "content": "⏹ Aborted during pause — direct run ended."})
        return
    yield await ctx.emit({
        "type": "run_resumed_manual", "run_id": ctx.run_id, "phase": phase,
        "chunks_done": 0, "chunks_remaining": 0,
    })


async def _inject_direct_steers(ctx, stream_msgs: list, vision_ok: bool = False):
    """A2 (2026-11-11): drain the steer queue at a DIRECT-run boundary.

    The queue (`/api/run/{run_id}/steer`) used to stay undrained on a pure
    direct run without tools — the text only reached the model via the
    frontend's after-run queue as a NEW message. Direct runs call this at
    the round boundary (before the next model POST): each queued steer is
    announced with the shared `steer` card event and appended to
    `stream_msgs` as an "[OWNER STEER]" user message the next round sees.

    Steer images ride only when the final model is multimodal — the pure
    direct path has no vision upgrade, so a text-only model would 500 on
    the raw parts (same conservative drop policy as the coder's raw mode).
    """
    _items = _drain_steer_messages(ctx.run_id)
    if not _items:
        return
    for _st in _items:
        _text = str(_st.get("text") or "")
        _imgs_all = [str(b).strip() for b in (_st.get("images") or [])
                     if str(b or "").strip()]
        _imgs = _imgs_all if vision_ok else []
        yield await ctx.emit({"type": "steer", "content": _text,
                              "images": len(_imgs_all)})
        if len(_imgs) < len(_imgs_all):
            yield await ctx.emit({
                "type": "status",
                "content": (f"⚠ {len(_imgs_all) - len(_imgs)} steer image(s) "
                            "dropped — the direct model is text-only."),
            })
        stream_msgs.append(_build_steer_user_message(_text, _imgs))
    yield await ctx.emit({"type": "status",
                          "content": "➡ Steer applied at the model boundary."})


async def run_direct(ctx):
    """Direct-Mode handler for simple/trivial complexity."""
    if ctx.complexity in ("trivial", "simple"):
        _available = list(ctx.models_cache) if ctx.models_cache else [ctx.pipeline.agents["direct"].model]
        _vision_mdl_name = ctx.vision_cfg.get("model", "") if ctx.prepro_success else ""
        if ctx.prepro_success:
            _direct_model = ctx.registry_get("direct")
            if (not _direct_model
                    or _direct_model not in _available
                    or _direct_model == _vision_mdl_name):
                _non_vision = [m for m in _available if m != _vision_mdl_name]
                _direct_model = _pick_direct_model(
                    ctx.complexity, ctx.effective_task_type,
                    _non_vision if _non_vision else _available
                )
        else:
            _direct_model = _pick_direct_model(ctx.complexity, ctx.effective_task_type, _available)

        _effective_task_for_vision = "vision" if (ctx.images and not ctx.image_description) else ctx.effective_task_type
        if ctx.images and not ctx.image_description:
            _chosen_caps = ctx.model_profile(_direct_model) if ctx.model_profile else {}
            if not bool(_chosen_caps.get("vision", False)):
                _vision_alt = _pick_direct_model(ctx.complexity, "vision", _available)
                if _vision_alt and _vision_alt != _direct_model:
                    _direct_log.info(
                        "[Direct-Vision] %r not multimodal — switching to %r for image request",
                        _direct_model, _vision_alt,
                    )
                    _direct_model = _vision_alt

        # P9 (2026-10-05): an explicit model selection rides the run body
        # as direct_model (the Telegram gateway's /setModel) — the user's
        # choice wins over the complexity auto-pick.
        _user_direct = str(ctx.settings.get("direct_model") or "").strip()
        if _user_direct:
            _direct_log.info(
                "[Direct] model override from run body: %s", _user_direct)
            _direct_model = _user_direct

        # no-hardcoded-models fail-fast: an empty selection means nothing
        # is downloaded/configured — say so instead of a cryptic loader
        # error (2026-10-05).
        if not str(_direct_model or "").strip():
            _direct_log.error("[Direct] no model available/downloaded")
            yield await ctx.emit({"type": "error",
                                  "content": "No model is downloaded or "
                                             "configured. Download a GGUF "
                                             "(setup_models.bat) or pick "
                                             "one in the UI."})
            return

        _preload_ev = await ctx.maybe_preload(_direct_model)
        if _preload_ev:
            yield _preload_ev

        agent = ctx.pipeline.agents["direct"]
        sys_p = ctx.get_effective_prompt_with_override("direct", ctx.active_preset, ctx.use_learned)
        # DATE-FIX (2026-10-06, telegram): the time note moved to the LAST
        # position of the system prompt (see append before make_messages) -
        # mid-prompt it sat before the tool/websearch notes and the small
        # direct models ignored it, confidently claiming "tomorrow" was a
        # year in the future.
        # SOUL INJECTION (2026-10-05, user: 'Soul soll auch im direct sein'):
        # the soul's style/stance/convictions prepended to the system prompt.
        # style_only=True keeps it compact (no full immutable text). Duo does
        # NOT get soul (user decision) — agentic runs are task-focused.
        if ctx.pipeline_soul:
            try:
                _soul_layer = ctx.build_soul_prompt_layer(ctx.pipeline_soul, style_only=True)
                if _soul_layer:
                    sys_p = _soul_layer + "\n\n" + sys_p
            except (AttributeError, TypeError, ValueError) as _soul_err:
                _direct_log.warning("[SOUL] prompt layer build failed: %s", _soul_err)
        direct_input = ctx.user_input
        if ctx.image_description:
            direct_input = ctx.user_input + f"\n\n[Image description from vision model]:\n{ctx.image_description}"

        _direct_caps = ctx.model_profile(_direct_model) if ctx.model_profile else {}
        _direct_vision = bool(_direct_caps.get("vision", False))
        _direct_images = ctx.images if (_direct_vision and ctx.images) else ctx.effective_images
        if not _direct_vision and _direct_images:
            # P9-override consistency (2026-10-06, live 11:45): the chat_run
            # gate judged the REGISTRY direct model while the P9 override
            # picked the FINAL model here. A non-vision final model must
            # never carry raw parts — the POST would 500 on a projector-less
            # slot ("image input is not supported"). The description-text
            # path (image_description) stays the carrier instead.
            _direct_log.info(
                "[Direct-Vision] final model %r not multimodal — dropping "
                "%d raw image part(s) from the request", _direct_model,
                len(_direct_images))
            _direct_images = []
        if _direct_vision and ctx.images and not ctx.image_description:
            _direct_log.debug(
                "[Direct-Vision] %r multimodal — %d raw images directly to the model",
                _direct_model, len(ctx.images),
            )
        # DIRECT-CHAT-TOOLS (2026-08-31): tiered tool use in direct chat.
        # The simple/direct mode can call real file/code/python/web tools via
        # the unified ToolLoop. Pure knowledge/conversation stays a plain token
        # stream (the model decides; no tool call => no round trip).
        _direct_tools_enabled = bool(ctx.settings.get("direct_tools_enabled", True))
        _direct_tier = str(ctx.settings.get("direct_tools_tier", "readonly") or "readonly").strip().lower()
        if _direct_tier == "websearch":  # accepted alias for the UI's "Websearch" label
            _direct_tier = "readonly"
        _direct_tool_mode = {
            "readonly": "direct",
            "research": "direct_research",
            "python": "direct_python",
            "full": "direct_full",
        }.get(_direct_tier, "")
        _direct_tc = _direct_caps.get("tool_call") is not False
        _direct_tools_active = bool(_direct_tool_mode) and _direct_tools_enabled and _direct_tc
        # tier="off" (or any unrecognised value) = total pure chat: no tool
        # loop AND no legacy websearch mini-round / inject in the direct chat.
        _direct_tier_off = not bool(_direct_tool_mode)

        # P2 + DIRECT-WS-TOOLCALLS (2026-08-27): Web search in direct chat.
        # Research mode: the model may call web_search/web_fetch as real tool
        # calls — active when duo_websearch_enabled OR pipeline_websearch_enabled
        # (default on) and SearXNG is reachable. pipeline_websearch_enabled also
        # serves as fallback injection if no tool call happened.
        # When the direct tool loop is active, web is part of its tool set and
        # the legacy mini-round / inject are skipped (avoid double search).
        _direct_ws_tool = bool(
            (ctx.settings.get("duo_websearch_enabled", False)
             or ctx.settings.get("pipeline_websearch_enabled", False))
            and ctx.websearch_available
        ) and not _direct_tools_active and not _direct_tier_off
        _direct_ws_inject = bool(
            ctx.settings.get("pipeline_websearch_enabled", False) and ctx.websearch_available
        ) and not _direct_tools_active and not _direct_tier_off
        if (_direct_ws_tool or (_direct_tools_active and ctx.websearch_available)) \
                and "WEB SEARCH IS AVAILABLE" not in sys_p:
            sys_p += _DIRECT_WS_NOTE
        if _direct_tools_active:
            sys_p += _direct_tools_note(_direct_tool_mode,
                                        ws_ok=bool(ctx.websearch_available))
            if (str((getattr(ctx, "settings", {}) or {}).get("source") or "").startswith("telegram")
                    and _direct_tool_mode == "direct_full"):
                sys_p += (
                    "\n\nFILE OUTPUT: when the user asks for a file or webpage, "
                    "write it with write_file into the workspace and queue it "
                    "with send_file. Do not paste the complete file content into "
                    "the chat. Only say it was sent after send_file returned "
                    "[FILE_QUEUED]."
                )
        # DATE-FIX (2026-10-06): LAST position + explicit tomorrow anchor +
        # a direct anti-hallucination instruction - small models weight the
        # prompt tail most and this is exactly the failure that produced
        # "Oktober 2026 liegt mehr als ein Jahr in der Zukunft".
        _now_dt = datetime.now()
        _tomorrow_dt = datetime.fromtimestamp(_now_dt.timestamp() + 86400)
        sys_p += (
            "\n\n=== TODAY (authoritative, set by the system) ===\n"
            f"Today is {_now_dt.strftime('%A, %Y-%m-%d')}. "
            f"Tomorrow is {_tomorrow_dt.strftime('%A, %Y-%m-%d')}. "
            f"Current local time: {_now_dt.strftime('%H:%M')}.\n"
            "NEVER claim a date is in the future or the past without checking "
            "this section. If asked about tomorrow, use this exact date. "
            "get_datetime() returns the same info as a tool call.\n"
            "\n=== LANGUAGE ===\n"
            "ALWAYS answer in the SAME LANGUAGE the user's message is written "
            "in (German question -> German answer). Match the user's language "
            "exactly, including on tool-only turns."
        )
        messages = ctx.make_messages(ctx.pipeline, sys_p, direct_input, _direct_images, True, True, cached_mem_ctx=ctx.pipeline_mem_ctx, cached_sess_msgs=ctx.pipeline_sess_msgs)
        # FIX (2026-09-01): emit the "Answer" agent event BEFORE the direct
        # tools loop. Otherwise the ToolLoop streams its token events before the
        # frontend has an S.curAgent bubble (appendToken drops tokens when
        # curAgent is null) → the answer was lost, the bubble stayed empty, and
        # the run ended "without error".
        yield await ctx.emit({"type": "agent", "content": "Answer", "model": _direct_model})
        _ws_used = False
        _tool_content = ""
        _tool_final_msgs = None
        _dt_result = None
        if _direct_tools_active:
            _dt_result = _DirectToolsResult()
            # LIVE-STREAM (2026-09-01): pass ToolLoop events through immediately —
            # previously _run_direct_tools collected all events and emitted them
            # only after the loop ended (UI showed nothing during the run).
            async for _ev in _run_direct_tools(
                    ctx, _direct_model, messages, _direct_tool_mode, _dt_result,
                    vision=bool(_direct_images)
                    or (bool(ctx.settings.get("duo_coder_always_vision", False))
                        and not bool(ctx.settings.get("duo_coder_images_off", False)))):
                yield await ctx.emit(_ev)
            _tool_content = _dt_result.content
            _tool_final_msgs = _dt_result.final_msgs
        elif _direct_ws_tool:
            _ws_events, _direct_msgs2, _ws_used = await _direct_websearch_tool_round(
                ctx, _direct_model, messages,
                ctx.get_num_ctx(_direct_model, "direct") or 8192,
            )
            for _ev in _ws_events:
                yield _ev
            if _ws_used:
                messages = _direct_msgs2
        if _direct_ws_inject and not _ws_used:
            _direct_ws_query = ctx.extract_ws_query(ctx.user_input)
            if _direct_ws_query:
                yield await ctx.emit({"type": "status",
                                      "content": f"🔍 Web search: {_direct_ws_query[:60]}…"})
                _direct_ws_result = await ctx.safe_web_search(
                    _direct_ws_query,
                    max_results=3,
                    phase="direct",
                )
                if _direct_ws_result.startswith("[web_search:"):
                    yield await ctx.emit({
                        "type": "status",
                        "content": f"⚠ Web search fallback: {_direct_ws_result}",
                    })
                else:
                    direct_input = (
                        direct_input
                        + f"\n\n[Web search results]\n{_direct_ws_result}\n[End search results]\n"
                    )
                    messages = ctx.make_messages(ctx.pipeline, sys_p, direct_input, _direct_images, True, True, cached_mem_ctx=ctx.pipeline_mem_ctx, cached_sess_msgs=ctx.pipeline_sess_msgs)
                    yield await ctx.emit({"type": "status", "content": "✓ Web search complete"})
        _direct_temp   = agent.temperature
        _direct_tokens = agent.max_tokens
        # AGENT-THINKING (2026-08-19): Per-agent switch from AgentConfig.
        _direct_think = bool(getattr(agent, "thinking", False))
        _direct_budget = int(getattr(agent, "thinking_budget", 0) or 0)
        if _direct_think:
            _direct_tokens = _direct_tokens + _direct_budget
        # OUTPUT-CAP (2026-08-27): max_tokens never above the effective context —
        # otherwise the answer is silently truncated (finish_reason=length).
        _direct_ctx_cap = ctx.get_num_ctx(_direct_model, "direct") or 8192
        _direct_tokens = min(int(_direct_tokens), max(256, int(_direct_ctx_cap)))
        parts, t = [], time.time()
        _pause_aborted = False   # A1: an abort during the pause gate ends the run
        if _direct_tools_active and _tool_content:
            # ToolLoop already streamed the answer live — reuse it for persistence.
            parts = [_tool_content]
        else:
            _d_ct_sum = _d_pt_sum = _d_ca_sum = 0
            try:
                # _pick_direct_model(complex, ...) → qwen3:8b → 131072 default ctx → ~8GB KV-Cache → OVERFLOW
                _stream_msgs = messages
                _usage: dict = {}   # RUN-STATS: filled by usage_out (llama include_usage)
                if _direct_tools_active and _tool_final_msgs:
                    # Tool loop ran but produced no text (abort / round cap) —
                    # answer with the tool-augmented context instead.
                    _stream_msgs = _tool_final_msgs
                # ROUND LOOP (A1/A2, 2026-11-11): each iteration is one model
                # round of the pure direct stream. At the boundaries between
                # rounds the runner consumes /pause (suspend + resume) and
                # drains the steer queue (injection into the next prompt) —
                # the boundaries Duo/chunk runs always had. A steer that
                # arrives DURING a round gets a bounded continuation round
                # (assistant answer + steer user message) instead of only
                # the after-run frontend queue.
                _direct_rounds = 0
                while True:
                    # A1 boundary: /pause before the next model round
                    _pg = _PauseGateResult()
                    async for _gev in _direct_manual_pause_gate(ctx, _pg):
                        yield _gev
                    if not _pg.continue_run:
                        _pause_aborted = True
                        break
                    # A2 boundary: steers queued so far ride into this prompt
                    async for _sev in _inject_direct_steers(ctx, _stream_msgs,
                                                            vision_ok=_direct_vision):
                        yield _sev
                    _round_tok_start = len(parts)
                    if _direct_think:
                        # Thinking mode: stream content and reasoning separately.
                        async for _cont, _thk in ctx.pipeline.ollama.chat_stream(
                                _direct_model, _stream_msgs, _direct_temp, _direct_tokens,
                                ctx=ctx.get_num_ctx(_direct_model, "direct") or 8192,
                                no_cache=ctx.prepro_success,
                                think=True,
                                thinking_budget=_direct_budget or None,
                                split_thinking=True,
                                usage_out=_usage):
                            if ctx.aborted(): break
                            # A1 boundary: pause at a token chunk — the gate
                            # waits here, the upstream generator suspends at
                            # its yield and llama-server backpressures.
                            _pg = _PauseGateResult()
                            async for _gev in _direct_manual_pause_gate(ctx, _pg):
                                yield _gev
                            if not _pg.continue_run:
                                _pause_aborted = True
                                break
                            if _thk:
                                yield await ctx.emit({"type": "thinking_token", "content": _thk})
                            if _cont:
                                parts.append(_cont)
                                yield await ctx.emit({"type": "token", "content": _cont})
                    else:
                        # LEAK-FIX (2026-11-02): models whose chat template reasons
                        # BY DEFAULT (LFM3, and any "thinking" model loaded without
                        # the per-agent switch) shipped their reasoning inside
                        # `content` - the answer started with "Also note system
                        # says ...". Ask for the split even here and drop the
                        # reasoning: with the switch off it is not part of the
                        # answer.
                        async for _tok, _thk_off in ctx.pipeline.ollama.chat_stream(
                                _direct_model, _stream_msgs, _direct_temp, _direct_tokens,
                                ctx=ctx.get_num_ctx(_direct_model, "direct") or 8192,
                                no_cache=ctx.prepro_success,
                                think=False,
                                split_thinking=True,
                                usage_out=_usage):
                            if ctx.aborted(): break
                            # A1 boundary: pause at a token chunk (see above).
                            _pg = _PauseGateResult()
                            async for _gev in _direct_manual_pause_gate(ctx, _pg):
                                yield _gev
                            if not _pg.continue_run:
                                _pause_aborted = True
                                break
                            if not _tok:
                                continue
                            parts.append(_tok)
                            yield await ctx.emit({"type": "token", "content": _tok})
                    if _pause_aborted or ctx.aborted():
                        break
                    # RUN-STATS: accumulate the round's real usage — the emit
                    # below stays ONE usage_meta event even with continuation
                    # rounds (the ledger adds per event).
                    _d_ct = int(_usage.get("completion_tokens") or 0)
                    if _d_ct > 0:
                        _d_det = _usage.get("prompt_tokens_details") or {}
                        _d_tim = _usage.get("timings") or {}
                        _d_ca = int((_d_det.get("cached_tokens") if isinstance(_d_det, dict) else 0)
                                    or _usage.get("cached_tokens") or 0
                                    or (_d_tim.get("cache_n") if isinstance(_d_tim, dict) else 0) or 0)
                        _d_ct_sum += _d_ct
                        _d_pt_sum += int(_usage.get("prompt_tokens") or 0)
                        _d_ca_sum += _d_ca
                        _usage.clear()
                    _direct_rounds += 1
                    # A2 continuation boundary: steers that arrived DURING the
                    # round get another round (bounded); without a queued steer
                    # this stays the single-round fast path it always was.
                    if _direct_rounds > _DIRECT_STEER_MAX_CONTINUATIONS:
                        break
                    if not _steer_queue_view(ctx.run_id):
                        break
                    _round_text = "".join(parts[_round_tok_start:]).strip()
                    if _round_text:
                        # the model must see its own answer before the steer
                        _stream_msgs.append({"role": "assistant", "content": _round_text})
            except Exception as _direct_err:
                _err_tok = f"[Direct error: {str(_direct_err)[:120]}]"
                parts.append(_err_tok)
                yield await ctx.emit({"type": "token", "content": _err_tok})
            # RUN-STATS (2026-10-23): real usage for the per-answer stats line —
            # the same include_usage pattern the pipeline wrapper uses. The model
            # rides run_meta so run records and the done event attribute direct
            # runs instead of showing the registry fallback only.
            if _d_ct_sum > 0:
                await ctx.emit({"type": "run_meta", "models": {"direct": str(_direct_model)}})
                await ctx.emit({"type": "usage_meta", "phase": "direct",
                                "timer_key": "direct_chat",
                                "completion_tokens": _d_ct_sum,
                                "prompt_tokens": _d_pt_sum,
                                "cached_tokens": _d_ca_sum})
        content = "".join(parts).strip()  # REMOVED: implicit _re.sub() — Thinking now explicitly controlled
        # Content (z.B. max_tokens/thinking_budget durch Reasoning verbraucht),
        if _direct_think and not content and not ctx.aborted():
            _hint = ("[Note: The model only thought (🧠) but did not deliver an answer — "
                     "the thinking budget or max_tokens were consumed by reasoning. "
                     "Increase think-budget/max_tokens or disable thinking.]")
            yield await ctx.emit({"type": "token", "content": _hint})
            parts.append(_hint)
            content = _hint
        yield await ctx.emit({"type": "agent_done", "elapsed": round(time.time() - t, 1)})
        ctx.memory.add_to_session("user", ctx.user_input)
        ctx.memory.add_to_session("assistant", content)
        if getattr(ctx, "chat_id", None):
            try:
                from context.chat import _mutate_chat_context

                def _persist_direct_session(cdict):
                    cdict["session"] = ctx.memory.get_session_messages(
                        limit=40, user_cap=10**6, assistant_cap=10**6,
                    )
                    cdict["ts"] = time.time()

                _mutate_chat_context(ctx.chat_id, _persist_direct_session)
            except Exception as _persist_exc:
                _direct_log.warning(
                    "[Direct] session persist failed (chat=%s): %s",
                    ctx.chat_id, _persist_exc,
                )
        # A1: an abort-during-pause signals through the pause gate, not the
        # run abort event — the run still ends as "aborted".
        _direct_stop_reason = ("aborted" if (ctx.aborted() or _pause_aborted)
                               else "completed")
        # SEND-FILE (direct mode): a file the model wrote in this run can be
        # queued with send_file, or auto-picked when exactly one deliverable
        # exists. The gateway uploads it before the done frame.
        if (_direct_stop_reason == "completed"
                and _dt_result is not None
                and str((getattr(ctx, "settings", {}) or {}).get("source") or "").startswith("telegram")):
            try:
                from core.send_files import choose_deliverables as _choose_sf
                _deliver_files = _choose_sf(
                    getattr(_dt_result, "send_files", []),
                    getattr(_dt_result, "written_files", []),
                    str(getattr(ctx, "workspace", "") or ""))
            except (ImportError, TypeError, ValueError):
                _deliver_files = list(getattr(_dt_result, "send_files", []))
            for _sfp in _deliver_files:
                _direct_log.info("[SEND-FILE] direct delivery: %s", _sfp)
                yield await ctx.emit({
                    "type": "send_file",
                    "path": _sfp,
                    "workspace": str(getattr(ctx, "workspace", "") or ""),
                })
        yield await ctx.emit(ctx.done_event(round(time.time() - ctx.t_total, 1), _direct_stop_reason, **ctx.collect_done_metrics()))
        try:
            from tools.runner import _files_written_by_run as _fw_cleanup
            _fw_cleanup.pop(str(ctx.run_id), None)
        except (ImportError, AttributeError, TypeError):
            _direct_log.debug(
                "[SEND-FILE] written-file registry cleanup failed",
                exc_info=True)
        ctx.unregister_abort(ctx.run_id)
        ctx.unregister_step_skip(ctx.run_id)  # P7 FIX: was missing
        # A1 (2026-11-11): drop a leftover pause/resume state — a /pause that
        # arrived after the last boundary (or on a tool-loop round) used to
        # keep the flag pending until the 2h registry sweep.
        _cleanup_pause_state(ctx.run_id)
        spawn(_refresh_judge_keepalive())
        if ctx.settings.get("pin_direct_after_response", False):
            async def _pin_direct_bg():
                try:
                    await _bk_pin(_direct_model, num_ctx=ctx.get_num_ctx(_direct_model, "direct"))
                except Exception:
                    pass
            spawn(_pin_direct_bg())
        run_count = ctx.increment_run_counter()
        def _observed(t):
            if t.exception():
                _direct_log.warning("background task failed: %s", t.exception())
        _t_ratings = asyncio.create_task(run_peer_ratings(
            run_id=ctx.run_id,
            user_input=ctx.user_input,
            outputs={"direct": content},
            use_learned=ctx.use_learned,
            rating_mode="direct",
            has_images=bool(ctx.images),
        ))
        _t_ratings.add_done_callback(_observed)
        _t_soul = asyncio.create_task(ctx.maybe_trigger_soul_evolution(run_count))
        _t_soul.add_done_callback(_observed)
        return

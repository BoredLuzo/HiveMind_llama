"""Core API-Router — Abort, Resume, Tool-Exec."""
import logging
import os
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from infra.run_control import (
    _get_abort_event, _step_skip_event, _abort_event,
    set_user_answer, _pause_events,
    request_graceful_stop, _run_abort_registry,
    request_pause_after_chunk, signal_resume, is_pause_requested,
    signal_abort_during_pause, _RESUME_SIGNALS, is_pause_pending,
)
from infra.ask_user_governor import (
    cancel_timeout as _gov_cancel_timeout,
    is_timeout_answer_sent as _gov_timeout_sent,
    clear_throttle_triggered as _gov_clear_throttle,
    clear_throttle_state as _gov_clear_throttle_state,
)
from core.state import settings
import core.state as _state
from context.pause_state import load_pause_state
from utils.tool import parse_tool_args as _parse_tool_args
from tools.errors import tool_error_response as _tool_error_response, parse_tool_error as _parse_tool_error
from tools.runner import _run_inline_tool

logger = logging.getLogger("hivemind.server")

router = APIRouter(prefix="", tags=["Core"])


@router.post("/abort")
async def abort_stream(req: Request):
    params = req.query_params
    chat_id = params.get("chat_id", "")
    silent = params.get("silent", "false").lower() == "true"

    if not chat_id:
        return JSONResponse({"error": "chat_id fehlt"}, status_code=400)

    ev = await _get_abort_event(chat_id)
    ev.set()

    logger.info("[abort] chat=%s silent=%s", chat_id, silent)

    if silent:
        return JSONResponse({"ok": True, "mode": "silent_kill"})
    else:
        return JSONResponse({"ok": True, "mode": "resume_saved"})


@router.post("/api/run/{run_id}/resume")
async def resume_run(run_id: str, req: Request):
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "JSON-Body erwartet"}, status_code=400)
    answer = body.get("answer", "")
    if not answer:
        return JSONResponse({"error": "Field 'answer' is required"}, status_code=400)
    if run_id not in _pause_events:
        return JSONResponse({"error": "Run not paused or not found"}, status_code=404)
    _gov_cancel_timeout(run_id)
    if _gov_timeout_sent(run_id):
        return JSONResponse({"error": "auto_answer_already_sent"}, status_code=409)
    _gov_clear_throttle_state(run_id)
    set_user_answer(run_id, answer)
    return {"status": "resumed", "run_id": run_id}


@router.post("/abort/step/{run_id}")
async def abort_step(run_id: str):
    ev = _step_skip_event(run_id)
    if ev:
        ev.set()
        return {"ok": True, "run_id": run_id, "action": "step_skip"}
    return JSONResponse({"ok": False, "reason": "run_id not found"}, status_code=404)


@router.post("/pause/{run_id}")
async def pause_run(run_id: str):
    if run_id not in _run_abort_registry:
        return JSONResponse({"ok": False, "reason": "run_id not found or not active"},
                            status_code=404)
    await request_pause_after_chunk(run_id)
    return {"status": "pause_requested", "run_id": run_id}


@router.post("/resume/{run_id}")
async def resume_paused_run(run_id: str):
    success = signal_resume(run_id)
    if not success:
        return JSONResponse({"error": "no active pause for this run"},
                            status_code=409)
    return {"status": "resumed", "run_id": run_id}


@router.post("/abort/graceful/{run_id}")
async def abort_graceful(run_id: str):
    if run_id not in _run_abort_registry:
        return JSONResponse({"ok": False, "reason": "run_id not found or not active"}, status_code=404)
    await request_graceful_stop(run_id)
    if is_pause_pending(run_id):
        signal_abort_during_pause(run_id)
        return {"status": "graceful_stop_during_pause_aborted", "run_id": run_id}
    return {"status": "graceful_stop_requested", "run_id": run_id}


@router.post("/abort/{run_id}")
async def abort_run(run_id: str):
    if is_pause_requested(run_id) or run_id in _RESUME_SIGNALS:
        signal_abort_during_pause(run_id)
        return {"ok": True, "run_id": run_id, "action": "abort_during_pause"}
    ev = _abort_event(run_id)
    if ev:
        ev.set()
        return {"ok": True, "run_id": run_id, "action": "abort"}
    return JSONResponse({"ok": False, "reason": "run_id not found"}, status_code=404)


@router.get("/pause-state/{chat_id}")
async def get_pause_state(chat_id: str):
    ps = load_pause_state(chat_id)
    if ps is None:
        return {"active": False}
    return {"active": True, **ps}


@router.post("/internal/tool/exec")
async def internal_tool_exec(req: Request):
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "Invalid JSON body"}, status_code=400)

    name = str(body.get("name", "") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Missing tool name"}, status_code=400)

    args = body.get("args", {})
    if not isinstance(args, dict):
        return JSONResponse({"ok": False, "error": "args must be an object"}, status_code=400)

    tool_mode = str(body.get("tool_mode", "mcp_agent") or "mcp_agent")
    include_websearch = bool(body.get(
        "include_websearch",
        bool(settings.get("duo_websearch_enabled", False)) and bool(_state._WEBSEARCH_AVAILABLE),
    ))
    # SECURITY: workspace_lock NEVER comes from the request body. It is
    # derived server-side from the active configuration, so an attacker
    # cannot weaken the workspace-containment checks of the tools.
    _ws_cfg = str(settings.get("workspace") or "").strip()
    _ws_env = os.environ.get("HIVEMIND_WORKSPACE", "").strip()
    workspace_lock = str(Path(_ws_cfg or _ws_env or ".").expanduser().resolve())
    # SECURITY (optional): HIVEMIND_INTERNAL_TOKEN enforces a shared secret
    # for this endpoint. Without the variable set, the CSRF origin guard applies.
    _internal_token = os.environ.get("HIVEMIND_INTERNAL_TOKEN", "").strip()
    if _internal_token:
        import hmac
        _sent = str(req.headers.get("x-hivemind-internal-token") or "")
        if not _sent or not hmac.compare_digest(_sent, _internal_token):
            return JSONResponse({"ok": False, "error": "Missing or invalid internal token"}, status_code=403)
    model_for_limits = str(body.get("model_for_limits", "") or body.get("model", "") or "")

    tool_args = dict(args)
    if model_for_limits:
        tool_args["__model__"] = model_for_limits

    try:
        from tools.runner import _external_dispatch as _ext_cv
        _ext_token = _ext_cv.set(True)
        try:
            result = await _run_inline_tool(
                name,
                tool_args,
                workspace_lock=workspace_lock,
                tool_mode=tool_mode,
                include_websearch=include_websearch,
            )
        finally:
            _ext_cv.reset(_ext_token)
        terr = _parse_tool_error(result)
        return {"ok": terr is None, "result": result, "tool_error": terr}
    except Exception as e:
        return JSONResponse(
            {
                "ok": False,
                "error": f"{type(e).__name__}: {str(e)[:220]}",
                "result": _tool_error_response(
                    "TOOL_EXEC_EXCEPTION",
                    f"{type(e).__name__}: {str(e)[:220]}",
                    tool=name,
                    mode=tool_mode,
                    retryable=False,
                ),
            },
            status_code=500,
        )

# -*- coding: utf-8 -*-


import json
import logging
import time

logger = logging.getLogger("hivemind.run_audit")

_RUN_AUDIT_CAP = 40


def record_run_audit(chat_id: str, entry: dict) -> None:


    if not chat_id:
        return
    try:
        from context.chat import _mutate_chat_context

        _rec = {
            "ts": time.time(),
            **(entry or {}),
        }
        def _append(ctx: dict):
            _audit = ctx.get("run_audit") or []
            if not isinstance(_audit, list):
                _audit = []
            _audit.append(_rec)
            ctx["run_audit"] = _audit[-_RUN_AUDIT_CAP:]

        _mutate_chat_context(chat_id, _append)
    except Exception as _e:
        logger.debug("[RUN-AUDIT] Append failed (chat=%s): %s", chat_id, _e)


def load_run_audit(chat_id: str) -> list:
    if not chat_id:
        return []
    try:
        from context.chat import _load_chat_context
        _audit = (_load_chat_context(chat_id) or {}).get("run_audit") or []
        return _audit if isinstance(_audit, list) else []
    except Exception:
        return []


def audit_plan_payload_facts(chat_id: str, run_id: str, *, dtool_msgs: list,
                             has_plan_flag: bool, is_bridge: bool,
                             pre_explore_msgs_count: int, explore_ctx_len: int,
                             plan_content_len: int, thinking_len: int,
                             chunking: bool, is_first_outer_round: bool) -> None:


    roles = [str(m.get("role", "?")) for m in (dtool_msgs or [])]
    payload_text = "".join(str(m.get("content", "")) for m in (dtool_msgs or []))
    record_run_audit(chat_id, {
        "run_id": run_id,
        "event": "coder_payload",
        "dtool_roles": roles,
        "dtool_len": len(dtool_msgs or []),
        "plan_marker_in_payload": "[IMPLEMENTATION PLAN]" in payload_text,
        "plan_briefing_in_payload": "[Plan Briefing" in payload_text,
        "has_plan_flag": bool(has_plan_flag),
        "is_bridge": bool(is_bridge),
        "pre_explore_msgs_count": int(pre_explore_msgs_count),
        "explore_ctx_len": int(explore_ctx_len),
        "plan_content_len": int(plan_content_len),
        "thinking_len": int(thinking_len),
        "chunking": bool(chunking),
        "first_outer_round": bool(is_first_outer_round),
    })


def _dump_for_test(chat_id: str) -> str:
    """Test-Helfer: liefert das rohe Audit-JSON als String (Debug)."""
    try:
        return json.dumps(load_run_audit(chat_id), ensure_ascii=False)
    except Exception:
        return ""

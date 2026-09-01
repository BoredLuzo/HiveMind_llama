"""Resume-Block-Management (aus server.py extrahiert)."""
from __future__ import annotations
import logging
import os
import time
from pathlib import Path

from context.chat import _load_chat_context, _mutate_chat_context

logger = logging.getLogger("hivemind.resume")
build_resume_block = None
load_resume_data = None
_clear_abort_event = None
_is_aborted = None


def init_resume_deps(build_resume_block_fn=None, load_resume_data_fn=None,
                     clear_abort_event_fn=None, is_aborted_fn=None):
    global build_resume_block, load_resume_data, _clear_abort_event, _is_aborted
    if build_resume_block_fn:
        build_resume_block = build_resume_block_fn
    if load_resume_data_fn:
        load_resume_data = load_resume_data_fn
    if clear_abort_event_fn:
        _clear_abort_event = clear_abort_event_fn
    if is_aborted_fn:
        _is_aborted = is_aborted_fn


def _write_resume_block(
    chat_id:          str,
    workspace:        str,
    chunks_total:     int,
    chunks_done:      list[str],
    chunks_remaining: list[dict],
    written_files:    list[str],
    last_summary:     str,
    plan_msgs:        list,
    explore_ctx:      str,
    halt_reason:      str = "user_abort",
    graceful_stop_chunk_index: int | None = None,
):
    def _apply(ctx: dict):
        _block = build_resume_block(
            workspace=workspace,
            chunks_total=chunks_total,
            chunks_done=chunks_done,
            chunks_remaining=chunks_remaining,
            written_files=written_files,
            last_summary=last_summary,
            plan_msgs=plan_msgs,
            explore_ctx=explore_ctx,
        )
        ctx.update(_block)
        ctx["halt_reason"] = halt_reason
        if halt_reason == "graceful_stop":
            ctx["last_chunk_clean_committed"] = True
            if graceful_stop_chunk_index is not None:
                ctx["graceful_stop_after_chunk"] = graceful_stop_chunk_index

    _mutate_chat_context(chat_id, _apply)
    logger.info("[resume] Block saved: %d/%d chunks done, halt_reason=%s, chat=%s",
                len(chunks_done), chunks_total, halt_reason, chat_id)


def _load_resume_block(chat_id: str) -> dict | None:
    ctx = _load_chat_context(chat_id)
    return load_resume_data(ctx)


def _clear_resume_block(chat_id: str):
    _mutate_chat_context(chat_id, lambda ctx: ctx.pop("resume", None))


async def _try_resume(chat_id: str, emit_fn) -> tuple[dict | None, list]:

    await _clear_abort_event(chat_id)
    data = _load_resume_block(chat_id)
    if not data:
        return None, []

    _resume_ctx = str(data.get("explore_ctx", "") or "").strip()
    if len(_resume_ctx) < 30:
        logger.info(
            "[resume] weak explore_ctx (%d chars) — discard resume, re-explore, chat=%s",
            len(_resume_ctx), chat_id,
        )
        _clear_resume_block(chat_id)
        return None, []

    r                = data["resume"]
    chunks_done      = r.get("chunks_done", [])
    chunks_remaining = r.get("chunks_remaining", [])
    _sse = await emit_fn({
        "type": "system",
        "msg": (
            f"▶ Resuming: {len(chunks_done)}/{r.get('chunks_total', '?')} chunks already done.\n"
            f"Done: {', '.join(chunks_done) or '—'}\n"
            f"Continuing with: {chunks_remaining[0].get('title', 'Chunk') if chunks_remaining else '—'}"
        ),
    })
    logger.info("[resume] Starting resume: %d chunks remaining, chat=%s",
                len(chunks_remaining), chat_id)
    return data, ([_sse] if _sse else [])


async def _check_abort_and_maybe_save_resume(
    *,
    chat_id:          str,
    workspace:        str,
    chunks_total:     int,
    chunks_done:      list,
    chunks_remaining: list,
    written_files:    list,
    last_summary:     str,
    plan_msgs:        list,
    explore_ctx:      str,
    emit_fn,
) -> tuple[bool, list]:

    if not _is_aborted(chat_id):
        return False, []
    if chunks_remaining:
        _write_resume_block(
            chat_id          = chat_id,
            workspace        = workspace,
            chunks_total     = chunks_total,
            chunks_done      = chunks_done,
            chunks_remaining = chunks_remaining,
            written_files    = written_files,
            last_summary     = last_summary,
            plan_msgs        = plan_msgs,
            explore_ctx      = explore_ctx,
        )
        _sse = await emit_fn({
            "type": "system",
            "msg":  (
                f"⏸ Interrupted after {len(chunks_done)}/{chunks_total} chunks. "
                f"Resume saved — send a new message to continue."
            ),
        })
        return True, ([_sse] if _sse else [])
    else:
        _clear_resume_block(chat_id)
    return True, []

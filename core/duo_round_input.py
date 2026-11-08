"""Duo round-input build (2026-11-05 split 2.4).

Runs at the top of each chunk-loop round: coder input build (chunking /
first-round / fix-round), mid-run steer drain, read-only header, session
budgeting, message assembly, vision raw-image attach and the `duo_coder`
emit. Verbatim code motion — no logic changes. If an empty chunk should be
skipped, sets `ps.skip_round = True` and returns; the runner does the
`continue` itself (loop control stays in the runner).
"""
from __future__ import annotations

import logging
import time

import httpx

from core.duo.preflight import coder_briefing_text

logger = logging.getLogger("hivemind.duo")


async def _duo_coder_round_input(ctx, ps):
    _di = ps._di
    _n_items = ps._n_items
    _subtask = ps._subtask
    _subtasks = ps._subtasks
    _done_tasks = ps._done_tasks
    _cs = ps._cs
    _written_files = ps._written_files
    critic_issues = ps.critic_issues
    coder_out = ps.coder_out
    _explore_ctx = ps._explore_ctx
    _img_plan = ps._img_plan
    _plan_tracker = ps._plan_tracker
    _coder_caps = ps._coder_caps
    _coder_map_budget = ps.carry["_coder_map_budget"]
    coder_mdl = ps.carry["coder_mdl"]
    _explore_has_contents = ps.carry.get("_explore_has_contents", False)
    final_verdict = ps.carry.get("_final_verdict", "")
    exec_mdl = ps.carry["exec_mdl"]
    _cached_coder_port = ps.carry["_cached_coder_port"]
    _coder_port = ps._coder_port
    _touched_block_persisted = ps._touched_block_persisted
    _duo_coder_sys = ps.carry["_duo_coder_sys"]
    _duo_coder_temp = ps.carry["_duo_coder_temp"]
    _duo_coder_tok = ps.carry["_duo_coder_tok"]
    _coder_dyn_hints = ps.carry["_coder_dyn_hints"]
    _matched_skills = ps.carry["_matched_skills"]
    _plan_thinking = ps.carry.get("_plan_thinking") or ""
    _plan_result = ps.carry.get("_plan_result")
    _inloop_plan_text = ps.carry.get("_inloop_plan_text") or ""
    _plan_content_txt = ps.carry.get("_plan_content_txt") or ""

    from core.duo_helpers import (
        attach_images_to_last_user, build_steer_user_message,
        count_image_parts, is_read_only_request,
    )
    _build_steer_user_message = build_steer_user_message
    from hive_functions.chunking import build_chunk_context
    from hive_functions.ctx_utils import (
        budget_explore_window, budget_session_msgs, extract_known_files,
    )
    from infra.run_control import drain_steer_messages

    # ── Coder ──────────────────────────────────────────────────────
    if _subtask:
        # Chunking mode: ChunkState.prepare_coder_input() handles
        # fix override and context building.
        _chunk_known_files = extract_known_files(
            _explore_ctx,
            cap_chars=_coder_caps.known_files,
            subtask=_subtask,
        )
        _coder_input = _cs.prepare_coder_input(
            user_input=ctx.user_input,
            explore_ctx=_explore_ctx,
            subtask=_subtask,
            di=_di,
            n_items=_n_items,
            all_subtasks=_subtasks,
            explore_cap=_coder_caps.explore_inject,
            static_map_chars=_coder_map_budget,
            known_files=_chunk_known_files,
        )
        if not _coder_input:
            # Fallback: should not happen, but safety net
            _coder_input = build_chunk_context(
                user_input=ctx.user_input,
                explore_ctx=_explore_ctx,
                done_tasks=_done_tasks,
                written_files=_written_files,
                subtask=_subtask,
                di=_di,
                n_items=_n_items,
                critic_issues=critic_issues,
                all_subtasks=_subtasks,
                explore_cap=_coder_caps.explore_inject,
                static_map_chars=_coder_map_budget,
                known_files=_chunk_known_files,
            )
        # O2: Empty chunk (no files, empty subtask) — skip
        if _coder_input is None:
            logger.warning("[CHUNKING] Skipping empty chunk %s/%s", _di + 1, _n_items)
            ps.skip_round = True
            return
    elif _di == 0:
        _coder_input = ctx.user_input
        # IMAGE DESC moved to the coder SYSTEM prompt (2026-10-01):
        # the old round-0 attachment vanished on chunked runs and
        # under compression; the system block covers both paths.
        if _explore_ctx:
            # EXPLORE-CONTENT-TRUTH (2026-09-06): rule text now keys off
            # the same truth flag as the system template choice above.
            # With a static symbol map only, it must NOT claim the
            # files are "pre-explored" (no read_file needed) — contents
            # are not in context, which caused blind write_file
            # overwrites. Also no dangling "below" reference to the
            # map (moved into the system message via repomap pin)
            # anymore.
            if _explore_has_contents:
                _explore_rule = (
                    "WORKSPACE PRE-EXPLORED — the static repo-map and architecture contracts below are confirmed.\n"
                    "The STATIC REPO-MAP section shows file paths, symbols, and imports (deterministic, ground truth).\n"
                    "Use paths from the map directly. Do NOT re-run list_dir or find_files on listed directories.\n"
                    "If a file is already covered by pre-exploration:\n"
                    "  → call edit_file directly (no read_file needed).\n"
                    "Only call read_file if the file was NOT in the pre-exploration results.\n\n"
                )
            else:
                _explore_rule = (
                    "STATIC SYMBOL INDEX ONLY — the list below contains file paths, symbols, and imports, "
                    "NOT the files' contents.\n"
                    "Existing files may contain code you have not seen. BEFORE changing any listed existing file:\n"
                    "  → call read_file on it first.\n"
                    "write_file with full content is ONLY for NEW files. For existing files use "
                    "edit_file(old_text, new_text) AFTER reading the current content.\n\n"
                )
            _MAX_EXPLORE_INJECT = _coder_caps.explore_inject
            _explore_inject = _explore_ctx
            if len(_explore_inject) > _MAX_EXPLORE_INJECT:
                # MARKER-AWARE (2026-08-17): Static map first (up to
                _explore_inject = budget_explore_window(
                    _explore_inject, _MAX_EXPLORE_INJECT, _coder_map_budget,
                )
            _coder_input += f"\n\n[Codebase analysis]:\n{_explore_rule}{_explore_inject}"
    else:
        _issues_txt = "\n".join(f"- {iss}" for iss in critic_issues) if critic_issues else final_verdict
        _coder_input = (
            f"Original request:\n{ctx.user_input}\n\n"
            f"Current code:\n{coder_out}\n\n"
            f"Issues from code reviewer:\n{_issues_txt}\n\n"
            f"Fix ALL listed issues. Write the complete corrected code."
        )

    # ── MID-RUN STEER (chunk boundary, 2026-09-29): drain queued user
    # messages once per round and append them to the coder input — the
    # next model round sees them as user context (ZCode-style steering;
    # the tool loop drains again at its own round boundaries below).
    # SINCE 2026-10-04 steers can carry IMAGES: text-only items go
    # into the coder input as before, image items are appended as
    # their own content-parts user message after _coder_msgs is built.
    _steer_items = drain_steer_messages(ctx.run_id)
    _steer_img_items = []
    _coder_imgs_off = bool(ctx.settings.get("duo_coder_images_off", False))
    # AUDIT (1.3.3): image steers belong to the RAW path only. In
    # preprocess mode the coder is text-only by contract - the old
    # code force-upgraded its slot to vision (~15 s stall + VRAM)
    # behind a mode that promises "every agent stays text-based".
    _steer_imgs_allowed = (
        str(ctx.settings.get("duo_image_mode") or "") != "preprocess")
    if (any(_st["images"] for _st in _steer_items)
            and not _coder_imgs_off and _steer_imgs_allowed):
        # STEER-IMAGE UPGRADE (2026-10-06, live T2): the coder slot
        # may have loaded without the projector — upgrade BEFORE the
        # image parts ride into the next chunk POST. The port MUST
        # be the CODER port (retest live: _find_loaded(exec_mdl)
        # matched the PLANNER slot first when planner==coder and
        # upgraded the wrong one).
        try:
            _vu_port = int(_cached_coder_port or _coder_port or 0)
            if _vu_port:
                from backend.llama_server_manager import manager as _mgr_vu
                yield await ctx.emit({"type": "status", "content":
                    "🖼 image steer received — loading vision support (~15 s)…"})
                await _mgr_vu.upgrade_port_to_vision(_vu_port)
        except (RuntimeError, OSError, httpx.HTTPError) as _vu_err:
            logger.warning("[STEER-IMAGE] vision upgrade failed: %s", _vu_err)
    for _st in _steer_items:
        # STEER CARD (2026-10-06): dedicated event — the UI renders
        # a persistent card, the gateway relays its own phone note.
        yield await ctx.emit({"type": "steer",
                              "content": str(_st["text"] or ""),
                              "images": len(_st["images"] or [])})
    if _steer_items:
        for _st in _steer_items:
            if _st["images"]:
                _steer_img_items.append(_st)
            else:
                # STEER FRAME (2026-10-07): same priority framing as
                # build_steer_user_message (see duo_helpers).
                _coder_input += ("\n\n[OWNER STEER - highest priority, arrived mid-run]\n"
                                 + _st["text"].strip()
                                 + "\nIncorporate this immediately: it overrides your current "
                                   "approach. Act on it with tools in this round if applicable.")

    # RO-DETECT (2026-09-03): decide BEFORE building coder messages.
    # A read-only phrase (e.g. "DO NOT MODIFY") is often only a content
    # constraint inside a real implementation task — in that case the tool
    # round must stay ON. See is_read_only_request() in duo_helpers.py.
    _read_only_task_request = is_read_only_request(ctx.user_input or "")
    if _read_only_task_request:
        # Read-only tasks run WITHOUT tools (see :2163) — never tell the
        # coder to open with a tool call, and forbid textual tool-call
        # syntax. Otherwise a tool-finetuned model free-styles
        # "[TOOL_CALL] …"/"edit_file<argkey>…" that nothing parses.
        from core.duo_helpers import _READONLY_CODER_NOTE
        _exec_header = _READONLY_CODER_NOTE
        if _subtasks:
            # PLAN-DEDUP (2026-09-09): the [Plan — N subtasks] preview is
            # gone — build_chunk_context already embeds the FULL PLAN
            # block with ✓/→/○ badges for chunk runs.
            _coder_input = _exec_header + "\n" + _coder_input
        elif _plan_thinking:
            _brief_txt, _brief_mode, _brief_tok = coder_briefing_text(
                _plan_thinking, _plan_content_txt, None, ctx.settings, exec_mdl)
            logger.warning("[CODER-BRIEFING] mode=%s tokens=%d",
                           _brief_mode, _brief_tok)
            if _brief_txt:
                _coder_input = (
                    _exec_header + "\n" + _coder_input +
                    f"\n\n[YOUR EXECUTION PLAN — IMPLEMENT THIS, DO NOT RE-PLAN]:\n{_brief_txt}"
                )
    elif _subtasks:
        _exec_header = (
            "[EXECUTION MODE — DO NOT CREATE YOUR OWN PLAN]\n"
            "The plan below is ALREADY DECIDED. Your ONLY job is to implement it.\n"
            "Start with a tool call NOW. Do not restate, summarize, or re-plan.\n"
        )
        # PLAN-DEDUP (2026-09-09): preview removed — chunk runs already
        # carry the FULL PLAN block with ✓/→/○ badges from
        # build_chunk_context; a second bare list added nothing.
        _coder_input = _exec_header + "\n" + _coder_input
    elif _plan_thinking:
        _exec_header = (
            "[EXECUTION MODE — DO NOT CREATE YOUR OWN PLAN]\n"
            "The briefing below is ALREADY DONE. Your ONLY job is to IMPLEMENT it.\n"
            "Start with a tool call NOW. Do not restate or summarize it.\n"
        )
        _brief_txt, _brief_mode, _brief_tok = coder_briefing_text(
            _plan_thinking, _plan_content_txt, _subtasks, ctx.settings, exec_mdl)
        logger.warning("[CODER-BRIEFING] mode=%s tokens=%d",
                       _brief_mode, _brief_tok)
        if _brief_txt:
            _coder_input = (
                _exec_header + "\n" + _coder_input +
                f"\n\n[YOUR EXECUTION PLAN — IMPLEMENT THIS, DO NOT RE-PLAN]:\n{_brief_txt}"
            )

    _coder_skipped_images = False
    _sess_budgeted: list = []
    if _di == 0:
        # fixed 600/1200-char truncation.
        _sess_budgeted = budget_session_msgs(
            ctx.pipeline_sess_msgs,
            budget_chars=_coder_caps.session_budget,
            user_cap=_coder_caps.session_user,
            assistant_cap=_coder_caps.session_assistant,
        )
        _coder_msgs = ctx.make_messages(
            ctx.pipeline, _duo_coder_sys, _coder_input, [],
            use_session=True,
            use_memory=False,
            cached_mem_ctx=ctx.pipeline_mem_ctx,
            cached_sess_msgs=_sess_budgeted,  # COMPRESSION-FIX + BUDGET
        )
    else:
        _coder_msgs = [
            {"role": "system", "content": _duo_coder_sys},
            {"role": "user",   "content": _coder_input},
        ]
    # RAW-IMAGES (2026-10-01): a vision-capable coder receives the
    # attached images as OpenAI content parts on its first user
    # message (mode=direct only — preprocess/pipeline already paid
    # for a text description). The payload reaches llama-server
    # untransformed, so parts survive the wire as-is.
    if _img_plan.get("coder") == "raw":
        # MMproj-Precheck (2026-10-01): NEVER send parts to a server
        # without a projector — skip with a UI-visible status instead
        # (a non-multimodal model would answer an image it never saw).
        from backend.llama_manager_utils import resolve_mmproj_strict as _mms
        _mm_ok = _mms(exec_mdl) is not None
        # VISION-TRUTH (2026-10-04): even with a resolved mmproj FILE,
        # a mid-run reload can leave the server running without the
        # projector loaded - check the slot's /props state when known.
        if _mm_ok:
            try:
                from backend.llama_server_manager import manager as _lsm_vgate3
                _ap = int(_cached_coder_port or _coder_port or 0)
                _cslot3 = _lsm_vgate3.get_slot_by_port(_ap) if _ap else None
                if _cslot3 is not None and getattr(_cslot3, "vision_active", None) is False:
                    _mm_ok = False
            except (ImportError, AttributeError, TypeError, ValueError) as _vg_err3:
                logger.debug("[DUO][VISION-GATE] attach check skipped: %s", _vg_err3)
        if _mm_ok:
            _coder_msgs = attach_images_to_last_user(_coder_msgs, ctx.images)
        else:
            _coder_skipped_images = True
            _img_plan["coder"] = "none"
            logger.warning("[DUO] coder '%s' has no mmproj/projector — raw image SKIPPED", exec_mdl)
            yield await ctx.emit({"type": "status",
                "content": "⚠ Image skipped for coder '" + exec_mdl
                           + "': no projector (mmproj) available — the coder answers text-only."})

    # STEER-IMAGES (2026-10-04): image-carrying steers become their
    # own content-parts user message AFTER the base build - they must
    # not be merged into the first user message (that already holds
    # the round-1 image when the plan says raw).
    for _st in _steer_img_items:
        if _coder_imgs_off or not _steer_imgs_allowed:
            yield await ctx.emit({"type": "status", "content":
                "⚠ Image steer dropped - in preprocess mode the coder "
                "stays text-only (switch to Raw image mode for image "
                "steers)."})
            continue
        _coder_msgs.append(_build_steer_user_message(_st["text"], _st["images"]))
        logger.info("[DUO] steer with %d image(s) appended as user parts message",
                    len(_st["images"]))
        _steer_img_items = []

    _prompt_prev = (_coder_input or "")[:500].replace("\n", " ").strip()
    if len(_coder_input or "") > 500:
        _prompt_prev += "…"
    # ── Inject pre-explore touched-file contents for all chunks (was only chunk 1) ──
    if _touched_block_persisted and _di > 0:
        _coder_input = (
            "[Pre-Explore File Contents]\n"
            + _touched_block_persisted
            + "\n\n" + _coder_input
        )
    # IMAGES-IN-REQUEST (2026-10-04): per-round trace - ALSO
    # at 0 once images are attached to the task, so "the image
    # stopped being sent" is measurable instead of looking like a
    # missing log line.
    if getattr(ctx, "images", None):
        logger.info("[DUO] coder round %d/%d: images_in_request=%d plan=planner:%s/coder:%s",
                    _di + 1, _n_items, count_image_parts(_coder_msgs),
                    _img_plan.get("planner"), _img_plan.get("coder"))
    yield await ctx.emit({"type": "duo_coder", "model": coder_mdl, "round": _di + 1,
                      "subtask": _subtask or None, "n_total": _n_items,
                      "prompt_preview": _prompt_prev})
    _parts, _t = [], time.time()
    # ── sync back: everything the runner consumes after this block ──
    ps._coder_input = _coder_input
    ps._coder_msgs = _coder_msgs
    ps._read_only_task_request = _read_only_task_request
    ps._coder_skipped_images = _coder_skipped_images
    ps._coder_imgs_off = _coder_imgs_off
    ps._img_plan = _img_plan
    ps._touched_block_persisted = _touched_block_persisted
    ps._parts = _parts
    ps._t = _t
    ps.carry["_coder_map_budget"] = _coder_map_budget

import asyncio
import logging
from utils.bg_tasks import spawn  # noqa: E402
import time

_pipe_log = logging.getLogger("hivemind.pipeline_runner")
from vision.preprocess import _VISION_POISON_MARKERS


from learning.peer_ratings import run_peer_ratings
from vram.loader import _bk_evict
from vram.loader import _bk_load
from vram.loader import _get_loaded_models_set
from vram.loader import _refresh_judge_keepalive
from core.model_sampling import resolve_agent_sampling
from core.duo_helpers import _apply_thinking_fields


def _resolve_stage_call(ctx, a, model: str, *, mode: str = "") -> tuple[float, bool, int, int, dict]:
    """Resolve (temp, think, budget, max_tokens, sampler) for a pipeline stage."""
    _t, _th, _b, _smp = resolve_agent_sampling(
        model, agent=a, settings=ctx.settings, role=a.key, mode=mode,
    )
    _ctx_tok = ctx.get_num_ctx(model, a.key) or 8192
    _max_tok = min(int(a.max_tokens + _b), max(256, _ctx_tok))
    return _t, _th, _b, _max_tok, _smp


async def _stream_stage_once(
    ctx,
    model: str,
    msgs,
    temp: float,
    max_tok: int,
    *,
    agent_role: str,
    think: bool,
    thinking_budget: int,
    sampler,
    parts_out: list[str],
    no_cache: bool = False,
    error_label: str = "",
    emit_tokens: bool = True,
    strip_inline_think: bool = False,
):
    """Stream one pipeline stage call into parts_out and yield UI SSE events."""
    import contextlib as _ctxlib_pr
    try:
        if think:
            async with _ctxlib_pr.aclosing(
                ctx.pipeline_chat_stream(
                    model, msgs, temp, max_tok,
                    agent_role=agent_role, think=True,
                    thinking_budget=thinking_budget or None,
                    split_thinking=True, no_cache=no_cache, sampler=sampler,
                )
            ) as _p_stream:
                async for _c, _th in _p_stream:
                    if ctx.aborted():
                        break
                    if _th:
                        yield await ctx.emit({"type": "thinking_token", "content": _th})
                    if _c:
                        parts_out.append(_c)
                        if emit_tokens:
                            yield await ctx.emit({"type": "token", "content": _c})
        else:
            _think_open = False
            async with _ctxlib_pr.aclosing(
                ctx.pipeline_chat_stream(
                    model, msgs, temp, max_tok,
                    agent_role=agent_role, think=False,
                    no_cache=no_cache, sampler=sampler,
                )
            ) as _p_stream_nt:
                async for tok in _p_stream_nt:
                    if ctx.aborted():
                        break
                    parts_out.append(tok)
                    if not emit_tokens:
                        continue
                    if strip_inline_think:
                        if not _think_open and "<think>" in tok:
                            before = tok.split("<think>")[0]
                            if before:
                                yield await ctx.emit({"type": "token", "content": before})
                            _think_open = True
                        if _think_open and "</think>" in tok:
                            _think_open = False
                            after = tok.split("</think>")[-1]
                            if after:
                                yield await ctx.emit({"type": "token", "content": after})
                        elif not _think_open and "<think>" not in tok:
                            yield await ctx.emit({"type": "token", "content": tok})
                    else:
                        yield await ctx.emit({"type": "token", "content": tok})
    except Exception as e:
        if error_label:
            err = f"[{error_label} error: {str(e)[:100]}]"
            parts_out.append(err)
            yield await ctx.emit({"type": "token", "content": err})

async def run_pipeline(ctx):
    """Full Hivemind Pipeline mode — multi-agent analysis → synthesis.

    Extracted from server.py run_stream() closure; all closure variables
    are accessed through ``ctx``."""
    vision_agent_images = list(ctx.images) if ctx.images else []

    # Full pipeline
    yield await ctx.emit({"type": "pipeline_start", "content": ctx.user_input})

    _va_parallel_task = None
    # Logik:
    _pinned_models: set[str] = set()
    _restore_keep_alive = str(ctx.settings.get("smart_preload_keep_alive", "10m"))

    # Correct: MAX(GB(seq[i]) + GB(seq[i+1])) = peak SIMULTANEOUS need (active + prefetch).
    _judge_m = ctx.registry.get("judge", "")
    if _judge_m:
        _pipeline_unique = list(dict.fromkeys(
            ctx.registry_get(k) for k in ("analyst","refiner","critic","synthesizer")
            if k not in ctx.skip_agents
        ))
        _judge_gb_pre   = ctx.vram_lookup_gb.get(_judge_m, 2.1)
        _budget_eff_pre = ctx.vram_budget  # Konfigurierbares VRAM-Budget (Default: 7.5GB)

        _judge_base = _judge_m.split(":")[0]
        _judge_in_pipeline = any(m.split(":")[0] == _judge_base for m in _pipeline_unique)

        if not _judge_in_pipeline:
            _seq_gb = [ctx.vram_lookup_gb.get(m, 4.0) for m in _pipeline_unique]
            if len(_seq_gb) >= 2:
                _max_pair_gb = max(_seq_gb[i] + _seq_gb[i+1] for i in range(len(_seq_gb)-1))
            else:
                _max_pair_gb = _seq_gb[0] if _seq_gb else 0.0

            if _max_pair_gb + _judge_gb_pre > _budget_eff_pre:
                try:
                    await _bk_evict(_judge_m)
                except Exception:
                    pass

    try:
        # Release any stale pinned Direct model that is not part of this pipeline
        # so its 30-minute pin cannot block pipeline VRAM eviction.
        try:
            from core import direct_runner as _dr
            _prev_dp = getattr(_dr, "_LAST_DIRECT_PINNED_MODEL", "")
            _pipe_models = {
                ctx.registry_get(k) for k in ("analyst", "refiner", "critic", "synthesizer")
                if k not in ctx.skip_agents
            }
            if _prev_dp and _prev_dp not in _pipe_models:
                await _dr.unpin_previous_direct_model("", evict=True)
        except (ImportError, AttributeError, RuntimeError, OSError, ValueError) as _dp_err:
            _pipe_log.debug("[Pipeline] release previous direct pin skipped: %s", _dp_err)
        await _pin_pipeline_models(ctx, _pinned_models)
        previous    = ""
        analyst_out = ""
        refiner_out = ""
        critic_out  = ""

        soul       = ctx.pipeline_soul
        soul_layer = ctx.build_soul_prompt_layer(soul, style_only=True)

        # ── Vision-Agent ─────────────────────────────────────────────────────
        # Describe-first mode (pipeline_vision_direct=False) keeps every stage
        # text-only even if pipeline_vision_roles still holds prior selections.
        _pipeline_vision_direct = bool(ctx.settings.get("pipeline_vision_direct", False))
        _vision_roles_cfg = dict(ctx.settings.get("pipeline_vision_roles") or {}) if _pipeline_vision_direct else {}
        if _pipeline_vision_direct and not any(_vision_roles_cfg.values()):
            _vision_roles_cfg = {"analyst": True}

        _analyst_vision = _role_vision(ctx, "analyst", 0, vision_agent_images, _vision_roles_cfg)
        _va_model   = ctx.settings.get("vision_agent_model", "") or ctx.pipeline.agents["vision"].model
        _va_has_model = bool(_va_model)
        _va_enabled = (not _analyst_vision) and not getattr(ctx, "image_description", "") and bool(vision_agent_images) and _va_has_model and (
            ctx.settings.get("vision_agent_enabled", False)
            or ctx.effective_task_type in ("vision", "ocr")
            or not _pipeline_vision_direct)
        _va_mode    = ctx.settings.get("vision_agent_mode", "sequential")  # "sequential"|"parallel"
        _vision_agent_out = ""

        async def _run_vision_agent():


            if not vision_agent_images or not _va_model:
                yield {"_va_out": ""}
                return
            import logging as _valog
            _va_logger = _valog.getLogger("hivemind.vision_agent")

            va      = ctx.pipeline.agents["vision"]
            _va_ctx = ctx.get_num_ctx(_va_model, agent_role="vision") or 8192

            _va_img_data = [
                b.split(",", 1)[1] if isinstance(b, str) and b.startswith("data:") and "," in b else b
                for b in vision_agent_images
            ]

            _va_user_content: list = [{"type": "text", "text": f"[USER]\n{ctx.user_input}"}]
            for _b64_orig, _b64 in zip(vision_agent_images, _va_img_data):
                _mime = "image/jpeg"
                if isinstance(_b64_orig, str) and _b64_orig.startswith("data:image/"):
                    _mime = _b64_orig.split(";")[0][5:]  # "data:image/png;base64,..." → "image/png"
                _va_user_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{_mime};base64,{_b64}"},
                })
            _va_msgs = [
                {"role": "system", "content": ctx.vision_agent_prompt},
                {"role": "user",   "content": _va_user_content},
            ]

            yield await ctx.emit({
                "type": "agent", "content": "Vision-Agent",
                "model": _va_model,
                "role": "Analyzes the image directly"
            })
            parts, t = [], time.time()
            try:
                from backend.llama_server_manager import manager as _lsm_va
                _va_port = await _lsm_va.ensure_loaded(_va_model, num_ctx=_va_ctx, vision=True)
                _va_read_s = float(ctx.settings.get("duo_llm_slow_timeout_s", 300))
                import httpx
                from core.llm_transport import stream_chat_chunks as _stream_chat_chunks
                _va_temp, _va_think, _va_budget, _va_smp = resolve_agent_sampling(
                    _va_model, agent=va, settings=ctx.settings, role="vision", mode="sampling_vl",
                )
                _va_payload = {
                    "model":              _va_model,
                    "messages":           _va_msgs,
                    "stream":             True,
                    "temperature":        _va_temp,
                    "top_p":              _va_smp.get("top_p", 0.9),
                    "top_k":              int(_va_smp.get("top_k", 40)),
                    "min_p":              float(_va_smp.get("min_p", 0.0)),
                    "presence_penalty":   float(_va_smp.get("presence_penalty", 0.0)),
                    "repeat_penalty":     float(_va_smp.get("repeat_penalty", _va_smp.get("repetition_penalty", 1.0))),
                    "repetition_penalty": float(_va_smp.get("repetition_penalty", _va_smp.get("repeat_penalty", 1.0))),
                    "max_tokens":         va.max_tokens,
                    "stop":               ["<|im_end|>", "<|endoftext|>", "\n\n\n"],
                    "cache_prompt":       False,
                }
                if _va_smp.get("seed") is not None:
                    _va_payload["seed"] = int(_va_smp["seed"])
                _apply_thinking_fields(
                    _va_payload, _va_think, _va_budget,
                    reasoning_effort=str(_va_smp.get("reasoning_effort") or ""),
                    model_name=_va_model,
                )
                async for _va_d in _stream_chat_chunks(
                    _va_port,
                    _va_payload,
                    timeout=httpx.Timeout(connect=10.0, read=_va_read_s, write=10.0, pool=5.0),
                    max_attempts=3,
                    log_label="Vision-Agent",
                ):
                    if ctx.aborted():
                        break
                    _va_tok = ((_va_d.get("choices") or [{}])[0]
                                   .get("delta", {})
                                   .get("content", "") or "")
                    if _va_tok:
                        parts.append(_va_tok)
                        yield await ctx.emit({"type": "token", "content": _va_tok})
            except Exception as e:
                err = f"[Vision-Agent error: {str(e)[:100]}]"
                parts.append(err)
                yield await ctx.emit({"type": "token", "content": err})

            out = "".join(parts).strip()  # REMOVED: implicit _re.sub() — Thinking now explicitly controlled
            # Trim trailing special tokens
            for _stop in ("<|im_end|>", "<|endoftext|>", "<|"):
                if _stop in out:
                    out = out[:out.index(_stop)].strip()

            # Sanity check: detect KV-cache poisoning (same as vision preprocessing)
            if any(m in out[:120].lower() for m in _VISION_POISON_MARKERS):
                _va_logger.error(
                    f"Vision-agent KV-cache poisoning detected — discarded. "
                    f"First 80 chars: {out[:80]!r}"
                )
                out = ""

            yield await ctx.emit({"type": "agent_done", "elapsed": round(time.time() - t, 1)})
            yield {"_va_out": out}

        if _va_enabled and _va_mode == "sequential" and vision_agent_images and _va_model:
            async for ev in _run_vision_agent():
                if isinstance(ev, dict) and "_va_out" in ev:
                    _vision_agent_out = ev["_va_out"]  # Sentinel: Output-String
                else:
                    yield ev
            if not _vision_agent_out and vision_agent_images:
                yield await ctx.emit({"type": "status", "content": "⚠ Vision-Agent: KV-cache poisoning detected — output discarded. Next request should be clean."})

        _va_parallel_task = None
        # ABORT-BEFORE-ROUND (2026-11-09): the synthesizer block reads this
        # after the loop; an abort before round 1 left it unbound and raised
        # UnboundLocalError instead of ending the run cleanly.
        _pipeline_ws_ctx = ""
        for i in range(ctx.iterations):
            if ctx.aborted():
                yield await ctx.emit({"type": "status", "content": "Aborted."})
                if _va_parallel_task is not None:
                    _va_parallel_task.cancel()
                    _va_parallel_task = None
                break
            yield await ctx.emit({"type": "round", "n": i + 1, "total": ctx.iterations})

            analyst_input = (
                ctx.user_input
                + (f"\n\nPrevious analysis:\n{previous}\n\nImprove it now." if previous else "")
            )
            # Vision-Agent Output (sequential) → Analyst bekommt strukturierte Bildanalyse
            if _vision_agent_out and i == 0:
                analyst_input += f"\n\n[Vision-Agent image analysis]:\n{_vision_agent_out}"

            if _va_enabled and _va_mode == "parallel" and vision_agent_images and _va_model and i == 0:
                async def _va_parallel_collect():
                    out = ""
                    async for ev in _run_vision_agent():
                        if isinstance(ev, dict) and "_va_out" in ev:
                            out = ev["_va_out"]  # Sentinel
                    return out
                _va_parallel_task = asyncio.create_task(_va_parallel_collect())

            # ── Pipeline Websearch ─────────────────────────────────────────────────
            _pipeline_ws_enabled = ctx.settings.get("pipeline_websearch_enabled", False) and ctx.websearch_available
            _pipeline_ws_ctx     = ""
            if _pipeline_ws_enabled:
                _ws_query = ""
                _ws_auto  = ctx.settings.get("websearch_auto_trigger", True)

                if i == 0 and analyst_input.lower().startswith("/search "):
                    _ws_query    = analyst_input[8:].strip()
                    analyst_input = analyst_input[8:]
                elif _ws_auto:
                    # P2: smart trigger — replaces brittle keyword list
                    _ws_query = ctx.extract_ws_query(ctx.user_input) or ""

                if _ws_query:
                    _analyst_mdl  = ctx.registry_get("analyst")
                    _analyst_prof = ctx.model_profile(_analyst_mdl
                    )
                    if _analyst_prof and _analyst_prof.get("tool_call") is False:
                        yield await ctx.emit({"type": "status",
                            "content": f"⚠ Analyst ({_analyst_mdl}) does not support tool calling "
                                       f"— search result inserted as context"})
                    yield await ctx.emit({"type": "status",
                                          "content": f"🔍 Websearch (round {i+1}): {_ws_query[:60]}…"})
                    _ws_result = await ctx.safe_web_search(
                        _ws_query,
                        max_results=4,
                        phase=f"pipeline_round_{i+1}",
                    )
                    if _ws_result.startswith("[web_search:"):
                        yield await ctx.emit({
                            "type": "status",
                            "content": f"⚠ Websearch fallback: {_ws_result}",
                        })
                    else:
                        _pipeline_ws_ctx = f"\n\n[Web research round {i+1}]\n{_ws_result}\n[End of research]\n"
                        yield await ctx.emit({"type": "status", "content": "✓ Websearch complete"})

            if _pipeline_ws_ctx:
                analyst_input = analyst_input + _pipeline_ws_ctx

            # Analyst
            if "analyst" not in ctx.skip_agents:
                a     = ctx.pipeline.agents["analyst"]
                sys_p = ctx.get_effective_prompt_with_override("analyst", ctx.active_preset, ctx.use_learned)
                if soul_layer:
                    sys_p = soul_layer + "\n\n" + sys_p
                imgs_for_analyst = (ctx.images if (_analyst_vision and i == 0) else [])
                # Inject image description and persistent file paths as context if available
                if ctx.image_description and i == 0 and not _analyst_vision:
                    from core.duo_helpers import is_vision_error_text as _is_vis_err
                    if not _is_vis_err(ctx.image_description):
                        analyst_input = analyst_input + f"\n\n[Image description from vision model]:\n{ctx.image_description}"
                _img_paths = getattr(ctx, "image_paths", None) or []
                if _img_paths and i == 0:
                    sys_p += (
                        "\n\n[IMAGE FILES]: the user's uploaded image(s) are saved at: "
                        + ", ".join(_img_paths)
                        + ". Reference them by path if needed (binary - do not read raw bytes)."
                    )
                msgs  = ctx.make_messages(ctx.pipeline, sys_p, analyst_input, imgs_for_analyst, True, True, cached_mem_ctx=ctx.pipeline_mem_ctx, cached_sess_msgs=ctx.pipeline_sess_msgs)
                yield await ctx.emit({
                    "type": "agent", "content": "Analyst", "model": ctx.registry_get("analyst"),
                    "role": "Breaks the problem into core components"
                })
                parts, t = [], time.time()
                if "refiner" not in ctx.skip_agents:
                    ctx.schedule_prefetch(ctx.registry_get("refiner"), "analyst", t)
                _ag_temp, _ag_think, _ag_budget, _ag_max, _ag_smp = _resolve_stage_call(ctx, a, a.model)
                _ag_no_cache = bool(_analyst_vision and ctx.images)
                async for _ev in _stream_stage_once(
                    ctx, a.model, msgs, _ag_temp, _ag_max,
                    agent_role="analyst", think=_ag_think,
                    thinking_budget=_ag_budget, sampler=_ag_smp,
                    parts_out=parts, no_cache=_ag_no_cache,
                    error_label="Analyst", strip_inline_think=True,
                ):
                    yield _ev
                analyst_out = "".join(parts)
                # Truncation check: retry with more tokens if output cut off
                if ctx.is_truncated(analyst_out) and not ctx.aborted():
                    _an_ctx = ctx.get_num_ctx(a.model, "analyst") or 8192
                    retry_tokens = min(
                        min(int(a.max_tokens * 2), 2000) + (_ag_budget if _ag_think and _ag_budget > 0 else 0),
                        max(256, _an_ctx),
                    )  # INFO-3 FIX: 1200→2000 + preserve thinking budget headroom
                    yield await ctx.emit({"type": "status", "content": f"⚠ Analyst output truncated - retry with {retry_tokens} tokens"})
                    yield await ctx.emit({"type": "clear_agent"})
                    retry_msgs = ctx.make_messages(ctx.pipeline, sys_p, analyst_input + "\n\n[IMPORTANT: Full answer, do not truncate!]", imgs_for_analyst, True, True, cached_mem_ctx=ctx.pipeline_mem_ctx, cached_sess_msgs=ctx.pipeline_sess_msgs)
                    retry_parts = []
                    async for _ev in _stream_stage_once(
                        ctx, a.model, retry_msgs, _ag_temp, retry_tokens,
                        agent_role="analyst", think=_ag_think,
                        thinking_budget=_ag_budget, sampler=_ag_smp,
                        parts_out=retry_parts,
                    ):
                        yield _ev
                    retry_out = "".join(retry_parts).strip()
                    if retry_out and not ctx.is_truncated(retry_out):
                        analyst_out = retry_out
                    elif retry_out and len(retry_out) > len(analyst_out):
                        analyst_out = retry_out  # take longer version even if still truncated
                _elapsed_analyst = round(time.time() - t, 1)
                ctx.agent_elapsed["analyst"] = _elapsed_analyst
                ctx.update_prefetch_lead("analyst", _elapsed_analyst, state=ctx.prefetch_state)
                yield await ctx.emit({"type": "agent_done", "elapsed": _elapsed_analyst})
            else:
                analyst_out = analyst_out or ctx.user_input
                yield await ctx.emit({"type": "status", "content": "Analyst: skipped"})
            if ctx.step_skipped() and not ctx.aborted():
                ctx.clear_step_skip(ctx.run_id)
                yield await ctx.emit({"type": "status", "content": "⏭ Step skipped"})

            # Refiner
            if "refiner" not in ctx.skip_agents:
                a     = ctx.pipeline.agents["refiner"]
                sys_p = ctx.get_effective_prompt_with_override("refiner", ctx.active_preset, ctx.use_learned)
                if soul_layer:
                    sys_p = soul_layer + "\n\n" + sys_p

                from core.duo_helpers import is_vision_error_text as _is_vis_err
                _pipe_desc = (_vision_agent_out or ctx.image_description or "").strip()
                if _is_vis_err(_pipe_desc):
                    _pipe_desc = ""
                _refiner_vision = _role_vision(ctx, "refiner", i, vision_agent_images, _vision_roles_cfg)
                _img_ctx = (f"\n\n[Image description]:\n{_pipe_desc}"
                            if _pipe_desc and not _refiner_vision and ctx.settings.get("image_desc_full_pipeline", False)
                            else "")
                if ctx.constraint_mode and critic_out and i > 0:
                    refiner_input = (
                        f"Problem: {ctx.user_input}{_img_ctx}\n\nAnalysis:\n{analyst_out}"
                        f"\n\nCritic constraints (MUST be fixed):\n{critic_out}"
                        f"\n\nFix all constraints and improve the analysis."
                    )
                    is_repair = True
                else:
                    refiner_input = (
                        f"Original question: {ctx.user_input}{_img_ctx}\n\n"
                        f"Analyst analysis:\n{analyst_out}\n\n"
                        f"Task: improve ONLY the analysis above. "
                        f"Do NOT invent new tasks, data or examples. "
                        f"Stay strictly on the original question. "
                        f"If the analysis is already complete, polish it linguistically only."
                    )
                    is_repair = False

                imgs_for_refiner = ctx.images if _refiner_vision else []
                msgs = ctx.make_messages(ctx.pipeline, sys_p, refiner_input, imgs_for_refiner, False, True, cached_mem_ctx=ctx.pipeline_mem_ctx)

                yield await ctx.emit({
                    "type": "agent",
                    "content": "Refiner",
                    "model": ctx.registry_get("refiner"),
                    "role": "Improves and completes the analysis",
                    "repair": is_repair
                })

                parts, t = [], time.time()
                if "critic" not in ctx.skip_agents:
                    ctx.schedule_prefetch(ctx.registry_get("critic"), "refiner", t)
                _ag_temp, _ag_think, _ag_budget, _ag_max, _ag_smp = _resolve_stage_call(ctx, a, a.model)
                async for _ev in _stream_stage_once(
                    ctx, a.model, msgs, _ag_temp, _ag_max,
                    agent_role=a.key, think=_ag_think,
                    thinking_budget=_ag_budget, sampler=_ag_smp,
                    parts_out=parts, no_cache=_refiner_vision,
                    error_label="Refiner",
                ):
                    yield _ev

                refiner_out = "".join(parts).strip()
                # Truncation check
                if ctx.is_truncated(refiner_out) and not ctx.aborted():
                    _rf_ctx = ctx.get_num_ctx(a.model, a.key) or 8192
                    retry_tokens = min(
                        min(int(a.max_tokens * 2), 2000) + (_ag_budget if _ag_think and _ag_budget > 0 else 0),
                        max(256, _rf_ctx),
                    )  # INFO-3 FIX: 1200→2000 + preserve thinking budget headroom
                    yield await ctx.emit({"type": "status", "content": f"⚠ Refiner output truncated - retry with {retry_tokens} tokens"})
                    yield await ctx.emit({"type": "clear_agent"})
                    retry_msgs2 = ctx.make_messages(ctx.pipeline, sys_p, refiner_input + "\n\n[IMPORTANT: Full answer, do not truncate!]", imgs_for_refiner, False, True, cached_mem_ctx=ctx.pipeline_mem_ctx)
                    retry_parts2 = []
                    async for _ev in _stream_stage_once(
                        ctx, a.model, retry_msgs2, _ag_temp, retry_tokens,
                        agent_role=a.key, think=_ag_think,
                        thinking_budget=_ag_budget, sampler=_ag_smp,
                        parts_out=retry_parts2,
                    ):
                        yield _ev
                    retry_out2 = "".join(retry_parts2).strip()
                    if retry_out2 and len(retry_out2) > len(refiner_out):
                        refiner_out = retry_out2
                _elapsed_refiner = round(time.time() - t, 1)
                ctx.agent_elapsed["refiner"] = _elapsed_refiner
                ctx.update_prefetch_lead("refiner", _elapsed_refiner, state=ctx.prefetch_state)
                yield await ctx.emit({
                    "type": "agent_done",
                    "elapsed": _elapsed_refiner
                })

                # → granite4(2.1) + ministral(3.5) + qwen2.5(2.0) = 7.6GB → Ollama-Overhead → Eviction.
                _refiner_model = ctx.registry_get("refiner")
                _synth_model   = ctx.registry_get("synthesizer")
                _critic_model  = ctx.registry_get("critic")
                _refiner_base  = _refiner_model.split(":")[0] if _refiner_model else ""
                _synth_needs_refiner  = _refiner_base and _synth_model.split(":")[0]  == _refiner_base
                _critic_needs_refiner = _refiner_base and _critic_model.split(":")[0] == _refiner_base
                if _refiner_model and not _synth_needs_refiner and not _critic_needs_refiner:
                    try:
                        await _bk_evict(_refiner_model)
                    except Exception:
                        pass

            else:
                refiner_out = refiner_out or analyst_out
                yield await ctx.emit({"type": "status", "content": "Refiner: skipped"})
            if ctx.step_skipped() and not ctx.aborted():
                ctx.clear_step_skip(ctx.run_id)
                yield await ctx.emit({"type": "status", "content": "⏭ Step skipped"})

            # Critic
            if "critic" not in ctx.skip_agents:
                a     = ctx.pipeline.agents["critic"]
                sys_p = ctx.get_effective_prompt_with_override("critic", ctx.active_preset, ctx.use_learned)
                if soul_layer:
                    sys_p = soul_layer + "\n\n" + sys_p

                from core.duo_helpers import is_vision_error_text as _is_vis_err_c
                _pipe_desc_c = (_vision_agent_out or ctx.image_description or "").strip()
                if _is_vis_err_c(_pipe_desc_c):
                    _pipe_desc_c = ""
                _critic_vision = _role_vision(ctx, "critic", i, vision_agent_images, _vision_roles_cfg)
                _img_ctx_c = (f"\n\n[Image description]:\n{_pipe_desc_c}"
                              if _pipe_desc_c and not _critic_vision and ctx.settings.get("image_desc_full_pipeline", False)
                              else "")
                if ctx.constraint_mode:
                    critic_input = (
                        f"Problem: {ctx.user_input}{_img_ctx_c}\n\nAnalysis:\n{refiner_out}"
                        f"\n\nGive your critique in Tune format. One line per point:"
                        f"\nERR: <logical error or wrong assumption>"
                        f"\nMISS: <what is missing or ignored>"
                        f"\nFIX: <what the Refiner should change concretely>"
                        f"\nCONTRA: <contradiction between two statements>"
                        f"\n\nOnly these lines, no free text, no JSON."
                    )
                else:
                    critic_input = (
                        f"Problem: {ctx.user_input}{_img_ctx_c}\n\nAnalysis:\n{refiner_out}\n\nWhat is being overlooked?"
                    )

                imgs_for_critic = ctx.images if _critic_vision else []
                msgs = ctx.make_messages(ctx.pipeline, sys_p, critic_input, imgs_for_critic, False, True, cached_mem_ctx=ctx.pipeline_mem_ctx)

                yield await ctx.emit({
                    "type": "agent",
                    "content": "Critic",
                    "model": ctx.registry_get("critic"),
                    "role": "Finds blind spots -- adversarial"
                })

                parts, t = [], time.time()
                # → ollama evicts the synthesizer → VRAM spike (in/out/in).
                _is_last_iter = (i == ctx.iterations - 1)
                if "synthesizer" not in ctx.skip_agents and _is_last_iter:
                    ctx.schedule_prefetch(ctx.registry_get("synthesizer"), "critic", t)
                _ag_temp, _ag_think, _ag_budget, _ag_max, _ag_smp = _resolve_stage_call(ctx, a, a.model)
                async for _ev in _stream_stage_once(
                    ctx, a.model, msgs, _ag_temp, _ag_max,
                    agent_role=a.key, think=_ag_think,
                    thinking_budget=_ag_budget, sampler=_ag_smp,
                    parts_out=parts, no_cache=_critic_vision,
                    error_label="Critic", emit_tokens=not ctx.constraint_mode,
                ):
                    yield _ev

                _critic_raw = "".join(parts).strip()  # REMOVED: implicit _re.sub() — Thinking now explicitly controlled
                _tune_lines = [l.strip() for l in _critic_raw.split("\n")
                               if l.strip().startswith(("ERR:", "MISS:", "FIX:", "CONTRA:"))]
                critic_out = "\n".join(_tune_lines) if _tune_lines else _critic_raw
                _elapsed_critic = round(time.time() - t, 1)
                ctx.agent_elapsed["critic"] = _elapsed_critic
                ctx.update_prefetch_lead("critic", _elapsed_critic, state=ctx.prefetch_state)
                # BUG-34: critic_json im agent_done → UI rendert Constraint-Cards direkt
                _agent_done_ev: dict = {"type": "agent_done", "elapsed": _elapsed_critic}
                if ctx.constraint_mode and critic_out:
                    _agent_done_ev["critic_tune"] = critic_out
                yield await ctx.emit(_agent_done_ev)

            else:
                critic_out = ""
                yield await ctx.emit({
                    "type": "status",
                    "content": "Critic: skipped"
                })
            if ctx.step_skipped() and not ctx.aborted():
                ctx.clear_step_skip(ctx.run_id)
                yield await ctx.emit({"type": "status", "content": "⏭ Step skipped"})

            previous = refiner_out or analyst_out

        # Log pipeline run
        for agent_key, output in [
            ("analyst", analyst_out),
            ("refiner", refiner_out),
            ("critic",  critic_out),
        ]:
            model = ctx.registry_get(agent_key)
            ctx.append_learning_log(ctx.this_file, model, {
                "event":           "pipeline_run",
                "run_id":          ctx.run_id,
                "agent":           agent_key,
                "input_length":    len(ctx.user_input),
                "output_length":   len(output),
                "elapsed_seconds": ctx.agent_elapsed.get(agent_key, 0),
                "temperature":     ctx.pipeline.agents[agent_key].temperature,
                "max_tokens":      ctx.pipeline.agents[agent_key].max_tokens,
                "preset":          ctx.active_preset,
                "mode":            ctx.mode,
                "learning":        ctx.use_learned,
            })

        # Synthesizer
        if "synthesizer" not in ctx.skip_agents:
            a     = ctx.pipeline.agents["synthesizer"]
            sys_p = ctx.get_effective_prompt_with_override("synthesizer", ctx.active_preset, ctx.use_learned)
            if soul_layer:
                sys_p = soul_layer + "\n\n" + sys_p

            if _va_parallel_task is not None:
                try:
                    _vision_agent_out = await asyncio.wait_for(_va_parallel_task, timeout=float(ctx.settings.get("duo_llm_slow_timeout_s", 300)))
                except Exception:
                    _vision_agent_out = ""
                _va_parallel_task = None
                # Show vision output as its own bubble in the chat
                if _vision_agent_out:
                    yield await ctx.emit({"type": "agent", "content": "Vision-Agent", "model": _va_model,
                                          "role": "Image analysis (parallel)"})
                    yield await ctx.emit({"type": "token", "content": _vision_agent_out})
                    yield await ctx.emit({"type": "agent_done", "elapsed": 0.0})

            _synth_parts = []
            if analyst_out:
                _synth_parts.append(f"[Analysis]\n{analyst_out}")
            if refiner_out and refiner_out != analyst_out:
                _synth_parts.append(f"[Refinement]\n{refiner_out}")
            if critic_out:
                _synth_parts.append(f"[Critical points]\n{critic_out}")
            # P4: Feed compressed web context to synthesizer so it can ground the final answer
            if _pipeline_ws_ctx:
                _ws_for_synth = _pipeline_ws_ctx.strip()[:1000]
                _synth_parts.append(f"[Research facts]\n{_ws_for_synth}")
            if _vision_agent_out:
                _synth_parts.append(f"[Vision-Agent image analysis]\n{_vision_agent_out}")
            full_analysis = "\n\n".join(_synth_parts)

            _synth_vision = _role_vision(ctx, "synthesizer", 0, vision_agent_images, _vision_roles_cfg)
            imgs_for_synth = ctx.images if _synth_vision else []

            msgs = ctx.make_messages(
                ctx.pipeline,
                sys_p,
                (
                    f"[INTERNAL INTERMEDIATE ANALYSES — not visible to user]\n"
                    f"{full_analysis}\n"
                    f"[END INTERNAL ANALYSES]\n\n"
                    f"User question: {ctx.user_input}\n\n"
                    f"Write the final answer directly for the user. "
                    f"Start immediately with the answer — no internal labels or analysis scores."
                ),
                imgs_for_synth,
                True,
                True,
                cached_mem_ctx=ctx.pipeline_mem_ctx,
                cached_sess_msgs=ctx.pipeline_sess_msgs,
            )

            yield await ctx.emit({
                "type": "agent",
                "content": "Synthesizer",
                "model": ctx.registry_get("synthesizer"),
                "role": "Integrates into the final answer"
            })

            parts, t = [], time.time()
            _synth_mdl = ctx.registry_get("synthesizer") or a.model
            _ag_temp, _ag_think, _ag_budget, _ag_max, _ag_smp = _resolve_stage_call(ctx, a, _synth_mdl)
            from core.model_sampling import (
                POSITIVE_REASONING_EFFORTS as _PIPE_POS_RE,
                format_reasoning_effort_status as _fmt_pipe_re,
                model_supports_thinking as _pipe_mst,
                normalize_reasoning_effort as _norm_pipe_re,
            )
            _pipe_user_eff = _norm_pipe_re((ctx.settings or {}).get("reasoning_effort"))
            _pipe_st = _fmt_pipe_re(_synth_mdl, _pipe_user_eff, supported=_pipe_mst(_synth_mdl))
            if _pipe_st:
                yield await ctx.emit({"type": "status", "content": _pipe_st})
            _pipe_ro_set = False
            if _ag_think and _pipe_user_eff in _PIPE_POS_RE and _pipe_mst(_synth_mdl):
                try:
                    from backend.llama_client import manager as _pipe_mgr
                    from backend.manager_load import set_reasoning_override as _set_pipe_ro
                    _set_pipe_ro(_pipe_mgr, _synth_mdl, True)
                    _pipe_ro_set = True
                except (ImportError, AttributeError):
                    pass
            try:
                async for _ev in _stream_stage_once(
                    ctx, _synth_mdl, msgs, _ag_temp, _ag_max,
                    agent_role=a.key, think=_ag_think,
                    thinking_budget=_ag_budget, sampler=_ag_smp,
                    parts_out=parts, no_cache=_synth_vision,
                    error_label="Synthesizer",
                ):
                    yield _ev
            finally:
                if _pipe_ro_set:
                    try:
                        from backend.llama_client import manager as _pipe_mgr_clr
                        from backend.manager_load import clear_reasoning_override as _clr_pipe_ro
                        _clr_pipe_ro(_pipe_mgr_clr, _synth_mdl)
                    except (ImportError, AttributeError):
                        pass

            synth_out = "".join(parts).strip()
            if ctx.is_truncated(synth_out) and not ctx.aborted():
                _sy_ctx = ctx.get_num_ctx(_synth_mdl, a.key) or 8192
                retry_tokens_s = min(
                    min(int(a.max_tokens * 2), 2000) + (_ag_budget if _ag_think and _ag_budget > 0 else 0),
                    max(256, _sy_ctx),
                )
                yield await ctx.emit({"type": "status",
                                      "content": f"⚠ Synthesizer output truncated - retry with {retry_tokens_s} tokens"})
                yield await ctx.emit({"type": "clear_agent"})
                _synth_retry_input = (
                    f"Problem: {ctx.user_input}\n\n"
                    f"Analysis: {analyst_out}\n\nRefinement: {refiner_out}\n\nCritique: {critic_out}"
                    "\n\n[IMPORTANT: Full answer, do not truncate!]"
                )
                _synth_retry_msgs = ctx.make_messages(ctx.pipeline, sys_p, _synth_retry_input, [], False, True, cached_mem_ctx=ctx.pipeline_mem_ctx)
                _synth_retry_parts: list[str] = []
                async for _ev in _stream_stage_once(
                    ctx, _synth_mdl, _synth_retry_msgs, _ag_temp, retry_tokens_s,
                    agent_role=a.key, think=_ag_think,
                    thinking_budget=_ag_budget, sampler=_ag_smp,
                    parts_out=_synth_retry_parts,
                ):
                    yield _ev
                _synth_retry_out = "".join(_synth_retry_parts).strip()
                if _synth_retry_out and len(_synth_retry_out) > len(synth_out):
                    synth_out = _synth_retry_out
            _elapsed_synth = round(time.time() - t, 1)
            ctx.agent_elapsed["synthesizer"] = _elapsed_synth
            ctx.update_prefetch_lead("synthesizer", _elapsed_synth, state=ctx.prefetch_state)
            yield await ctx.emit({
                "type": "agent_done",
                "elapsed": _elapsed_synth
            })

            _synth_is_error = synth_out.startswith("[Synthesizer error:")
            if not _synth_is_error:
                ctx.memory.add_to_session("user", ctx.user_input)
                ctx.memory.add_to_session("assistant", synth_out)
                _persist_pipeline_ctx_session(ctx)

            run_count = ctx.increment_run_counter()
            spawn(run_peer_ratings(
                run_id      = ctx.run_id,
                user_input  = ctx.user_input,
                outputs     = {"analyst": analyst_out, "refiner": refiner_out, "critic": critic_out, "synthesizer": synth_out},
                use_learned = ctx.use_learned,
                has_images  = bool(ctx.images),
            ))
            spawn(ctx.maybe_trigger_soul_evolution(run_count))

            yield await ctx.emit(
                ctx.done_event(
                    round(time.time() - ctx.t_total, 1),
                    "aborted" if ctx.aborted() else "completed",
                    **ctx.collect_done_metrics(),
                )
            )

        else:
            synth_out = refiner_out or analyst_out

            yield await ctx.emit({
                "type": "agent",
                "content": "Answer (synthesizer off)",
                "model": ctx.registry_get("direct")
            })

            yield await ctx.emit({"type": "token", "content": synth_out})

            yield await ctx.emit({"type": "agent_done", "elapsed": 0})

            ctx.memory.add_to_session("user", ctx.user_input)  # SESSION-ORDER FIX
            ctx.memory.add_to_session("assistant", synth_out)

            _persist_pipeline_ctx_session(ctx)

            # FIX: start peer ratings BEFORE "done"
            run_count = ctx.increment_run_counter()
            spawn(run_peer_ratings(
                run_id      = ctx.run_id,
                user_input  = ctx.user_input,
                outputs     = {"analyst": analyst_out, "refiner": refiner_out, "critic": critic_out, "synthesizer": synth_out},
                use_learned = ctx.use_learned,
            ))
            spawn(ctx.maybe_trigger_soul_evolution(run_count))

            if not ctx.aborted() and ctx.exec_ctrl and ctx.exec_ctrl.stop_reason:
                yield await ctx.emit({"type": "token", "content": f"\n\n{ctx.exec_ctrl.get_summary()}\n"})

            _final_stop_reason = "aborted" if ctx.aborted() else (
                ctx.exec_ctrl.stop_reason.value
                if (ctx.exec_ctrl and ctx.exec_ctrl.stop_reason) else "completed"
            )
            yield await ctx.emit(
                ctx.done_event(
                    round(time.time() - ctx.t_total, 1),
                    _final_stop_reason,
                    **ctx.collect_done_metrics(),
                )
            )

        spawn(asyncio.to_thread(ctx.flush_prefetch_settings, ctx.prefetch_state))
    finally:
        if _va_parallel_task is not None and not _va_parallel_task.done():
            _va_parallel_task.cancel()
        ctx.prefetch_run_active = False
        ctx.unregister_abort(ctx.run_id)
        ctx.unregister_step_skip(ctx.run_id)  # P7 FIX: was missing — ensure cleanup on finally path
        spawn(_unpin_pipeline_models(ctx, _pinned_models, _restore_keep_alive))
        spawn(_refresh_judge_keepalive())

# -- Pipeline helpers (extracted from run_pipeline closures, mechanical) -------


async def _pin_pipeline_models(ctx, pinned_models: set) -> None:
    """Ersten Pipeline-Agenten im VRAM pinnen (aus run_pipeline-Closure)."""
    try:
        _agent_order = [k for k in ("analyst", "refiner", "critic", "synthesizer")
                        if k not in ctx.skip_agents]
        if not _agent_order:
            return

        currently_loaded = await _get_loaded_models_set(max_age=0)
        ctx.vram_cache = currently_loaded

        first_model = ctx.registry_get(_agent_order[0])
        first_gb    = ctx.vram_lookup_gb.get(first_model, 4.0)
        budget_eff  = ctx.vram_budget  # Konfigurierbares VRAM-Budget (Default: 7.5GB)

        if first_gb > budget_eff:
            return

        _pin_ctx  = ctx.get_num_ctx(first_model)
        _pin_opts = {"num_predict": 0}
        if _pin_ctx:
            _pin_opts["num_ctx"] = _pin_ctx

        await _bk_load(first_model, keep_alive="-1", num_ctx=ctx.get_num_ctx(first_model))
        pinned_models.add(first_model)
        ctx.vram_cache |= {first_model}

    except Exception:
        pass


async def _unpin_pipeline_models(ctx, pinned_models: set, restore_keep_alive: str) -> None:
    """Gepinnte Pipeline-Modelle zurueck auf Normal-Keep-Alive (aus Closure)."""
    if not pinned_models:
        return
    try:
        for _m in pinned_models:
            try:
                _unpin_ctx = ctx.get_num_ctx(_m)
                await _bk_load(_m, keep_alive=restore_keep_alive, num_ctx=_unpin_ctx)
            except Exception:
                pass
    except Exception:
        pass


def _persist_pipeline_ctx_session(ctx) -> None:
    """Persist pipeline session turns to chat sidecar JSON (same contract as direct_runner)."""
    if not getattr(ctx, "chat_id", None):
        return
    try:
        import logging as _logging
        from context.chat import _mutate_chat_context

        def _persist_pipeline_session(cdict):
            cdict["session"] = ctx.memory.get_session_messages(
                limit=40, user_cap=10**6, assistant_cap=10**6,
            )
            cdict["ts"] = time.time()

        _mutate_chat_context(ctx.chat_id, _persist_pipeline_session)
    except (OSError, ValueError) as _persist_exc:
        _logging.getLogger("hivemind.pipeline").warning(
            "[Pipeline] session persist failed (chat=%s): %s",
            ctx.chat_id, _persist_exc,
        )


def _role_vision(ctx, role_key: str, iteration: int, vision_agent_images: list,
                 vision_roles_cfg: dict) -> bool:
    """Vision role for a pipeline agent (from the run_pipeline closure)."""
    if iteration != 0 or not vision_agent_images:
        return False
    if not vision_roles_cfg.get(role_key):
        return False
    _m = ctx.registry_get(role_key)
    _caps = ctx.model_profile(_m) if ctx.model_profile else {}
    return bool(_caps.get("vision", False))

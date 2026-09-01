import asyncio
import time
from vision.preprocess import _VISION_POISON_MARKERS


from learning.peer_ratings import run_peer_ratings
from vram.loader import _bk_evict
from vram.loader import _bk_load
from vram.loader import _get_loaded_models_set
from vram.loader import _refresh_judge_keepalive

async def run_pipeline(ctx):
    """Full Hivemind Pipeline mode — multi-agent analysis → synthesis.

    Extracted from server.py run_stream() closure; all closure variables
    are accessed through ``ctx``."""
    vision_agent_images = list(ctx.images) if ctx.images else []

    # Full pipeline
    yield await ctx.emit({"type": "pipeline_start", "content": ctx.user_input})

    # Logik:
    _pinned_models: set[str] = set()
    _restore_keep_alive = str(ctx.settings.get("smart_preload_keep_alive", "10m"))

    # Korrekt: MAX(GB(seq[i]) + GB(seq[i+1])) = maximaler SIMULTANER Bedarf (aktiv + prefetch).
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
        await _pin_pipeline_models(ctx, _pinned_models)
        previous    = ""
        analyst_out = ""
        refiner_out = ""
        critic_out  = ""

        soul       = ctx.pipeline_soul
        soul_layer = ctx.build_soul_prompt_layer(soul, style_only=True)

        # ── Vision-Agent ─────────────────────────────────────────────────────
        # leerer pipeline_vision_roles + pipeline_vision_direct → {"analyst": True}.
        _vision_roles_cfg = dict(ctx.settings.get("pipeline_vision_roles") or {})
        _pipeline_vision_direct = bool(ctx.settings.get("pipeline_vision_direct", False))
        if not _vision_roles_cfg and _pipeline_vision_direct:
            _vision_roles_cfg = {"analyst": True}

        _analyst_vision = _role_vision(ctx, "analyst", 0, vision_agent_images, _vision_roles_cfg)
        _va_model   = ctx.settings.get("vision_agent_model", "") or ctx.pipeline.agents["vision"].model
        _va_has_model = bool(_va_model)
        _va_enabled = (not _analyst_vision) and bool(vision_agent_images) and _va_has_model and (
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

            _va_user_content: list = [{"type": "text", "text": f"[NUTZER]\n{ctx.user_input}"}]
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
                import json
                async with httpx.AsyncClient(timeout=httpx.Timeout(
                        connect=10.0, read=_va_read_s, write=10.0, pool=5.0)) as _vac:
                    async with _vac.stream(
                        "POST",
                        f"http://127.0.0.1:{_va_port}/v1/chat/completions",
                        json={
                            "model":          _va_model,
                            "messages":       _va_msgs,
                            "stream":         True,
                            "temperature":    va.temperature,
                            "max_tokens":     va.max_tokens,
                            "repeat_penalty": 1.3,
                            "stop":           ["<|im_end|>", "<|endoftext|>", "\n\n\n"],
                            "cache_prompt":   False,
                        },
                    ) as _va_resp:
                        async for _va_line in _va_resp.aiter_lines():
                            if ctx.aborted(): break
                            if not _va_line or not _va_line.startswith("data:"):
                                continue
                            _va_raw = _va_line[6:].strip()
                            if _va_raw == "[DONE]":
                                break
                            try:
                                _va_d   = json.loads(_va_raw)
                                _va_tok = (_va_d.get("choices", [{}])[0]
                                               .get("delta", {})
                                               .get("content", "") or "")
                                if _va_tok:
                                    parts.append(_va_tok)
                                    yield await ctx.emit({"type": "token", "content": _va_tok})
                            except Exception:
                                continue
            except Exception as e:
                err = f"[Vision-Agent error: {str(e)[:100]}]"
                parts.append(err)
                yield await ctx.emit({"type": "token", "content": err})

            out = "".join(parts).strip()  # REMOVED: implicit _re.sub() — Thinking now explicitly controlled
            # Trailing Sondertokens abschneiden
            for _stop in ("<|im_end|>", "<|endoftext|>", "<|"):
                if _stop in out:
                    out = out[:out.index(_stop)].strip()

            # Sanity-Check: KV-Cache-Poisoning erkennen (identisch zu Vision-Preprocessing)
            if any(m in out[:120].lower() for m in _VISION_POISON_MARKERS):
                _va_logger.error(
                    f"Vision-Agent KV-Cache-Poisoning erkannt — verworfen. "
                    f"Erste 80 Zeichen: {out[:80]!r}"
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
                            "content": f"⚠ Analyst ({_analyst_mdl}) unterstuetzt kein Tool-Calling "
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
                        _pipeline_ws_ctx = f"\n\n[Web-Recherche Runde {i+1}]\n{_ws_result}\n[Ende Recherche]\n"
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
                # Inject image description as context if available
                if ctx.image_description and i == 0 and not _analyst_vision:
                    analyst_input = analyst_input + f"\n\n[Image description from vision model]:\n{ctx.image_description}"
                msgs  = ctx.make_messages(ctx.pipeline, sys_p, analyst_input, imgs_for_analyst, True, True, cached_mem_ctx=ctx.pipeline_mem_ctx, cached_sess_msgs=ctx.pipeline_sess_msgs)
                yield await ctx.emit({
                    "type": "agent", "content": "Analyst", "model": ctx.registry_get("analyst"),
                    "role": "Breaks the problem into core components"
                })
                parts, t = [], time.time()
                if "refiner" not in ctx.skip_agents:
                    ctx.schedule_prefetch(ctx.registry_get("refiner"), "analyst", t)
                # AGENT-THINKING (2026-08-19): Per-Agent-Schalter aus AgentConfig.
                _ag_think = bool(getattr(a, "thinking", False))
                _ag_budget = int(getattr(a, "thinking_budget", 0) or 0)
                _ag_no_cache = bool(_analyst_vision and ctx.images)
                _ag_ctx = ctx.get_num_ctx(a.model, "analyst") or 8192
                _ag_max = min(int(a.max_tokens + _ag_budget), max(256, _ag_ctx))
                try:
                    if _ag_think:
                        async for _c, _th in ctx.pipeline_chat_stream(
                                a.model, msgs, a.temperature, _ag_max,
                                agent_role="analyst", think=True,
                                thinking_budget=_ag_budget or None,
                                split_thinking=True, no_cache=_ag_no_cache):
                            if ctx.aborted(): break
                            if _th:
                                yield await ctx.emit({"type": "thinking_token", "content": _th})
                            if _c:
                                parts.append(_c)
                                yield await ctx.emit({"type": "token", "content": _c})
                    else:
                        _think_open = False
                        async for tok in ctx.pipeline_chat_stream(a.model, msgs, a.temperature, _ag_max,
                                                               agent_role="analyst", no_cache=_ag_no_cache):
                            if ctx.aborted(): break
                            parts.append(tok)
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
                except Exception as e:
                    err = f"[Analyst error: {str(e)[:100]}]"
                    parts.append(err)
                    yield await ctx.emit({"type": "token", "content": err})
                analyst_out = "".join(parts)
                # REMOVED: implicit _re.sub("") — Thinking budget is now explicitly controlled via agent_name="analyst"
                # If <think> still appears, it's a model bug not a system failure
                # Truncation check: retry with more tokens if output cut off
                if ctx.is_truncated(analyst_out) and not ctx.aborted():
                    retry_tokens = min(int(a.max_tokens * 2), 2000)  # INFO-3 FIX: 1200→2000
                    yield await ctx.emit({"type": "status", "content": f"⚠ Analyst output truncated - retry with {retry_tokens} tokens"})
                    yield await ctx.emit({"type": "clear_agent"})
                    retry_msgs = ctx.make_messages(ctx.pipeline, sys_p, analyst_input + "\n\n[IMPORTANT: Full answer, do not truncate!]", imgs_for_analyst, True, True, cached_mem_ctx=ctx.pipeline_mem_ctx, cached_sess_msgs=ctx.pipeline_sess_msgs)
                    t_original_analyst = t
                    retry_parts, retry_t = [], time.time()
                    try:
                        async for tok in ctx.pipeline_chat_stream(a.model, retry_msgs, a.temperature, retry_tokens,
                                                               agent_role="analyst"):
                            if ctx.aborted(): break
                            retry_parts.append(tok)
                            yield await ctx.emit({"type": "token", "content": tok})
                        retry_out = "".join(retry_parts).strip()  # REMOVED: implicit _re.sub() — Thinking now explicitly controlled
                        if retry_out and not ctx.is_truncated(retry_out):
                            analyst_out = retry_out
                        elif retry_out and len(retry_out) > len(analyst_out):
                            analyst_out = retry_out  # take longer version even if still truncated
                    except Exception:
                        pass
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

                _img_ctx = (f"\n\n[Image description]:\n{ctx.image_description}"
                            if ctx.image_description and ctx.settings.get("image_desc_full_pipeline", False)
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

                _refiner_vision = _role_vision(ctx, "refiner", i, vision_agent_images, _vision_roles_cfg)
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
                _ag_think = bool(getattr(a, "thinking", False))
                _ag_budget = int(getattr(a, "thinking_budget", 0) or 0)
                _ag_ctx = ctx.get_num_ctx(a.model) or 8192
                _ag_max = min(int(a.max_tokens + _ag_budget), max(256, _ag_ctx))
                try:
                    if _ag_think:
                        async for _c, _th in ctx.pipeline_chat_stream(
                                a.model, msgs, a.temperature, _ag_max,
                                agent_role=a.key, think=True, thinking_budget=_ag_budget or None,
                                split_thinking=True, no_cache=_refiner_vision):
                            if ctx.aborted(): break
                            if _th:
                                yield await ctx.emit({"type": "thinking_token", "content": _th})
                            if _c:
                                parts.append(_c)
                                yield await ctx.emit({"type": "token", "content": _c})
                    else:
                        async for tok in ctx.pipeline_chat_stream(
                            a.model, msgs, a.temperature, _ag_max, agent_role=a.key, no_cache=_refiner_vision
                        ):
                            if ctx.aborted(): break
                            parts.append(tok)
                            yield await ctx.emit({"type": "token", "content": tok})
                except Exception as e:
                    err = f"[Refiner error: {str(e)[:100]}]"
                    parts.append(err)
                    yield await ctx.emit({"type": "token", "content": err})

                refiner_out = "".join(parts).strip()  # REMOVED: implicit _re.sub() — Thinking now explicitly controlled
                # Truncation check
                if ctx.is_truncated(refiner_out) and not ctx.aborted():
                    retry_tokens = min(int(a.max_tokens * 2), 2000)  # INFO-3 FIX: 1200→2000
                    yield await ctx.emit({"type": "status", "content": f"⚠ Refiner output truncated - retry with {retry_tokens} tokens"})
                    yield await ctx.emit({"type": "clear_agent"})
                    retry_msgs2 = ctx.make_messages(ctx.pipeline, sys_p, refiner_input + "\n\n[IMPORTANT: Full answer, do not truncate!]", imgs_for_refiner, False, True, cached_mem_ctx=ctx.pipeline_mem_ctx)
                    retry_parts2 = []
                    try:
                        async for tok in ctx.pipeline_chat_stream(a.model, retry_msgs2, a.temperature, retry_tokens, agent_role=a.key):
                            if ctx.aborted(): break
                            retry_parts2.append(tok)
                            yield await ctx.emit({"type": "token", "content": tok})
                        retry_out2 = "".join(retry_parts2).strip()  # REMOVED: implicit _re.sub() — Thinking now explicitly controlled
                        if retry_out2 and len(retry_out2) > len(refiner_out):
                            refiner_out = retry_out2
                    except Exception:
                        pass
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

                _img_ctx_c = (f"\n\n[Image description]:\n{ctx.image_description}"
                              if ctx.image_description and ctx.settings.get("image_desc_full_pipeline", False)
                              else "")
                if ctx.constraint_mode:
                    critic_input = (
                        f"Problem: {ctx.user_input}{_img_ctx_c}\n\nAnalysis:\n{refiner_out}"
                        f"\n\nGive your critique in Tune format. One line per point:"
                        f"\nERR: <logischer Fehler oder falsche Annahme>"
                        f"\nMISS: <was fehlt oder ignoriert wird>"
                        f"\nFIX: <was der Refiner konkret aendern soll>"
                        f"\nCONTRA: <Widerspruch zwischen zwei Aussagen>"
                        f"\n\nNur diese Zeilen, kein Freitext, kein JSON."
                    )
                else:
                    critic_input = (
                        f"Problem: {ctx.user_input}{_img_ctx_c}\n\nAnalysis:\n{refiner_out}\n\nWhat is being overlooked?"
                    )

                _critic_vision = _role_vision(ctx, "critic", i, vision_agent_images, _vision_roles_cfg)
                imgs_for_critic = ctx.images if _critic_vision else []
                msgs = ctx.make_messages(ctx.pipeline, sys_p, critic_input, imgs_for_critic, False, True, cached_mem_ctx=ctx.pipeline_mem_ctx)

                yield await ctx.emit({
                    "type": "agent",
                    "content": "Critic",
                    "model": ctx.registry_get("critic"),
                    "role": "Finds blind spots -- adversarial"
                })

                parts, t = [], time.time()
                # → Ollama evictet Synthesizer → VRAM-Spike (rein/raus/rein).
                _is_last_iter = (i == ctx.iterations - 1)
                if "synthesizer" not in ctx.skip_agents and _is_last_iter:
                    ctx.schedule_prefetch(ctx.registry_get("synthesizer"), "critic", t)
                _ag_think = bool(getattr(a, "thinking", False))
                _ag_budget = int(getattr(a, "thinking_budget", 0) or 0)
                _ag_ctx = ctx.get_num_ctx(a.model) or 8192
                _ag_max = min(int(a.max_tokens + _ag_budget), max(256, _ag_ctx))
                try:
                    if _ag_think:
                        async for _c, _th in ctx.pipeline_chat_stream(
                                a.model, msgs, a.temperature, _ag_max,
                                agent_role=a.key, think=True, thinking_budget=_ag_budget or None,
                                split_thinking=True, no_cache=_critic_vision):
                            if ctx.aborted(): break
                            if _th:
                                yield await ctx.emit({"type": "thinking_token", "content": _th})
                            if _c:
                                parts.append(_c)
                                if not ctx.constraint_mode:
                                    yield await ctx.emit({"type": "token", "content": _c})
                    else:
                        async for tok in ctx.pipeline_chat_stream(
                            a.model, msgs, a.temperature, _ag_max, agent_role=a.key, no_cache=_critic_vision
                        ):
                            if ctx.aborted(): break
                            parts.append(tok)
                            if not ctx.constraint_mode:
                                yield await ctx.emit({"type": "token", "content": tok})
                except Exception as e:
                    err = f"[Critic error: {str(e)[:100]}]"
                    parts.append(err)
                    yield await ctx.emit({"type": "token", "content": err})

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
                # Vision-Output als eigene Bubble im Chat anzeigen
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
            _ag_think = bool(getattr(a, "thinking", False))
            _ag_budget = int(getattr(a, "thinking_budget", 0) or 0)
            _ag_ctx = ctx.get_num_ctx(a.model) or 8192
            _ag_max = min(int(a.max_tokens + _ag_budget), max(256, _ag_ctx))
            try:
                _synth_mdl = ctx.registry_get("synthesizer")
                if _ag_think:
                    async for _c, _th in ctx.pipeline_chat_stream(
                            _synth_mdl, msgs, a.temperature, _ag_max,
                            agent_role=a.key, think=True, thinking_budget=_ag_budget or None,
                            split_thinking=True, no_cache=_synth_vision):
                        if ctx.aborted(): break
                        if _th:
                            yield await ctx.emit({"type": "thinking_token", "content": _th})
                        if _c:
                            parts.append(_c)
                            yield await ctx.emit({"type": "token", "content": _c})
                else:
                    async for tok in ctx.pipeline_chat_stream(
                        _synth_mdl, msgs, a.temperature, _ag_max, agent_role=a.key, no_cache=_synth_vision
                    ):
                        if ctx.aborted(): break
                        parts.append(tok)
                        yield await ctx.emit({"type": "token", "content": tok})
            except Exception as e:
                err = f"[Synthesizer error: {str(e)[:100]}]"
                parts.append(err)
                yield await ctx.emit({"type": "token", "content": err})

            synth_out = "".join(parts).strip()  # REMOVED: implicit _re.sub() — Thinking now explicitly controlled
            if ctx.is_truncated(synth_out) and not ctx.aborted():
                retry_tokens_s = min(int(a.max_tokens * 2), 2000)
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
                try:
                    async for tok in ctx.pipeline_chat_stream(a.model, _synth_retry_msgs, a.temperature, retry_tokens_s, agent_role=a.key):
                        if ctx.aborted(): break
                        _synth_retry_parts.append(tok)
                        yield await ctx.emit({"type": "token", "content": tok})
                    _synth_retry_out = "".join(_synth_retry_parts).strip()  # REMOVED: implicit _re.sub() — Thinking now explicitly controlled
                    if _synth_retry_out and len(_synth_retry_out) > len(synth_out):
                        synth_out = _synth_retry_out
                except Exception:
                    pass
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

            run_count = ctx.increment_run_counter()
            asyncio.create_task(run_peer_ratings(
                run_id      = ctx.run_id,
                user_input  = ctx.user_input,
                outputs     = {"analyst": analyst_out, "refiner": refiner_out, "critic": critic_out, "synthesizer": synth_out},
                use_learned = ctx.use_learned,
                has_images  = bool(ctx.images),
            ))
            asyncio.create_task(ctx.maybe_trigger_soul_evolution(run_count))

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
                "content": "Antwort (Synthesizer off)",
                "model": ctx.registry_get("direct")
            })

            yield await ctx.emit({"type": "token", "content": synth_out})

            yield await ctx.emit({"type": "agent_done", "elapsed": 0})

            ctx.memory.add_to_session("user", ctx.user_input)  # SESSION-ORDER FIX
            ctx.memory.add_to_session("assistant", synth_out)

            # FIX: Peer-Ratings VOR "done" starten
            run_count = ctx.increment_run_counter()
            asyncio.create_task(run_peer_ratings(
                run_id      = ctx.run_id,
                user_input  = ctx.user_input,
                outputs     = {"analyst": analyst_out, "refiner": refiner_out, "critic": critic_out, "synthesizer": synth_out},
                use_learned = ctx.use_learned,
            ))
            asyncio.create_task(ctx.maybe_trigger_soul_evolution(run_count))

            if not ctx.aborted() and ctx.exec_ctrl.stop_reason:
                yield await ctx.emit({"type": "token", "content": f"\n\n{ctx.exec_ctrl.get_summary()}\n"})

            _final_stop_reason = "aborted" if ctx.aborted() else (
                ctx.exec_ctrl.stop_reason.value if ctx.exec_ctrl.stop_reason else "completed"
            )
            yield await ctx.emit(
                ctx.done_event(
                    round(time.time() - ctx.t_total, 1),
                    _final_stop_reason,
                    **ctx.collect_done_metrics(),
                )
            )

        asyncio.create_task(asyncio.to_thread(ctx.flush_prefetch_settings, ctx.prefetch_state))
    finally:
        ctx.prefetch_run_active = False
        ctx.unregister_abort(ctx.run_id)
        ctx.unregister_step_skip(ctx.run_id)  # P7 FIX: was missing — ensure cleanup on finally path
        asyncio.create_task(_unpin_pipeline_models(ctx, _pinned_models, _restore_keep_alive))
        asyncio.create_task(_refresh_judge_keepalive())

# -- Pipeline-Helfer (aus run_pipeline-Closures extrahiert, mechanisch) -------


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


def _role_vision(ctx, role_key: str, iteration: int, vision_agent_images: list,
                 vision_roles_cfg: dict) -> bool:
    """Vision-Rolle fuer einen Pipeline-Agenten (aus run_pipeline-Closure)."""
    if iteration != 0 or not vision_agent_images:
        return False
    if not vision_roles_cfg.get(role_key):
        return False
    _m = ctx.registry_get(role_key)
    _caps = ctx.model_profile(_m) if ctx.model_profile else {}
    return bool(_caps.get("vision", False))

# -*- coding: utf-8 -*-
"""AgenticToolLoop  ToolLoop subclass with duo_runner's POST+retry logic."""
from __future__ import annotations
import asyncio, json, logging, time
import httpx

from core.tool_loop import ToolLoop, ToolLoopConfig, _build_tool_call_grammar, _grammar_compatible, _USE_GBNF_GRAMMAR
from core.agentic_duo_state import DuoRoundState
from core.duo_helpers import RE_THINK_CLEANUP as _RE_THINK_CLEANUP, _inject_no_think_directive, parse_sse_delta as _parse_sse_delta

logger = logging.getLogger("hivemind.agentic_tool_loop")


class AgenticToolLoop(ToolLoop):
    """ToolLoop with duo_runner's configurable-attempt POST+retry+SSE-parse."""

    round_state: DuoRoundState

    def __init__(self, config: ToolLoopConfig, http_client: httpx.AsyncClient, *,
                 round_state: DuoRoundState, workspace: str = "", run_id: str = "",
                 emit_fn=None):
        super().__init__(config, http_client, round_state.current_port,
                         workspace=workspace, run_id=run_id)
        self.round_state = round_state
        self._emit_fn = emit_fn
        self._pending_events: list[str] = []
        # THINKING-RESCUE: Thinking-Parts zustzlich als Instanz-Attribut akkumulieren
        self._dr_thinking_parts: list[str] = []

    #  Public 

    async def post_with_retry(self, payload: dict, *, dtool_msgs: list[dict],
                              ctx, _parts: list[str]) -> dict:
        """POST with configurable-attempt retry + SSE parse. Returns result dict with keys:
        dr_msg, dr_content_parts, dr_thinking_parts, dr_tool_calls_acc, dport.
        Modifies duo state (think_runtime, inject_no_think, parse_errors, etc.).
        Calls ctx.emit() for status/token events.
        Raises RuntimeError if all attempts fail.
        """
        result = {
            "dr_msg": None, "dr_content_parts": [], "dr_thinking_parts": [],
            "dr_tool_calls_acc": {}, "dport": self.round_state.current_port,
            "dr_finish_reason": None,
        }
        _dr_post_errors: dict[str, int] = {}
        # _dr_aborted_mid = User brach mitten im Stream ab (akkumulierte
        _dr_stream_partial = False
        _dr_aborted_mid = False
        if dtool_msgs and dtool_msgs[-1].get("role") == "assistant":
            self._tail_guard_fires = getattr(self, "_tail_guard_fires", 0) + 1
            logger.warning(
                "[TAIL-GUARD] Messages end with assistant — fixing before POST "
                "(consecutive=%d, last 500 body: %s, first_err_body: %s)",
                self._tail_guard_fires,
                str(getattr(self.round_state, "last_err_body", "") or "")[:80],
                str(getattr(self.round_state, "first_err_body", "") or "")[:80],
            )
            if self._tail_guard_fires >= 4:
                logger.error(
                    "[TAIL-GUARD] %dx in a row without tool results  "
                    "tool round repeatedly yields nothing (server retry loop? VRAM?)  stopping.",
                    self._tail_guard_fires,
                )
                result["loop_detected"] = True
                result["dr_stream_ok"] = False
                raise RuntimeError(
                    "Tool round returned no tool results 4x in a row "
                    "(assistant ended without results) — loop stopped."
                )
            _recent = ""
            for _m in dtool_msgs[-8:]:
                try:
                    _recent += str(_m.get("content") or "") + "\n"
                except Exception:
                    continue
            if "No tests found" in _recent and "[TEST-RESULT]" in _recent:
                _coach = (
                    "run_tests found NO test suite. If the project documents its own check "
                    "(e.g. 'python selftest.py'), run it via run_bash NOW; otherwise call "
                    "task_complete with status {completed, blockers, build_status}."
                )
            elif "VERIFY REQUIRED" in _recent:
                _coach = (
                    "Your task_complete was BLOCKED because a file write was not followed by "
                    "a successful verification. Call run_tests now (or the project's documented "
                    "check via run_bash), then call task_complete again."
                )
            else:
                _coach = (
                    "If your work is COMPLETE, call task_complete now with status "
                    "{completed: [...], blockers: [...], build_status}. "
                    "Otherwise continue with your next tool call."
                )
            dtool_msgs.append({
                "role": "user",
                "content": (
                    "[SYSTEM] Your previous response contained NO tool call — nothing was "
                    "executed and text-only responses do not advance the task. " + _coach
                ),
            })
            payload = {**payload, "messages": dtool_msgs}
        else:
            self._tail_guard_fires = 0

        for _post_attempt in range(self.cfg.max_post_attempts):
            try:
                # ── S2 (2026-08-23): Constrained Decoding auf Retry-Runden ──
                if _USE_GBNF_GRAMMAR and getattr(self.round_state, "force_grammar", False):
                    self.round_state.force_grammar = False
                    # GRAMMAR-GUARD (2026-08-25): llama.cpp lehnt grammar+tools
                    if not _grammar_compatible(payload):
                        await self._emit({"type": "status",
                            "content": "\u26a0 Grammar-forced retry skipped \u2014 tools active (server rejects grammar+tools)"})
                    else:
                        _tn = [t.get("function", {}).get("name", "")
                               for t in (payload.get("tools") or [])]
                        _g = _build_tool_call_grammar([n for n in _tn if n])
                        if _g:
                            payload = {**payload, "grammar": _g}
                            await self._emit({"type": "status",
                                "content": "🔒 Grammar-forced retry (tool-call JSON constrained)"})
                _timeout = httpx.Timeout(connect=10.0, read=self.round_state.tool_read_timeout_s, write=10.0, pool=5.0)
                _gen_t0 = time.monotonic()
                async with self._client.stream(
                    "POST", f"http://127.0.0.1:{result['dport']}/v1/chat/completions",
                    json=payload, timeout=_timeout,
                ) as _resp:
                    #  5xx Handler 
                    if _resp.status_code in (500, 502, 503, 504) and _post_attempt < self.cfg.max_post_attempts - 1:
                        _dr_post_errors["5xx"] = _dr_post_errors.get("5xx", 0) + 1
                        await _resp.aread()
                        _err_body = _resp.text[:300]
                        if not self.round_state.first_err_body and _err_body:
                            self.round_state.first_err_body = f"HTTP 500: {_err_body[:150]}"
                            await self._emit({"type": "status", "content": f"? {self.round_state.first_err_body}"})

                        if _resp.status_code == 500 and "parse" in _err_body.lower():
                            _dr_post_errors["500-parse-json"] = _dr_post_errors.get("500-parse-json", 0) + 1
                            self.round_state.parse_errors += 1
                            if self.round_state.think_runtime:
                                self.round_state.think_runtime = False
                                self.round_state.parse_errors = 0
                                if not self.round_state.inject_no_think:
                                    self.round_state.inject_no_think = True
                                    dtool_msgs[:] = _inject_no_think_directive(dtool_msgs)
                                    payload = {**payload, "messages": dtool_msgs}
                                payload = {**payload, "thinking": False, "thinking_budget": 0,
                                           "max_tokens": int(self.round_state.dtool_opts.get("num_predict", 800) or 800)}
                                _ctk_parse = dict(payload.get("chat_template_kwargs") or {})
                                _ctk_parse["enable_thinking"] = False
                                payload["chat_template_kwargs"] = _ctk_parse
                                self.round_state.force_grammar = True
                                await self._emit({"type": "status",
                                    "content": "? Tool thinking caused JSON errors — disabling thinking for tool calls and retrying."})
                                dtool_msgs.append({"role": "assistant", "content": "[tool-call JSON parse failed while thinking was enabled]"})
                                dtool_msgs.append({"role": "user", "content": "Retry the same step with thinking disabled."})
                                continue
                            if self.round_state.parse_errors >= 3:
                                _parts.append(f"[JSON parse error {self.round_state.parse_errors}x: too large patches]")
                                await self._emit({"type": "token", "content": _parts[-1]})
                                result["loop_detected"] = True
                                break
                            self.round_state.force_grammar = True
                            dtool_msgs.append({"role": "assistant", "content": "[previous tool call was truncated]"})
                            dtool_msgs.append({"role": "user", "content": (
                                f"[JSON parse error #{self.round_state.parse_errors}]: "
                                f"Use edit_file for smaller changes. "
                                f"Use plain JSON for tool call arguments  no markdown code blocks (```json ... ```)."
                            )})
                            await self._emit({"type": "token", "content": f"\n[JSON parse error #{self.round_state.parse_errors}]\n"})
                            continue

                        if _resp.status_code == 500 and "CallExpression" in _err_body:
                            if self.round_state.think_runtime:
                                self.round_state.think_runtime = False
                                payload = {**payload, "thinking": False, "thinking_budget": 0,
                                           "max_tokens": int(self.round_state.dtool_opts.get("num_predict", 800) or 800)}
                                _ctk_call = dict(payload.get("chat_template_kwargs") or {})
                                _ctk_call["enable_thinking"] = False
                                payload["chat_template_kwargs"] = _ctk_call
                                if not self.round_state.inject_no_think:
                                    self.round_state.inject_no_think = True
                                    dtool_msgs[:] = _inject_no_think_directive(dtool_msgs)
                                    payload = {**payload, "messages": dtool_msgs}
                                await self._emit({"type": "status",
                                    "content": "? Grammar crash (CallExpression) — disabling tool thinking and retrying."})
                                self.round_state.force_grammar = True
                                continue
                            else:
                                if not self.round_state.no_extras_fallback:
                                    _dr_post_errors["500-fallback-stripped"] = _dr_post_errors.get("500-fallback-stripped", 0) + 1
                                    self.round_state.no_extras_fallback = True
                                    payload.pop("cache_prompt", None)
                                    payload.pop("min_p", None)
                                    self.round_state.force_grammar = True
                                    _ctk_fb = dict(payload.get("chat_template_kwargs") or {})
                                    _ctk_fb["enable_thinking"] = False
                                    payload["chat_template_kwargs"] = _ctk_fb
                                    await self._emit({"type": "status",
                                        "content": "? 500 despite Thinking=off — stripping extras and retrying."})
                                    continue
                                _dr_post_errors["500-like(thinking_off)"] = _dr_post_errors.get("500-like(thinking_off)", 0) + 1
                                self.round_state.last_err_body = _err_body[:200] or "(HTTP 500 with empty body)"
                                break

                        await asyncio.sleep(1.5 + min(1.0, _post_attempt * 0.1))
                        await self._emit({"type": "status", "content": f"? Server {_resp.status_code}, retry {_post_attempt+1}/{self.cfg.max_post_attempts}"})
                        continue

                    #  4xx Handlers 
                    if _resp.status_code >= 400:
                        await _resp.aread()
                        _err_body = _resp.text[:300]

                        if _resp.status_code == 404:
                            _dr_post_errors["404"] = _dr_post_errors.get("404", 0) + 1
                            self.round_state.http_404_retries += 1
                            if self.round_state.http_404_retries <= 2:
                                await self._emit({"type": "status",
                                    "content": f"? Tool loop HTTP 404 (attempt {self.round_state.http_404_retries}/2) — rebinding model port"})
                                try:
                                    from backend.llama_server_manager import manager as _lsm404
                                    await _lsm404.evict(self.round_state.exec_model)
                                except Exception:
                                    logger.debug("404 evict failed for %s", self.round_state.exec_model, exc_info=True)
                                result["dport"] = await self._ensure_loaded(self.round_state.exec_model)
                                self.round_state.cached_port = result["dport"]
                                continue
                            _parts.append("[Tool loop HTTP 404: endpoint still unavailable after recovery.]")
                            await self._emit({"type": "token", "content": _parts[-1]})
                            result["loop_detected"] = True
                            break

                        if _resp.status_code == 400 and any(k in _err_body.lower() for k in ("exceed", "context", "token", "prompt")):
                            _dr_post_errors["400-context-overflow"] = _dr_post_errors.get("400-context-overflow", 0) + 1
                            await self._emit({"type": "status", "content": "? Context overflow (HTTP 400) — forcing compression"})
                            result["force_compress"] = True
                            result["dr_stream_ok"] = True  # Signal: no crash, just needs compress
                            break

                        # GRAMMAR-GUARD recovery (2026-08-25): 400 'Cannot use custom
                        # grammar' -> strip grammar + retry immediately
                        if _resp.status_code == 400 and payload.get("grammar") and "grammar" in _err_body.lower():
                            _dr_post_errors["400-grammar-stripped"] = _dr_post_errors.get("400-grammar-stripped", 0) + 1
                            payload.pop("grammar", None)
                            await self._emit({"type": "status",
                                "content": "\u26a0 Grammar+tools rejected by server \u2014 stripped grammar, retrying\u2026"})
                            continue

                        if _resp.status_code == 400:
                            _dr_post_errors["400-other"] = _dr_post_errors.get("400-other", 0) + 1
                            _parts.append(f"[Tool round error HTTP 400: {_err_body[:200]}]")
                            await self._emit({"type": "token", "content": _parts[-1]})
                            result["loop_detected"] = True
                            break

                        _dr_post_errors[f"unhandled-{_resp.status_code}"] = _dr_post_errors.get(f"unhandled-{_resp.status_code}", 0) + 1
                        raise RuntimeError(f"llama-server {_resp.status_code}: {_err_body}")

                    #  status 200: parse SSE stream 
                    result["dr_content_parts"] = []
                    result["dr_thinking_parts"] = []
                    result["dr_tool_calls_acc"] = {}
                    _dr_finish_reason: str | None = None
                    # (server.py usage_meta) UND Client (perfRealTokens/tok/s).
                    _dr_usage_final: dict | None = None
                    async for _sse_line in _resp.aiter_lines():
                        if not _sse_line.startswith("data:"): continue
                        _sse_data = _sse_line[5:].strip()
                        if _sse_data == "[DONE]": break
                        try: _sse_chunk = json.loads(_sse_data)
                        except Exception: continue
                        _sse_usage = _sse_chunk.get("usage")
                        if _sse_usage and _sse_usage.get("completion_tokens"):
                            # D2-DIAG (2026-08-21): cached_tokens aus
                            # prompt_tokens_details → Cache-Reuse pro Request messbar.
                            _cached = 0
                            try:
                                _cached = int((_sse_usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                            except Exception:
                                pass
                            _dr_usage_final = {
                                "completion_tokens": int(_sse_usage["completion_tokens"]),
                                "prompt_tokens": int(_sse_usage.get("prompt_tokens") or 0),
                                "cached_tokens": _cached,
                                "gen_ms": int((time.monotonic() - _gen_t0) * 1000),
                            }
                        _sse_choices = _sse_chunk.get("choices") or [{}]
                        # Choice-Chunk ⇒ Generation lief ins max_tokens-Limit ⇒ Tool-Call-
                        _sse_fr = _sse_choices[0].get("finish_reason")
                        if _sse_fr:
                            _dr_finish_reason = _sse_fr
                        _sse_delta = _sse_choices[0].get("delta", {})
                        if ctx.aborted() or (ctx.chat_id and ctx.is_aborted_chat(ctx.chat_id)):
                            _dr_aborted_mid = True
                            break
                        _sse_cont, _sse_think = _parse_sse_delta(
                            _sse_delta,
                            tool_calls_acc=result["dr_tool_calls_acc"],
                            thinking_keys=("thinking", "reasoning_content", "reasoning"),
                        )
                        if _sse_cont:
                            result["dr_content_parts"].append(_sse_cont)
                            await self._emit({"type": "token", "content": _sse_cont})
                            _dr_stream_partial = True
                        if _sse_think:
                            result["dr_thinking_parts"].append(_sse_think)
                            self._dr_thinking_parts.append(_sse_think)
                            await self._emit({"type": "thinking_token", "content": _sse_think})
                            _dr_stream_partial = True

                    # send usage EXACTLY ONCE after the stream ends
                    if _dr_usage_final:
                        await self._emit({"type": "usage_meta", "phase": "coder", **_dr_usage_final})
                    result["dr_finish_reason"] = _dr_finish_reason
                    # duo_write_chars_per_token sammeln. Pro Request: completion_tokens
                    try:
                        _cal_chars = 0
                        _cal_names = []
                        for _ck in sorted(result["dr_tool_calls_acc"].keys()):
                            _ctc = result["dr_tool_calls_acc"][_ck]
                            _cfn = str((_ctc.get("function") or {}).get("name", "") or "")
                            if _cfn in ("write_file", "write_file_append", "edit_file",
                                        "patch_file", "replace_lines"):
                                _cal_chars += len(str((_ctc.get("function") or {}).get("arguments", "") or ""))
                                _cal_names.append(_cfn)
                        if _cal_chars > 0 and _dr_usage_final:
                            _cal_toks = max(1, int(_dr_usage_final.get("completion_tokens") or 0))
                            logger.info(
                                "[WRITE-CALIBRATION] completion_tokens=%d write_args_chars=%d "
                                "ratio=%.2f chars/tok tools=%s finish=%s",
                                _cal_toks, _cal_chars, _cal_chars / _cal_toks,
                                ",".join(_cal_names) or "?", _dr_finish_reason or "?")
                    except Exception:
                        pass
                    _dr_tcs = [result["dr_tool_calls_acc"][k] for k in sorted(result["dr_tool_calls_acc"].keys())]
                    _dr_content_joined = "".join(result["dr_content_parts"]).strip()
                    if _dr_content_joined and "<think" in _dr_content_joined:
                        _dr_content_joined = _RE_THINK_CLEANUP.sub("", _dr_content_joined).strip()
                    # AUDIT-R2 N3 (2026-08-25): Bei Abort akkumulierte Tool-Calls
                    result["dr_msg"] = {"role": "assistant", "content": _dr_content_joined or None,
                                        "tool_calls": [] if _dr_aborted_mid else _dr_tcs}
                    if _dr_aborted_mid and not _dr_finish_reason:
                        result["dr_finish_reason"] = "abort"
                    result["dr_stream_ok"] = True
                    break

            except httpx.ConnectError:
                if _post_attempt < self.cfg.max_post_attempts - 1:
                    _dr_post_errors["ConnectError"] = _dr_post_errors.get("ConnectError", 0) + 1
                    _srv_alive = False
                    try:
                        _hr = await self._client.get(f"http://127.0.0.1:{result['dport']}/health",
                                                      timeout=httpx.Timeout(connect=2.0, read=2.0, write=1.0, pool=1.0))
                        _srv_alive = (_hr.status_code == 200)
                    except Exception:
                        logger.debug("Health check failed on port %s", result["dport"], exc_info=True)
                    if not _srv_alive:
                        try:
                            from backend.llama_server_manager import manager as _lsm_cr
                            await _lsm_cr.evict(self.round_state.exec_model)
                        except Exception:
                            logger.debug("Evict failed during crash recovery for %s", self.round_state.exec_model, exc_info=True)
                        await self._emit({"type": "status",
                            "content": f"? llama-server for {self.round_state.exec_model} crashed — restarting (attempt {_post_attempt+2}/{self.cfg.max_post_attempts})"})
                    else:
                        await self._emit({"type": "status",
                            "content": f"? Port {result['dport']} unreachable, retrying in 5s"})
                    await asyncio.sleep(5.0)
                    result["dport"] = await self._ensure_loaded(self.round_state.exec_model)
                    self.round_state.cached_port = result["dport"]
                else:
                    raise
            except (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError) as _dr_rt_err:
                _dr_post_errors[type(_dr_rt_err).__name__] = _dr_post_errors.get(type(_dr_rt_err).__name__, 0) + 1
                if _dr_stream_partial:
                    raise RuntimeError(
                        "stream interrupted after partial output; no retry to avoid duplicated tokens"
                    ) from _dr_rt_err
                if _post_attempt < self.cfg.max_post_attempts - 1:
                    await asyncio.sleep(3.0)
                    await self._emit({"type": "status", "content": f"? Stream interrupted, retry ({_post_attempt+1}/{self.cfg.max_post_attempts})"})
                    continue
                raise

        if not result.get("dr_stream_ok"):
            _err_detail = ", ".join(f"{k}={v}x" for k, v in sorted(_dr_post_errors.items())) if _dr_post_errors else "none"
            raise RuntimeError(f"Tool-POST: stream not received after {_post_attempt+1} attempts. Errors: {_err_detail}"
                              + (f" | last 500 body: {self.round_state.last_err_body}" if self.round_state.last_err_body else ""))

        return result

    #  Helpers 

    async def _emit(self, event: dict) -> None:
        # THINKING-RESCUE (2026-08-22): buffer events serially — so the
        # duo_runner can replay the thinking chunks when the stream was aborted.
        try:
            self._pending_events.append(
                f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            )
            if len(self._pending_events) > 400:
                del self._pending_events[: len(self._pending_events) - 400]
        except Exception:
            pass
        if self._emit_fn:
            try:
                await self._emit_fn(event)
            except Exception:
                pass

    async def _ensure_loaded(self, model: str) -> int:
        from backend.llama_server_manager import manager as _lsm
        return await _lsm.ensure_loaded(model, num_ctx=self.round_state.dtool_opts.get("num_ctx", 4096), n_parallel=1)

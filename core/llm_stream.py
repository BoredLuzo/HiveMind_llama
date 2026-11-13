"""Server-free LLM stream wrappers (2026-11-05 cycle break 3/5).

`_pipeline_chat_stream` lived in server.py; learning/peer_ratings had to
import the running server to reach it (and the direct-rating branch forgot
the import entirely — a latent NameError). All deps are reachable via
core.state / core.duo_helpers, so the function is happy in the core layer.
server.py keeps a re-export shim for its remaining internal callers.
"""
from __future__ import annotations

import logging

from core.duo_helpers import count_image_parts

_logger = logging.getLogger("hivemind.server")


def _history_budget_tokens(settings) -> int:
    """History budget: override wins; 0 derives from the direct model's
    ctx (35% of n_ctx minus a 4096 reserve for system prompt + output).

    Lives here (not in chat_run) so the chat_* split helpers can import it
    without a deferred back-import into the runner module.
    """
    from core.state import registry_get, _get_num_ctx

    _ov = int(settings.get("session_history_budget_tokens", 0) or 0)
    if _ov > 0:
        return _ov
    try:
        _ref = registry_get("direct") or registry_get("analyst") or ""
        _nctx = int(_get_num_ctx(_ref) or 0) if _ref else 0
    except (OSError, RuntimeError, ValueError, TypeError, LookupError,
            AttributeError, ImportError):
        _nctx = 0
    # NOTE: with llama-server parallel slots the per-slot ctx is
    # n_ctx / n_parallel - divide here if parallel slots ever get enabled
    # (the current config runs one slot, default n_parallel=1).
    if _nctx <= 0:
        _nctx = 16384  # sane default when no ctx override is configured
    return max(1024, int((_nctx - 4096) * 0.35))


async def _pipeline_chat_stream(model: str, msgs: list, temp: float | None, max_tok: int,
                                agent_role: str | None = None,
                                force_ctx: int | None = None,
                                think: bool | None = None,
                                thinking_budget: int | None = None,
                                split_thinking: bool = False,
                                no_cache: bool = False,
                                usage_out: dict | None = None,
                                sampler: dict | None = None):
    from core.state import _get_num_ctx_with_source, pipeline, settings as _st_settings
    from core.model_sampling import resolve_agent_sampling

    if sampler is None or temp is None:
        _r_temp, _r_think, _r_budget, _r_smp = resolve_agent_sampling(
            model,
            temperature_override=temp,
            thinking_override=think,
            thinking_budget_override=thinking_budget,
            settings=_st_settings,
            role=agent_role or "",
        )
        if temp is None:
            temp = _r_temp
        if think is None and agent_role:
            think = _r_think
        if thinking_budget is None and _r_budget:
            thinking_budget = _r_budget
        if sampler is None:
            sampler = _r_smp

    if force_ctx:
        effective_ctx = force_ctx
    else:
        num_ctx = _get_num_ctx_with_source(model, agent_role)[0]
        effective_ctx = num_ctx if num_ctx else 8192
    # IMAGES-IN-REQUEST (2026-10-04): per-model-call trace - how many
    # image parts actually went on the wire. Makes "did the model SEE the
    # image" checkable in the log instead of arguable. Duo roles log ALWAYS
    # (also 0 - "the image stopped being sent" after a compression must be
    # visible, not look like a missing line); every other role logs on >0.
    _n_imgs = count_image_parts(msgs) + sum(
        1 for _m in msgs if isinstance(_m, dict) and _m.get("images"))
    if _n_imgs or (agent_role or "").startswith("duo"):
        _logger.info("[LLM-CALL] model=%s role=%s images_in_request=%d ctx=%d",
                     model, agent_role or "-", _n_imgs, effective_ctx)
    _extra_kw = {"sampler": sampler} if sampler is not None else {}
    async for tok in pipeline.ollama.chat_stream(model, msgs, temp, max_tok, ctx=effective_ctx,
                                                  think=think, thinking_budget=thinking_budget,
                                                  split_thinking=split_thinking,
                                                  no_cache=no_cache, usage_out=usage_out,
                                                  **_extra_kw):
        yield tok

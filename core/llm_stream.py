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


async def _pipeline_chat_stream(model: str, msgs: list, temp: float, max_tok: int,
                                agent_role: str | None = None,
                                force_ctx: int | None = None,
                                think: bool | None = None,
                                thinking_budget: int | None = None,
                                split_thinking: bool = False,
                                no_cache: bool = False,
                                usage_out: dict | None = None):
    from core.state import _get_num_ctx_with_source, pipeline

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
    async for tok in pipeline.ollama.chat_stream(model, msgs, temp, max_tok, ctx=effective_ctx,
                                                  think=think, thinking_budget=thinking_budget,
                                                  split_thinking=split_thinking,
                                                  no_cache=no_cache, usage_out=usage_out):
        yield tok

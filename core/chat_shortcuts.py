"""Chat-run memory and self-query shortcuts (2026-11-06 split 3.1).

The four early exits at the top of run_stream: list memories, forget,
store a memory, and the "who are you / what is your configuration"
self-query. Verbatim code motion — no logic changes. Each exit sets
`ps.handled = True` and returns; the runner does the `return`.
"""
from __future__ import annotations

import dataclasses
import logging
import time

from context.chat_util import _extract_memory, _make_messages
from core import state as _state
from core.state import registry_all
from hive_functions.prompts import HIVEMIND_SOUL
from hive_functions.soul_engine import build_soul_prompt_layer
from infra.run_control import _unregister_abort, _unregister_step_skip
from model_configs import get_learned_config, read_learning_log

logger = logging.getLogger("hivemind.chat_run")


@dataclasses.dataclass
class _ChatShortcutState:
    """Shortcut inputs (split 3.1); `handled` is the early-exit signal,
    `carry` holds the runner closures (emit, _done_event)."""
    user_input: str = ""
    images: list = dataclasses.field(default_factory=list)
    run_id: str = ""
    mode: str = ""
    iterations: int = 0
    use_learned: bool = False
    constraint_mode: bool = False
    t_total: float = 0.0
    pipeline_soul: dict = dataclasses.field(default_factory=dict)
    handled: bool = False
    carry: dict = dataclasses.field(default_factory=dict)


async def _chat_memory_shortcuts(ps):
    from server import S_models_cache, THIS_FILE
    from core.llm_stream import _pipeline_chat_stream
    emit = ps.carry["emit"]
    _done_event = ps.carry["_done_event"]
    user_input = ps.user_input
    images = ps.images
    run_id = ps.run_id
    mode = ps.mode
    iterations = ps.iterations
    use_learned = ps.use_learned
    constraint_mode = ps.constraint_mode
    t_total = ps.t_total
    _pipeline_soul = ps.pipeline_soul

    # Memory special cases
    if _state.pipeline._is_list_memory_request(user_input):
        logger.warning("[RUN-ENTRY] early return: list_memory_request")
        mems = list(_state.memory.list_memories())
        if not mems:
            yield await emit({"type": "token", "content": "Nothing stored."})
        else:
            for k, v, d in mems:
                yield await emit({"type": "token", "content": f"{k}: {v} ({d})\n"})
        yield await emit(_done_event(0, "memory_list"))
        _unregister_abort(run_id)
        _unregister_step_skip(run_id)
        ps.handled = True
        return

    if _state.pipeline._is_forget_request(user_input):
        logger.warning("[RUN-ENTRY] early return: forget (user_input=%s)", user_input[:80].replace("\n", " "))
        words   = user_input.lower().split()
        deleted = [k for k in list(_state.memory.get_all()) if k in words]
        for k in deleted:
            _state.memory.forget(k)
        msg = f"Deleted: {', '.join(deleted)}" if deleted else "Nothing found."
        yield await emit({"type": "token", "content": msg})
        yield await emit(_done_event(0, "memory_forget"))
        _unregister_abort(run_id)
        _unregister_step_skip(run_id)
        ps.handled = True
        return

    if _state.pipeline._is_memory_request(user_input):
        logger.warning("[RUN-ENTRY] early return: memory_request")
        _mem_key, _mem_val = _extract_memory(user_input)
        if _mem_key and _mem_val:
            _state.memory.remember(_mem_key, _mem_val)
            result = f"Stored: {_mem_key} = {_mem_val}"
        else:
            result = await _state.pipeline._handle_memory_request(user_input)
        _state.memory.add_to_session("user", user_input)
        _state.memory.add_to_session("assistant", result)
        yield await emit({"type": "token", "content": result})
        yield await emit({"type": "memory_saved"})
        yield await emit(_done_event(0, "memory_write"))
        _unregister_abort(run_id)
        _unregister_step_skip(run_id)
        ps.handled = True
        return

    # Self-query
    if _state.pipeline._is_self_question(user_input):
        logger.warning("[RUN-ENTRY] early return: self_question")
        reg         = registry_all()
        model_lines = []
        for a_key, a in _state.pipeline.agents.items():
            model       = reg.get(a_key, a.model)
            has_learned = get_learned_config(THIS_FILE, model, a_key) is not None
            log_entries = len(read_learning_log(THIS_FILE, model, limit=100))
            model_lines.append(
                f"- {a.name} ({a_key}): {model}"
                + (" [learned config]" if has_learned else "")
                + (f" [{log_entries} log entries]" if log_entries else "")
            )
        model_info = "\n".join(model_lines)
        mem_count  = len(list(_state.memory.list_memories()))
        soul       = _pipeline_soul
        soul_layer = build_soul_prompt_layer(soul)

        custom_sys = (
            HIVEMIND_SOUL
            + (f"\n\n{soul_layer}" if soul_layer else "")
            + f"\n\nCurrent configuration:\n{model_info}"
            + f"\nIterations: {iterations}  Mode: {mode}"
            + f"\nPersistent memory entries: {mem_count}"
            + f"\nLearning preset mode: {'ON -- peer ratings active' if use_learned else 'off'}"
            + f"\nConstraint feedback loop: {'ON -- adversarial JSON constraints' if constraint_mode else 'off'}"
            + f"\nAutomap: {'available' if S_models_cache else 'no model cache'}"
            + f"\nSoul evolution: v{soul.get('version', 1)}, {soul.get('evolution_count', 0)} evolutions"
        )

        agent    = _state.pipeline.agents["direct"]
        messages = _make_messages(_state.pipeline, custom_sys, user_input, images, False, False)

        yield await emit({"type": "agent", "content": "Hivemind", "model": agent.model})

        parts, t = [], time.time()
        async for tok in _pipeline_chat_stream(agent.model, messages, 0.3, 600):  # BUG-11 FIX: num_ctx via wrapper
            parts.append(tok)
            yield await emit({"type": "token", "content": tok})
        yield await emit({"type": "agent_done", "elapsed": round(time.time() - t, 1)})
        _state.memory.add_to_session("user", user_input)
        _state.memory.add_to_session("assistant", "".join(parts))
        yield await emit(_done_event(round(time.time() - t_total, 1), "self_query_completed"))
        _unregister_abort(run_id)
        _unregister_step_skip(run_id)
        ps.handled = True
        return

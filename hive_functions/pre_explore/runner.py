"""Pre-Explore: run_pre_explore-Orchestrierung (Teil von hive_functions/pre_explore)."""

from __future__ import annotations

from backend.llama_models import _strip_alias
import asyncio
import time

from .context import _build_explore_ctx
from .contracts import _contract_fail_count
from .partition import _make_fallback_partition
from .context import _merge_partition_messages
from .llm import _reset_pre_explore_usage
from .llm import _snapshot_pre_explore_usage
from .partition import _worker_drain
from .contracts import logger

async def run_pre_explore(
    *,
    model:         str,
    port:          int,
    partitions:    list[dict],
    workspace:     str,
    tree_ctx:      str = "",
    user_input:    str = "",
    worker_slots:  list[dict] | None = None,
    parallel:      bool = True,
    msg_cap:       int = 14,
    max_tool_rounds: int = 20,
    websearch_fn=None,
    aborted_fn=None,
    emit_fn=None,
    pctx:          int = 0,
    thinking_override: bool | None = None,
    llm_read_timeout: float = 300.0,
) -> tuple[str, list, list]:


    global _contract_fail_count
    _contract_fail_count = 0
    _reset_pre_explore_usage()
    if not partitions:
        partitions = await _make_fallback_partition(workspace, tree_ctx, user_input)
        if not partitions:
            logger.warning("run_pre_explore: no partitions and fallback empty - skipping explore")
            return "", [], []
        logger.info("run_pre_explore: fallback partition generated (%d files)", len(partitions[0].get("paths", [])))

    # Worker-Slots aufbauen
    slots: list[dict] = []
    if parallel and worker_slots:
        slots = list(worker_slots)
    if not slots:
        slots = [{
            "model": _strip_alias(model),
            "port":  port,
            "key":   f"{_strip_alias(model)}@{port}",
            "ctx":   pctx,
        }]

    n_workers = len(slots)
    n_parts = len(partitions)
    logger.info(
        "run_pre_explore: %d partitions, %d workers, pctx=%d (LLM-based)",
        n_parts, n_workers, pctx,
    )

    # Sibling-Map vorab berechnen
    _sibling_map: dict[str, list[str]] = {}
    for part in partitions:
        if part is None:
            continue
        _lbl = part.get("label", "?")
        _base = _lbl.split(":sz")[0]
        _sibling_map.setdefault(_base, []).append(_lbl)

    queue: asyncio.Queue = asyncio.Queue()
    for part in partitions:
        if part is not None:
            await queue.put(part)

    results: list[dict] = []

    # Worker starten
    tasks = []
    for i, slot in enumerate(slots):
        slot_ctx = int(slot.get("ctx") or slot.get("num_ctx") or pctx)
        task = asyncio.create_task(
            _worker_drain(
                worker_model=slot.get("model", model),
                worker_port=int(slot.get("port", port)),
                worker_key=slot.get("key", f"{_strip_alias(slot.get('model', model))}@{slot.get('port', port)}"),
                worker_idx=i,
                worker_total=n_workers,
                queue=queue,
                results=results,
                workspace=workspace,
                tree_ctx=tree_ctx,
                pctx=slot_ctx,
                task=user_input,
                sibling_map=_sibling_map,
                max_tool_rounds=max_tool_rounds,
                emit_fn=emit_fn,
                aborted_fn=aborted_fn,
                thinking_override=thinking_override,
            )
        )
        tasks.append(task)

    _gather_task = asyncio.create_task(asyncio.gather(*tasks, return_exceptions=True))
    _last_live_emit = 0.0
    while not _gather_task.done():
        await asyncio.sleep(0.5)
        _now_mono = time.monotonic()
        if emit_fn and (_now_mono - _last_live_emit) >= 2.0 and any(t and not t.done() for t in tasks):
            _last_live_emit = _now_mono
            try:
                _lu = _snapshot_pre_explore_usage()
                if _lu.get("completion_tokens"):
                    await emit_fn({"type": "usage_meta", "phase": "pre_explore",
                                   "completion_tokens": int(_lu["completion_tokens"]),
                                   "prompt_tokens": int(_lu.get("prompt_tokens") or 0),
                                   "cached_tokens": int(_lu.get("cached_tokens") or 0)})
                if pctx and int(pctx) > 0:
                    _est = int((_lu.get("prompt_tokens") or 0) + (_lu.get("completion_tokens") or 0))
                    await emit_fn({"type": "ctx_meter", "est_tokens": _est,
                                   "ctx_limit": int(pctx), "compressing": False})
            except Exception:
                pass
    _gather_out = await _gather_task

    # Log task-level exceptions from gather (crashes BEFORE results.append)
    if _gather_out:
        import traceback as _tb_ex2
        for _i, _r in enumerate(_gather_out):
            if isinstance(_r, BaseException):
                logger.error("[PRE-EXPLORE-GATHER-EXCEPTION] Worker %d crashed: %s\n%s", _i, _r, _tb_ex2.format_exception(type(_r), _r, _r.__traceback__))

    # Filter non-dict results — log exceptions instead of silently dropping them
    _valid = []
    for _i, _r in enumerate(results):
        if isinstance(_r, BaseException):
            import traceback as _tb_ex
            logger.error("[PRE-EXPLORE-EXCEPTION] Worker %d crashed: %s\n%s", _i, _r, _tb_ex.format_exception(type(_r), _r, _r.__traceback__))
        elif isinstance(_r, dict):
            _valid.append(_r)
        else:
            logger.warning("[PRE-EXPLORE-UNKNOWN] Worker %d returned non-dict: %s", _i, type(_r).__name__)
    results = _valid

    contracts = [r.get("contract", {}) for r in results if r.get("contract")]

    # Explore-Context bauen
    explore_ctx = _build_explore_ctx(results)

    # Statistiken
    total_files_read = sum(r.get("n_files_read", 0) for r in results)
    total_read_calls = sum(r.get("read_calls", 0) for r in results)
    total_files = sum(len(p.get("paths") or p.get("files") or []) for p in partitions if p is not None)

    # Collect chat messages from all workers for coder bridge
    all_msgs = _merge_partition_messages(results)

    logger.info(
        "run_pre_explore done — %d partitions, %d/%d files read (%d tool calls), %d contracts, ctx=%d chars, %d bridge msgs",
        len(results), total_files_read, total_files, total_read_calls,
        len(contracts), len(explore_ctx), len(all_msgs),
    )

    _u = _snapshot_pre_explore_usage()
    if _u.get("completion_tokens") and emit_fn:
        try:
            await emit_fn({"type": "usage_meta", "phase": "pre_explore",
                           "completion_tokens": int(_u["completion_tokens"]),
                           "prompt_tokens": int(_u.get("prompt_tokens") or 0),
                           # TOKEN-TRACKER (2026-08-25): cached mitliefern — fehlte,
                           "cached_tokens": int(_u.get("cached_tokens") or 0)})
        except Exception:
            pass

    return explore_ctx, results, contracts, all_msgs

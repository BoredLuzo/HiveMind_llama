"""Explore-only round detection (F13, 2026-11-08).

The per-round exploration-loop detector and the write-tool reset lived
inline in run_code_duo. Moved into a small async helper because the
run_bash snapshot uses `asyncio.to_thread`; the runner keeps the break.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging

from core.duo_helpers import _collect_new_explore_paths, _explore_size_tolerance
from hive_functions.chunking import compute_bash_changed, resolve_explore_reset

logger = logging.getLogger("hivemind.duo")

_EXPLORE_TOOLS = {"find_files", "search_code", "list_dir", "read_file"}
_WRITE_TOOLS = {"write_file", "edit_file", "patch_file", "write_file_append",
                "replace_lines", "undo_last", "run_bash", "run_python"}


@dataclasses.dataclass
class _ExploreRoundResult:
    explore_only_rounds: int = 0
    loop_detected: bool = False
    action: str = "proceed"          # "break" when the threshold is reached
    events: list = dataclasses.field(default_factory=list)


async def _explore_round_gate(
    ctx, *, round_tool_names, dr_tcs, seen_explore_paths,
    explore_only_rounds, contracts_raw, explore_ctx, exec_mdl,
    until_finished, snap_before, ws_str, pre_explore_msgs, written_files,
    ld_setter, snapshot_fn,
) -> _ExploreRoundResult:
    """Update the explore-only streak or reset it on write tools."""
    res = _ExploreRoundResult(explore_only_rounds=explore_only_rounds)
    if round_tool_names and round_tool_names.issubset(_EXPLORE_TOOLS):
        _new_explore = _collect_new_explore_paths(dr_tcs, seen_explore_paths)
        if not _new_explore:
            res.explore_only_rounds += 1
        _total_exports = sum(len(c.get("exports", [])) for c in (contracts_raw or []))
        _any_fallback = any(c.get("_fallback") for c in (contracts_raw or [])
                            if isinstance(c, dict))
        _explore_was_partial = not explore_ctx or _total_exports < 3 or _any_fallback
        _partial_bonus = 6 if _explore_was_partial else 4
        _threshold = _partial_bonus if _total_exports == 0 else (5 if _any_fallback else _partial_bonus)
        _threshold = max(2, _threshold + _explore_size_tolerance(exec_mdl))
        if until_finished:
            _threshold *= 2
        if res.explore_only_rounds:
            logger.warning(
                "[EXPLORE-METER] explore_only_rounds=%d/%d (exports=%d partial=%s fallback=%s)",
                res.explore_only_rounds, _threshold,
                _total_exports, _explore_was_partial, _any_fallback)
        if res.explore_only_rounds >= _threshold:
            res.events.append(await ctx.emit({
                "type": "status",
                "content": f"⚠ Aborting: {res.explore_only_rounds} explore-only rounds without progress.",
            }))
            res.events.append(await ctx.emit({
                "type": "token",
                "content": (f"\n⚠ [Until-Finished] {res.explore_only_rounds} consecutive "
                            "explore-only rounds — no progress. Stopping."),
            }))
            ld_setter(3408)
            res.loop_detected = True
            res.action = "break"
        return res
    if round_tool_names & _WRITE_TOOLS:
        _bash_changed = None
        if round_tool_names == {"run_bash"} and snap_before is not None:
            _snap_after = await asyncio.to_thread(
                snapshot_fn, ws_str, explore_ctx, pre_explore_msgs,
                written_files, contracts_raw)
            _bash_changed = compute_bash_changed(snap_before, _snap_after)
        res.explore_only_rounds = resolve_explore_reset(
            round_tool_names, _bash_changed, res.explore_only_rounds)
    return res

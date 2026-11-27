# -*- coding: utf-8 -*-
"""Regression suite: 400+ file large-repo exploration across Agentic & Direct modes."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.budgets import resolve, set_active  # noqa: E402
from core.duo_compress_cycle import (  # noqa: E402
    _format_compress_inventory,
    _walk_workspace_inventory,
)
from core.duo_ctx_meter import _ctx_meter_round, _ctx_shrink_round  # noqa: E402
from core.duo_explore_gate import _explore_round_gate  # noqa: E402
from core.plan_tracker import Plan, PlanStep, PlanTracker  # noqa: E402
from core.tool_exec_helpers import ToolRoundState, _track_same_file_read  # noqa: E402
from core.tool_guards import make_tool_context_compactor  # noqa: E402
from tests._harness import Harness  # noqa: E402
from tools.limits import READ_SAME_FILE_ABORT  # noqa: E402
from tools.runner import (  # noqa: E402
    _files_read_in_run,
    _files_seen_in_run,
    _run_inline_tool,
    get_read_file_stats,
    reset_read_file_stats,
)

h = Harness()
check = h.check


class _DummyCtx:
    async def emit(self, ev):
        return ev


class _FakeLRU:
    def __init__(self, entries):
        self._entries = [dict(e) for e in entries]

    def candidates(self):
        return [e for e in self._entries if not e.get("evicted")]

    def mark_evicted(self, idx):
        for e in self._entries:
            if e.get("idx") == idx:
                e["evicted"] = True


async def _run_all() -> None:
    import core.state as _st
    _prev_lsp = _st.settings.get("lsp_enabled")
    _st.settings["lsp_enabled"] = False
    set_active(resolve(32768, 3.8, 4096, {}))

    try:
      with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        ws = Path(tmp)
        total_files = 420
        for i in range(total_files):
            pkg = ws / f"pkg_{i // 30:02d}"
            pkg.mkdir(parents=True, exist_ok=True)
            fp = pkg / f"mod_{i:03d}.py"
            fp.write_text(
                f"class Service{i}:\n"
                f"    def compute_{i}(self, x: int) -> int:\n"
                f"        return x + {i}\n",
                encoding="utf-8",
            )
        # Secret file must never leak into directory signatures or find_files
        (ws / ".env").write_text("API_SECRET=hunter2\n", encoding="utf-8")

        # ── 1. find_files default 150 + pagination / limit=500 over 420 files ──
        ff_default = await _run_inline_tool(
            "find_files", {"pattern": "**/*.py"}, workspace_lock=str(ws), tool_mode="duo_core"
        )
        ff_def_lines = [ln for ln in ff_default.splitlines() if ln and not ln.startswith("…")]
        check(
            "find_files default returns 150 lines + more-than-150 pagination hint",
            len(ff_def_lines) == 150 and "more than 150 matches" in ff_default and "offset=150" in ff_default,
            f"lines={len(ff_def_lines)}",
        )

        ff_all = await _run_inline_tool(
            "find_files",
            {"pattern": "**/*.py", "limit": 500, "offset": 0},
            workspace_lock=str(ws),
            tool_mode="duo_core",
        )
        ff_all_lines = [ln for ln in ff_all.splitlines() if ln and not ln.startswith("…")]
        check(
            "find_files limit=500 lists all 420 files in one call without secrets",
            len(ff_all_lines) == total_files and ".env" not in ff_all,
            f"got={len(ff_all_lines)}",
        )

        # ── 2. Directory get_signatures paginates cleanly across all 420 files ──
        offset = 0
        pages = 0
        covered_headers = 0
        while pages < 20:
            sig_out = await _run_inline_tool(
                "get_signatures",
                {"path": ".", "offset": offset, "max_items": 150},
                workspace_lock=str(ws),
                tool_mode="duo_core",
            )
            pages += 1
            check(f"dir get_signatures page {pages} excludes .env", ".env" not in sig_out and "hunter2" not in sig_out)
            covered_headers += sum(1 for ln in sig_out.splitlines() if ln.startswith("### pkg_"))
            if "[end of directory signatures:" in sig_out:
                break
            import re as _re
            m = _re.search(r"offset=(\d+)", sig_out)
            if not m:
                break
            offset = int(m.group(1))
        check(
            "directory get_signatures covers all 420 files in <=6 paginated calls",
            covered_headers == total_files and 1 <= pages <= 6,
            f"covered={covered_headers} pages={pages}",
        )

        # ── 3. Batch read_file(path=..., paths=[...]) + READ_REQUIRED + cap defer ──
        read_set: set[str] = set()
        seen_set: set[str] = set()
        tok_r = _files_read_in_run.set(read_set)
        tok_s = _files_seen_in_run.set(seen_set)
        reset_read_file_stats()
        try:
            batch_targets = [f"pkg_00/mod_{i:03d}.py" for i in range(1, 12)]
            batch_out = await _run_inline_tool(
                "read_file",
                {"path": "pkg_00/mod_000.py", "paths": batch_targets},
                workspace_lock=str(ws),
                tool_mode="duo_core",
            )
            stats = get_read_file_stats()
            check(
                "batch read_file reads all 12 files in 1 call and records read-guard + stats",
                "Service0" in batch_out
                and "Service11" in batch_out
                and len(read_set) == 12
                and len(stats["paths"]) == 12,
                f"read_set={len(read_set)} stats={len(stats['paths'])}",
            )

            # edit_file on a batch-read file must pass READ_REQUIRED immediately
            edit_res = await _run_inline_tool(
                "edit_file",
                {
                    "path": "pkg_00/mod_005.py",
                    "old_text": "return x + 5",
                    "new_text": "return x + 500",
                },
                workspace_lock=str(ws),
                tool_mode="duo_core",
            )
            check(
                "edit_file succeeds on file read via batch read_file (no READ_REQUIRED block)",
                "READ_REQUIRED" not in edit_res and not edit_res.startswith("[TOOL_ERROR"),
                edit_res[:160],
            )
        finally:
            _files_read_in_run.reset(tok_r)
            _files_seen_in_run.reset(tok_s)

        # ── 4. PlanTracker: 420 consecutive unrelated file reads never stall ──
        plan_obj = Plan(
            id="p1",
            intent="optimize quantumflux calculation",
            allowed_paths=["pkg_13/mod_419.py"],
            steps=[
                PlanStep(
                    id="1",
                    intent="optimize quantumflux calculation",
                    keywords=["quantumflux", "service419", "compute_419"],
                    expected_ops=["read", "edit"],
                    expected_paths=["pkg_13/mod_419.py"],
                )
            ],
            current_step_id="1",
        )
        tracker = PlanTracker(plan=plan_obj, workspace=str(ws))
        any_replan = False
        for i in range(total_files):
            rel = f"pkg_{i // 30:02d}/mod_{i:03d}.py"
            tracker.tick(
                tool_name="read_file",
                tool_result=f"[{rel} total lines: 3]\nclass Service{i}: pass",
                touched_paths=[rel],
                file_changes_count=0,
            )
            if tracker.needs_replan():
                any_replan = True
                break
        check(
            "PlanTracker allows 420 consecutive unrelated reads without replan/stall",
            not any_replan and tracker._plan.deviation.status != "DEVIATES",
            f"any_replan={any_replan} status={tracker._plan.deviation.status} streak={tracker._plan.deviation.streak}",
        )

        # Unrelated edit outside allowed_paths MUST still trigger hard DEVIATES
        tracker.tick(
            tool_name="edit_file",
            tool_result="Edited pkg_00/mod_000.py",
            touched_paths=["pkg_00/mod_000.py"],
            file_changes_count=1,
        )
        tracker.tick(
            tool_name="edit_file",
            tool_result="Edited pkg_00/mod_001.py",
            touched_paths=["pkg_00/mod_001.py"],
            file_changes_count=2,
        )
        check(
            "PlanTracker still flags out-of-scope edits as DEVIATES + needs_replan",
            tracker._plan.deviation.status == "DEVIATES" and tracker.needs_replan(),
        )

        # ── 5. Explore gate + masking sync + same-file-read across 420 files ──
        seen_explore: set[str] = set()
        explore_rounds = 0
        trs = ToolRoundState()
        max_same_cnt = 0
        dctx = _DummyCtx()
        for i in range(total_files):
            rel = f"pkg_{i // 30:02d}/mod_{i:03d}.py"
            tcs = [{"function": {"name": "read_file", "arguments": {"path": rel}}}]
            eg = await _explore_round_gate(
                dctx,
                round_tool_names={"read_file"},
                dr_tcs=tcs,
                seen_explore_paths=seen_explore,
                explore_only_rounds=explore_rounds,
                contracts_raw=[],
                explore_ctx="",
                exec_mdl="qwen3.5:9b",
                until_finished=False,
                snap_before=None,
                ws_str=str(ws),
                pre_explore_msgs=[],
                written_files=[],
                ld_setter=lambda *_: None,
                snapshot_fn=lambda *_: {},
            )
            explore_rounds = eg.explore_only_rounds
            _, cnt = _track_same_file_read(
                trs, "read_file", False, rel, dargs={"path": rel}, is_tool_error=False
            )
            max_same_cnt = max(max_same_cnt, cnt)
        check(
            "420 distinct read_file rounds keep explore_only_rounds=0 and same_file_cnt=1",
            explore_rounds == 0 and max_same_cnt == 1 and max_same_cnt < READ_SAME_FILE_ABORT,
            f"explore_rounds={explore_rounds} max_same={max_same_cnt}",
        )

        # Masking evicts an old read_file output and exposes masked_paths so re-reading is fresh
        mask_msgs = [
            {"role": "system", "content": "SYS"},
            {"role": "user", "content": "Audit all files"},
            {"role": "tool", "name": "read_file", "content": "A " * 1200},
        ]
        mask_lru = _FakeLRU([{"idx": 2, "path": "pkg_00/mod_000.py", "kind": "read_file"}])
        masked_out: list[str] = []
        sr = _ctx_shrink_round(
            dtool_ctx=1000,
            compress_threshold=600,
            est_tokens=900,
            guard_tokens=900,
            can_compress=True,
            force_compress_next=False,
            total_tool_rounds=5,
            dtool_msgs=mask_msgs,
            tool_ctx_lru=mask_lru,
            coder_real_prompt_tokens=[900],
            coder_est_at_real=[900],
            recent_focus_paths=[],
            mask_enabled=True,
            cache_horizon=0,
            evicted_paths_out=masked_out,
        )
        check(
            "_ctx_shrink_round reports masked_paths for evicted read_file entries",
            sr.masked_n == 1 and "pkg_00/mod_000.py" in masked_out,
            f"masked_paths={masked_out}",
        )

        # CTX CRITICAL notice does NOT tell model to stop reading when compressions remain
        cm_open = _ctx_meter_round(
            dtool_ctx=10000, dport=1, est_tokens=8700, real_prompt_tokens=8700,
            real_cached_tokens=0, guard_tokens=8700, ctx_compressions=2, max_compressions=40,
            ctx_pressure_peak=0.0, swa_triggered_compress=False, force_compress_next=False,
            ctx_critical_warned=False, ctx_pressure_warned=False,
        )
        cm_exhausted = _ctx_meter_round(
            dtool_ctx=10000, dport=1, est_tokens=8700, real_prompt_tokens=8700,
            real_cached_tokens=0, guard_tokens=8700, ctx_compressions=40, max_compressions=40,
            ctx_pressure_peak=0.0, swa_triggered_compress=False, force_compress_next=False,
            ctx_critical_warned=False, ctx_pressure_warned=False,
        )
        check(
            "CTX CRITICAL encourages batching while compressions remain, stops only at cap",
            "CTX CRITICAL" in cm_open.messages[0]["content"]
            and "Stop reading new files" not in cm_open.messages[0]["content"]
            and "Stop reading new files" in cm_exhausted.messages[0]["content"],
        )

        # ── 6. Post-compress inventory tracks read vs unread across 420 files ──
        inv = _walk_workspace_inventory(str(ws), cap=2000)
        read_120 = [f"pkg_{i // 30:02d}/mod_{i:03d}.py" for i in range(120)]
        inv_txt = _format_compress_inventory(inv, str(ws), read_120)
        check(
            "post-compress inventory scales to 420+ files, excludes secrets, and highlights unread progress",
            len(inv) >= total_files
            and ".env" not in inv_txt
            and "[PROJECT FILES" in inv_txt
            and "120/420" in inv_txt
            and "pkg_04/mod_120.py" in inv_txt
            and "pkg_00/mod_000.py" not in inv_txt,
            inv_txt[:240],
        )
    finally:
        if _prev_lsp is None:
            _st.settings.pop("lsp_enabled", None)
        else:
            _st.settings["lsp_enabled"] = _prev_lsp

    # ── 7. Direct Mode rolling tool-context compactor handles 100+ rounds ──
    compactor = make_tool_context_compactor(num_ctx=4096, keep_recent_tools=4)
    small_msgs = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "Inspect repo"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "[a.py total lines: 10]\nprint(1)"},
    ]
    untouched = await compactor(small_msgs, 0)
    check("Direct compactor leaves under-budget messages untouched (prefix cache safe)", untouched is small_msgs)

    big_msgs: list[dict] = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "Audit all 420 files"},
    ]
    for r in range(80):
        big_msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": f"tc_{r}", "function": {"name": "read_file", "arguments": f'{{"path":"mod_{r}.py"}}'}}],
        })
        big_msgs.append({
            "role": "tool",
            "tool_call_id": f"tc_{r}",
            "name": "read_file",
            "content": f"[mod_{r}.py total lines: 60]\n" + ("x = 1234567890\n" * 40),
        })
    compacted = await compactor(big_msgs, 80)
    chars_before = sum(len(str(m.get("content") or "")) for m in big_msgs)
    chars_after = sum(len(str(m.get("content") or "")) for m in compacted)
    check(
        "Direct compactor shrinks 80-round history below budget while keeping newest reads intact",
        chars_after < chars_before * 0.35
        and "mod_79.py total lines: 60" in compacted[-1]["content"]
        and compacted[0]["role"] == "system",
        f"before={chars_before} after={chars_after} msgs={len(compacted)}",
    )


asyncio.run(_run_all())
sys.exit(h.finish("large_repo_exploration"))

# -*- coding: utf-8 -*-
"""Pyright baseline: fail on NEW type errors, allow the existing debt.

pyright has no native baseline. This compares the current error set with
deploy/pyright_baseline.json, keyed by (file, rule) and count - line shifts do
not invalidate the baseline, a new file/rule or a higher count does.

    python deploy/pyright_baseline.py --write   # regenerate after a fix
    python deploy/pyright_baseline.py           # check (CI)
"""
from __future__ import annotations

import argparse
import collections
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "deploy" / "pyright_baseline.json"

# The core surface: the files the audits touched. Extend deliberately.
TARGETS = [
    "tools/runner.py",
    "core/duo_runner.py",
    "core/tool_loop.py",
    "core/tool_executor.py",
    "core/chat_run.py",
    # Split surface (2026-11-06): the modules the carve moved code into.
    "core/duo_finalize.py",
    "core/duo_epilogue.py",
    "core/duo_finalgate.py",
    "core/duo_round_input.py",
    "core/duo_dtool_setup.py",
    "core/duo_inloop_planner.py",
    "core/duo_coder_load.py",
    "core/duo_coder_preloop.py",
    "core/duo_task_complete_gate.py",
    "core/duo_loop_guards.py",
    "core/duo_compress_cycle.py",
    "core/chat_shortcuts.py",
    "core/chat_tools_route.py",
    "core/chat_vision_pre.py",
    "core/chat_history_seed.py",
    "core/chat_route.py",
    "core/chat_route_decision.py",
    # Cycle-break / extracted surface (Sprint 1).
    "core/llm_stream.py",
    "core/ctx_overrides.py",
    "core/run_context.py",
    "core/stream.py",
    "learning/soul_evolution.py",
    "backend/llama_start_phases.py",
    "deploy/updater.py",
    "infra/mcp_server.py",
    "hive_functions/git_tools.py",
    "deploy/fetch_models.py",
    "routers/config.py",
    "backend/llama_slots.py",
    "backend/runtime_sync.py",
    "infra/run_control.py",
    "infra/lsp_registry.py",
    "vision/preprocess.py",
    # Agentic-loop / helper surface (2026-11-07): high-value modules batch 3.
    "core/agentic_tool_loop.py",
    "core/tool_exec_helpers.py",
    "backend/llama_client.py",
    "backend/llama_config.py",
    "backend/llama_models.py",
    "backend/manager_prefetch.py",
    "tools/definitions.py",
    "tools/sandbox.py",
    "context/chat.py",
    "context/compression.py",
    "context/resume.py",
    "context/pause_state.py",
    "context/ctx_guard.py",
    "infra/ask_user_governor.py",
    "infra/lsp_client.py",
    "infra/mcp_client.py",
    "learning/insights.py",
    "server.py",
    "hivemind_gateway/bridge.py",
    "hivemind_gateway/main.py",
    "hivemind_gateway/render.py",
    "run.py",
    "settings.py",
]


def _pyright_cmd() -> list[str]:
    exe = shutil.which("pyright")
    if exe:
        return [exe, "--level", "error", "--outputjson", *TARGETS]
    return ["npx", "--yes", "pyright", "--level", "error",
            "--outputjson", *TARGETS]


def _run() -> collections.Counter:
    proc = subprocess.run(_pyright_cmd(), cwd=ROOT, capture_output=True,
                          text=True, timeout=900)
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        print(proc.stdout[-2000:], file=sys.stderr)
        print(proc.stderr[-2000:], file=sys.stderr)
        raise SystemExit("pyright produced no JSON output")
    counts: collections.Counter = collections.Counter()
    for diag in data.get("generalDiagnostics") or []:
        if diag.get("severity") != "error":
            continue
        try:
            rel = Path(diag["file"]).resolve().relative_to(ROOT).as_posix()
        except (KeyError, ValueError):
            rel = str(diag.get("file") or "?")
        counts[f"{rel}|{diag.get('rule') or 'unknown'}"] += 1
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="write the current errors as the baseline")
    args = ap.parse_args()
    current = _run()
    total = sum(current.values())
    if args.write:
        BASELINE.write_text(json.dumps(dict(sorted(current.items())), indent=2)
                            + "\n", encoding="utf-8")
        print(f"baseline written: {total} errors in {len(current)} buckets")
        return 0
    if not BASELINE.exists():
        print("no baseline - run with --write first", file=sys.stderr)
        return 1
    base = collections.Counter(json.loads(BASELINE.read_text(encoding="utf-8")))
    new = {k: current[k] - base.get(k, 0) for k in current
           if current[k] > base.get(k, 0)}
    fixed = {k: base[k] - current.get(k, 0) for k in base
             if current.get(k, 0) < base[k]}
    if new:
        print(f"NEW pyright errors: {sum(new.values())}")
        for key, n in sorted(new.items()):
            print(f"  +{n} {key}")
    if fixed:
        print(f"resolved since baseline: {sum(fixed.values())} "
              "(run --write to lower it)")
    print(f"total errors: {total} (baseline {sum(base.values())})")
    return 1 if new else 0


if __name__ == "__main__":
    raise SystemExit(main())

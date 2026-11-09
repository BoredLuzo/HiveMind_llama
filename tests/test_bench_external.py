# -*- coding: utf-8 -*-
"""Behavior checks: bench/external/run_external.py (D8 scaffold, 2026-11-09).

No CLI is ever executed: the harness invocation is stubbed, parsers run on
synthetic claude/codex output, and the run path is verified end-to-end with
fake binaries. Prerequisite discovery (missing binary -> skip) is covered
via explicit nonexistent paths, so the checks are machine-independent.
"""
import io
import json
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bench.external import run_external  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

_CLAUDE_JSON = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "duration_ms": 5123, "num_turns": 3, "result": "done, patched calc.py",
    "session_id": "abc", "total_cost_usd": 0.0123456,
    "usage": {"input_tokens": 40, "cache_creation_input_tokens": 100,
              "cache_read_input_tokens": 200, "output_tokens": 300},
})
_CODEX_JSONL = "\n".join([
    json.dumps({"type": "session_configured",
                "model": "gpt-5.1-codex", "approval_policy": "never"}),
    json.dumps({"type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 500,
                                               "cached_output_tokens": 250,
                                               "output_tokens": 150,
                                               "total_tokens": 900}}}),
    json.dumps({"type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 700,
                                               "cached_output_tokens": 300,
                                               "output_tokens": 200,
                                               "total_tokens": 1200}}}),
])

parsed = run_external.parse_claude(_CLAUDE_JSON, "")
check("claude parse: tokens incl. cached prompt",
      parsed["tokens"] == 640, f" ({parsed['tokens']})")
check("claude parse: cached and net split",
      parsed["cached_tokens"] == 300 and parsed["net_tokens"] == 340,
      f" ({parsed['cached_tokens']}/{parsed['net_tokens']})")
check("claude parse: rounds, cost, reported duration",
      parsed["rounds"] == 3 and parsed["cost_usd"] == 0.012346
      and parsed["duration_reported_s"] == 5.1,
      f" ({parsed['rounds']}/{parsed['cost_usd']}/{parsed['duration_reported_s']})")
check("claude parse: is_error is flagged in detail",
      run_external.parse_claude(
          json.dumps({"is_error": True, "result": "boom"}), "")["detail"]
      .startswith("harness reported is_error"))
check("claude parse: noisy prefix tolerated",
      run_external.parse_claude("warning: update available\n" + _CLAUDE_JSON,
                                "")["tokens"] == 640)
_junk = run_external.parse_claude("not json at all", "stderr noise")
check("claude parse: junk output degrades to zeros, no crash",
      _junk["tokens"] == 0 and _junk["rounds"] == 0
      and _junk["cost_usd"] is None and "not json" in _junk["detail"],
      f" ({_junk})")

parsed = run_external.parse_codex(_CODEX_JSONL, "")
check("codex parse: last token_count event wins",
      parsed["tokens"] == 1200, f" ({parsed['tokens']})")
check("codex parse: cached/net split",
      parsed["cached_tokens"] == 300 and parsed["net_tokens"] == 900,
      f" ({parsed['cached_tokens']}/{parsed['net_tokens']})")
check("codex parse: model detected",
      parsed["model_detected"] == "gpt-5.1-codex", f" ({parsed['model_detected']})")
check("codex parse: no cost field is None",
      parsed["cost_usd"] is None, f" ({parsed['cost_usd']})")
_junk = run_external.parse_codex("no jsonl here\n", "")
check("codex parse: junk degrades to zeros",
      _junk["tokens"] == 0 and _junk["net_tokens"] == 0, f" ({_junk})")

# ── binary discovery ──────────────────────────────────────────────────────
with tempfile.TemporaryDirectory(prefix="hm_bench_ext_t_") as td:
    root = Path(td)
    check("bins: explicit missing path is rejected",
          run_external.resolve_bin("claude", str(root / "no-claude.exe")) == "")
    fake_bin = root / "fake-claude.exe"
    fake_bin.write_text("", encoding="utf-8")
    check("bins: explicit existing path is used",
          run_external.resolve_bin("claude", str(fake_bin)) == str(fake_bin))
    try:
        run_external._parse_harnesses("aider")
        check("harnesses: unknown name rejected", False, " (no SystemExit)")
    except SystemExit as exc:
        check("harnesses: unknown name rejected", "unknown harness" in str(exc),
              f" ({exc})")
    check("harnesses: comma list deduped",
          run_external._parse_harnesses("claude,codex,claude") == ["claude", "codex"])

    # two synthetic tasks, same shape as the runner tests
    for name in ("fix_alpha", "feature_beta"):
        task_dir = root / name
        task_dir.mkdir()
        (task_dir / "task.json").write_text(json.dumps({
            "id": name, "prompt": f"fix {name}",
            "check": "python -c \"pass\"", "timeout_s": 60,
        }), encoding="utf-8")
        (task_dir / "repo").mkdir()
        (task_dir / "repo" / "a.py").write_text("x = 1\n", encoding="utf-8")
    tasks = run_external.load_tasks(root)
    check("tasks: both loaded",
          sorted(t.id for t in tasks) == ["feature_beta", "fix_alpha"])

    # ── plan (--list) ─────────────────────────────────────────────────────
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = run_external.main([
            "--list", "--tasks", str(root),
            "--claude-bin", str(fake_bin),
            "--codex-bin", str(root / "no-codex.exe"),
            "--claude-model", "claude-sonnet-4-5",
        ])
    out = buf.getvalue()
    check("list: exits 0 without executing", code == 0)
    check("list: every task printed",
          "fix_alpha" in out and "feature_beta" in out, f" ({out[:120]!r})")
    check("list: found binary shown with command preview",
          "would run" in out and "--output-format json" in out
          and "condition claude_code" in out,
          f" ({out[:200]!r})")
    check("list: missing binary flagged with prerequisite",
          "NOT FOUND" in out and "OPENAI_API_KEY" in out, f" ({out[:200]!r})")
    check("list: dry run states nothing was claimed", "dry run" in out.lower())
    check("list: model override recorded", "claude-sonnet-4-5" in out)

    _claude_cmd = run_external.build_claude_command(
        "claude.exe", "the prompt", "m1", Path(root), 30)
    check("plan: claude command is -p json non-interactive",
          _claude_cmd[1:4] == ["-p", "the prompt", "--output-format"]
          and "--permission-mode" in _claude_cmd
          and "bypassPermissions" in _claude_cmd
          and _claude_cmd[_claude_cmd.index("--model") + 1] == "m1",
          f" ({_claude_cmd})")
    plan = run_external.plan_cell("codex", tasks[0], "codex.exe", "", 30)
    check("plan: codex command is exec --json with workspace cwd",
          plan["command_preview"].startswith("codex.exe exec --json")
          and "--sandbox workspace-write" in plan["command_preview"]
          and plan["model"] == "default",
          f" ({plan['command_preview']})")

    # ── execute_cell with a stubbed CLI ───────────────────────────────────
    _real_invoke = run_external._invoke

    def _fake_invoke(cmd, work, timeout_s):
        if "fake-claude" in str(cmd[0]):
            return 0, _CLAUDE_JSON, ""
        return 0, _CODEX_JSONL, ""

    run_external._invoke = _fake_invoke
    try:
        work = run_external.prepare_workspace(tasks[0], root / "work")
        row = run_external.execute_cell(
            "claude", tasks[0], str(fake_bin), "", work,
            max_turns=30, timeout_s=60)
        check("cell: pass decided by the shared hidden-test check",
              row["ok"] is True and row["condition"] == "claude_code",
              f" ({row['condition']}/{row['ok']})")
        check("cell: token/cost/round fields recorded",
              row["tokens"] == 640 and row["cached_tokens"] == 300
              and row["rounds"] == 3 and row["cost_usd"] == 0.012346,
              f" ({row['tokens']}/{row['cost_usd']})")
        check("cell: command and model recorded",
              "-p" in row["command"] and row["model"] == "default",
              f" ({row['command'][:60]!r})")
        check("cell: churn metrics like runner rows",
              row["files_changed"] == 0 and row["lines_added"] == 0,
              f" ({row.get('files_changed')})")

        def _timeout_invoke(cmd, work, timeout_s):
            raise subprocess.TimeoutExpired(cmd, timeout_s)

        run_external._invoke = _timeout_invoke
        row = run_external.execute_cell(
            "claude", tasks[0], str(fake_bin), "", work,
            max_turns=30, timeout_s=60)
        check("cell: harness timeout recorded, check still decides",
              "timed out" in row["detail"] and row["tokens"] == 0,
              f" ({row['detail']!r})")
    finally:
        run_external._invoke = _real_invoke

    # ── full run with stubbed CLIs: rows, summary, persistence ────────────
    out_file = root / "ext_results" / "run.json"
    fake_codex = root / "fake-codex.exe"
    fake_codex.write_text("", encoding="utf-8")

    def _run_invoke(cmd, work, timeout_s):
        if "fake-claude" in str(cmd[0]):
            return 0, _CLAUDE_JSON, ""
        return 0, _CODEX_JSONL, ""

    run_external._invoke = _run_invoke
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = run_external.main([
                "--tasks", str(root), "--out", str(out_file),
                "--harness", "claude,codex",
                "--claude-bin", str(fake_bin),
                "--codex-bin", str(fake_codex),
                "--runs", "1",
            ])
    finally:
        run_external._invoke = _real_invoke
    out = buf.getvalue()
    check("run: exits 0", code == 0, f" ({out[-200:]!r})")
    check("run: per-cell progress printed",
          out.count("PASS") == 4 and "claude_code" in out and "codex" in out,
          f" ({out[-200:]!r})")
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    check("run: rows persisted with external metadata",
          saved["kind"] == "external"
          and [r["condition"] for r in saved["results"]]
          == ["claude_code", "codex", "claude_code", "codex"],
          f" ({[r['condition'] for r in saved['results']]})")
    check("run: both tasks ran under both harnesses",
          {r["task"] for r in saved["results"]} == {"fix_alpha", "feature_beta"},
          f" ({[r['task'] for r in saved['results']]})")
    check("run: summary and relative block present",
          saved["summary"]["claude_code"]["pass_rate"] == 1.0
          and saved["relative"]["conditions"]["claude_code"]["delta_vs_raw"] is None,
          f" ({saved['summary'].get('claude_code')})")
    check("run: models recorded", saved["models"] == {"claude": "default",
                                                      "codex": "default"},
          f" ({saved.get('models')})")

    # ── missing binaries: skip cleanly, exit 3, no result file ────────────
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = run_external.main([
            "--tasks", str(root), "--out", str(root / "none" / "run.json"),
            "--harness", "claude,codex",
            "--claude-bin", str(root / "no-claude.exe"),
            "--codex-bin", str(root / "no-codex.exe"),
        ])
    out = buf.getvalue()
    check("run: all-missing exits 3", code == 3, f" ({code})")
    check("run: each missing harness skipped with its prerequisite",
          out.count("[skip]") == 2 and "ANTHROPIC_API_KEY" in out
          and "OPENAI_API_KEY" in out, f" ({out!r})")
    check("run: no result file written for a skipped run",
          not (root / "none" / "run.json").exists())

sys.exit(h.finish("bench_external"))

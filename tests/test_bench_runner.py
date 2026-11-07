# -*- coding: utf-8 -*-
"""Behavior checks: bench/runner.py (D1 scaffold, 2026-11-07).

No model or server: task loading, workspace isolation, check execution,
diff extraction/application and the aggregation math are covered.
"""
import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bench.runner import (  # noqa: E402
    Config, Task, _extract_patch, _hivemind_body, _parse_token_count,
    _persist, aggregate, apply_patch, load_tasks, prepare_workspace,
    print_summary, run_check,
)
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def _task_dir(root: Path, name: str, *, with_repo: bool) -> None:
    task = root / name
    task.mkdir(parents=True)
    (task / "task.json").write_text(json.dumps({
        "id": name, "prompt": f"fix {name}", "check": "python -c \"pass\"",
        "timeout_s": 30,
    }), encoding="utf-8")
    if with_repo:
        repo = task / "repo"
        repo.mkdir()
        (repo / "a.txt").write_text("one\n", encoding="utf-8")


with tempfile.TemporaryDirectory(prefix="hm_bench_t_") as td:
    root = Path(td)

    # ── task loading ───────────────────────────────────────────────────────
    _task_dir(root, "alpha", with_repo=True)
    hidden = root / "alpha" / "tests"
    hidden.mkdir()
    (hidden / "test_task.py").write_text("hidden\n", encoding="utf-8")
    _task_dir(root, "beta", with_repo=False)
    tasks = load_tasks(root)
    check("load: both tasks found", [t.id for t in tasks] == ["alpha", "beta"],
          f" ({[t.id for t in tasks]})")
    check("load: prompt/check carried",
          tasks[0].prompt == "fix alpha" and tasks[0].check.startswith("python"),
          f" ({tasks[0].prompt!r})")
    check("load: repo only when present",
          tasks[0].repo is not None and tasks[1].repo is None)
    check("load: hidden tests detected",
          tasks[0].tests is not None and tasks[1].tests is None)

    # ── workspace isolation ────────────────────────────────────────────────
    work = prepare_workspace(tasks[0], root / "work")
    check("workspace: repo is copied", (work / "a.txt").read_text(encoding="utf-8") == "one\n")
    (work / "a.txt").write_text("changed\n", encoding="utf-8")
    check("workspace: source repo is untouched",
          (tasks[0].repo / "a.txt").read_text(encoding="utf-8") == "one\n")

    # ── check execution ────────────────────────────────────────────────────
    ok, out = run_check(Task("p", "p", "python -c \"print('ok')\""), work)
    check("check: exit 0 is a pass", ok is True, f" ({out!r})")
    ok, out = run_check(Task("f", "f", "python -c \"raise SystemExit(3)\""), work)
    check("check: non-zero is a fail", ok is False)
    ok, out = run_check(Task("e", "e", "  "), work)
    check("check: empty command fails closed", ok is False and "no check" in out,
          f" ({out!r})")

    # ── hidden tests: installed late, tamper-proof ─────────────────────────
    tamper = prepare_workspace(tasks[0], root / "work_hidden")
    (tamper / "test_task.py").write_text("tampered\n", encoding="utf-8")
    tamper_task = Task(
        "alpha", "fix", "python -c \"import sys; "
        "sys.exit(0 if open('test_task.py').read().strip() == 'hidden' else 1)\"",
        tests=tasks[0].tests,
    )
    ok, out = run_check(tamper_task, tamper)
    check("hidden: agent copy is overwritten before the check", ok is True,
          f" ({out!r})")
    check("hidden: fixture itself is untouched",
          (tasks[0].tests / "test_task.py").read_text(encoding="utf-8") == "hidden\n")
    blocked = prepare_workspace(tasks[0], root / "work_block")
    (blocked / "test_task.py").mkdir()  # a directory must not shadow the test
    ok, out = run_check(tamper_task, blocked)
    check("hidden: a shadowing directory is replaced", ok is True, f" ({out!r})")
    check("hidden: the installed test is a real file",
          (blocked / "test_task.py").is_file())

    # ── diff handling ──────────────────────────────────────────────────────
    fenced = "here\n```diff\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n```\n"
    check("patch: fenced diff is extracted",
          _extract_patch(fenced).startswith("--- a/a.txt"),
          f" ({_extract_patch(fenced)[:20]!r})")
    check("patch: plain text passes through",
          _extract_patch("--- a/x\n") == "--- a/x")
    if shutil.which("git"):
        (work / "a.txt").write_text("one\n", encoding="utf-8")
        patch = "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n"
        ok, detail = apply_patch(patch, work)
        check("patch: valid diff applies", ok is True, f" ({detail})")
        check("patch: file content changed",
              (work / "a.txt").read_text(encoding="utf-8") == "two\n")
        ok, detail = apply_patch("not a diff at all\n", work)
        check("patch: invalid diff is reported", ok is False, f" ({detail})")
    else:
        h.skip("patch: git not installed", "git apply needs git")

    # ── condition bodies / token parsing / incremental result ──────────────
    direct_body = _hivemind_body(Task("t", "p", "c"), work, duo=False,
                                 model="qwen3.5:4b-mtp")
    check("body: direct condition uses a real engine mode",
          direct_body["mode"] == "simple", f" ({direct_body['mode']})")
    check("body: direct condition asks for full tools",
          direct_body.get("direct_tools_enabled") is True
          and direct_body.get("direct_tools_tier") == "full")
    check("body: direct condition forces the same model",
          direct_body.get("direct_model") == "qwen3.5:4b-mtp",
          f" ({direct_body.get('direct_model')})")
    duo_body = _hivemind_body(Task("t", "p", "c"), work, duo=True,
                              model="qwen3.5:4b-mtp")
    check("body: duo condition is code_duo + agentic",
          duo_body["mode"] == "code_duo" and duo_body.get("duo_agentic_mode") is True)
    check("body: duo forces the same model for every role",
          all(duo_body.get(k) == "qwen3.5:4b-mtp"
              for k in ("duo_coder_model", "duo_critic_model", "duo_planner_model")),
          f" ({ {k: duo_body.get(k) for k in ('duo_coder_model','duo_critic_model','duo_planner_model')} })")

    check("tokens: plain number", _parse_token_count("345") == 345)
    check("tokens: thousands separator", _parse_token_count("1,234") == 1234)
    check("tokens: k suffix", _parse_token_count("1.2", "k") == 1200)
    check("tokens: m suffix", _parse_token_count("1.5", "M") == 1_500_000)
    check("tokens: junk is zero", _parse_token_count("nope") == 0)

    out_file = root / "results" / "run.json"
    cfg = Config(tasks_dir=root, out=out_file, model="m")
    _persist(out_file, [{"condition": "raw", "ok": True, "seconds": 1, "tokens": 2}],
             cfg, ["raw"], "2026-11-07T00:00:00")
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    check("results: persisted after each cell",
          saved["results"][0]["condition"] == "raw"
          and saved["summary"]["raw"]["pass_rate"] == 1.0,
          f" ({saved.get('summary')})")

    # ── aggregation ────────────────────────────────────────────────────────
    rows = [
        {"condition": "raw", "ok": False, "seconds": 10, "tokens": 100},
        {"condition": "raw", "ok": False, "seconds": 20, "tokens": 300},
        {"condition": "hivemind_direct", "ok": True, "seconds": 40, "tokens": 500},
        {"condition": "hivemind_direct", "ok": True, "seconds": 60, "tokens": 700},
        {"condition": "hivemind_duo", "ok": True, "seconds": 30, "tokens": 200},
        {"condition": "hivemind_duo", "ok": False, "seconds": 50, "tokens": 400},
    ]
    summary = aggregate(rows)
    check("aggregate: raw baseline is 0.0", summary["raw"]["pass_rate"] == 0.0)
    check("aggregate: direct is 1.0 with +1.0 delta",
          summary["hivemind_direct"]["pass_rate"] == 1.0
          and summary["hivemind_direct"]["delta_vs_raw"] == 1.0,
          f" ({summary['hivemind_direct']})")
    check("aggregate: duo is 0.5 with +0.5 delta",
          summary["hivemind_duo"]["pass_rate"] == 0.5
          and summary["hivemind_duo"]["delta_vs_raw"] == 0.5,
          f" ({summary['hivemind_duo']})")
    check("aggregate: median duration",
          summary["raw"]["median_seconds"] == 15.0,
          f" ({summary['raw']['median_seconds']})")
    check("aggregate: tokens summed",
          summary["hivemind_direct"]["tokens_total"] == 1200,
          f" ({summary['hivemind_direct']['tokens_total']})")

    buf = io.StringIO()
    with redirect_stdout(buf):
        print_summary(summary)
    out = buf.getvalue()
    check("report: prints every condition", all(
        name in out for name in ("raw", "hivemind_direct", "hivemind_duo")), f" ({out!r})")
    check("report: shows the delta column", "+1.00" in out and "+0.50" in out,
          f" ({out!r})")

# ── shipped starter corpus ─────────────────────────────────────────────────
corpus = load_tasks(ROOT / "bench" / "tasks")
check("corpus: at least 20 tasks", len(corpus) >= 20, f" ({len(corpus)})")
check("corpus: ids are unique", len({t.id for t in corpus}) == len(corpus))
check("corpus: every task has prompt/check/tests",
      all(t.prompt and t.check and t.tests is not None for t in corpus),
      f" ({[t.id for t in corpus if not (t.prompt and t.check and t.tests)]})")
check("corpus: every task has a repo fixture",
      all(t.repo is not None for t in corpus),
      f" ({[t.id for t in corpus if t.repo is None]})")
check("corpus: hidden tests use the standard check",
      all("test_task" in t.check for t in corpus),
      f" ({[t.id for t in corpus if 'test_task' not in t.check]})")

sys.exit(h.finish("bench_runner"))

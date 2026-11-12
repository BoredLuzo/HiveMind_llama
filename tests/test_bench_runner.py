# -*- coding: utf-8 -*-
"""Behavior checks: bench/runner.py (D1 scaffold, 2026-11-07).

No model or server: task loading, workspace isolation, check execution,
diff extraction/application and the aggregation math are covered.
"""
import io
import json
import shutil
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bench.runner import (  # noqa: E402
    Config, DEFAULT_CONDITIONS, Task, _agent_timeout_s, _aider_files, _extract_patch,
    _hivemind_body, _load_resume_state, _parse_file_blocks, _parse_pi_json_output,
    _parse_token_count, _persist, _read_hermes_state_usage,
    _resolve_aider_bin, _run_aider, _resolve_hermes_bin, _run_hermes,
    _resolve_pi_bin, _run_pi, _safe_rel_path, aggregate,
    apply_full_files, apply_patch, diff_stats, load_tasks, prepare_workspace,
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

    # ── full-file raw variant ──────────────────────────────────────────────
    _blocks = _parse_file_blocks(
        "noise\n### FILE: calc.py\n```python\ndef add(a, b):\n    return a + b\n```\n"
        "### FILE: new/helper.py\n```python\nX = 1\n```\n")
    check("files: two blocks parsed", list(_blocks) == ["calc.py", "new/helper.py"],
          f" ({list(_blocks)})")
    check("files: content kept", "return a + b" in _blocks.get("calc.py", ""))
    check("files: no blocks -> empty", _parse_file_blocks("just prose") == {})
    check("files: relative path accepted", _safe_rel_path("src/x.py") is not None)
    check("files: parent escape rejected", _safe_rel_path("../evil.py") is None)
    check("files: absolute posix rejected", _safe_rel_path("/etc/passwd") is None)
    check("files: windows drive rejected", _safe_rel_path("C:\\evil.py") is None)

    full_work = prepare_workspace(tasks[0], root / "work_files")
    ok, detail = apply_full_files(
        "### FILE: calc.py\n```python\nprint('fixed')\n```\n", full_work)
    check("files: full-file block is written", ok is True, f" ({detail})")
    check("files: writer creates new files",
          apply_full_files("### FILE: pkg/new.py\n```\nY = 2\n```\n", full_work)[0]
          and (full_work / "pkg" / "new.py").is_file())
    ok, detail = apply_full_files("### FILE: ../evil.py\n```\nx\n```\n", full_work)
    check("files: unsafe path refuses the batch", ok is False and "unsafe" in detail,
          f" ({detail})")
    check("files: unsafe path did not write outside",
          not (root / "evil.py").exists())
    check("default conditions do not spend cells on the meaningless raw baselines",
          "raw" not in DEFAULT_CONDITIONS and "raw_files" not in DEFAULT_CONDITIONS)

    # ── code-churn metrics ─────────────────────────────────────────────────
    _base = root / "churn_repo"
    _changed = root / "churn_work"
    _base.mkdir()
    _changed.mkdir()
    (_base / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (_base / "b.py").write_text("old\n", encoding="utf-8")
    (_changed / "a.py").write_text("one\nTWO\nthree\nfour\n", encoding="utf-8")
    (_changed / "new.py").write_text("new\n", encoding="utf-8")
    (_changed / "model.patch").write_text("ignored\n", encoding="utf-8")
    (_changed / ".ruff_cache").mkdir()
    (_changed / ".ruff_cache" / "x.py").write_text("cache\n", encoding="utf-8")
    (_changed / ".aider.chat.history.md").write_text("history\n", encoding="utf-8")
    (_changed / ".aider.tags.cache.v4").mkdir()
    (_changed / ".aider.tags.cache.v4" / "tags").write_text("tags\n", encoding="utf-8")
    _churn = diff_stats(_base, _changed)
    check("churn: changed files are listed",
          _churn["changed_files"] == ["a.py", "b.py", "new.py"], f" ({_churn})")
    check("churn: new and deleted files",
          _churn["new_files"] == ["new.py"] and _churn["deleted_files"] == ["b.py"],
          f" ({_churn})")
    check("churn: added/removed lines",
          _churn["lines_added"] == 3 and _churn["lines_removed"] == 2,
          f" ({_churn['lines_added']}/{_churn['lines_removed']})")
    check("churn: model.patch is ignored", "model.patch" not in _churn["changed_files"])
    check("churn: tool caches are ignored",
          not any(".ruff_cache" in f for f in _churn["changed_files"] + _churn["new_files"]))
    check("churn: aider history/tag caches are ignored",
          not any(".aider" in f for f in _churn["changed_files"] + _churn["new_files"]
                  + _churn["deleted_files"]), f" ({_churn})")

    # ── aider discovery ────────────────────────────────────────────────────
    _cfg_bad = Config(tasks_dir=root, out=root / "x.json", model="m",
                      aider_bin=str(root / "missing-aider.exe"))
    check("aider: missing explicit path is rejected",
          _resolve_aider_bin(_cfg_bad) == "")
    _fake_aider = root / "fake-aider.exe"
    _fake_aider.write_text("", encoding="utf-8")
    _cfg_ok = Config(tasks_dir=root, out=root / "x.json", model="m",
                     aider_bin=str(_fake_aider))
    check("aider: explicit path is used",
          _resolve_aider_bin(_cfg_ok) == str(_fake_aider))
    _missing = _run_aider(Task("t", "p", "c"), root, _cfg_bad)
    check("aider: unavailable is reported, not raised",
          "not installed" in _missing.get("detail", ""), f" ({_missing})")
    files = _aider_files(work)
    check("aider: workspace files are passed to the chat",
          "a.txt" in files and all(not f.startswith(".") for f in files),
          f" ({files})")
    check("aider: file list is bounded and sorted",
          files == sorted(files) and len(files) <= 40, f" ({len(files)})")

    # ── hermes & pi discovery ──────────────────────────────────────────────
    _cfg_hermes_bad = Config(tasks_dir=root, out=root / "x.json", model="m",
                             hermes_bin=str(root / "missing-hermes.exe"))
    check("hermes: missing explicit path is rejected",
          _resolve_hermes_bin(_cfg_hermes_bad) == "")
    _fake_hermes = root / "fake-hermes.exe"
    _fake_hermes.write_text("", encoding="utf-8")
    _cfg_hermes_ok = Config(tasks_dir=root, out=root / "x.json", model="m",
                            hermes_bin=str(_fake_hermes))
    check("hermes: explicit path is used",
          _resolve_hermes_bin(_cfg_hermes_ok) == str(_fake_hermes))
    _hermes_missing = _run_hermes(Task("t", "p", "c"), root, _cfg_hermes_bad)
    check("hermes: unavailable is reported, not raised",
          "not installed" in _hermes_missing.get("detail", ""), f" ({_hermes_missing})")

    _cfg_pi_bad = Config(tasks_dir=root, out=root / "x.json", model="m",
                         pi_bin=str(root / "missing-pi.exe"))
    check("pi: missing explicit path is rejected",
          _resolve_pi_bin(_cfg_pi_bad) == "")
    _fake_pi = root / "fake-pi.exe"
    _fake_pi.write_text("", encoding="utf-8")
    _cfg_pi_ok = Config(tasks_dir=root, out=root / "x.json", model="m",
                        pi_bin=str(_fake_pi))
    check("pi: explicit path is used",
          _resolve_pi_bin(_cfg_pi_ok) == str(_fake_pi))
    _pi_missing = _run_pi(Task("t", "p", "c"), root, _cfg_pi_bad)
    check("pi: unavailable is reported, not raised",
          "not installed" in _pi_missing.get("detail", ""), f" ({_pi_missing})")

    # hermes state.db usage extraction
    _hhome = root / "hermes_home"
    _hhome.mkdir(parents=True)
    check("hermes: missing state.db returns empty dict",
          _read_hermes_state_usage(_hhome) == {})
    _hconn = sqlite3.connect(str(_hhome / "state.db"))
    _hconn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, input_tokens INTEGER, "
        "output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
        "api_call_count INTEGER)"
    )
    _hconn.execute("INSERT INTO sessions VALUES ('s1', 400, 100, 1500, 0, 3)")
    _hconn.commit()
    _hconn.close()
    _husage = _read_hermes_state_usage(_hhome)
    check("hermes: state.db tokens, cached, net, rounds extracted",
          _husage == {"tokens": 2000, "cached_tokens": 1500, "net_tokens": 500, "rounds": 3},
          f" ({_husage})")

    # pi --mode json JSONL extraction
    _pi_jsonl = "\n".join([
        json.dumps({"type": "agent_start"}),
        json.dumps({"type": "message_end", "message": {
            "role": "user", "content": "hi"}}),
        json.dumps({"type": "message_end", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "reading file"}],
            "usage": {"input": 500, "output": 50, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 550},
        }}),
        json.dumps({"type": "message_end", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "fixed bug"}],
            "usage": {"input": 100, "output": 40, "cacheRead": 500, "cacheWrite": 0, "totalTokens": 640},
        }}),
    ])
    _pusage = _parse_pi_json_output(_pi_jsonl)
    check("pi: --mode json tokens, cached, net, rounds extracted",
          _pusage["parsed"] is True
          and _pusage["tokens"] == 1190
          and _pusage["cached_tokens"] == 500
          and _pusage["net_tokens"] == 690
          and _pusage["rounds"] == 2
          and "fixed bug" in _pusage["detail"],
          f" ({_pusage})")

    # shared per-agent timeout parity
    _to_task = Task("t", "p", "c", timeout_s=300)
    check("timeout: parity uses task timeout (300) even when cell_timeout is 900",
          _agent_timeout_s(_to_task, Config(tasks_dir=root, out=root / "x.json", model="m", cell_timeout_s=900)) == 300.0)
    check("timeout: explicit task_timeout overrides task default",
          _agent_timeout_s(_to_task, Config(tasks_dir=root, out=root / "x.json", model="m", task_timeout_s=600, cell_timeout_s=900)) == 600.0)
    check("timeout: cell_timeout caps tighter than task timeout",
          _agent_timeout_s(_to_task, Config(tasks_dir=root, out=root / "x.json", model="m", cell_timeout_s=120)) == 120.0)

    # sync_dir mirroring and resume restoration
    _sync_dir = root / "drive_sync"
    _local_out = root / "local" / "suite.json"
    _sync_cfg = Config(tasks_dir=root, out=_local_out, model="m", ctx_tokens=131072, sync_dir=_sync_dir, resume=True)
    _sync_rows = [
        {"run": 1, "task": "t1", "condition": "hivemind_duo", "model": "m", "ok": True, "seconds": 10.0, "tokens": 500},
        {"run": 1, "task": "t1", "condition": "aider", "model": "m", "ok": False, "seconds": 12.0, "tokens": 300},
    ]
    _persist(_local_out, _sync_rows, _sync_cfg, ["hivemind_duo", "aider"], "2026-11-12T00:00:00+00:00")
    _mirrored = _sync_dir / "suite.json"
    check("sync: persist mirrors result file to sync_dir", _mirrored.is_file())
    _local_out.unlink()
    _res_rows, _done_keys, _res_start = _load_resume_state(_local_out, _sync_dir, "m", 131072)
    check("resume: restores rows from sync_dir when local checkpoint missing",
          len(_res_rows) == 2 and (1, "t1", "hivemind_duo") in _done_keys and (1, "t1", "aider") in _done_keys
          and _res_start == "2026-11-12T00:00:00+00:00",
          f" ({len(_res_rows)}/{_done_keys})")
    _mismatch_rows, _mismatch_keys, _ = _load_resume_state(_local_out, _sync_dir, "other_model", 131072)
    check("resume: mismatched model ignores checkpoint",
          _mismatch_rows == [] and _mismatch_keys == set())
    _mismatch_ctx, _, _ = _load_resume_state(_local_out, _sync_dir, "m", 32768)
    check("resume: mismatched ctx_tokens ignores checkpoint",
          _mismatch_ctx == [])

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
    check("body: agentic config is pinned",
          direct_body.get("until_finished") is True)
    duo_body = _hivemind_body(Task("t", "p", "c"), work, duo=True,
                              model="qwen3.5:4b-mtp")
    check("body: duo condition is code_duo + agentic",
          duo_body["mode"] == "code_duo" and duo_body.get("duo_agentic_mode") is True)
    check("body: duo pins planner on and chunking off",
          duo_body.get("duo_planner") is True
          and duo_body.get("duo_chunking") is False,
          f" ({duo_body.get('duo_planner')}/{duo_body.get('duo_chunking')})")
    check("body: duo pins tool rounds (install 0 would disable tools)",
          duo_body.get("duo_tool_rounds") == 50,
          f" ({duo_body.get('duo_tool_rounds')})")
    check("body: duo pins the core tool tier",
          duo_body.get("duo_coder_tool_tier") == "core",
          f" ({duo_body.get('duo_coder_tool_tier')})")
    duo_full_body = _hivemind_body(Task("t", "p", "c"), work, duo=True,
                                   model="qwen3.5:4b-mtp", duo_tier="full")
    check("body: duo supports full tool tier",
          duo_full_body.get("duo_coder_tool_tier") == "full",
          f" ({duo_full_body.get('duo_coder_tool_tier')})")
    duo_lean_body = _hivemind_body(Task("t", "p", "c"), work, duo=True,
                                   model="qwen3.5:4b-mtp", duo_tier="lean")
    check("body: duo supports lean tool tier",
          duo_lean_body.get("duo_coder_tool_tier") == "lean",
          f" ({duo_lean_body.get('duo_coder_tool_tier')})")
    check("body: duo disables test-feedback loops",
          duo_body.get("duo_test_feedback_final") is False
          and duo_body.get("duo_test_feedback_chunk") is False)
    work_tf = prepare_workspace(tasks[0], root / "work_tf")
    check("workspace: hidden tests never enter the agent workspace",
          not (work_tf / "test_task.py").exists())
    check("body: duo pins the coder context",
          duo_body.get("duo_coder_ctx_agentic") == 32768
          and duo_body.get("duo_coder_ctx_normal") == 32768,
          f" ({duo_body.get('duo_coder_ctx_agentic')})")
    duo_128k = _hivemind_body(Task("t", "p", "c"), work, duo=True,
                              model="qwen3.5:4b-mtp", ctx_tokens=131072)
    check("body: duo accepts custom ctx_tokens (e.g. 128k for deepswe)",
          duo_128k.get("duo_coder_ctx_agentic") == 131072
          and duo_128k.get("duo_coder_ctx_normal") == 131072,
          f" ({duo_128k.get('duo_coder_ctx_agentic')})")
    duo_solo = _hivemind_body(Task("t", "p", "c"), work, duo=True,
                              model="qwen3.5:4b-mtp", duo_planner=False)
    check("body: coder-alone mode turns the planner off",
          duo_solo.get("duo_planner") is False)
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
          summary["hivemind_direct"]["tokens_total"] == 1200
          and summary["hivemind_direct"]["net_tokens_total"] == 1200,
          f" ({summary['hivemind_direct']['tokens_total']})")

    buf = io.StringIO()
    with redirect_stdout(buf):
        print_summary(summary)
    out = buf.getvalue()
    check("report: prints every condition", all(
        name in out for name in ("raw", "hivemind_direct", "hivemind_duo")), f" ({out!r})")
    check("report: shows the delta column", "+1.00" in out and "+0.50" in out,
          f" ({out!r})")

    # ── D1 relative metrics ───────────────────────────────────────────────
    # 4 synthetic tasks. Cell pass rates: raw 0.0, raw_files 0.25 (fix_a),
    # hivemind_direct 0.5 (fix_a+fix_c), aider 0.75.
    rows = [
        {"task": "fix_a", "condition": "raw", "ok": False, "seconds": 1, "tokens": 100},
        {"task": "fix_c", "condition": "raw", "ok": False, "seconds": 1, "tokens": 100},
        {"task": "feature_b", "condition": "raw", "ok": False, "seconds": 1, "tokens": 100},
        {"task": "feature_d", "condition": "raw", "ok": False, "seconds": 1, "tokens": 100},
        {"task": "fix_a", "condition": "raw_files", "ok": True, "seconds": 1, "tokens": 100},
        {"task": "fix_c", "condition": "raw_files", "ok": False, "seconds": 1, "tokens": 100},
        {"task": "feature_b", "condition": "raw_files", "ok": False, "seconds": 1, "tokens": 100},
        {"task": "feature_d", "condition": "raw_files", "ok": False, "seconds": 1, "tokens": 100},
        {"task": "fix_a", "condition": "hivemind_direct", "ok": True, "seconds": 2,
         "tokens": 400, "cached_tokens": 200, "net_tokens": 300, "rounds": 4},
        {"task": "fix_c", "condition": "hivemind_direct", "ok": True, "seconds": 2,
         "tokens": 400, "cached_tokens": 200, "net_tokens": 300, "rounds": 2},
        {"task": "feature_b", "condition": "hivemind_direct", "ok": False, "seconds": 2,
         "tokens": 400, "cached_tokens": 200, "net_tokens": 300, "rounds": 0},
        {"task": "feature_d", "condition": "hivemind_direct", "ok": False, "seconds": 2,
         "tokens": 400, "cached_tokens": 200, "net_tokens": 300, "rounds": 0},
        {"task": "fix_a", "condition": "aider", "ok": True, "seconds": 1, "tokens": 50},
        {"task": "fix_c", "condition": "aider", "ok": True, "seconds": 1, "tokens": 50},
        {"task": "feature_b", "condition": "aider", "ok": True, "seconds": 1, "tokens": 50},
        {"task": "feature_d", "condition": "aider", "ok": False, "seconds": 1, "tokens": 50},
    ]
    rel_summary = aggregate(rows)
    check("relative: delta_vs_raw kept",
          rel_summary["hivemind_direct"]["delta_vs_raw"] == 0.5,
          f" ({rel_summary['hivemind_direct'].get('delta_vs_raw')})")
    check("relative: delta_vs_raw_files when the baseline exists",
          rel_summary["hivemind_direct"]["delta_vs_raw_files"] == 0.25
          and rel_summary["raw"]["delta_vs_raw_files"] == -0.25,
          f" ({rel_summary['hivemind_direct'].get('delta_vs_raw_files')}/"
          f"{rel_summary['raw'].get('delta_vs_raw_files')})")
    check("relative: cached share is cached/total",
          rel_summary["hivemind_direct"]["cached_share"] == 0.5
          and rel_summary["aider"]["cached_share"] == 0.0,
          f" ({rel_summary['hivemind_direct'].get('cached_share')})")
    check("relative: rounds counted",
          rel_summary["hivemind_direct"]["rounds_total"] == 6
          and rel_summary["hivemind_direct"]["rounds_median"] == 1.0,
          f" ({rel_summary['hivemind_direct'].get('rounds_total')})")

    from bench.relative import build_relative  # noqa: E402
    rel = build_relative(rows)
    direct = rel["conditions"]["hivemind_direct"]
    check("relative: block lists present baselines",
          rel["baselines_present"] == ["raw", "raw_files"], f" ({rel['baselines_present']})")
    check("relative: condition deltas vs both baselines",
          direct["delta_vs_raw"] == 0.5 and direct["delta_vs_raw_files"] == 0.25,
          f" ({direct['delta_vs_raw']}/{direct['delta_vs_raw_files']})")
    check("relative: category rates and deltas",
          direct["categories"]["bugfix"] == {"pass_rate": 1.0, "delta_vs_raw": 1.0,
                                             "delta_vs_raw_files": 0.5}
          and direct["categories"]["feature"]["delta_vs_raw"] == 0.0,
          f" ({direct['categories']})")
    check("relative: baseline itself has zero self-delta",
          rel["conditions"]["raw"]["delta_vs_raw"] == 0.0
          and rel["conditions"]["raw_files"]["delta_vs_raw"] == 0.25,
          f" ({rel['conditions']['raw']}/{rel['conditions']['raw_files']})")
    check("relative: token ratios vs aider",
          direct["tokens"]["net_vs_aider_ratio"] == 6.0
          and direct["tokens"]["total_vs_aider_ratio"] == 8.0
          and rel["conditions"]["aider"]["tokens"]["net_vs_aider_ratio"] == 1.0,
          f" ({direct['tokens']})")
    check("relative: cached share and rounds in the block",
          direct["tokens"]["cached_share"] == 0.5
          and direct["rounds"] == {"total": 6, "median": 1.0},
          f" ({direct['tokens']['cached_share']}/{direct['rounds']})")

    # Missing baseline: no raw rows -> every delta_vs_raw is None, not a crash.
    no_raw = build_relative([r for r in rows if r["condition"] != "raw"])
    check("relative: missing raw baseline -> delta None, flagged absent",
          no_raw["baselines_present"] == ["raw_files"]
          and all(v["delta_vs_raw"] is None for v in no_raw["conditions"].values()),
          f" ({no_raw['baselines_present']})")
    check("relative: missing baseline -> category delta None",
          no_raw["conditions"]["hivemind_direct"]["categories"]["bugfix"]["delta_vs_raw"] is None,
          f" ({no_raw['conditions']['hivemind_direct']['categories']})")

    # Zero tokens: shares and ratios are None, never ZeroDivisionError.
    zero_tokens = build_relative([
        {"task": "fix_a", "condition": "raw", "ok": True, "seconds": 1, "tokens": 0},
        {"task": "fix_a", "condition": "hivemind_direct", "ok": True, "seconds": 1,
         "tokens": 0, "cached_tokens": 0, "net_tokens": 0},
        {"task": "fix_a", "condition": "aider", "ok": True, "seconds": 1, "tokens": 0},
    ])
    zero_entry = zero_tokens["conditions"]["hivemind_direct"]
    check("relative: zero tokens -> share and ratios None",
          zero_entry["tokens"]["cached_share"] is None
          and zero_entry["tokens"]["net_vs_aider_ratio"] is None
          and zero_entry["tokens"]["total_vs_aider_ratio"] is None,
          f" ({zero_entry['tokens']})")
    check("relative: all-pass -> all deltas 0.0",
          zero_entry["delta_vs_raw"] == 0.0,
          f" ({zero_entry['delta_vs_raw']})")
    all_fail = build_relative([
        {"task": "fix_a", "condition": "raw", "ok": False, "seconds": 1, "tokens": 10},
        {"task": "fix_a", "condition": "hivemind_direct", "ok": False, "seconds": 1,
         "tokens": 10, "rounds": 0},
    ])
    check("relative: all-fail -> deltas 0.0 and rounds reported",
          all_fail["conditions"]["hivemind_direct"]["delta_vs_raw"] == 0.0
          and all_fail["conditions"]["hivemind_direct"]["rounds"]["total"] == 0,
          f" ({all_fail['conditions']['hivemind_direct']})")

    # The persisted JSON carries the relative block alongside the old fields.
    out_file = root / "results" / "run_relative.json"
    _persist(out_file, rows, cfg,
             ["raw", "raw_files", "hivemind_direct", "aider"], "2026-11-09T00:00:00")
    saved = json.loads(out_file.read_text(encoding="utf-8"))
    check("results: relative block persisted",
          saved["relative"]["conditions"]["hivemind_direct"]["delta_vs_raw"] == 0.5,
          f" ({str(saved.get('relative'))[:120]})")
    check("results: old summary fields untouched",
          saved["summary"]["hivemind_direct"]["pass_rate"] == 0.5
          and saved["summary"]["hivemind_direct"]["delta_vs_raw"] == 0.5
          and "tokens_total" in saved["summary"]["hivemind_direct"],
          f" ({saved['summary']['hivemind_direct']})")

    buf = io.StringIO()
    with redirect_stdout(buf):
        print_summary(rel_summary, rel)
    out = buf.getvalue()
    check("report: relative block is readable output",
          "d_raw" in out and "d_raw_files" in out and "net/aider" in out
          and "6.00x" in out, f" ({out[-400:]!r})")
    check("report: category deltas printed",
          "bugfix" in out and "feature" in out, f" ({out[-400:]!r})")

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

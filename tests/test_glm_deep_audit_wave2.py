# -*- coding: utf-8 -*-
"""Regression suite for GLM 5.3 Deep-Audit Wave 2 (5 P1 + 8 P2 + Colab Autopilot).

Covers:
  - P1-1: core/chat_run.py binds `complexity` before `_done_event` closure
  - P1-2 & P2: backend/llama_start_phases.py guards `_loading` during eviction and resets it in `finally:`
  - P1-3: run_tests / _quote_path reject out-of-workspace traversal paths
  - P1-4: native file & search tools enforce secret-file denylist
  - P1-5: deploy/updater.py fails closed when remote metadata lacks a sha256 digest
  - P2-1: core/tool_loop.py pairs unknown tool calls with `role: "tool"` + empty `bad_content`
  - P2-2: context/compression.py merges adjacent `role: "user"` across summary/tail boundary
  - P2-3: core/duo_round_input.py injects `[Pre-Explore File Contents]` before building `_coder_msgs`
  - P2-4: core/stream.py uses `utils.bg_tasks.spawn` instead of bare `asyncio.create_task`
  - P2-5: static/app.js `esc()` escapes quotes, VRAM tooltip escapes `short`, approval card carries `dataset.decisionId`
  - P2-6: model_configs rejects traversal segments (`..`, `/`, `\\`)
  - P2-7: deploy/fetch_llamacpp.py supports `deploy/llama_build_pin.txt` with fallback
  - Bench/Notebook: runner heartbeat/scoreboard + numbered Colab logs + STATUS_OVERVIEW.txt
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="summary")
check = h.check


# ── P1-1: complexity bound before _done_event ────────────────────────────────
_chat_run_src = (ROOT / "core" / "chat_run.py").read_text(encoding="utf-8")
check(
    "P1-1 complexity initialized before _done_event",
    "complexity: str = \"\"" in _chat_run_src
    and _chat_run_src.index("complexity: str = \"\"") < _chat_run_src.index("def _done_event("),
)


# ── P1-2 & P2-Backend: _loading guard & finally reset ────────────────────────
_phases_src = (ROOT / "backend" / "llama_start_phases.py").read_text(encoding="utf-8")
check(
    "P1-2 both eviction filters skip _loading peers",
    _phases_src.count('not getattr(s, "_loading", False)') >= 2
    and 'if not s.pinned and not _slot_in_flight(s) and not getattr(s, "_loading", False)' in _phases_src,
)
_wait_ready_tail = _phases_src[_phases_src.index("async def _phase_preflight_and_spawn") :]
check(
    "P2-Backend _phase_preflight_and_spawn resets slot._loading = False in finally",
    "_wait_done = False" in _wait_ready_tail
    and "if not _wait_done:" in _wait_ready_tail
    and "await _kill_slot_async(slot)" in _wait_ready_tail
    and "    finally:\n        slot._loading = False" in _wait_ready_tail.replace("\r\n", "\n"),
)


# ── P1-3: run_tests path traversal containment ───────────────────────────────
from hive_functions.test_runner import _quote_path, run_tests as _real_run_tests  # noqa: E402
from tools.handlers import _shared as _handler_shared  # noqa: E402
from tools.handlers.exec_tools import _inline_tool_run_tests  # noqa: E402

with tempfile.TemporaryDirectory(prefix="hm_p1_3_") as _td:
    _ws = Path(_td) / "ws"
    (_ws / "tests").mkdir(parents=True)
    check("P1-3 _quote_path allows relative path inside ws", _quote_path("tests/a.py", str(_ws)) == "tests/a.py")
    _rejected_quote = False
    try:
        _quote_path("../outside.py", str(_ws))
    except ValueError:
        _rejected_quote = True
    check("P1-3 _quote_path rejects ../outside", _rejected_quote)

    _prev_runner = _handler_shared._run_test_suite
    _handler_shared._run_test_suite = _real_run_tests
    try:
        _bad_res = asyncio.run(_inline_tool_run_tests({"targets": ["../outside.py"]}, _ws, str(_ws)))
    finally:
        _handler_shared._run_test_suite = _prev_runner
    check("P1-3 _inline_tool_run_tests rejects ../outside", "PATH_OUTSIDE_WORKSPACE" in _bad_res, _bad_res[:120])


# ── P1-4: native file & search secret denylist ───────────────────────────────
from utils.file import (  # noqa: E402
    _inline_check_workspace,
    filter_secret_search_rows,
    is_secret_path,
)
from tools.handlers.code_intel import _inline_tool_find_files, _python_content_search  # noqa: E402

with tempfile.TemporaryDirectory(prefix="hm_p1_4_") as _td:
    _ws = Path(_td)
    (_ws / ".env").write_text("API_KEY=supersecret\n", encoding="utf-8")
    (_ws / ".env.local").write_text("DB_PASS=secret2\n", encoding="utf-8")
    (_ws / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")
    (_ws / "cert.pem").write_text("PEM\n", encoding="utf-8")
    (_ws / "credentials.json").write_text("{\"token\": \"x\"}\n", encoding="utf-8")
    (_ws / "sessions").mkdir()
    (_ws / "sessions" / "c1.json").write_text("{\"secret\": 1}\n", encoding="utf-8")
    (_ws / "ok.py").write_text("API_KEY = 'demo'\n", encoding="utf-8")

    check("P1-4 is_secret_path detects .env/.env.local/id_rsa/cert.pem/credentials/sessions",
          all([
              is_secret_path(_ws / ".env", _ws),
              is_secret_path(_ws / ".env.local", _ws),
              is_secret_path(_ws / "id_rsa", _ws),
              is_secret_path(_ws / "cert.pem", _ws),
              is_secret_path(_ws / "credentials.json", _ws),
              is_secret_path(_ws / "sessions" / "c1.json", _ws),
              not is_secret_path(_ws / "ok.py", _ws),
          ]))

    _err_env = _inline_check_workspace(_ws / ".env", str(_ws), "read_file")
    _err_ok = _inline_check_workspace(_ws / "ok.py", str(_ws), "read_file")
    check("P1-4 _inline_check_workspace denies .env and allows ok.py",
          isinstance(_err_env, str) and "SECRET_PATH_DENIED" in _err_env and _err_ok is None)

    _ff_out = asyncio.run(_inline_tool_find_files({"pattern": "**/*"}, _ws, str(_ws)))
    check("P1-4 find_files excludes secret files",
          "ok.py" in _ff_out and ".env" not in _ff_out and "id_rsa" not in _ff_out and "cert.pem" not in _ff_out,
          _ff_out)

    _sc_out = _python_content_search(_ws, "secret") or ""
    check("P1-4 _python_content_search skips secret files",
          "supersecret" not in _sc_out and ".env" not in _sc_out and "c1.json" not in _sc_out)

    _filtered_rows = filter_secret_search_rows(
        "C:\\ws\\.env:1:API_KEY=bad\n/tmp/ws/id_rsa:2:KEY\nC:\\ws\\ok.py:5:API_KEY='demo'"
    )
    check("P1-4 filter_secret_search_rows drops Windows & POSIX secret rows",
          ".env" not in _filtered_rows and "id_rsa" not in _filtered_rows and "ok.py:5:" in _filtered_rows,
          _filtered_rows)


# ── P1-5: deploy/updater.py fail-closed missing digest ───────────────────────
import deploy.updater as _upd  # noqa: E402

with tempfile.TemporaryDirectory(prefix="hm_p1_5_") as _td:
    _z = Path(_td) / "rel.zip"
    _z.write_bytes(b"abc")
    check("P1-5 digest_ok still returns None when digest absent", _upd.digest_ok(_z, "") is None)
_upd_src = (ROOT / "deploy" / "updater.py").read_text(encoding="utf-8")
_ok_none_idx = _upd_src.index("if ok is None:")
_ok_none_block = _upd_src[_ok_none_idx : _ok_none_idx + 240]
check("P1-5 updater remote download fails closed when ok is None",
      "refusing unverified download" in _ok_none_block and "return 1" in _ok_none_block)


# ── P2-1: core/tool_loop.py pairs unknown tool_calls before retry feedback ───
_loop_src = (ROOT / "core" / "tool_loop.py").read_text(encoding="utf-8")
_unk_idx = _loop_src.index("if _name and _name not in _known_tool_names:")
_unk_block = _loop_src[_unk_idx : _unk_idx + 1100]
check("P2-1 unknown tool appends role:tool before retry feedback with empty bad_content",
      '"role": "tool"' in _unk_block
      and "bad_content=\"\"" in _unk_block
      and _unk_block.index('"role": "tool"') < _unk_block.index("_inject_retry_feedback"))


# ── P2-2: context/compression.py merges adjacent user messages ───────────────
import context.compression as _comp  # noqa: E402

_msgs_user_tail = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "goal"},
    {"role": "assistant", "content": "step1"},
    {"role": "tool", "content": "out1" * 100, "tool_call_id": "1", "name": "read_file"},
    {"role": "assistant", "content": "step2"},
    {"role": "tool", "content": "out2" * 100, "tool_call_id": "2", "name": "read_file"},
    {"role": "user", "content": "tail user reminder"},
    {"role": "assistant", "content": "tail assistant reply"},
]
_compressed, _, _ = asyncio.run(
    _comp._compress_tool_context(
        _msgs_user_tail,
        model="dummy",
        port=8080,
        client=None,
        system_prompt="sys",
        original_task="goal",
        written_files=[],
        done_tasks=[],
        keep_recent_msgs=2,
        local_only=True,
    )
)
_adj_users = any(
    _compressed[i].get("role") == "user" and _compressed[i + 1].get("role") == "user"
    for i in range(len(_compressed) - 1)
)
check("P2-2 _compress_tool_context avoids consecutive user messages",
      not _adj_users and any("tail user reminder" in str(m.get("content", "")) for m in _compressed),
      str([m.get("role") for m in _compressed]))


# ── P2-3: core/duo_round_input.py pre-explore injection before _coder_msgs ───
_dri_src = (ROOT / "core" / "duo_round_input.py").read_text(encoding="utf-8")
check("P2-3 pre-explore block runs before _coder_msgs construction",
      "[Pre-Explore File Contents]" in _dri_src
      and _dri_src.index("[Pre-Explore File Contents]") < _dri_src.index("_coder_msgs = ctx.make_messages("))


# ── P2-4: core/stream.py uses spawn ──────────────────────────────────────────
_stream_src = (ROOT / "core" / "stream.py").read_text(encoding="utf-8")
check("P2-4 core/stream.py uses spawn() instead of bare create_task",
      "from utils.bg_tasks import spawn" in _stream_src
      and 'name="auto_memory"' in _stream_src
      and "asyncio.create_task" not in _stream_src)


# ── P2-5: static/app.js XSS quote escaping & decisionId preservation ─────────
_app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
check("P2-5a esc() escapes double and single quotes",
      '.replace(/"/g,\'&quot;\').replace(/\'/g,\'&#39;\')' in _app_js)
check("P2-5b VRAM tooltip escapes short model name",
      'title="\'+esc(short)+\': ~' in _app_js)
check("P2-5c approval update stores dataset.decisionId and submit reads it",
      "_apOld.dataset.decisionId = d.decision_id" in _app_js
      and "_ap.dataset.decisionId || d.decision_id || ''" in _app_js)


# ── P2-6: model_configs path traversal guard ─────────────────────────────────
import model_configs as _mc  # noqa: E402

with tempfile.TemporaryDirectory(prefix="hm_p2_6_") as _td:
    _base = Path(_td) / "server.py"
    _base.write_text("# dummy", encoding="utf-8")
    _mc.init_base_configs(_base)
    _mc.save_learned_config(_base, "qwen3.5:4b", "coder", {"temperature": 0.2})
    check("P2-6 valid model/agent still loads cleanly",
          (_mc.get_learned_config(_base, "qwen3.5:4b", "coder") or {}).get("temperature") == 0.2)
    _bad_ids = ("../etc", "..\\evil", "a/b", "a\\b", ".", "..", "")
    _all_blocked = True
    for _bad in _bad_ids:
        for _fn in (
            lambda b=_bad: _mc.get_learned_config(_base, b, "coder"),
            lambda b=_bad: _mc.save_learned_config(_base, "qwen3.5:4b", b, {"temperature": 0.1}),
            lambda b=_bad: _mc.read_learning_log(_base, b),
        ):
            try:
                _fn()
                _all_blocked = False
            except ValueError:
                pass
    check("P2-6 model_configs rejects traversal/separator identifiers", _all_blocked)


# ── P2-7: fetch_llamacpp build pin ───────────────────────────────────────────
import deploy.fetch_llamacpp as _fl  # noqa: E402

_pin_file = ROOT / "deploy" / "llama_build_pin.txt"
check("P2-7 llama_build_pin.txt exists and _read_pinned_tag parses bNNNN",
      _pin_file.is_file() and _fl._read_pinned_tag().startswith("b"))
_fl_src = (ROOT / "deploy" / "fetch_llamacpp.py").read_text(encoding="utf-8")
check("P2-7 fetch_llamacpp exposes --tag and --latest",
      '"--tag"' in _fl_src and '"--latest"' in _fl_src)


# ── Bench runner + Colab autopilot notebook ──────────────────────────────────
_nb_path = ROOT / "bench" / "external" / "hivemind_bench_colab_autopilot.ipynb"
_nb_raw = _nb_path.read_text(encoding="utf-8")
_nb = json.loads(_nb_raw)
_ast_ok = True
for _cell in _nb.get("cells", []):
    if _cell.get("cell_type") == "code":
        _code_src = "\n".join(
            l for l in _cell.get("source", []) if not l.lstrip().startswith("!")
        )
        ast.parse(_code_src)
check("NB1 colab autopilot notebook parses as JSON + valid Python AST", _ast_ok and len(_nb["cells"]) >= 16)
check(
    "NB2 colab notebook wires numbered run logs, cleanup, AFK inspector & STATUS_OVERVIEW.txt",
    all(
        tok in _nb_raw
        for tok in (
            "run1_log1_core_run1.log",
            "run1_log2_core_run2.log",
            "run1_log3_core_run3.log",
            "run1_log4_polyglot_run1.log",
            "run1_log5_polyglot_run2.log",
            "run1_log6_polyglot_run3.log",
            "run1_log4_polyglot_part1.log",
            "run1_log4_polyglot_part2.log",
            "run1_log4_polyglot.log",
            "run1_log7_deepswe_run1.log",
            "run1_log8_deepswe_run2.log",
            "run1_log9_deepswe_run3.log",
            "STATUS_OVERVIEW.txt",
            "clean_old_bench_logs",
            "update_status_overview",
            "[bench] Saved ",
        )
    ),
)
_runner_src = (ROOT / "bench" / "runner.py").read_text(encoding="utf-8")
check("NB3 bench/runner.py emits heartbeat and inter-task scoreboard",
      "_run_cell_with_heartbeat" in _runner_src and "[progress] task" in _runner_src)

sys.exit(h.finish("glm_deep_audit_wave2"))

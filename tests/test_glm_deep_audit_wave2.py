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
from tests.ui_sources import read_all_ui_js  # noqa: E402
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
    and "if not _wait_done" in _wait_ready_tail
    and "await _kill_slot_async(slot)" in _wait_ready_tail
    and "    finally:\n" in _wait_ready_tail.replace("\r\n", "\n")
    and "slot._loading = False" in _wait_ready_tail.replace("\r\n", "\n").rsplit("    finally:", 1)[1],
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
_app_js = read_all_ui_js()
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


# ── P2-7: fetch_llamacpp build pin & latest-default / upgrade ─────────────────
import io as _io  # noqa: E402
import os as _os  # noqa: E402
import tarfile as _tarfile  # noqa: E402
import zipfile as _zipfile  # noqa: E402
from unittest import mock as _mock  # noqa: E402
import deploy.fetch_llamacpp as _fl  # noqa: E402

_pin_file = ROOT / "deploy" / "llama_build_pin.txt"
check("P2-7 llama_build_pin.txt exists and _read_pinned_tag parses bNNNN",
      _pin_file.is_file() and _fl._read_pinned_tag().startswith("b"))
_fl_src = (ROOT / "deploy" / "fetch_llamacpp.py").read_text(encoding="utf-8")
check("P2-7 fetch_llamacpp exposes --tag, --latest, --pinned, and --upgrade",
      all(f in _fl_src for f in ('"--tag"', '"--latest"', '"--pinned"', '"--upgrade"')))


def _p2_7_verify() -> None:
    os_tag = _fl._OS_TAG
    ext = "zip" if _fl._IS_WIN else "tar.gz"
    pin_tag = _fl._read_pinned_tag()

    # 1) Default resolution hits newest nightlies and skips incomplete newer CUDA nightly
    seen_urls: list[str] = []
    fake_rels = [
        {
            "tag_name": "b12501",
            "assets": [{"name": f"llama-b12501-bin-{os_tag}-cuda-13.1-x64.{ext}"}],
        },
        {
            "tag_name": "b12500",
            "assets": [
                {"name": f"llama-b12500-bin-{os_tag}-cuda-12.4-x64.{ext}"},
                {"name": f"llama-b12500-bin-{os_tag}-cuda-13.1-x64.{ext}"},
            ],
        },
    ]

    def _fake_http_json_cuda(url: str):
        seen_urls.append(url)
        if "releases?per_page=100" in url:
            return fake_rels
        raise AssertionError(f"unexpected URL in default nightly test: {url}")

    with _mock.patch.object(_fl, "http_json", side_effect=_fake_http_json_cuda), \
            _mock.patch.dict(_os.environ, {"HIVEMIND_LLAMA_TAG": ""}, clear=False):
        rels = _fl._resolve_releases("cuda", _fl.ASSET_REGEX["cuda"], max_cuda_version="12.8")
    picked, picked_tag = _fl._pick_asset(rels, _fl.ASSET_REGEX["cuda"], max_cuda_version="12.8")
    check("P2-7 default resolution uses nightlies (not pin) & skips incomplete CUDA 13-only nightly",
          any("releases?per_page=100" in u for u in seen_urls)
          and not any(f"/tags/{pin_tag}" in u for u in seen_urls)
          and picked_tag == "b12500"
          and picked is not None
          and "cuda-12.4" in picked["name"])

    # 2) --pinned (use_pinned=True) resolves pinned tag; default falls back to pin when latest fails
    pinned_urls: list[str] = []

    def _fake_http_json_pinned(url: str):
        pinned_urls.append(url)
        if f"/tags/{pin_tag}" in url:
            return {
                "tag_name": pin_tag,
                "assets": [{"name": f"llama-{pin_tag}-bin-{os_tag}-vulkan-x64.{ext}"}],
            }
        raise OSError("GitHub latest unavailable")

    with _mock.patch.object(_fl, "http_json", side_effect=_fake_http_json_pinned), \
            _mock.patch.dict(_os.environ, {"HIVEMIND_LLAMA_TAG": ""}, clear=False):
        pin_rels = _fl._resolve_releases("vulkan", _fl.ASSET_REGEX["vulkan"], use_pinned=True)
        fb_rels = _fl._resolve_releases("vulkan", _fl.ASSET_REGEX["vulkan"])
    check("P2-7 --pinned targets deploy/llama_build_pin.txt and latest failure falls back to pin",
          len(pin_rels) == 1 and pin_rels[0]["tag_name"] == pin_tag
          and len(fb_rels) == 1 and fb_rels[0]["tag_name"] == pin_tag
          and any(f"/tags/{pin_tag}" in u for u in pinned_urls))

    # 3) http_json attaches Authorization: Bearer when GITHUB_TOKEN / GH_TOKEN is set
    captured_auth: list[str] = []

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def _fake_urlopen(req, timeout=60):
        captured_auth.append(req.get_header("Authorization") or "")
        return _FakeResp()

    with _mock.patch.object(_fl.urllib.request, "urlopen", side_effect=_fake_urlopen), \
            _mock.patch.dict(_os.environ, {"GITHUB_TOKEN": "secret-tok-123"}, clear=False):
        res_tok = _fl.http_json("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=1")
    check("P2-7 http_json sends Bearer token when GITHUB_TOKEN is set",
          res_tok == {"ok": True} and captured_auth == ["Bearer secret-tok-123"])

    # 4) --upgrade skips when installed build >= target and upgrades when target is newer
    with tempfile.TemporaryDirectory(prefix="hm_p2_7_") as td:
        llama_dir = Path(td) / "llama"
        cur_dir = llama_dir / f"llama-b12000-bin-{os_tag}-vulkan-x64"
        cur_dir.mkdir(parents=True)
        (cur_dir / _fl.SERVER_NAME).write_text("old", encoding="utf-8")

        def _fake_dl(_url: str, dest: Path) -> None:
            lib_name = "ggml-vulkan.dll" if _fl._IS_WIN else "lib/libggml-vulkan.so"
            if dest.name.endswith(".tar.gz"):
                with _tarfile.open(dest, "w:gz") as tf:
                    for arc_name, data in ((_fl.SERVER_NAME, b"new-bin"), (lib_name, b"so")):
                        ti = _tarfile.TarInfo(arc_name)
                        ti.size = len(data)
                        tf.addfile(ti, _io.BytesIO(data))
            else:
                with _zipfile.ZipFile(dest, "w") as zf:
                    zf.writestr(_fl.SERVER_NAME, b"MZ-new")
                    zf.writestr(lib_name, b"DLL")

        def _rel_for(build_num: int) -> list[dict]:
            return [{
                "tag_name": f"b{build_num}",
                "assets": [{
                    "name": f"llama-b{build_num}-bin-{os_tag}-vulkan-x64.{ext}",
                    "browser_download_url": f"https://example.invalid/b{build_num}.{ext}",
                }],
            }]

        orig_llama_dir = _fl.LLAMA_DIR
        _fl.LLAMA_DIR = llama_dir
        try:
            dl_calls: list[str] = []

            def _dl_spy(url: str, dest: Path) -> None:
                dl_calls.append(url)
                _fake_dl(url, dest)

            buf_skip = _io.StringIO()
            with _mock.patch.object(_fl, "http_json", return_value=_rel_for(12000)), \
                    _mock.patch.object(_fl, "download", side_effect=_dl_spy), \
                    _mock.patch.object(sys, "stdout", buf_skip):
                rc_skip = _fl.main(["--backend", "vulkan", "--upgrade"])
            check("P2-7 --upgrade skips download when installed build is already current",
                  rc_skip == 0 and not dl_calls and "already up to date" in buf_skip.getvalue())

            with _mock.patch.object(_fl, "http_json", return_value=_rel_for(12500)), \
                    _mock.patch.object(_fl, "download", side_effect=_dl_spy):
                rc_up = _fl.main(["--backend", "vulkan", "--upgrade"])
            new_have = _fl.existing_build("vulkan")
            check("P2-7 --upgrade downloads and installs when newer build is available",
                  rc_up == 0 and len(dl_calls) == 1
                  and new_have is not None and new_have[0] == 12500)
        finally:
            _fl.LLAMA_DIR = orig_llama_dir


_p2_7_verify()


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


# ── Wave 3 (3-Agent Master Audit P0-1..P0-4, P1-1..P1-3) ────────────────────
import types as _types  # noqa: E402
import core.duo_task_complete_gate as _tcg  # noqa: E402
from core.tool_exec_helpers import _run_bash_fail_fix_pass_insight  # noqa: E402
from hive_functions.loop_machine import ExecutionController  # noqa: E402
from hive_functions.test_runner import TestResult as _TR, _parse_failures  # noqa: E402
from bench.import_polyglot import _extract_test_contract  # noqa: E402
from bench.runner import _workspace_pythonpath, _run_with_tree_kill  # noqa: E402

# W3 P0-1a: run_python exit 0 bumps verify_last_ok_serial, error run_python does not
_r_ok = _types.SimpleNamespace(verify_mutation_serial=2, verify_last_ok_serial=0)
asyncio.run(_run_bash_fail_fix_pass_insight("run_python", {}, "[TOOL_ERROR:RUN_PYTHON_EXEC_ERROR] boom", _r_ok, None, None, 0))
check("W3 P0-1a failed run_python does not bump verify_last_ok_serial", _r_ok.verify_last_ok_serial == 0)
asyncio.run(_run_bash_fail_fix_pass_insight("run_python", {}, "ok\n[exit code: 0]", _r_ok, None, None, 0))
check("W3 P0-1a exit-0 run_python bumps verify_last_ok_serial", _r_ok.verify_last_ok_serial == 2)

# W3 P0-1b: _duo_task_complete_gate nudges ONCE for smoke check when NO_TESTS_RE
with tempfile.TemporaryDirectory(prefix="hm_w3_p01_") as _td:
    (_td_py := Path(_td) / "mod.py").write_text("x = 1\n", encoding="utf-8")
    _orig_rt = _tcg._run_test_suite
    async def _fake_no_tests(**_kw):
        return _TR(success=False, language="python", command="pytest",
                   failure_count=0, error_lines=[], raw_output="collected 0 items / 0 errors",
                   inject_msg="")
    _tcg._run_test_suite = _fake_no_tests
    async def _fake_emit(ev):
        return f"data: {json.dumps(ev)}\n\n"
    _fake_ctx = _types.SimpleNamespace(
        duo_config=_types.SimpleNamespace(agentic_mode=True),
        chat_id="c1",
        emit=_fake_emit,
    )
    _st: dict = {}
    _dmsgs: list = []
    _ex1 = _types.SimpleNamespace(task_complete_called=True)
    _ps1 = _tcg._DuoTaskCompleteGateState(
        exec_result=_ex1, written_files=["mod.py"], dtool_msgs=_dmsgs, ws_str=_td,
        verify_mutation_serial=1, state=_st, verify_warned=False,
        verify_last_ok_serial=0, verify_gate_blocks=0, duo_deadline_at=1e18,
    )
    async def _drain(ps):
        async for _ in _tcg._duo_task_complete_gate(_fake_ctx, ps):
            pass
    try:
        asyncio.run(_drain(_ps1))
        check("W3 P0-1b first no-suite task_complete vetoes with SMOKE CHECK REQUIRED",
              not _ex1.task_complete_called and _st.get("_smoke_check_nudged") is True
              and any("[SMOKE CHECK REQUIRED]" in str(m.get("content", "")) for m in _dmsgs))
        _ex2 = _types.SimpleNamespace(task_complete_called=True)
        _ps2 = _tcg._DuoTaskCompleteGateState(
            exec_result=_ex2, written_files=["mod.py"], dtool_msgs=_dmsgs, ws_str=_td,
            verify_mutation_serial=1, state=_st, verify_warned=_ps1.verify_warned,
            verify_last_ok_serial=_ps1.verify_last_ok_serial,
            verify_gate_blocks=_ps1.verify_gate_blocks, duo_deadline_at=1e18,
        )
        asyncio.run(_drain(_ps2))
        check("W3 P0-1b second call after smoke nudge accepts cleanly without infinite loop",
              _ex2.task_complete_called and _st.get("verify_status") == "syntax_verified")
    finally:
        _tcg._run_test_suite = _orig_rt

# W3 P0-2: cross-edit verify_fail_streak survives reset_stuck_detection
_ec = ExecutionController(max_iterations=10, max_repeats_stuck=5)
for _i in range(5):
    _ec.reset_stuck_detection()
    _stk = _ec.note_verify_result(True, f'File "mod.py", line {10 + _i}\nAssertionError: 1 != 2\n[exit code: 1]\n[cmd] pytest -k test_{_i}')
check("W3 P0-2 note_verify_result hits streak 5 despite interleaved edits & reset_stuck_detection", _stk == 5)
_ec.note_verify_result(False, "[exit code: 0]")
check("W3 P0-2 passing verify resets verify_fail_streak to 0", _ec.verify_fail_streak == 0)

# W3 P0-3: _extract_test_contract extracts constants & error message patterns
with tempfile.TemporaryDirectory(prefix="hm_w3_p03_") as _td:
    _tf = Path(_td) / "dot_dsl_test.py"
    _tf.write_text(
        "from dot_dsl import Node, Edge, Graph, NODE, EDGE, ATTR\n"
        "def test_err(self):\n"
        "    with self.assertRaisesWithMessage(ValueError, 'Graph item incomplete'):\n"
        "        Graph([('bad',)])\n",
        encoding="utf-8",
    )
    _contract = _extract_test_contract([_tf], "dot-dsl")
    check("W3 P0-3 _extract_test_contract extracts imported symbols & exception message",
          all(s in _contract for s in ("Node", "NODE", "EDGE", "Graph item incomplete")),
          _contract)

# W3 P0-4 & P1-3: src/ PYTHONPATH + tree kill + DeepSWE trustme dep
import subprocess as _subprocess  # noqa: E402
with tempfile.TemporaryDirectory(prefix="hm_w3_p04_") as _td:
    _ws = Path(_td)
    (_ws / "src").mkdir()
    _pp = _workspace_pythonpath(_ws, "")
    check("W3 P0-4 _workspace_pythonpath includes workspace and workspace/src",
          str(_ws / "src") in _pp and str(_ws) in _pp, _pp)
    _timed_out = False
    try:
        _run_with_tree_kill([sys.executable, "-c", "import time; time.sleep(4)"], cwd=_ws, env=dict(), timeout=0.6)
    except _subprocess.TimeoutExpired:
        _timed_out = True
    check("W3 P0-4 _run_with_tree_kill raises TimeoutExpired cleanly", _timed_out)
check("W3 P1-3 rent_bootstrap & autopilot install trustme cryptography",
      "trustme" in (ROOT / "bench" / "external" / "rent_bootstrap.sh").read_text(encoding="utf-8")
      and "trustme" in _nb_raw)

# W3 P1-1 & P1-2: run_python workspace/src import & head+tail cap + _parse_failures E-lines
from tools.handlers.exec_tools import _inline_tool_run_python  # noqa: E402
with tempfile.TemporaryDirectory(prefix="hm_w3_p11_") as _td:
    _ws = Path(_td)
    (_ws / "src").mkdir()
    (_ws / "src" / "pkg_demo.py").write_text("VAL = 77\n", encoding="utf-8")
    _py_res = asyncio.run(_inline_tool_run_python({"code": "import pkg_demo; print(pkg_demo.VAL)"}, _ws, None))
    check("W3 P1-1 run_python imports module from workspace/src", "77" in _py_res, _py_res)
    _py_long = asyncio.run(_inline_tool_run_python({"code": "print('HEAD_' + 'x'*5000 + '_TAIL_OK')"}, _ws, None))
    check("W3 P1-1 run_python preserves head and tail when >4000 chars",
          "HEAD_" in _py_long and "_TAIL_OK" in _py_long and "[truncated]" in _py_long)
_, _p_errs = _parse_failures("tests/a_test.py:42: AssertionError\nE   assert 3 == 4\nFAILED tests/a_test.py::test_x\n", "python")
check("W3 P1-2 _parse_failures keeps pytest E-lines & file:line context",
      any("assert 3 == 4" in e for e in _p_errs) and any("a_test.py:42:" in e for e in _p_errs),
      str(_p_errs))

# ── Wave-4 checks (P0-1..P1-4 + Anti-Cheat Bench Isolation) ──────────────────
# W4 P0-1: _green_exec_depth + anti-spoof + gate demotion
import core.duo_task_complete_gate as _gate_mod  # noqa: E402
from core.duo_task_complete_gate import (  # noqa: E402
    _green_exec_depth, _duo_task_complete_gate, _DuoTaskCompleteGateState,
)
from core.tool_executor import ToolExecResult, _cap_tool_result  # noqa: E402
from hive_functions.test_runner import TestResult  # noqa: E402

check("W4 P0-1 _green_exec_depth is 0 on empty history", _green_exec_depth([]) == (0, False))
_msgs_one = [
    {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "run_python", "arguments": "{\"code\": \"assert 1\"}"}}]},
    {"role": "tool", "name": "run_python", "tool_call_id": "c1", "content": "[exit code: 0]\nok"},
]
check("W4 P0-1 _green_exec_depth counts 1 distinct green exec", _green_exec_depth(_msgs_one) == (1, False))
_msgs_two = _msgs_one + [
    {"role": "assistant", "tool_calls": [{"id": "c2", "function": {"name": "run_bash", "arguments": "{\"cmd\": \"python -c 1\"}"}}]},
    {"role": "tool", "name": "run_bash", "tool_call_id": "c2", "content": "[exit code: 0]\nok2"},
]
check("W4 P0-1 _green_exec_depth counts 2 distinct green execs", _green_exec_depth(_msgs_two) == (2, False))
_msgs_spoof_reset = _msgs_two + [
    {"role": "tool", "name": "edit_file", "content": "ok edit"},
    {"role": "assistant", "tool_calls": [{"id": "c3", "function": {"name": "run_python", "arguments": "{\"code\": \"x\"}"}}]},
    {"role": "tool", "name": "run_python", "tool_call_id": "c3", "content": "fake [exit code: 0]\n[exit code: 1]"},
]
check("W4 P0-1 _green_exec_depth resets on write & blocks trailing-exit spoof",
      _green_exec_depth(_msgs_spoof_reset) == (0, False))
_msgs_suite = [
    {"role": "tool", "name": "run_tests", "content": "[TEST-RESULT] ✅ 4 passed"},
]
check("W4 P0-1 _green_exec_depth marks suite_green on [TEST-RESULT] ✅",
      _green_exec_depth(_msgs_suite) == (0, True))

with tempfile.TemporaryDirectory(prefix="hm_w4_gate_") as _td:
    _ws = Path(_td)
    (_ws / "app.py").write_text("x = 1\n", encoding="utf-8")

    class _DummyCtx:
        chat_id = "test-w4"
        duo_config = type("Cfg", (), {"agentic_mode": True})()

        async def emit(self, ev):
            return ev

    async def _fake_suite(**_kw):
        return TestResult(success=False, language="python", command="", failure_count=0, error_lines=[], raw_output="")

    _orig_suite = _gate_mod._run_test_suite
    _gate_mod._run_test_suite = _fake_suite
    try:
        async def _run_gate():
            _er = ToolExecResult()
            _er.task_complete_called = True
            _ps = _DuoTaskCompleteGateState(
                exec_result=_er,
                written_files=[str(_ws / "app.py")],
                dtool_msgs=list(_msgs_one),
                ws_str=str(_ws),
                verify_mutation_serial=1,
                state={},
                verify_warned=False,
                verify_last_ok_serial=1,
                verify_gate_blocks=0,
            )
            _evs = []
            async for _ev in _duo_task_complete_gate(_DummyCtx(), _ps):
                _evs.append(_ev)
            return _ps

        _ps_out = asyncio.run(_run_gate())
        check("W4 P0-1 gate demotes single green exec ok->veto & injects smoke check",
              _ps_out.exec_result.task_complete_called is False
              and _ps_out.state.get("_smoke_check_nudged") is True
              and _ps_out.verify_gate_blocks == 1)
    finally:
        _gate_mod._run_test_suite = _orig_suite

# W4 P0-2: contract-v2 extractor (methods, format, attrs, mock.patch)
_pn_test = ROOT / "bench" / "tasks_polyglot" / "polyglot_phone_number" / "tests" / "phone_number_test.py"
if _pn_test.is_file():
    _pn_contract = _extract_test_contract([_pn_test], "phone-number")
    check("W4 P0-2 phone_number contract-v2 includes pretty format & area_code",
          "(contract-v2)" in _pn_contract
          and ".pretty() -> str" in _pn_contract
          and "(###)-###-####" in _pn_contract
          and ".area_code" in _pn_contract,
          _pn_contract)
with tempfile.TemporaryDirectory(prefix="hm_w4_mock_") as _td:
    _mf = Path(_td) / "demo_mod_test.py"
    _mf.write_text(
        "from unittest import mock\n"
        "from demo_mod import Box\n"
        "def test_it():\n"
        "    b = Box()\n"
        "    assert b.size == 3\n"
        "    with mock.patch('demo_mod.open'):\n"
        "        pass\n",
        encoding="utf-8",
    )
    _mc = _extract_test_contract([_mf], "demo-mod")
    check("W4 P0-2 contract-v2 extracts attr reads and mock.patch targets",
          ".size" in _mc and "demo_mod.open" in _mc, _mc)

# duo_lean 8 orthogonal tools + prompts free of unadvertised tool hardcodes
from tools.definitions import _TOOL_MODE_ALLOWLISTS  # noqa: E402
from hive_functions.prompts import DUO_CODER_BASE, DUO_CODER_EXECUTION, DUO_CODER_UNEXPLORED  # noqa: E402
check("duo_lean has 8 orthogonal core tools",
      _TOOL_MODE_ALLOWLISTS["duo_lean"] == {
          "read_file", "edit_file", "write_file", "run_bash",
          "run_tests", "search_code", "find_files", "task_complete",
      })
check("DUO_CODER_BASE/EXECUTION/UNEXPLORED omit unadvertised tools",
      "write_file_append" not in DUO_CODER_BASE
      and "write_file_append" not in DUO_CODER_EXECUTION
      and "get_signatures" not in DUO_CODER_BASE
      and "run_python" not in DUO_CODER_BASE
      and "install_package" not in DUO_CODER_EXECUTION
      and "list_dir" not in DUO_CODER_UNEXPLORED)

# W4 P1-1: test_runner workspace PYTHONPATH & pytest in _is_build
_tr_src = (ROOT / "hive_functions" / "test_runner.py").read_text(encoding="utf-8")
_et_src = (ROOT / "tools" / "handlers" / "exec_tools.py").read_text(encoding="utf-8")
check("W4 P1-1 test_runner injects workspace/src PYTHONPATH & exec_tools builds include pytest",
      "_workspace_test_env" in _tr_src and '"pytest"' in _et_src)

# W4 P1-4: _cap_tool_result head+tail for non-read_file, head-only for read_file
_long_out = "HEAD_MARKER\n" + ("middle line\n" * 200) + "TAIL_MARKER\n"
_capped_bash = _cap_tool_result(_long_out, max_chars=800, tool_name="run_bash")
_capped_read = _cap_tool_result(_long_out, max_chars=800, tool_name="read_file")
check("W4 P1-4 _cap_tool_result keeps head+tail for run_bash within cap+200",
      "HEAD_MARKER" in _capped_bash and "TAIL_MARKER" in _capped_bash
      and "truncated at" in _capped_bash and len(_capped_bash) <= 1000)
check("W4 P1-4 _cap_tool_result keeps read_file head-only",
      "HEAD_MARKER" in _capped_read and "TAIL_MARKER" not in _capped_read
      and "truncated at" in _capped_read)

# W4 Anti-Cheat & Net-Tok/s & Bootstrap/Autopilot wiring
from bench.runner import (  # noqa: E402
    load_tasks as _bench_load_tasks,
    install_hidden_tests as _bench_install_hidden,
    _stash_hidden_tests_on_disk,
)
with tempfile.TemporaryDirectory(prefix="hm_w4_ac_") as _td:
    _tdir = Path(_td) / "t1"
    (_tdir / "repo").mkdir(parents=True)
    (_tdir / "tests").mkdir(parents=True)
    (_tdir / "repo" / "sol.py").write_text("x = 1\n", encoding="utf-8")
    (_tdir / "tests" / "test_sol.py").write_text("SECRET_MARKER = 42\n", encoding="utf-8")
    (_tdir / "task.json").write_text(json.dumps({"id": "t1", "prompt": "hi", "check": "true"}), encoding="utf-8")
    _loaded = _bench_load_tasks(Path(_td))
    _work = Path(_td) / "work"
    _work.mkdir()
    with _stash_hidden_tests_on_disk(_loaded):
        _hidden_absent_during = not (_tdir / "tests").exists()
        _bench_install_hidden(_loaded[0], _work)
        _copied_from_mem = (_work / "test_sol.py").is_file() and "SECRET_MARKER" in (_work / "test_sol.py").read_text(encoding="utf-8")
    _restored_after = (_tdir / "tests" / "test_sol.py").is_file()
    check("W4 Anti-Cheat stashes on-disk tests during run & installs from memory",
          _hidden_absent_during and _copied_from_mem and _restored_after)

_runner_src = (ROOT / "bench" / "runner.py").read_text(encoding="utf-8")
_rb_sh_src = (ROOT / "bench" / "external" / "rent_bootstrap.sh").read_text(encoding="utf-8")
check("W4 Anti-Cheat --no-detect-urls + Net-Tok/s + bootstrap wiring",
      '"--no-detect-urls"' in _runner_src
      and "tok_sec = round(net_tok / seconds, 1)" in _runner_src
      and "HM_NO_DUO_PLANNER" in _rb_sh_src
      and '"llama_cache_ram_mib": 2048' in _rb_sh_src
      and "contract-v2" in _rb_sh_src
      and "contract-v2" in _nb_raw
      and "HM_NO_DUO_PLANNER" in _nb_raw)


# ── Sonnet 1.6.0 Review Must-Fixes (M1-M3 + H3/H4) ───────────────────────────
def _test_m1_busy_slot_guard():
    import time as _t
    from unittest import mock as _mock
    from backend.llama_slots import ModelSlot
    from backend.manager_load import LlamaLoadMixin
    from backend.manager_process import LlamaProcessMixin

    class _Proc:
        def poll(self):
            return None

    class _Mgr(LlamaLoadMixin, LlamaProcessMixin):
        def __init__(self):
            self._slots = [ModelSlot(0)]
            self._lock = asyncio.Lock()
            self._last_orphan_scan_ts = _t.time() + 999.0
            self._load_seq = 0
            self.metrics = {}
            self.starts = 0

        def _metric_inc(self, name, amount=1):
            self.metrics[name] = self.metrics.get(name, 0) + amount

        async def _port_alive(self, _port):
            return True

        async def _enforce_vram_budget(self, _slot):
            return None

        async def _find_free_slot(self):
            return next((s for s in self._slots if not s.is_running and not s._loading), None)

        async def _evict_lru_async(self, exclude_slot=None):
            return None

        async def _adopt_orphan_slots(self):
            return 0

        async def _start_process(self, slot, model, num_ctx, vision=False,
                                 n_parallel=1, **_kw):
            self.starts += 1
            slot.model = model
            slot.process = _Proc()
            slot._num_ctx = int(num_ctx)
            slot._n_parallel = int(n_parallel)
            slot._vision = bool(vision)
            slot._jinja = True
            slot._loading = False
            slot._ready_event.set()

    async def _fake_kill(_slot):
        raise AssertionError("busy slot must not be killed")

    async def _scenario_in_flight():
        mgr = _Mgr()
        await mgr.ensure_loaded("coder:4b", num_ctx=8192, n_parallel=1)
        mgr._slots[0].mark_in_flight()
        with _mock.patch("backend.manager_load._kill_slot_async", new=_fake_kill):
            port = await mgr.ensure_loaded("coder:4b", num_ctx=32768, n_parallel=2)
        return mgr, port

    mgr_if, port_if = asyncio.run(_scenario_in_flight())
    check("M1a in-flight slot is reused instead of killed on larger ctx/parallel",
          mgr_if.starts == 1 and port_if == mgr_if._slots[0].port
          and mgr_if.metrics.get("evictions_ctx_reload", 0) == 0)

    async def _scenario_pinned():
        mgr = _Mgr()
        await mgr.ensure_loaded("coder:4b", num_ctx=8192, pin=True)
        with _mock.patch("backend.manager_load._kill_slot_async", new=_fake_kill):
            await mgr.ensure_loaded("coder:4b", num_ctx=32768, pin=False)
        return mgr

    mgr_pin = asyncio.run(_scenario_pinned())
    check("M1b pinned slot is reused for unpinned larger-ctx caller",
          mgr_pin.starts == 1 and mgr_pin.metrics.get("evictions_ctx_reload", 0) == 0)


_test_m1_busy_slot_guard()

from core.duo_helpers import _collect_new_explore_paths  # noqa: E402

_seen_m2: set[str] = set()
_n1 = _collect_new_explore_paths(
    [{"function": {"name": "search_code", "arguments": {"pattern": "alpha", "path": "."}}}],
    _seen_m2,
)
_n2 = _collect_new_explore_paths(
    [{"function": {"name": "search_code", "arguments": {"pattern": "beta", "path": "."}}}],
    _seen_m2,
)
_n3 = _collect_new_explore_paths(
    [{"function": {"name": "search_code", "arguments": {"pattern": "beta", "path": "."}}}],
    _seen_m2,
)
check("M2 search_code keys pattern+scope so varied searches with path='.' count as new",
      _n1 == ["alpha @ ."] and _n2 == ["beta @ ."] and _n3 == [],
      f"n1={_n1} n2={_n2} n3={_n3}")

from core.agentic_duo_state import DuoRoundState  # noqa: E402
from core.tool_exec_helpers import _handle_too_large  # noqa: E402
from hive_functions.prompts import DUO_CODER_BASE  # noqa: E402


class _NoopHooks:
    async def emit(self, _data):
        return None


def _run_too_large(mode: str) -> str:
    msgs: list[dict] = []
    rs = DuoRoundState()
    asyncio.run(_handle_too_large(
        "write_file", {"path": "x.py"}, "[write_file error: content too large]",
        {"id": "c1"}, msgs, [""], rs, _NoopHooks(), tool_mode=mode,
    ))
    return "\n".join(str(m.get("content", "")) for m in msgs)


_lean_split = _run_too_large("duo_lean")
_core_split = _run_too_large("duo_core")
check("M3a _handle_too_large omits write_file_append in duo_lean and keeps it in duo_core",
      "write_file_append" not in _lean_split and "write_file_append" in _core_split)
check("M3b DUO_CODER_BASE qualifies ask_user on tool list availability",
      "If ask_user is in your tool list" in DUO_CODER_BASE)

_runner_src_tail = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
_helpers_src_tail = (ROOT / "core" / "tool_exec_helpers.py").read_text(encoding="utf-8")
check("H3/H4 gate_vetoed guard, RUN_BASH_NONZERO slice, generic explore limit & README core tier",
      "_gate_vetoed = not _exec_result.task_complete_called" in _runner_src_tail
      and "and not _gate_vetoed):" in _runner_src_tail
      and 'f"Last output: {_dresult[:300]}"' in _helpers_src_tail
      and "`write_file_append`, `undo_last`" in (ROOT / "README.md").read_text(encoding="utf-8"))

from core.duo_explore_gate import _explore_round_gate  # noqa: E402
from core.duo_task_complete_gate import _VERIFY_REQUIRED_PROMPT  # noqa: E402
from core.tool_exec_helpers import _inject_tool_error_hints  # noqa: E402
from hive_functions.prompts import (  # noqa: E402
    DUO_CODER_EXECUTION, DUO_CODER_EXPLORED, DUO_CODER_UNEXPLORED,
)

_lean_forbidden = (
    "run_python", "list_dir", "get_signatures", "undo_last",
    "write_file_append", "replace_lines", "edit_ast",
    "install_package", "find_references", "lsp_diagnostics",
)
_eg_res = asyncio.run(_explore_round_gate(
    _NoopHooks(), round_tool_names={"read_file"},
    dr_tcs=[{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}],
    seen_explore_paths={"a.py#rng:full"}, explore_only_rounds=1,
    contracts_raw=[], explore_ctx="ctx", exec_mdl="m", until_finished=False,
    snap_before=None, ws_str="/ws", pre_explore_msgs=[], written_files=[],
    ld_setter=lambda _l: None, snapshot_fn=lambda *_a: {},
))
_err_samples = []
for _ec, _tn in (
    ("[TOOL_ERROR:EDIT_FILE_OLD_TEXT_NOT_FOUND] miss", "edit_file"),
    ("[TOOL_ERROR:RUN_BASH_TIMEOUT] timeout", "run_bash"),
    ("[TOOL_ERROR:RUN_BASH_NONZERO] rc=1", "run_bash"),
    ("[TOOL_ERROR:FILE_READ_FAILED] missing", "read_file"),
    ("[TOOL_ERROR:TOOL_NOT_ALLOWED] denied", "write_file_append"),
):
    _em: list[dict] = []
    _dr, _ = _inject_tool_error_hints(_tn, {"path": "x.py"}, _ec, {"id": "c"}, _em, {}, {})
    _err_samples.append(_dr + "\n" + "\n".join(str(m.get("content", "")) for m in _em))

_lean_surfaces = {
    "DUO_CODER_BASE": DUO_CODER_BASE,
    "DUO_CODER_EXECUTION": DUO_CODER_EXECUTION,
    "DUO_CODER_UNEXPLORED": DUO_CODER_UNEXPLORED,
    "DUO_CODER_EXPLORED": DUO_CODER_EXPLORED,
    "_VERIFY_REQUIRED_PROMPT": _VERIFY_REQUIRED_PROMPT,
    "explore_gate_nudge": _eg_res.nudge_message,
    "split_too_large_lean": _lean_split,
    "shared_error_hints": "\n".join(_err_samples),
}
_leaked = [
    f"{_s}:{_t}" for _s, _txt in _lean_surfaces.items()
    for _t in _lean_forbidden if _t in _txt
]
check("lean prompt and gate surfaces contain no core/full-only tool names",
      not _leaked, f" (leaked={_leaked})")

sys.exit(h.finish("glm_deep_audit_wave2"))

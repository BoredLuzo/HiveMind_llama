# -*- coding: utf-8 -*-
"""Regression suite for Audit Wave 2A: security, containment, Linux/A100, and backend slot fixes.

Covers all 13 verified Wave 2A fixes hermetically (no live LLM, GPU, or network):
  1. find_references & _python_content_search: secret exclusion, pruned walk, symlink/junction containment
  2. pre_explore/partition.py & tooling.py: .env excluded, secret/junction/containment on read_file/list_dir/find_files
  3. routers/config.py + backend/manager_load.py: chat_template path-traversal validation
  4. hive_functions/skills.py + core/duo_runner.py: skill slug traversal guard & pruned workspace_file_paths offload
  5. static/app.js + index.html: loadSkillsUI listener XSS fix, esc() hardening, detached DOM fix, stray call removed
  6. routers/vram.py: /budget/debug gate, 304 If-None-Match on cache hit, cache invalidation
  7. utils/file.py: protected HiveMind root state files (without blocking external workspaces)
  8. backend/llama_preflight.py: Linux /proc/meminfo MemAvailable parser + sysconf fallback
  9. context/compression.py: POSIX read paths, orphan tool tail tracking, multimodal list content safety
 10. backend/llama_vram_table.py: Windows PDH Dedicated Limit dGPU selection + Linux sysfs card pairing
 11. backend/llama_start_phases.py + manager_evict.py: vram_gb_override drafter drop & pre-ready /props+headroom
 12. backend/manager_health.py + llama_slots.py + manager_load.py: in_flight idle guard, lock claim, pin/retry fixes
 13. deploy/install_linux.sh: UV_NO_DEV=1, UV_PYTHON=3.14, and rsync/tar gateway runtime excludes
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.ui_sources import read_all_ui_js  # noqa: E402
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


# ── 1. find_references & _python_content_search security & containment ──────

def test_code_intel_containment_and_secrets() -> None:
    from hive_functions.hivemind_feature.ast_tools import find_references_report
    from tools.handlers import _shared
    from tools.handlers.code_intel import _inline_tool_find_references, _python_content_search

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        outside = root.parent / f"_outside_{root.name}.py"
        outside.write_text("SECRET_TARGET_TOKEN = 'outside_secret'\n", encoding="utf-8")
        try:
            (root / ".env").write_text("SECRET_TARGET_TOKEN=top_secret_env\n", encoding="utf-8")
            (root / "app.py").write_text("SECRET_TARGET_TOKEN = 42\nprint(SECRET_TARGET_TOKEN)\n", encoding="utf-8")
            nm_dir = root / "node_modules" / "pkg"
            nm_dir.mkdir(parents=True)
            (nm_dir / "idx.py").write_text("SECRET_TARGET_TOKEN = 'ignored'\n", encoding="utf-8")

            report = find_references_report(root, "SECRET_TARGET_TOKEN")
            check("find_references_report skips .env and node_modules",
                  "app.py" in report and ".env" not in report and "node_modules" not in report,
                  report[:120])

            with mock.patch.object(_shared, "find_references_report", find_references_report):
                inline_res = asyncio.run(
                    _inline_tool_find_references({"symbol": "SECRET_TARGET_TOKEN", "path": "."}, root, str(root))
                )
            check("_inline_tool_find_references enforces secret exclusion",
                  "app.py" in inline_res and ".env" not in inline_res,
                  inline_res[:120])

            # Simulate a junction file pointing outside / skipped by _is_junction
            with mock.patch("tools.handlers.code_intel._is_junction", side_effect=lambda p: p.name == "evil_link.py"):
                evil = root / "evil_link.py"
                evil.write_text("SECRET_TARGET_TOKEN = 'evil'\n", encoding="utf-8")
                sc_res = _python_content_search(root, "SECRET_TARGET_TOKEN") or ""
                check("_python_content_search skips junctions and .env",
                      "app.py" in sc_res and "evil_link.py" not in sc_res and ".env" not in sc_res,
                      sc_res[:120])
        finally:
            outside.unlink(missing_ok=True)


# ── 2. Pre-Explore .env removal, secret/junction/containment & list_dir ─────

def test_pre_explore_containment() -> None:
    from hive_functions.pre_explore import partition as P
    from hive_functions.pre_explore import tooling as T

    check("pre_explore._CODE_EXTS excludes .env", ".env" not in P._CODE_EXTS)

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / ".env").write_text("API_KEY=12345\n", encoding="utf-8")
        (ws / "src").mkdir()
        (ws / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")

        r_sec = asyncio.run(T._exec_tool("read_file", {"path": ".env"}, str(ws), T._DummyTx()))
        check("_exec_tool(read_file) blocks .env secret path",
              "[ERROR:SECRET]" in r_sec and "12345" not in r_sec, r_sec)

        r_out = asyncio.run(T._exec_tool("list_dir", {"path": ".."}, str(ws), T._DummyTx()))
        check("_exec_tool(list_dir) blocks parent directory escape",
              "[ERROR:CONTAINMENT]" in r_out, r_out)

        r_ls = asyncio.run(T._exec_tool("list_dir", {"path": "."}, str(ws), T._DummyTx()))
        check("_exec_tool(list_dir) filters .env from workspace listing",
              "main.py" in r_ls or "dir src" in r_ls)
        check("_exec_tool(list_dir) omits .env entry", ".env" not in r_ls, r_ls)

        r_ff = asyncio.run(T._exec_tool("find_files", {"pattern": "*", "path": "."}, str(ws), T._DummyTx()))
        check("_exec_tool(find_files) omits .env entry", ".env" not in r_ff, r_ff)

        # Verify partition.py denies outside list_dir and passes resolved _fp_abs for read_file
        part_src = (ROOT / "hive_functions" / "pre_explore" / "partition.py").read_text(encoding="utf-8")
        check("partition.py passes _fp_abs (not raw ta_arg) and gates list_dir",
              '{"path": _fp_abs}' in part_src and "path outside workspace" in part_src)


# ── 3. chat_template path traversal validation ──────────────────────────────

def test_chat_template_validation() -> None:
    from routers.config import _validate_model_profile_key
    from backend.manager_load import _append_chat_template

    err_rel = _validate_model_profile_key("chat_template", "../secret.jinja")
    err_abs = _validate_model_profile_key("chat_template", "/etc/passwd")
    ok_base = _validate_model_profile_key("chat_template", "chat_template22.5.jinja")
    check("config._validate_model_profile_key blocks chat_template path traversal",
          err_rel is not None and err_abs is not None and ok_base is None,
          f"rel={err_rel!r} abs={err_abs!r} ok={ok_base!r}")

    cmd_trav = ["llama-server"]
    _append_chat_template(cmd_trav, "custom", "../settings.json")
    cmd_abs = ["llama-server"]
    _append_chat_template(cmd_abs, "custom", str(ROOT / "settings.json"))
    cmd_good = ["llama-server"]
    _append_chat_template(cmd_good, "custom", "chat_template22.5.jinja")
    check("_append_chat_template blocks paths outside model_configs/ and allows valid file",
          "--chat-template-file" not in cmd_trav
          and "--chat-template-file" not in cmd_abs
          and "--chat-template-file" in cmd_good,
          f"trav={cmd_trav} good={cmd_good}")


# ── 4. Skills traversal guard & pruned workspace_file_paths ─────────────────

def test_skills_traversal_and_walk() -> None:
    from hive_functions.skills import skill_file_exists, workspace_file_paths, write_skill_md

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        outside_md = ws.parent / f"_pwned_{ws.name}.md"
        try:
            ok_trav = write_skill_md(ws, {
                "name": "../../_pwned_" + ws.name,
                "description": "test",
                "instructions": ["step 1"],
            })
            # Even if name had slashes, _safe_skill_slug sanitizes it inside .hivemind/skills/
            check("write_skill_md never escapes .hivemind/skills/",
                  not outside_md.exists() and (ok_trav or not outside_md.exists()))
            check("write_skill_md rejects pure traversal dots",
                  not write_skill_md(ws, {"name": "../..", "description": "x"})
                  and not skill_file_exists(ws, "../.."))

            (ws / "node_modules" / "dep").mkdir(parents=True)
            (ws / "node_modules" / "dep" / "ignored.js").write_text("x", encoding="utf-8")
            (ws / "src").mkdir()
            (ws / "src" / "keep.py").write_text("y = 1\n", encoding="utf-8")
            files = workspace_file_paths(ws, max_files=50)
            check("workspace_file_paths prunes node_modules and returns relative posix path",
                  "src/keep.py" in files and not any("node_modules" in f for f in files),
                  f"files={files}")
        finally:
            outside_md.unlink(missing_ok=True)


# ── 5. Frontend XSS, detached DOM & stray call checks ───────────────────────

def test_frontend_app_js_fixes() -> None:
    js = read_all_ui_js()
    html = (ROOT / "index.html").read_text(encoding="utf-8")

    # Verify loadSkillsUI uses addEventListener instead of inline onchange
    skills_block = js.split("async function loadSkillsUI()", 1)[1].split("async function toggleSkill", 1)[0]
    check("loadSkillsUI uses addEventListener instead of inline onchange XSS",
          'onchange="toggleSkill' not in skills_block
          and "addEventListener('change'" in skills_block)

    check("loadMemory escapes e.message",
          "esc(e.message || String(e))" in js)
    check("intent-model-sel escapes model names",
          '<option value="${esc(m)}"${m===S.intentModel?\' selected\':\'\'}>${esc(m)}</option>' in js)

    planner_block = js.split("var _am2 = _phdr2.querySelector('.amodel');", 1)[1].split("document.getElementById('planner-plan-block')", 1)[0]
    check("planner bubble escapes model name and re-queries new spans after innerHTML",
          "esc(window._plannerModelShort || '')" in planner_block
          and "_am2.querySelector('#planner-tokrate')" in planner_block
          and "_am2.querySelector('#planner-elapsed')" in planner_block)

    chip_section = js.split("if (_exports.length > 8)", 1)[1].split("_body2.appendChild(_chipRow);", 1)[0]
    check("stray updateMoeVisibility() removed from partition chipRow",
          "updateMoeVisibility" not in chip_section)
    import re as _re_cb
    _m_cb = _re_cb.search(r"/static/app\.js\?v=(\d{8}-\d{2})", html)
    check("index.html bumped app.js cache-buster to >= 20261121-03",
          bool(_m_cb and _m_cb.group(1) >= "20261121-03"))


# ── 6. routers/vram.py debug gate, 304 cache hit & invalidation ─────────────

def test_vram_router_security_and_etag() -> None:
    import routers.vram as vr

    async def _run() -> None:
        with mock.patch.dict(vr.settings, {"debug_api_enabled": False}, clear=False):
            res_off = await vr.vram_budget_debug()
            check("/vram/budget/debug returns 404 when debug_api_enabled is False",
                  getattr(res_off, "status_code", 200) == 404)
        with mock.patch.dict(vr.settings, {"debug_api_enabled": True}, clear=False):
            res_on = await vr.vram_budget_debug()
            check("/vram/budget/debug returns payload when debug_api_enabled is True",
                  isinstance(res_on, dict) and "in_memory" in res_on)

        vr._invalidate_vram_status_cache()
        fake_req_no_etag = SimpleNamespace(headers={})
        with mock.patch.object(vr._vldr, "_bk_ps", new=mock.AsyncMock(return_value=[])), \
             mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
            first = await vr.vram_status(fake_req_no_etag)
            etag = first.headers.get("ETag", "")
            check("/vram/status initial response carries ETag", bool(etag))

            fake_req_match = SimpleNamespace(headers={"If-None-Match": etag})
            hit_304 = await vr.vram_status(fake_req_match)
            check("/vram/status cached hit returns 304 when If-None-Match matches",
                  getattr(hit_304, "status_code", None) == 304)

            vr._invalidate_vram_status_cache()
            check("_invalidate_vram_status_cache resets ts and payload",
                  vr._vram_status_cache["payload"] is None and vr._vram_status_cache["ts"] == 0.0)

    asyncio.run(_run())


# ── 7. utils/file.py protected root state files ─────────────────────────────

def test_protected_root_state_files() -> None:
    from utils.file import _HIVEMIND_ROOT, _is_protected_path

    for fname in ("memory.json", "memory.json.", "automations.json", "presets.json", "runtime_models.json", "vision_model.json"):
        check(f"_is_protected_path blocks root state file '{fname}'",
              _is_protected_path(_HIVEMIND_ROOT / fname) is True)

    with tempfile.TemporaryDirectory() as td:
        ext_ws = Path(td)
        check("_is_protected_path allows presets.json in external user workspace",
              _is_protected_path(ext_ws / "presets.json") is False
              and _is_protected_path(ext_ws / "memory.json") is False)


# ── 8. backend/llama_preflight.py Linux MemAvailable ────────────────────────

def test_preflight_linux_memavailable() -> None:
    import backend.llama_preflight as LP

    with tempfile.TemporaryDirectory() as td:
        fake_meminfo = Path(td) / "meminfo"
        fake_meminfo.write_text(
            "MemTotal:       65536000 kB\n"
            "MemFree:         2097152 kB\n"
            "MemAvailable:   33554432 kB\n",
            encoding="ascii",
        )
        real_path = Path

        def _fake_path(arg):
            if str(arg) == "/proc/meminfo":
                return fake_meminfo
            return real_path(arg)

        with mock.patch.object(LP.os, "name", "posix"), \
             mock.patch.object(LP, "Path", side_effect=_fake_path):
            gb = LP.free_ram_gb()
            check("Linux free_ram_gb reads MemAvailable (32.0 GB) over MemFree (2.0 GB)",
                  abs(gb - 32.0) < 0.01, f"got {gb:.3f}")

        # Fallback to sysconf when /proc/meminfo absent
        missing = Path(td) / "no_meminfo"
        with mock.patch.object(LP.os, "name", "posix"), \
             mock.patch.object(LP, "Path", side_effect=lambda a: missing if str(a) == "/proc/meminfo" else real_path(a)), \
             mock.patch.object(LP.os, "sysconf", create=True, side_effect=lambda k: 4096 if "PAGE_SIZE" in k else 1024 * 1024):
            gb_fb = LP.free_ram_gb()
            check("Linux free_ram_gb falls back to sysconf when /proc/meminfo missing",
                  abs(gb_fb - 4.0) < 0.01, f"got {gb_fb:.3f}")


# ── 9. context/compression.py POSIX paths, orphan tool, list content ────────

def test_compression_posix_orphan_and_lists() -> None:
    import context.compression as C

    # POSIX path matching without matching URLs
    hits = C._RE_READ_PATH.findall(
        "Read /workspace/repo/src/main.py and https://example.com/a/b/c and C:\\repo\\a.py"
    )
    check("_RE_READ_PATH matches POSIX and Windows paths while ignoring URLs",
          "/workspace/repo/src/main.py" in hits
          and "C:\\repo\\a.py" in hits
          and not any("example.com" in h for h in hits),
          f"hits={hits}")

    # Orphan leading tool message in kept tail gets recorded in _condensed_files
    # Cut lands at index 1 of [user, tool(read_file), assistant, user] with keep_recent_msgs=2:
    # _older=[user], _tail=[tool(read_file), assistant]; orphan pop moves tool(read_file) to _older!
    msgs_orphan = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do task"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "name": "read_file", "content": "Reading /workspace/proj/orphan.py lines 1-5"},
        {"role": "assistant", "content": "done exploring"},
    ]

    async def _run_comp():
        class _DummyClient:
            async def post(self, *a, **k):
                raise RuntimeError("offline")

        # keep_recent_msgs=2 makes _recent_tail_msgs start with role="tool" (orphan.py)!
        out_msgs, condensed, _ = await C._compress_tool_context(
            msgs_orphan, "m:1b", 8199, _DummyClient(), "sys", "do task",
            [], [], keep_recent_msgs=2, local_only=True,
            tail_tool_max_chars=50,
        )
        # Also test multimodal list in kept tool tail & user message
        msgs_list = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "working on step 1"},
            {"role": "tool", "name": "read_file", "content": "step 1 ok"},
            {"role": "assistant", "content": "ok"},
            {"role": "tool", "name": "read_image", "content": [
                {"type": "text", "text": "A" * 200},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ]},
        ]
        out_list, _, _ = await C._compress_tool_context(
            msgs_list, "m:1b", 8199, _DummyClient(), "sys", "hi",
            [], [], keep_recent_msgs=2, local_only=True,
            tail_tool_max_chars=40,
        )

        # Chat session compression with list user content at index 0 of kept tail
        C.init_compression(settings_obj={"session_compress_threshold": 2})
        sess = [
            {"role": "user", "content": f"msg {i}"} for i in range(6)
        ] + [
            {"role": "user", "content": [{"type": "text", "text": "hello img"}]},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "next"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "last"},
            {"role": "assistant", "content": "end"},
        ]
        sess_out = await C._compress_chat_session(sess)
        C.init_compression(settings_obj={})
        return condensed, out_list, sess_out

    condensed, out_list, sess_out = asyncio.run(_run_comp())
    check("orphan leading tool result popped from tail lands in _condensed_files",
          "/workspace/proj/orphan.py" in condensed, f"condensed={condensed}")
    _tool_tail = [m for m in out_list if m.get("role") == "tool"][-1]
    check("tail_tool_max_chars handles list content safely without TypeError",
          isinstance(_tool_tail["content"], list)
          and "[... first " in _tool_tail["content"][0]["text"])
    check("_compress_chat_session merges summary cleanly when first kept user msg is a list",
          isinstance(sess_out[0]["content"], list)
          and "[CONVERSATION SUMMARY" in sess_out[0]["content"][0]["text"])


# ── 10. llama_vram_table dGPU PDH & Linux sysfs pairing ─────────────────────

def test_vram_table_gpu_selection() -> None:
    import backend.llama_vram_table as lvt

    saved_sel = lvt._sel_path_cache
    saved_total = lvt.TOTAL_VRAM_MIB
    try:
        lvt.TOTAL_VRAM_MIB = 16384
        lvt._sel_path_cache = None

        igpu_usage = r"\GPU Adapter Memory(luid_igpu_0)\Dedicated Usage"
        dgpu_usage = r"\GPU Adapter Memory(luid_dgpu_1)\Dedicated Usage"
        igpu_lim = r"\GPU Adapter Memory(luid_igpu_0)\Dedicated Limit"
        dgpu_lim = r"\GPU Adapter Memory(luid_dgpu_1)\Dedicated Limit"

        fake_pdh = types.ModuleType("win32pdh")
        fake_pdh.error = RuntimeError
        fake_pdh.PDH_FMT_LARGE = 1

        def _expand(pattern: str):
            if "Dedicated Limit" in pattern:
                return [igpu_lim, dgpu_lim]
            return [igpu_usage, dgpu_usage]

        fake_pdh.ExpandCounterPath = mock.Mock(side_effect=_expand)
        fake_pdh.OpenQuery = mock.Mock(return_value=1)
        fake_pdh.AddCounter = mock.Mock(side_effect=lambda _h, p: p)
        fake_pdh.CollectQueryData = mock.Mock()
        # iGPU uses 1000 MiB (more than idle dGPU's 200 MiB), but dGPU has 16384 MiB limit!
        _vals = {
            igpu_usage: 1000 * 1024 * 1024,
            dgpu_usage: 200 * 1024 * 1024,
            igpu_lim: 128 * 1024 * 1024,
            dgpu_lim: 16384 * 1024 * 1024,
        }
        fake_pdh.GetFormattedCounterValue = mock.Mock(side_effect=lambda c, _f: (1, _vals[c]))
        fake_pdh.CloseQuery = mock.Mock()

        with mock.patch("platform.system", return_value="Windows"), \
             mock.patch.dict(sys.modules, {"win32pdh": fake_pdh}):
            free_mib = lvt.get_live_gpu_free_mib()
            check("Windows PDH picks dGPU (highest Dedicated Limit) even when iGPU uses more VRAM",
                  free_mib == 16184.0 and lvt._sel_path_cache == dgpu_usage,
                  f"free={free_mib} sel={lvt._sel_path_cache}")

        # Linux sysfs card pairing: card0=2GiB(used=1.5GiB), card1=16GiB(used=0GiB) -> must NOT mix card0 used with card1 total!
        with tempfile.TemporaryDirectory() as td:
            drm = Path(td)
            c0 = drm / "card0" / "device"
            c1 = drm / "card1" / "device"
            c0.mkdir(parents=True)
            c1.mkdir(parents=True)
            (c0 / "mem_info_vram_total").write_text(str(2048 * 1024 * 1024), encoding="ascii")
            (c0 / "mem_info_vram_used").write_text(str(1500 * 1024 * 1024), encoding="ascii")
            (c1 / "mem_info_vram_total").write_text(str(16384 * 1024 * 1024), encoding="ascii")
            (c1 / "mem_info_vram_used").write_text(str(0), encoding="ascii")

            real_path = Path
            with mock.patch("platform.system", return_value="Linux"), \
                 mock.patch("pathlib.Path", side_effect=lambda a: real_path(td) if str(a) == "/sys/class/drm" else real_path(a)):
                linux_free = lvt.get_live_gpu_free_mib()
                check("Linux sysfs pairs total and used from same card (16384 MiB free)",
                      linux_free == 16384.0, f"got {linux_free}")
    finally:
        lvt._sel_path_cache = saved_sel
        lvt.TOTAL_VRAM_MIB = saved_total


# ── 11. llama_start_phases drafter drop & pre-ready /props + headroom ──────

def test_start_phases_drafter_and_props_before_ready() -> None:
    import backend.llama_start_phases as lsp
    import backend.manager_load as ML
    from backend.llama_manager_utils import CanFitResult

    # Calibrated vram_gb_override starts with moe_cache_used=0.0, draft_save_mib=1250.0
    def _can_fit_cal(_m, _ctx, safety_margin_mib=768, exclude_slot_id=None, extra_needed_mib=0.0):
        # base+drafter=4400, dropping drafter subtracts 1250 -> needed=3150, margin=768 <= free=4200
        needed = max(0.0, 4400.0 + float(extra_needed_mib or 0.0))
        return CanFitResult((needed + safety_margin_mib) <= 4200.0, needed, 4200.0, safety_margin_mib, "live")

    mgr_d = SimpleNamespace(can_fit=_can_fit_cal)
    slot_d = SimpleNamespace(slot_id=0)
    cmd_d = ["llama-server", "--ctx-size", "8192", "--model-draft", "d.gguf", "--spec-type", "draft-dflash"]
    fit_in = mgr_d.can_fit("bolt:e4b", 8192, extra_needed_mib=0.0)
    fit_out, new_extra, dropped = lsp._try_drop_external_drafter(
        mgr_d, slot_d, cmd_d, "bolt:e4b", 8192, fit_in, 0.0, 1250.0,
    )
    check("_try_drop_external_drafter works for vram_gb_override models (no 0.0 clamp)",
          dropped is True and fit_out.ok is True and new_extra == -1250.0 and "--model-draft" not in cmd_d,
          f"dropped={dropped} extra={new_extra}")

    # Verify /props and headroom run BEFORE _loading=False and _ready_event.set()
    ev_states = []

    class _RecordingEvent(asyncio.Event):
        def set(self) -> None:
            ev_states.append(("set", slot_s._loading, slot_s.vision_active, slot_s.swa_window, slot_s.last_warning))
            super().set()

    slot_s = SimpleNamespace(
        slot_id=0, port=8199, model="test:4b", _num_ctx=4096, _vision=True,
        _n_parallel=1, _jinja=True, _lifecycle_gen=1, _entry_load_seq=None,
        swa_window=0, vision_active=None, last_warning="stale tight VRAM warning",
        is_running=False, _loading=True, _ready_event=_RecordingEvent(), process=None,
        wait_ready=mock.AsyncMock(return_value=True),
    )

    fake_shared_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(
            200,
            json={"modalities": {"vision": True}, "attention": {"sliding_window_size": 4096}},
            request=req,
        ))
    )

    class _MgrS:
        _slots = [slot_s]
        _load_seq = 0
        _pending_prefetch = []
        _vulkan_init_sem = asyncio.Semaphore(1)

        def can_fit(self, *_a, **_kw):
            return CanFitResult(True, 2000.0, 8000.0, 768, "live")

        async def _kill_port(self, _port):
            return None

        def _get_shared_http_client(self):
            return fake_shared_client

    async def _run_spawn():
        fake_log = SimpleNamespace(write=lambda *_: None, flush=lambda: None, close=lambda: None)

        fake_proc = SimpleNamespace(pid=1234, poll=lambda: None)
        with mock.patch.object(ML, "_archive_old_slot_logs"), \
             mock.patch("builtins.open", return_value=fake_log), \
             mock.patch.object(lsp.subprocess, "Popen", return_value=fake_proc), \
             mock.patch("backend.llama_vram_table.get_live_gpu_free_mib", return_value=5000.0):
            try:
                await lsp._phase_preflight_and_spawn(
                    _MgrS(), slot_s, ["llama", "--ctx-size", "4096"], "test:4b",
                    Path("m.gguf"), 4096, True, 1, True, "test", "none", None, 0.0, False,
                )
            finally:
                await fake_shared_client.aclose()

    asyncio.run(_run_spawn())
    check("_phase_preflight_and_spawn populates /props & clears stale last_warning BEFORE _ready_event.set()",
          ev_states == [("set", False, True, 4096, "")],
          f"states={ev_states}")


# ── 12. Slot lifecycle races (in_flight idle guard, kill reset, waiter) ──────

def test_slot_lifecycle_fixes() -> None:
    import backend.manager_health as MH
    import backend.manager_load as ML
    from backend.llama_slots import ModelSlot

    slot = ModelSlot(0)
    slot.model = "qwen3.5:4b"
    slot.process = SimpleNamespace(pid=99999, poll=lambda: None, terminate=lambda: None, wait=lambda **k: 0)
    slot._idle_timeout = 10.0
    slot.last_used = time.monotonic() - 100.0
    slot.last_warning = "tight VRAM"
    slot.mark_in_flight()
    check("_idle_evictable returns False while slot has active in-flight round",
          not MH._idle_evictable(slot, time.monotonic()))
    slot.kill()
    check("ModelSlot.kill() resets in_flight, in_flight_since, and last_warning",
          slot.in_flight == 0 and slot.in_flight_since == 0.0 and slot.last_warning == "")

    # ensure_loaded waiter promotes pin=True and re-validates mismatched num_ctx
    class _WaiterMgr(ML.LlamaLoadMixin):
        def __init__(self, s):
            self._slots = [s]
            self._lock = asyncio.Lock()
            self._load_seq = 0
            self._last_orphan_scan_ts = time.time()
            self.start_count = 0

        def _metric_inc(self, *_a, **_k):
            pass

        async def _find_loaded(self, model, **_kw):
            for sl in self._slots:
                if ML._nm(sl.model) == ML._nm(model) and (sl.is_running or sl._loading):
                    return sl
            return None

        async def _find_free_slot(self):
            for sl in self._slots:
                if not sl.is_running and not sl._loading:
                    return sl
            return None

        async def _kill_claimed_slot(self, sl):
            sl.model = None
            sl._loading = False
            sl.process = None

        async def _enforce_vram_budget(self, _slot):
            pass

        async def _port_alive(self, _port):
            return True

        async def _start_process(self, sl, model, num_ctx, vision=False, n_parallel=1, ctx_graceful=True, **_kw):
            self.start_count += 1
            sl.process = SimpleNamespace(pid=111, poll=lambda: None)
            sl.model = model
            sl._num_ctx = num_ctx
            sl._vision = vision
            sl._jinja = True
            sl._n_parallel = n_parallel
            sl._loading = False
            sl._ready_event.set()

    async def _run_waiter():
        ws = ModelSlot(0)
        ws.model = "qwen3.5:4b"
        ws._loading = True
        ws._vision = False
        ws._jinja = True
        ws._num_ctx = 4096
        ws._n_parallel = 1
        ws.process = SimpleNamespace(pid=222, poll=lambda: None)
        ws._ready_event.clear()
        mgr = _WaiterMgr(ws)

        async def _finish_owner_load():
            await asyncio.sleep(0.03)
            ws._loading = False
            ws._ready_event.set()

        t_owner = asyncio.create_task(_finish_owner_load())
        with mock.patch.object(ML, "_kill_slot_async", side_effect=lambda sl: setattr(sl, "process", None) or setattr(sl, "model", None) or setattr(sl, "_loading", False) or asyncio.sleep(0)):
            port = await mgr.ensure_loaded("qwen3.5:4b", num_ctx=16384, pin=True)
        await t_owner
        return port, ws.port, mgr.start_count, ws._num_ctx, ws.pinned, ws._idle_timeout

    port, expected_port, starts, final_ctx, is_pinned, idle_to = asyncio.run(_run_waiter())
    check("ensure_loaded non-owner waiter reloads on insufficient num_ctx and promotes pin=True",
          port == expected_port and starts == 1 and final_ctx == 16384 and is_pinned is True and idle_to is None,
          f"port={port}/{expected_port} starts={starts} ctx={final_ctx} pin={is_pinned} idle={idle_to}")


# ── 13. deploy/install_linux.sh env & gateway excludes ──────────────────────

def test_install_linux_updates() -> None:
    sh = (ROOT / "deploy" / "install_linux.sh").read_text(encoding="utf-8")
    check("install_linux.sh sets UV_NO_DEV=1 and UV_PYTHON=3.14 on uv sync",
          "UV_NO_DEV=1 UV_PYTHON=3.14" in sh)
    _rsync_block = sh[sh.index("rsync -a --delete"):sh.index('"$SCRIPT_DIR"/ "$INSTALL_DIR"/')]
    _tar_block = sh[sh.index('tar -C "$SCRIPT_DIR"'):sh.index("-cf - . | tar")]
    for pat in ("/gateway.pid", "/gateway.lock", "/gateway_state.*", "/gateway_audit.jsonl.*", "/update_removed_*"):
        check(f"install_linux.sh rsync + tar both exclude {pat}",
              f"'{pat}'" in _rsync_block and f"'." + f"{pat}'" in _tar_block)


if __name__ == "__main__":
    test_code_intel_containment_and_secrets()
    test_pre_explore_containment()
    test_chat_template_validation()
    test_skills_traversal_and_walk()
    test_frontend_app_js_fixes()
    test_vram_router_security_and_etag()
    test_protected_root_state_files()
    test_preflight_linux_memavailable()
    test_compression_posix_orphan_and_lists()
    test_vram_table_gpu_selection()
    test_start_phases_drafter_and_props_before_ready()
    test_slot_lifecycle_fixes()
    test_install_linux_updates()
    sys.exit(h.finish("audit_wave2a_security_backend"))

# -*- coding: utf-8 -*-
"""Regression suite for HiveMind 1.6.2 installer, fetcher, updater, prune,
and clean-install config fixes.

All cases are hermetic (temp dirs + mocks; no network or real install writes).
Run: python tests/test_installer_updater_1_6_2.py
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


def test_settings_deepcopy_and_safe_policy() -> None:
    import backend.llama_models as lm
    import hive_functions.safe_profile_policy as spp
    import settings as st

    with tempfile.TemporaryDirectory(prefix="hm162_cfg_") as td:
        tmp = Path(td)
        fake_sf = tmp / "settings.json"
        fake_sf.write_text("{}", encoding="utf-8")
        orig_sf = st.SETTINGS_FILE
        orig_agent_dfl = copy.deepcopy(st.DEFAULT_AGENT_CFG)
        orig_dfl = copy.deepcopy(st.DEFAULT_SETTINGS)
        st.SETTINGS_FILE = fake_sf
        try:
            loaded = st._load_settings_from_disk()
            loaded["agents"]["duo_coder"]["model"] = "mutated:coder"
            loaded["exploration_agent"]["model"] = "mutated:explorer"
            check("settings: DEFAULT_AGENT_CFG isolated on load+mutate",
                  st.DEFAULT_AGENT_CFG == orig_agent_dfl)
            check("settings: DEFAULT_SETTINGS isolated on load+mutate",
                  st.DEFAULT_SETTINGS == orig_dfl)
        finally:
            st.SETTINGS_FILE = orig_sf

        # Safe profile policy skips unloaded matrix models when workspace_root is given
        (tmp / "model_configs").mkdir()
        (tmp / "model_configs" / "safe_profile_matrix.json").write_text(
            json.dumps({
                "safe_default": {
                    "model_overrides": {"duo_coder": {"model": "qwen3.5:4b-mtp"}},
                }
            }),
            encoding="utf-8",
        )
        empty_cfg: dict = {"safe_profile_policy": "safe_default", "agents": {"duo_coder": {"model": ""}}}
        with mock.patch.object(lm, "list_available_models", return_value=[]):
            spp.apply_safe_profile_policy(empty_cfg, tmp)
        check("safe_policy: skips unloaded matrix model on empty workspace",
              empty_cfg["agents"]["duo_coder"]["model"] == "")

        gguf = tmp / "qwen3.5-4b-q4_k_m.gguf"
        gguf.write_bytes(b"GGUF")
        (tmp / "models.json").write_text(json.dumps({"qwen3.5:4b-mtp": str(gguf)}), encoding="utf-8")
        installed_cfg: dict = {"safe_profile_policy": "safe_default", "agents": {"duo_coder": {"model": ""}}}
        with mock.patch.object(lm, "list_available_models", return_value=[]):
            spp.apply_safe_profile_policy(installed_cfg, tmp)
        check("safe_policy: applies matrix model once installed in models.json",
              installed_cfg["agents"]["duo_coder"]["model"] == "qwen3.5:4b-mtp")


def test_llama_config_and_models() -> None:
    import backend.llama_config as lc
    import backend.llama_models as lm
    import model_configs.models_registry as reg

    with tempfile.TemporaryDirectory(prefix="hm162_llama_") as td:
        root = Path(td)
        bin_name = "llama-server.exe" if os.name == "nt" else "llama-server"
        cuda_dir = root / "llama-b12001-bin-win-cuda-13.1-x64"
        vk_dir = root / "llama-b12050-bin-win-vulkan-x64"
        cuda_dir.mkdir()
        vk_dir.mkdir()
        (cuda_dir / bin_name).write_text("cuda")
        (vk_dir / bin_name).write_text("vulkan")

        orig_root, orig_be = lc._LLAMA_ROOT, lc.GPU_BACKEND
        try:
            lc._LLAMA_ROOT = root
            lc.GPU_BACKEND = "cuda"
            picked = lc._find_llama_server()
            check("llama_config: backend match outranks higher foreign build",
                  picked.parent.name == cuda_dir.name, f"got {picked}")
        finally:
            lc._LLAMA_ROOT = orig_root
            lc.GPU_BACKEND = orig_be

        # _known_filename_tags falls back to builtin map without gguf_filename_tags.json
        orig_tag_file, orig_cache = lm._FILENAME_TAG_MAP_FILE, lm._filename_tag_map
        try:
            lm._FILENAME_TAG_MAP_FILE = root / "missing_tags.json"
            lm._filename_tag_map = None
            tags = lm._known_filename_tags()
            check("llama_models: builtin filename tags loaded cleanly",
                  tags.get("qwen3.5-4b-q4_k_m.gguf") == "qwen3.5:4b-mtp")
        finally:
            lm._FILENAME_TAG_MAP_FILE = orig_tag_file
            lm._filename_tag_map = orig_cache

        # resolve_mmproj_path matches E4B over alphabetically earlier E2B
        mdir = root / "models"
        mdir.mkdir()
        (mdir / "mmproj-gemma-4-E2B-it-F16.gguf").write_bytes(b"e2b")
        (mdir / "mmproj-gemma-4-E4B-it-F16.gguf").write_bytes(b"e4b")
        orig_mdir = lm.MODELS_DIR
        try:
            lm.MODELS_DIR = mdir
            with mock.patch.object(reg, "get_mmproj_filename", return_value=None), \
                    mock.patch.object(lm, "_load_overrides", return_value={}):
                mm = lm.resolve_mmproj_path("gemma-4:e4b-it-qat")
            check("llama_models: E4B mmproj chosen over alphabetically earlier E2B",
                  mm is not None and "E4B" in mm.name, f"got {mm}")
        finally:
            lm.MODELS_DIR = orig_mdir


def test_fetchers_hardening() -> None:
    import deploy.fetch_llamacpp as fl
    import deploy.fetch_whisper as fw
    import deploy.fetch_woof as fwoof

    # Linux blas whisper.cpp maps to ubuntu tarball
    check("fetch_whisper: linux blas maps to whisper-bin-ubuntu-x64.tar.gz",
          bool(fw.ASSETS[("linux", "blas")].match("whisper-bin-ubuntu-x64.tar.gz"))
          and bool(fw.ASSETS[("ubuntu", "blas")].match("whisper-bin-ubuntu-x64.tar.gz")))

    # install_models rejects partial files >= 50 MB when below 85% of model size
    with tempfile.TemporaryDirectory(prefix="hm162_wh_") as td:
        root = Path(td)
        (root / "models").mkdir()
        m = fw.MODELS["parakeet-q4k"]
        partial = root / "models" / m.filename
        partial.write_bytes(b"\0" * (60 * 1024 * 1024))  # 60 MB > 50 MB flat floor, < 85% of 396 MB
        seen_min: list[int] = []

        def _fake_dl(_url, _dest, min_bytes=0, max_attempts=3):
            seen_min.append(min_bytes)
            return True

        with mock.patch.object(fw, "stt_root", return_value=root), \
                mock.patch.object(fw, "_download", side_effect=_fake_dl):
            ok = fw.install_models(["parakeet-q4k"])
        want_floor = max(fw.MIN_MODEL_BYTES, int(m.size_mb * 1024 * 1024 * 0.85))
        check("fetch_whisper: re-fetches 60MB partial parakeet below 85% floor",
              ok and len(seen_min) == 1 and seen_min[0] == want_floor,
              f"seen={seen_min} want={want_floor}")

        # existing_build skips .old / .staging / foreign backends and finds nested binary
        llama_root = root / "llama"
        (llama_root / "llama-b12000-bin-win-cuda-13.1-x64" / "bin").mkdir(parents=True)
        (llama_root / "llama-b12000-bin-win-cuda-13.1-x64" / "bin" / fl.SERVER_NAME).write_text("cu")
        (llama_root / "llama-b12099-bin-win-cuda-13.1-x64.old").mkdir(parents=True)
        (llama_root / "llama-b12099-bin-win-cuda-13.1-x64.old" / fl.SERVER_NAME).write_text("old")
        (llama_root / "llama-b12088-bin-win-vulkan-x64").mkdir(parents=True)
        (llama_root / "llama-b12088-bin-win-vulkan-x64" / fl.SERVER_NAME).write_text("vk")
        orig_ldir = fl.LLAMA_DIR
        try:
            fl.LLAMA_DIR = llama_root
            found_cu = fl.existing_build("cuda")
            check("fetch_llamacpp: existing_build filters backend and ignores .old",
                  found_cu is not None and found_cu[0] == 12000 and "cuda" in str(found_cu[1]).lower(),
                  f"got {found_cu}")
        finally:
            fl.LLAMA_DIR = orig_ldir

        # Locked target rename fails loudly without orphaned .staging dir
        zip_p = root / "good.zip"
        with zipfile.ZipFile(zip_p, "w") as zf:
            zf.writestr(fl.SERVER_NAME, "MZ")
        locked_target = root / "locked_target"
        locked_target.mkdir()
        (locked_target / fl.SERVER_NAME).write_text("running")
        raised_msg = ""
        with mock.patch.object(Path, "rename", side_effect=OSError("WinError 32 locked")):
            try:
                fl._extract_and_swap(zip_p, locked_target, None)
            except OSError as exc:
                raised_msg = str(exc)
        check("fetch_llamacpp: locked target raises clear error + cleans staging",
              "locked" in raised_msg.lower()
              and not locked_target.with_name(locked_target.name + ".staging").exists(),
              raised_msg)

        # Linux cudart regex matches Ubuntu cuda tarballs
        linux_cu_rx = re.compile(
            r"^cudart-llama(?:-b\d+)?-bin-ubuntu-cuda-([\d.]+)-x64\.(?:zip|tar\.gz)$",
            re.IGNORECASE,
        )
        m_cu = linux_cu_rx.match("cudart-llama-b10900-bin-ubuntu-cuda-13.1-x64.tar.gz")
        check("fetch_llamacpp: Linux cudart tarball pattern matches",
              m_cu is not None and m_cu.group(1) == "13.1")

        # CUDA asset selection falls back to highest compatible runtime <= driver CUDA
        rels = [{
            "tag_name": "b12001",
            "assets": [
                {"name": "llama-b12001-bin-win-cuda-12.4-x64.zip"},
                {"name": "llama-b12001-bin-win-cuda-13.4-x64.zip"},
            ],
        }]
        picked, _ = fl._pick_asset(rels, fl.ASSET_REGEX["cuda"], max_cuda_version="12.8")
        check("fetch_llamacpp: _pick_asset selects cuda-12.4 <= max_cuda_version 12.8",
              picked is not None and picked["name"] == "llama-b12001-bin-win-cuda-12.4-x64.zip",
              f"got {picked}")

        class _FakeSmiRes:
            def __init__(self, stdout: str):
                self.stdout = stdout

        with mock.patch.object(fl, "_find_nvidia_smi", return_value="nvidia-smi"), \
                mock.patch.object(
                    fl.subprocess,
                    "run",
                    side_effect=[_FakeSmiRes("plain banner without version"), _FakeSmiRes("CUDA Version : 12.8\n")],
                ):
            det_cuda = fl._detect_cuda_version_from_driver()
        check("fetch_llamacpp: _detect_cuda_version_from_driver falls back to -q",
              det_cuda == "12.8", f"got {det_cuda!r}")

        # fetch_woof rejects truncated Content-Length downloads without writing dst
        class _ShortResp:
            headers = {"Content-Length": "1000"}

            def __init__(self):
                self._chunks = [b"x" * 200, b""]

            def read(self, _n=-1):
                return self._chunks.pop(0) if self._chunks else b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        woof_dst = root / "woof" / "woof_int8.npz"
        woof_err = False
        with mock.patch.object(fwoof.urllib.request, "urlopen", side_effect=lambda *a, **kw: _ShortResp()):
            try:
                fwoof._download_file("https://example.invalid/woof.npz", woof_dst, max_attempts=1)
            except OSError:
                woof_err = True
        check("fetch_woof: truncated Content-Length rejected and unlinked",
              woof_err and not woof_dst.exists()
              and not (woof_dst.with_name(woof_dst.name + ".part")).exists())


def test_updater_and_prune_and_update_check() -> None:
    import deploy.updater as upd
    import tools.update_check as uc

    # Pre-release stage ordering and ordinal parity
    check("updater: compare alpha < beta < rc < release",
          upd.compare_versions("1.6.2-alpha", "1.6.2-beta") == "newer"
          and upd.compare_versions("1.6.2-beta", "1.6.2-rc1") == "newer"
          and upd.compare_versions("1.6.2-rc1", "1.6.2-rc.2") == "newer"
          and upd.compare_versions("1.6.2-rc.2", "1.6.2") == "newer")
    check("updater: _prerelease_ordinal counts rc2 as 3",
          upd._prerelease_ordinal("1.6.2-rc2") == 3)

    # backup_cmd excludes voice_models/stt/tts-venv/learning_logs
    with tempfile.TemporaryDirectory(prefix="hm162_upd_") as td:
        root = Path(td)
        bk = upd.backup_cmd(root, "update_backup_1.6.2")
        for pat in ("voice_models", "stt", "tts-venv", "learning_logs", "update_removed_*"):
            check(f"updater: backup_cmd excludes {pat}", pat in bk)

        # refresh_uv_path PS snippet handles null/empty User Path safely
        upd_src = (ROOT / "deploy" / "updater.py").read_text(encoding="utf-8")
        check("updater: refresh_uv_path casts empty User Path to [string]",
              "[string][Environment]::GetEnvironmentVariable('Path','User')" in upd_src)

        # stop_watchdog treats markers >= 90s old as inactive
        wd_app = root / "localappdata" / "HiveMindGateway"
        wd_app.mkdir(parents=True)
        marker = wd_app / "watchdog.marker"
        marker.write_text("pid", encoding="utf-8")
        old_t = 1_000_000.0
        os.utime(marker, (old_t, old_t))
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root / "localappdata")}):
            check("updater: stale watchdog.marker returns False",
                  upd.stop_watchdog(root) is False)

        # _prune_legacy_orphans moves legacy release notes + removed profiles
        base = root / "install"
        exroot = root / "extracted"
        trash = root / "trash"
        (base / "model_configs" / "models").mkdir(parents=True)
        (exroot / "docs" / "releases").mkdir(parents=True)
        (base / "RELEASE_NOTES_1.5.0.md").write_text("old notes", encoding="utf-8")
        (base / "model_configs" / "models" / "qwen3.5_9b-ud.json").write_text("{}", encoding="utf-8")
        (base / "settings.json").write_text("{}", encoding="utf-8")
        moved = upd._prune_legacy_orphans(base, exroot, trash)
        check("updater: _prune_legacy_orphans moves old root notes & removed JSON",
              moved == 2
              and not (base / "RELEASE_NOTES_1.5.0.md").exists()
              and (trash / "RELEASE_NOTES_1.5.0.md").is_file()
              and (base / "settings.json").is_file())

        # update_prune.ps1: protects runtime subdirs, prunes removed code in same parents, cleans empty dirs, skips empty trash
        ps_base = root / "ps_base"
        ps_ex = root / "ps_ex"
        ps_trash = root / "ps_trash"
        ps_manifest = ps_base / "update_manifest_installed.txt"
        for rel in (
            "context/projects/notes.md",
            "context/last_workspace.json",
            "context/decider_cache.json",
            "learning/sessions/s1.json",
            "model_configs/learned/a.json",
            "context/obsolete_dir/removed_code.py",
            "learning/removed_learning.py",
        ):
            p = ps_base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x", encoding="utf-8")
        ps_ex.mkdir(parents=True)
        (ps_ex / "README.md").write_text("new", encoding="utf-8")
        ps_manifest.write_text(
            "\n".join([
                "context/projects/notes.md",
                "context/last_workspace.json",
                "context/decider_cache.json",
                "learning/sessions/s1.json",
                "model_configs/learned/a.json",
                "context/obsolete_dir/removed_code.py",
                "learning/removed_learning.py",
            ]) + "\n",
            encoding="utf-8",
        )
        ps_exe = (ROOT / "deploy" / "update_prune.ps1")
        rr = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ps_exe), "-Base", str(ps_base), "-Ex", str(ps_ex),
             "-Trash", str(ps_trash), "-Manifest", str(ps_manifest)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        check("prune_ps1: protects runtime subdirs & prunes removed code",
              rr.returncode == 0
              and rr.stdout.strip() == "2"
              and (ps_base / "context/projects/notes.md").is_file()
              and (ps_base / "context/last_workspace.json").is_file()
              and (ps_base / "context/decider_cache.json").is_file()
              and (ps_base / "learning/sessions/s1.json").is_file()
              and (ps_base / "model_configs/learned/a.json").is_file()
              and not (ps_base / "context/obsolete_dir/removed_code.py").exists()
              and not (ps_base / "context/obsolete_dir").exists()
              and not (ps_base / "learning/removed_learning.py").exists(),
              f"rc={rr.returncode} out={rr.stdout!r} err={rr.stderr[:120]!r}")

        ps_trash_empty = root / "ps_trash_empty"
        rr_empty = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(ps_exe), "-Base", str(ps_base), "-Ex", str(ps_ex),
             "-Trash", str(ps_trash_empty), "-Manifest", str(ps_manifest)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        check("prune_ps1: zero pruned creates no empty trash directory",
              rr_empty.returncode == 0 and rr_empty.stdout.strip() == "0"
              and not ps_trash_empty.exists())

        # update_check: unlinks bad digest & install_update rc 11 / 10
        uc_root = root / "uc_root"
        uc_root.mkdir()
        (uc_root / "update.bat").write_text("@echo off\n", encoding="utf-8")
        res_bad = uc.CheckResult(
            status="update", current="1.6.1", latest="1.6.2", tag="v1.6.2",
            url="https://github.com/BoredLuzo/HiveMind_llama/releases/download/v1.6.2/HiveMind.zip",
            digest="sha256:" + "0" * 64,
        )
        with mock.patch.object(uc.urllib.request, "urlopen",
                               return_value=io.BytesIO(b"PK-fake-zip")):
            dl = uc._download_asset(res_bad, uc_root)
        check("update_check: _download_asset deletes ZIP on sha256 mismatch",
              dl is None
              and not (uc_root / "logs" / "update_downloads" / "HiveMind_v1.6.2.zip").exists())

        zip_ok = uc_root / "ok.zip"
        zip_ok.write_bytes(b"PK-good")
        digest_ok = "sha256:" + hashlib.sha256(b"PK-good").hexdigest()
        res_ok = uc.CheckResult(
            status="update", current="1.6.1", latest="1.6.2", tag="v1.6.2",
            url=res_bad.url, digest=digest_ok,
        )
        with mock.patch.object(uc, "_download_asset", return_value=zip_ok), \
                mock.patch.object(uc.subprocess, "call", return_value=1):
            rc_fail = uc.install_update(res_ok, uc_root)
        check("update_check: install_update returns 11 when update.bat fails",
              rc_fail == 11 and zip_ok.exists())
        with mock.patch.object(uc, "_download_asset", return_value=zip_ok), \
                mock.patch.object(uc.subprocess, "call", return_value=0):
            rc_pass = uc.install_update(res_ok, uc_root)
        check("update_check: install_update returns 10 and cleans zip on success",
              rc_pass == 10 and not zip_ok.exists())


def test_script_hardening_guards() -> None:
    inst = (ROOT / "install.bat").read_text(encoding="utf-8")
    wd = (ROOT / "watchdog_engine.bat").read_text(encoding="utf-8")
    stop = (ROOT / "stop_llama.bat").read_text(encoding="utf-8")
    upd_bat = (ROOT / "update.bat").read_text(encoding="utf-8")
    sh = (ROOT / "deploy" / "install_linux.sh").read_text(encoding="utf-8")

    check("install.bat: uv winget uses -e + agreements + ps1 fallback",
          "-id astral-sh.uv -e --source winget --silent --accept-package-agreements --accept-source-agreements" in inst
          and "https://astral.sh/uv/install.ps1" in inst)
    check("install.bat: msstore Core Utils uses --silent",
          "9NBLGGH4R32N --silent --accept-package-agreements" in inst)
    check("install.bat: sets HIVEMIND_NO_PAUSE around child installer steps",
          inst.count('set "HIVEMIND_NO_PAUSE=1"') >= 2)
    check("watchdog_engine.bat: passes WDMARK via env + LiteralPath",
          "$env:WDMARK" in wd and "-LiteralPath $env:WDMARK" in wd)
    check("stop_llama.bat: verifies Win32_Process CommandLine matches llama-server",
          "Win32_Process" in stop and "llama-server" in stop and "EnableExtensions" in stop)
    check("update.bat: saves HIVEMIND_CALL_CWD before cd /d",
          'set "HIVEMIND_CALL_CWD=%CD%"' in upd_bat)
    check("install_linux.sh: configures render/video groups + excludes voice_models + patches backend",
          "for _grp in render video" in sh
          and 'usermod -aG "$_grp" hivemind' in sh
          and "--exclude '/voice_models'" in sh
          and "--exclude '/stt'" in sh
          and "--exclude '/tts-venv'" in sh
          and "Environment=HIVEMIND_GPU_BACKEND=$LLAMA_BACKEND" in sh)


def test_search_and_lsp_1_6_2() -> None:
    import asyncio
    import tools.handlers.code_intel as ci
    import tools.handlers.lsp as lsp_h
    import tools.websearch as ws

    sx_bat = (ROOT / "searxng.bat").read_text(encoding="utf-8")
    check("searxng.bat: external route executes before HM_PORT validation",
          sx_bat.index('if /i "%ACTION%"=="external" goto external')
          < sx_bat.index("Invalid SearXNG port"))

    # websearch: configure(language="") clears filter & site:github.com bypasses wiki shortcut
    ws.configure(host="http://localhost:8888", enabled=True, engines="", language="en")
    ws.configure(language="")
    check("websearch: configure(language='') clears _SEARXNG_LANGUAGE",
          ws._SEARXNG_LANGUAGE == "")
    ws.configure(language="en")

    called: list[str] = []

    async def _fake_wiki(*a, **kw):
        called.append("wiki")
        return "wiki-hit"

    async def _fake_fetch(q, m, *, engines_override=""):
        called.append("searxng")
        return {"results": [{"title": "GitHub", "url": "https://github.com/org/wiki", "content": "wikipedia client"}]}, None

    with mock.patch.object(ws, "_wiki_direct", side_effect=_fake_wiki), \
            mock.patch.object(ws, "_fetch_searxng_json", side_effect=_fake_fetch):
        res = asyncio.run(ws.web_search("site:github.com wikipedia"))
    check("websearch: non-wiki site: query stays on SearXNG path",
          called == ["searxng"] and "https://github.com/org/wiki" in res, f"called={called}")

    # code_intel: rg clean zero-hit does not fall through + find_files prunes .git/.venv
    with tempfile.TemporaryDirectory(prefix="hm162_ci_") as td:
        tmp = Path(td)
        (tmp / ".git").mkdir()
        (tmp / ".git" / "config").write_text("SECRET_NEEDLE", encoding="utf-8")
        (tmp / ".venv" / "lib").mkdir(parents=True)
        (tmp / ".venv" / "lib" / "mod.py").write_text("SECRET_NEEDLE", encoding="utf-8")
        (tmp / "src.py").write_text("hello = 1\n", encoding="utf-8")

        ff = asyncio.run(ci._inline_tool_find_files({"pattern": "**/*"}, tmp, None))
        check("code_intel: find_files prunes .git and .venv",
              "src.py" in ff and ".git" not in ff and ".venv" not in ff, ff)

        class _FakeRgProc:
            returncode = 1

            async def communicate(self):
                return b"", b""

        py_called: list[str] = []

        def _fake_py(*a, **kw):
            py_called.append("py")
            return "hit"

        with mock.patch.object(ci.shutil, "which", return_value="rg"), \
                mock.patch.object(ci.asyncio, "create_subprocess_exec",
                                  side_effect=[_FakeRgProc()]), \
                mock.patch.object(ci, "_python_content_search", side_effect=_fake_py):
            sc = asyncio.run(ci._inline_tool_search_code({"pattern": "SECRET_NEEDLE"}, tmp, None))
        check("code_intel: clean rg exit 1 stops without case-insensitive/Python fallback",
              not py_called and "no matches" in sc, f"sc={sc!r}")

    # lsp handler forwards lsp_forward_workspace_settings
    captured: list[dict] = []

    class _DummyMgr:
        def __init__(self, workspace, cfg):
            captured.append(cfg)

    with tempfile.TemporaryDirectory(prefix="hm162_lsp_") as td:
        lsp_h._MANAGERS.clear()
        with mock.patch.object(lsp_h._lsp, "LspManager", _DummyMgr), \
                mock.patch.object(lsp_h, "_lsp_settings",
                                  return_value={"lsp_servers": {}, "lsp_forward_workspace_settings": True}):
            async def _call():
                lsp_h._get_manager(Path(td))
            asyncio.run(_call())
        lsp_h._MANAGERS.clear()
        check("lsp: _get_manager forwards lsp_forward_workspace_settings",
              bool(captured and captured[0].get("lsp_forward_workspace_settings") is True))


def main() -> int:
    test_settings_deepcopy_and_safe_policy()
    test_llama_config_and_models()
    test_fetchers_hardening()
    test_updater_and_prune_and_update_check()
    test_script_hardening_guards()
    test_search_and_lsp_1_6_2()
    return h.finish("installer_updater_1_6_2")


if __name__ == "__main__":
    sys.exit(main())

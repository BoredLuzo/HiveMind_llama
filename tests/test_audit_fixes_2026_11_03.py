# -*- coding: utf-8 -*-
"""Audit fixes 2026-11-03 (round 2): the P0/P1 findings from the search wave.

Behavior where a pure function exists; targeted source pins for wiring that
needs a full engine/browser to exercise.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from infra.child_env import scrubbed_env          # noqa: E402
from infra.mcp_server import _is_safe_git          # noqa: E402
from tools.runner import _parse_approval_answer    # noqa: E402
from utils.file import _is_protected_path          # noqa: E402
from routers import config as RC                   # noqa: E402

ML = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
MP = (ROOT / "backend" / "manager_process.py").read_text(encoding="utf-8")
SRV = (ROOT / "server.py").read_text(encoding="utf-8")
CH = (ROOT / "routers" / "chats.py").read_text(encoding="utf-8")
MCP = (ROOT / "infra" / "mcp_server.py").read_text(encoding="utf-8")
LINUX = (ROOT / "deploy" / "install_linux.sh").read_text(encoding="utf-8")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── 1. child env: secrets stripped, shell basics survive ────────────────────
os.environ["HIVEMIND_INTERNAL_TOKEN"] = "tok-123"
os.environ["OPENAI_API_KEY"] = "sk-123"
os.environ["CARGO_REGISTRY_TOKEN"] = "cargo-secret"
_env = scrubbed_env()
check("1a engine token is stripped", "HIVEMIND_INTERNAL_TOKEN" not in _env)
check("1b API keys are stripped", "OPENAI_API_KEY" not in _env)
check("1c toolchain secrets are stripped", "CARGO_REGISTRY_TOKEN" not in _env)
check("1d PATH survives", _env.get("PATH") == os.environ.get("PATH"))
os.environ.pop("HIVEMIND_INTERNAL_TOKEN", None)
os.environ.pop("OPENAI_API_KEY", None)
os.environ.pop("CARGO_REGISTRY_TOKEN", None)

# ── 2. approval parser: no substring approvals ──────────────────────────────
check("2a 'ok' alone approves", _parse_approval_answer("ok") == "once")
check("2b 'output was broken' does not approve",
      _parse_approval_answer("output was broken") == "deny")
check("2c 'token invalid, yes maybe' does not approve",
      _parse_approval_answer("token invalid, yes maybe") == "deny")
check("2d '2' still means repo", _parse_approval_answer("2") == "repo")
check("2e empty stays deny", _parse_approval_answer("") == "deny")

# ── 3. read-tier git cannot write or exec ───────────────────────────────────
check("3a git diff --output is refused",
      not _is_safe_git("git diff --output=C:/tmp/x.txt"))
check("3b external diff drivers are refused",
      not _is_safe_git("git show --ext-diff HEAD"))
check("3c plain git status stays allowed", _is_safe_git("git status --short"))
check("3d rg gets a -- separator", '"--", pattern' in MCP)

# ── 4. served UI and transcripts are write-protected ────────────────────────
check("4a index.html is protected", _is_protected_path(ROOT / "index.html"))
check("4b static/app.js is protected",
      _is_protected_path(ROOT / "static" / "app.js"))
check("4c sessions/ is protected",
      _is_protected_path(ROOT / "sessions" / "x.json"))
check("4d a normal workspace file stays writable",
      not _is_protected_path(ROOT / "workspace_out.txt"))

# ── 5. presets cannot carry the security posture ────────────────────────────
for key in ("duo_action_approval_enabled", "telegram_phone_restricted",
            "mcp_client_enabled", "mcp_servers", "workspace"):
    check(f"5 {key} excluded from presets", key in RC._PRESET_NEVER_KEYS)

# ── 6. wiring pins ──────────────────────────────────────────────────────────
check("6a /v1 agent mode requires the internal token",
      "agent mode is disabled without HIVEMIND_INTERNAL_TOKEN" in SRV
      and "hmac.compare_digest(_v1_given, _v1_tok)" in SRV)
check("6b SSE error branch terminates the response",
      "TERMINAL (audit 2026-11-03)" in SRV)
check("6c idle monitor is started at boot",
      "_lsm_startup.start_idle_monitor()" in SRV)
check("6d peer eviction skips in-flight slots",
      "not s.pinned and not _slot_in_flight(s)" in ML)
check("6e _find_loaded does not release an unheld lock",
      "self._lock.locked()" in MP)
check("6f oversize chat saves answer 413", 'status_code=413' in CH
      and '"chat_too_large"' in CH)
check("6g Linux install keeps models/llama/.cache",
      "--exclude '/models' --exclude '/llama' --exclude '/.cache'" in LINUX)

# ── 7. LSP status card ──────────────────────────────────────────────────────
from infra import lsp_registry as _lreg              # noqa: E402
_rows = _lreg.available({})
check("7a the status report has one row per language", len(_rows) >= 10,
      f"({len(_rows)})")
check("7b every row says installed or missing",
      all(isinstance(r.get("found"), bool) and r.get("language") for r in _rows))
check("7c /lsp/status endpoint exists", '@app.get("/lsp/status"' in SRV)
check("7d the UI has the status list container",
      'id="lsp-status-list"' in (ROOT / "index.html").read_text(encoding="utf-8"))
check("7e the UI fetches and renders it",
      "function loadLspStatus" in (ROOT / "static" / "app.js").read_text(encoding="utf-8")
      and "fetch('/lsp/status')" in (ROOT / "static" / "app.js").read_text(encoding="utf-8"))
_INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
check("7f the server list is collapsible",
      "<details id=\"lsp-status-details\"" in _INDEX)

# ── 8. LFM3-Omni profile (vision+mmproj, non-reasoning) ────────────────────
from model_configs.models_registry import (          # noqa: E402
    get_capabilities as _caps, get_mmproj_filename as _mmproj,
)
_c = _caps("lfm3-omni:2b") or {}
check("8a lfm3-omni is declared multimodal", _c.get("vision") is True)
check("8b lfm3-omni carries its projector",
      str(_mmproj("lfm3-omni:2b") or "").lower().startswith("mmproj-lfm3-omni"))
check("8c the loader treats the base as vision-capable",
      '"lfm3-omni"' in (ROOT / "backend" / "llama_manager_utils.py").read_text(encoding="utf-8"))

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)

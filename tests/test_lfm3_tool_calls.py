# -*- coding: utf-8 -*-
"""LFM3 is a tool-calling model (2026-11-02).

The agentic UI showed a NO TC badge for `lfm3:*` because MODEL_PROFILES had no
entry for the base - the lookup answered "no tool support" and tool rounds were
skipped. LFM3 is post-trained for agentic harnesses; verified against
llama.cpp b12032 (finish_reason=tool_calls, arguments {"path":"."}).

Three things must agree for a tool call to reach the harness:
  1. MODEL_PROFILES: tool_call=True (the UI badge + the tool-round gate)
  2. --jinja: without it llama-server never applies the model's tool template
  3. BINARY_MIN_BUILD: b12001+, older builds cannot load the architecture
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from routing.model_automap import MODEL_PROFILES         # noqa: E402
from backend.llama_config import BINARY_MIN_BUILD        # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── 1. capability profile ──────────────────────────────────────────────────
_lfm3 = MODEL_PROFILES.get("lfm3") or {}
check("1a lfm3 has a profile", bool(_lfm3))
check("1b lfm3 declares tool calls", _lfm3.get("tool_call") is True)
check("1c lfm3 declares thinking (template default)",
      _lfm3.get("thinking") is True)
check("1d lfm3 is not marked as vision", _lfm3.get("vision") is False)
check("1e the lfm2.5 entry is unchanged (no drift)",
      (MODEL_PROFILES.get("lfm2.5") or {}).get("tool_call") is True)

# ── 2. jinja / template ────────────────────────────────────────────────────
SRC_LOAD = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
_jinja_line = next(ln for ln in SRC_LOAD.splitlines() if ln.startswith("_JINJA_BASES ="))
check("2a --jinja is enabled for lfm3", '"lfm3"' in _jinja_line)
check("2b lfm2.5 stays in the list", '"lfm2.5"' in _jinja_line)

# a forced chat template would override the tool-aware one
_chain = next(ln for ln in SRC_LOAD.splitlines() if ln.startswith("_TEMPLATE_CHAIN_BASES"))
check("2c no forced template chain for lfm3", "lfm3" not in _chain)

# ── 3. build gate ──────────────────────────────────────────────────────────
check("3a lfm3 requires a recent build", BINARY_MIN_BUILD.get("lfm3", 0) >= 12001,
      f"({BINARY_MIN_BUILD.get('lfm3')})")
check("3b the omni variant is gated too", BINARY_MIN_BUILD.get("lfm3-omni", 0) >= 12001)

# ── 4. the UI badge follows the profile ────────────────────────────────────
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
check("4a the UI trusts the profile flag",
      "prof.tool_call === 'boolean'" in APP or "prof.tool_call === 'boolean'" in APP
      or "typeof prof.tool_call === 'boolean'" in APP)
check("4b lfm3 is not in the hardcoded NO-TC fallback",
      "lfm3" not in APP[APP.index("_NO_TC_BASES"):APP.index("_NO_TC_BASES") + 120])

# ── 5. the catalog no longer calls it text-only ───────────────────────────
CAT = (ROOT / "deploy" / "fetch_models.py").read_text(encoding="utf-8")
check("5a the installer describes the tool-calling support",
      "tool-calling subagent" in CAT)
check("5b the build requirement is named", "b12001+" in CAT)

# ── 6. the MoE override must beat the profile (groot case) ────────────────
LOAD = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
_i_ovr = LOAD.index('getattr(_lc_live, "MOE_CPU_EXPERTS"')
_i_reg = LOAD.index("_try_registry_moe as _reg_moe")
check("6a the per-model override is resolved first", _i_ovr < _i_reg)
check("6b the override dict is read live, not from the boot import",
      "from . import llama_config as _lc_live" in LOAD)
check("6c the log names the source", "source=%s" in LOAD)
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")
check("6d the settings sync writes the canonical module",
      "_lc_moe.MOE_CPU_EXPERTS = dict(_moe_map)" in SERVER)

# ── 7. the read-aloud flags are re-read from the server ──────────────────
check("7a a refresh helper exists", "function refreshVoiceFlags(" in APP)
check("7b a run start refreshes them", "refreshVoiceFlags();" in APP)
check("7c a tts write refreshes them too", "k.indexOf('tts_') === 0" in APP)
check("7d the switches are repainted from the server",
      "'tts-read-direct-toggle', S.ttsReadDirect" in APP)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)

"""Regression guards for TTS speed/volume UI wiring and model/automap wiring fixes.

Covers:
  (a) TTS speed/volume JS & HTML wiring (zero volume label, speed reload sync, clamping, live volume)
  (b) routing/model_automap parameter regex, VRAM table integration, size-tier scoring
  (c) model_configs/models_registry walkback for underscore/MoE/ternary quant suffixes
  (d) backend/llama_vram_table & llama_config calibrated entries + MoE variant prefix lookup
  (e) shipped model profiles for lfm3:1.7b-qad, lfm3-omni:2b, ternary-bonsai-2:27b, gemma-4, hermes3.6
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check

html = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")
app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")

# ── (a) TTS speed & volume UI/JS wiring ─────────────────────────────────────
check("app.js defines _clampTtsSpeed and _ttsApplyVolume",
      "function _clampTtsSpeed(raw)" in app_js and "function _ttsApplyVolume(raw)" in app_js)
check("volume zero no longer renders 100 % via falsy OR",
      "(S.ttsVolume || 1)" not in app_js
      and "(typeof S.ttsVolume === 'number' ? S.ttsVolume : 1)" in app_js)
check("settings sync sets #tts-speed-inp directly",
      "document.getElementById('tts-speed-inp')" in app_js
      and "_spdInp.value = String(S.ttsSpeed)" in app_js)
check("_syncTtsCard syncs #tts-speed-inp instead of gating on !spd.value",
      "if (spd && !spd.value)" not in app_js
      and "spd.value = String(_clampTtsSpeed(S.ttsSpeed))" in app_js)
check("index.html wires clamped speed + live volume + dual-engine hint",
      "_clampTtsSpeed(this.value)" in html
      and "_ttsApplyVolume(this.value)" in html
      and "Pocket TTS and Underdog Woof" in html)

# ── (b) routing/model_automap size parsing, VRAM lookup, role scoring ───────
from routing import model_automap as MA  # noqa: E402

param_cases = {
    "hermes3.6:35b-a3b-uncensored-genesis-mtp-apex-compact": 35.0,
    "lfm2.5:24b-a2b": 24.0,
    "groot:19b-a3b-261023": 19.0,
    "mellum2.1:12b-a2.5b-thinking": 12.0,
    "lfm3:1.7b-qad": 1.7,
    "qwen3.5:0.8b-ud": 0.8,
    "gemma-4:e4b-it-qat": 4.0,
    "air:26b-a4b": 26.0,
    "ternary-bonsai-2:27b-ptq1-0": 27.0,
    "ling-3.0-tiny-mxfp4-moe": 7.9,
}
for name, want_b in param_cases.items():
    got_b = MA._estimate_param_b(name)
    check(f"_estimate_param_b({name}) == {want_b}", abs(got_b - want_b) < 1e-6, f"(got {got_b})")

synthetic_models = [
    "hermes3.6:35b-a3b-uncensored-genesis-mtp-apex-compact",
    "lfm3:1.7b-qad",
    "qwen3.5:4b-mtp",
    "gemma-4:e4b-it-qat",
]
am8 = MA.get_automap(available=synthetic_models, vram_budget_gb=8.0)
check("8GB automap never assigns 35B MoE to small roles (refiner/judge)",
      am8["assignments"].get("refiner") == "lfm3:1.7b-qad"
      and am8["assignments"].get("judge") == "lfm3:1.7b-qad",
      f"({am8['assignments']})")
check("ternary-bonsai-2:27b estimates ~5.98 GB via VRAM table (not 16.2 GB)",
      abs(MA._estimate_vram_gb_for_model("ternary-bonsai-2:27b-ptq1-0") - 5.98) < 0.05)
check("lfm3-omni:2b capabilities are non-thinking vision + tool_call",
      MA.get_model_capabilities("lfm3-omni:2b") == {"thinking": False, "vision": True, "tool_call": True})
check("gemma-4:e4b-it-obliterated capabilities reflect text-only obliterated GGUF",
      MA.get_model_capabilities("gemma-4:e4b-it-obliterated") == {"thinking": True, "vision": False, "tool_call": True})

# ── (c) models_registry variant walkback & (d) llama_config/VRAM table ──────
from backend import llama_config as LC, llama_vram_table as LVT  # noqa: E402
from model_configs import models_registry as REG  # noqa: E402

REG.refresh()
check("lfm3:1.7b-qad profile resolves with official temp=0.1, top_k=50, 32k ctx",
      REG.get_num_ctx("lfm3:1.7b-qad") == 32768
      and (REG.get_sampling("lfm3:1.7b-qad").get("thinking") or {}).get("temperature") == 0.1
      and (REG.get_sampling("lfm3:1.7b-qad").get("thinking") or {}).get("top_k") == 50)
check("lfm3-omni:2b profile resolves greedy temperature=0.0",
      (REG.get_sampling("lfm3-omni:2b").get("non_thinking") or {}).get("temperature") == 0.0)
for btag in ("ternary-bonsai-2:27b-ptq1-0", "ternary-bonsai-2:27b-abliterated-ptq1-0"):
    _bp = REG.get_profile(btag)
    check(f"{btag} walks back to ternary-bonsai-2:27b profile",
          _bp.get("model") == "ternary-bonsai-2:27b"
          and (_bp.get("sampling") or {}).get("thinking", {}).get("min_p") == 0.05)
check("qwen3.5:2b-q4_k_xl walks back to qwen3.5:2b profile",
      REG.get_profile("qwen3.5:2b-q4_k_xl").get("model") == "qwen3.5:2b")
check("ling-3.0-tiny-mxfp4-moe walks back to ling-3.0-tiny profile",
      REG.get_profile("ling-3.0-tiny-mxfp4-moe").get("model") == "ling-3.0-tiny")
check("llama_vram_table carries ternary-bonsai-2:27b & obliterated Gemma VRAM",
      abs(LVT.vram_of("ternary-bonsai-2:27b") - 5.98) < 0.05
      and abs(LVT.vram_of("gemma-4:e4b-it-obliterated") - 5.8) < 0.05)
check("detect_moe_count matches lfm2.5:24b-a2b-qad variant suffix",
      LC.detect_moe_count("lfm2.5:24b-a2b-qad") == 24)

_g4 = json.loads((ROOT / "model_configs" / "models" / "gemma-4_e4b-it.json").read_text(encoding="utf-8"))
check("gemma-4_e4b-it enables jinja, >=16k ctx, thinking",
      _g4.get("jinja") is True and (_g4.get("num_ctx") or 0) >= 16384
      and (_g4.get("capabilities") or {}).get("thinking") is True)
check("spark-x2.6:4b-uncensored-gsq-rco resolves its q8_0 KV profile",
      REG.get_profile("spark-x2.6:4b-uncensored-gsq-rco").get("kv_cache_type") == "q8_0"
      and REG.get_num_ctx("spark-x2.6:4b-uncensored-gsq-rco") == 32768)
check("minicpm-v-4-7:35b-a3b-mix enables 16k ctx + ubatch 512",
      REG.get_num_ctx("minicpm-v-4-7:35b-a3b-mix") == 16384
      and REG.get_ubatch("minicpm-v-4-7:35b-a3b-mix") == 512)

from backend import llama_models as LM  # noqa: E402
check("dflash drafter GGUFs are excluded by is_non_model_gguf",
      LM.is_non_model_gguf("NVIDIA-Nemotron-3.5-Bolt-E4B-4GB-DFlash.gguf") is True)
for ntag in ("nvidia-nemotron-3.5-bolt:e4b-4gb", "nemotron-3.5-bolt:e4b"):
    _np = REG.get_profile(ntag)
    check(f"{ntag} resolves Nemotron Bolt E4B profile + 4.4 GB VRAM",
          _np.get("model") == "nvidia-nemotron-3.5-bolt:e4b"
          and (_np.get("sampling") or {}).get("thinking", {}).get("temperature") == 1.0
          and abs(MA._estimate_vram_gb_for_model(ntag) - 4.4) < 0.05)
check("qwen3.5:9b and qwen3.5:9b-mtp resolve with expected mtp flags",
      REG.is_mtp("qwen3.5:9b") is False and REG.is_mtp("qwen3.5:9b-mtp") is True)

import asyncio  # noqa: E402
import tempfile  # noqa: E402
from unittest import mock  # noqa: E402
from backend import llama_server_manager as LSM, llama_start_phases as LSP, manager_load as ML  # noqa: E402

check("nemotron-3.5-bolt:e4b declares DFlash drafter n_max=7",
      REG.get_dflash_draft("nemotron-3.5-bolt:e4b") == "Nemotron-3.5-Bolt-E4B-DFlash-BF16.gguf"
      and REG.get_dflash_draft_n_max("nemotron-3.5-bolt:e4b") == 7)
_fake_mgr = LSM.LlamaServerManager.__new__(LSM.LlamaServerManager)
check("_speculation_planned returns True for DFlash-configured model",
      LSM.LlamaServerManager._speculation_planned(_fake_mgr, "nemotron-3.5-bolt:e4b") is True)
with tempfile.TemporaryDirectory() as _td:
    _draft = Path(_td) / "Nemotron-3.5-Bolt-E4B-DFlash-BF16.gguf"
    _draft.write_bytes(b"")
    _target = Path(_td) / "Nemotron-3.5-Bolt-E4B-Q4_K_M.gguf"
    _target.write_bytes(b"")
    with mock.patch.object(LSP, "MODELS_DIR", Path(_td)), \
            mock.patch.object(ML, "_help_supports", new=mock.AsyncMock(return_value=True)), \
            mock.patch.object(_fake_mgr, "_dspark_flag_supported", True):
        _mf = asyncio.run(LSP._phase_model_flags(
            _fake_mgr, [], "nemotron-3.5-bolt:e4b", "nemotron-3.5-bolt", _target, False))
    check("DFlash drafter appends --spec-type draft-dflash + --model-draft + n-max 7",
          "--spec-type" in _mf.cmd and "draft-dflash" in _mf.cmd
          and str(_draft) in _mf.cmd and "--spec-draft-n-max" in _mf.cmd
          and _mf.cmd[_mf.cmd.index("--spec-draft-n-max") + 1] == "7",
          f"({_mf.cmd})")
_ml_src = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
check("manager_load applies --lazy-mode off for non-large-MoE mmap loads",
      '"--lazy-mode"' in _ml_src and '_load_mode.startswith("mmap") and not _large_moe' in _ml_src)

sys.exit(h.finish("tts_and_model_wiring"))

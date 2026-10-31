# -*- coding: utf-8 -*-
"""GROOT profile + side-call sampler (2026-10-30).

GROOT (Groot 19B-A3B-261023, Q4_K_M) is registered through the normal
mechanism: a model_configs profile plus a models.json alias. This test pins
the decisions that were made from the GGUF header and the model card, so a
later edit cannot silently drift them:

  header facts   : arch qwen35moe, 41 blocks, 128 experts (8 used),
                   ctx 262144, nextn head present, KV 576 B/token/layer q4_0
  profile        : ctx 131072, ncmoe 32, KV q4_0, mmap+mlock,
                   MTP on, jinja on, native GGUF template (no override)
  sampler        : thinking temp 1.0 / presence 1.5; code temp 0.6 /
                   presence 0.0; side calls temp 0.7 / top_p 0.8 / top_k 20 /
                   min_p 0 / presence 1.5 with enable_thinking=false

Offline only - no model load, no generation.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def _pick(env_name: str, *candidates):
    """First existing candidate (or the env override) - no user name needed."""
    _v = os.environ.get(env_name)
    if _v:
        return Path(_v)
    for _c in candidates:
        if _c.exists():
            return _c
    return candidates[0]


_ROOT = Path(__file__).resolve().parent.parent
_INSTALL = _pick("HIVEMIND_INSTALL_DIR", _ROOT.parent / "live", _ROOT / "live")
_MODELS = _pick("HIVEMIND_MODELS_DIR", _ROOT.parent.parent / "Models",
                _ROOT.parent / "Models")
_DEV = _pick("HIVEMIND_DEV_DIR", _ROOT.parent.parent / "HiveMind_dev",
             _ROOT.parent / "HiveMind_dev")

PROFILE = _INSTALL / "model_configs" / "models" / "groot_19b-a3b-261023.json"
if not PROFILE.exists():
    # the repo copy is the same file; a checkout without live/ must still
    # run the sizing pins (audit 2026-10-31: they silently SKIPped).
    PROFILE = ROOT / "model_configs" / "models" / "groot_19b-a3b-261023.json"
MODELS = _INSTALL / "models.json"

if not PROFILE.exists():
    print("  FAIL groot profile (neither install dir nor repo copy found)")
    failed += 1
else:
    p = json.loads(PROFILE.read_text(encoding="utf-8"))
    check("model tag", p.get("model") == "groot:19b-a3b-261023", p.get("model"))
    check("ctx 131072 (card allows >= 128K)", p.get("num_ctx") == 131072, p.get("num_ctx"))
    check("KV q4_0", p.get("kv_cache_type") == "q4_0")
    check("mmap+mlock like Hermes", p.get("load_mode") == "mmap+mlock")
    # enabled 2026-10-30 by user decision: tools +60.7 %, prose -4.9 %
    check("MTP enabled (tools +60.7 %, prose -4.9 %)", p.get("mtp") is True)
    check("jinja on", p.get("jinja") is True)
    # NATIVE-TEMPLATE (2026-10-31): the GGUF carries its own qwen-style
    # template; the Sharp override was unnecessary (b12001 parses the
    # native XML tool calls in thinking-off and thinking-on mode).
    check("native GGUF template (no registry override)",
          not p.get("chat_template"), p.get("chat_template"))
    check("ncmoe 32 (real server: fastest + smallest shared delta vs baseline)",
          int(p.get("moe_cpu_experts") or 0) == 32,
          f"({p.get('moe_cpu_experts')})")
    check("thinking capable", (p.get("capabilities") or {}).get("thinking") is True)
    # the two fields the MoE sizing reads (llama_vram_table profile path)
    check("vram override 4.6 (measured non-KV resident)", p.get("vram_gb_override") == 4.6,
          p.get("vram_gb_override"))
    check("kv bytes/token 6336 (11 layers x 576 B)", p.get("kv_bytes_per_token") == 6336,
          p.get("kv_bytes_per_token"))

    s = p.get("sampling") or {}
    th = s.get("thinking") or {}
    check("thinking: temp 1.0", th.get("temperature") == 1.0, th.get("temperature"))
    check("thinking: presence 1.5", th.get("presence_penalty") == 1.5)
    check("thinking: top_k 20 / min_p 0",
          th.get("top_k") == 20 and th.get("min_p") == 0.0)
    for key in ("non_thinking", "sampling_thinking_code", "sampling_text"):
        blk = s.get(key) or {}
        check(f"{key}: temp 0.6 + presence 0.0",
              blk.get("temperature") == 0.6 and blk.get("presence_penalty") == 0.0,
              f"({blk.get('temperature')}/{blk.get('presence_penalty')})")

# models.json alias
if MODELS.exists():
    m = json.loads(MODELS.read_text(encoding="utf-8"))
    alias = m.get("groot:19b-a3b-261023")
    check("models.json alias present", bool(alias), str(alias)[-40:] if alias else "")
    check("alias points at the Q4_K_M gguf",
          bool(alias) and alias.lower().endswith("groot-19b-a3b-261023-q4_k_m.gguf"))
    # b12001 knows arch lfm3 (PR #31252); b11889 did not. The alias is
    # legitimate now - it just needs the right build.
    check("lfm3 alias present (needs b12001+, arch lfm3)",
          "lfm3:1.7b-qad" in m,
          f"({[k for k in m if k.startswith('lfm3')]})")

# side-call sampler constant + wiring
from backend.llama_client import SIDE_CALL_SAMPLING, _build_payload  # noqa: E402



check("SIDE_CALL_SAMPLING values",
      SIDE_CALL_SAMPLING == {"temperature": 0.7, "top_p": 0.8, "top_k": 20,
                             "min_p": 0.0, "presence_penalty": 1.5})
# the sampler is OPT-IN (audit fix 2026-10-30): only callers that pass it
# get it - the main chat path keeps its own temperature
pl = _build_payload([{"role": "user", "content": "x"}], temperature=0.7,
                    max_tokens=8, stream=False, thinking=False,
                    sampler=SIDE_CALL_SAMPLING)
_main = _build_payload([{"role": "user", "content": "x"}], temperature=0.9,
                       max_tokens=8, stream=False, thinking=False)
check("main chat payload keeps its own sampler (no side-call override)",
      _main.get("temperature") == 0.9 and "top_k" not in _main
      and "presence_penalty" not in _main,
      f"(temp {_main.get('temperature')}, top_k {_main.get('top_k')})")
for k, v in SIDE_CALL_SAMPLING.items():
    check(f"payload carries {k}={v}", pl.get(k) == v, f"({pl.get(k)})")
check("payload disables thinking via chat_template_kwargs",
      (pl.get("chat_template_kwargs") or {}).get("enable_thinking") is False)
check("payload has no prompt text in sampler keys",
      not any(isinstance(v, str) and "x" == v for k, v in pl.items()
              if k not in ("thinking", "stream")))

src = (ROOT / "backend" / "llama_client.py").read_text(encoding="utf-8")
check("[SAMPLER] line logs the outgoing params",
      "[SAMPLER] kind=side" in src and "payload.get(\"top_k\")" in src)

print(f"\ngroot_profile: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

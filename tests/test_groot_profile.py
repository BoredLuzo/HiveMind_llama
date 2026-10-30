# -*- coding: utf-8 -*-
"""GROOT profile + side-call sampler (2026-10-30).

GROOT (Groot 19B-A3B-261023, Q4_K_M) is registered through the normal
mechanism: a model_configs profile plus a models.json alias. This test pins
the decisions that were made from the GGUF header and the model card, so a
later edit cannot silently drift them:

  header facts   : arch qwen35moe, 41 blocks, 128 experts (8 used),
                   ctx 262144, nextn head present, KV 576 B/token/layer q4_0
  profile        : ctx 131072, ncmoe 32, KV q4_0, ub 512, mmap+mlock,
                   MTP off (mtp: false), jinja on
  sampler        : thinking temp 1.0 / presence 1.5; code temp 0.6 /
                   presence 0.0; side calls temp 0.7 / top_p 0.8 / top_k 20 /
                   min_p 0 / presence 1.5 with enable_thinking=false

Offline only - no model load, no generation.
"""
import json
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


PROFILE = Path(r"C:/Users/NtheP/Desktop/HiveMind/live/model_configs/models/groot_19b-a3b-261023.json")
MODELS = Path(r"C:/Users/NtheP/Desktop/HiveMind/live/models.json")

if not PROFILE.exists():
    print("  SKIP groot profile (live/ not present)")
else:
    p = json.loads(PROFILE.read_text(encoding="utf-8"))
    check("model tag", p.get("model") == "groot:19b-a3b-261023", p.get("model"))
    check("ctx 131072 (card allows >= 128K)", p.get("num_ctx") == 131072, p.get("num_ctx"))
    check("KV q4_0", p.get("kv_cache_type") == "q4_0")
    check("ub 512", p.get("ubatch") == 512, p.get("ubatch"))
    check("mmap+mlock like Hermes", p.get("load_mode") == "mmap+mlock")
    check("MTP off by default", p.get("mtp") is False)
    check("jinja on", p.get("jinja") is True)
    check("ncmoe in the measured band (28..36)",
          28 <= int(p.get("moe_cpu_experts") or 0) <= 36,
          f"({p.get('moe_cpu_experts')})")
    check("thinking capable", (p.get("capabilities") or {}).get("thinking") is True)

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
    side = p.get("side_call_sampling") or {}
    check("side: temp 0.7 / top_p 0.8 / top_k 20 / min_p 0 / presence 1.5",
          side.get("temperature") == 0.7 and side.get("top_p") == 0.8
          and side.get("top_k") == 20 and side.get("min_p") == 0.0
          and side.get("presence_penalty") == 1.5)
    check("side: thinking disabled", side.get("enable_thinking") is False)

# models.json alias
if MODELS.exists():
    m = json.loads(MODELS.read_text(encoding="utf-8"))
    alias = m.get("groot:19b-a3b-261023")
    check("models.json alias present", bool(alias), str(alias)[-40:] if alias else "")
    check("alias points at the Q4_K_M gguf",
          bool(alias) and alias.lower().endswith("groot-19b-a3b-261023-q4_k_m.gguf"))
    check("no loadable alias for the unsupported lfm3 arch",
          not any(k.startswith("lfm3") for k in m),
          f"({[k for k in m if k.startswith('lfm3')]})")

# side-call sampler constant + wiring
from backend.llama_client import SIDE_CALL_SAMPLING, _build_payload  # noqa: E402
check("SIDE_CALL_SAMPLING values",
      SIDE_CALL_SAMPLING == {"temperature": 0.7, "top_p": 0.8, "top_k": 20,
                             "min_p": 0.0, "presence_penalty": 1.5})
pl = _build_payload([{"role": "user", "content": "x"}], temperature=0.7,
                    max_tokens=8, stream=False, thinking=False)
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

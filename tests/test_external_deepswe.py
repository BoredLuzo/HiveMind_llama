# -*- coding: utf-8 -*-
"""DeepSWE/Qwen3.6-35B external runner stays in sync with the profile."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness

h = Harness(style="plain", pad=True)
check = h.check

sh = (ROOT / "bench" / "external" / "deepswe_qwen36.sh").read_text(
    encoding="utf-8", errors="replace")
nb_path = ROOT / "bench" / "external" / "deepswe_qwen36_colab.ipynb"
nb = json.loads(nb_path.read_text(encoding="utf-8"))
cfg = json.loads((ROOT / "model_configs" / "models" /
                  "qwen3.6_35b-a3b-ud.json").read_text(encoding="utf-8"))

check("official UD-Q4_K_XL model is pinned", "unsloth/Qwen3.6-35B-A3B-GGUF" in sh
      and "UD-Q4_K_XL" in sh)
check("sharp template is fetched from the pinned HiveMind commit",
      "chat_template_sharp_v22.5.0.jinja" in sh and "16fdd71" in sh)
check("server uses jinja + the sharp template",
      "--jinja" in sh and "--chat-template-file" in sh
      and "sharp_v22.5.0.jinja" in sh)
check("five deterministic tasks are the default",
      'N_TASKS="${DEEPSWE_TASKS:-5}"' in sh
      and "--sample-seed" in sh)
check("HiveMind qwen3.6 profile uses the sharp template",
      cfg.get("chat_template") == "chat_template_sharp_v22.5.0.jinja")
simple_nb = json.loads((ROOT / "bench" / "external" / "deepswe_qwen36_colab_simple.ipynb").read_text(encoding="utf-8"))
check("simple Colab notebook has the two token placeholders",
      any('MODAL_TOKEN_ID = ""' in "".join(c["source"]) for c in simple_nb["cells"])
      and any('MODAL_TOKEN_SECRET = ""' in "".join(c["source"]) for c in simple_nb["cells"]))
rent = (ROOT / "bench" / "external" / "rent_bootstrap.sh").read_text(encoding="utf-8", errors="replace")
check("rental bootstrap clones dev and passes the coder llama port",
      'HM_BRANCH="${HM_BRANCH:-dev}"' in rent and "--llama" in rent)
check("rental bootstrap keeps A100 MoE experts on the GPU",
      '"moe_cpu_experts"' in rent and '"qwen3.6:35b-a3b":0' in rent)
check("Colab notebook is A100/Modal/cloudflared based",
      len(nb.get("cells", [])) >= 8
      and any("A100" in "".join(c["source"]) for c in nb["cells"])
      and any("--env modal" in "".join(c["source"]) for c in nb["cells"])
      and any("trycloudflare" in "".join(c["source"]) for c in nb["cells"]))

sys.exit(h.finish("external_deepswe"))

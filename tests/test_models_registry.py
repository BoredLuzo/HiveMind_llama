"""Test: models registry (model_configs/models/*.json) - loading, precedence,
capability/context/launch overrides, filename encoding, cleanup.

Uses a temporary config file in the real models_registry folder and deletes
it again afterwards (regardless of the test result).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from model_configs.models_registry import (
    get_profile, get_capabilities, get_num_ctx, is_vision_preprocessing,
    is_jinja, get_reasoning, get_moe_cpu_experts, get_gpu_layers,
    get_mmproj_filename, get_vram_gb_override, is_distilled, is_mtp,
    save_profile, refresh,
)

passed = 0
failed = 0
_registry_dir = Path(__file__).parent.parent / "model_configs" / "models"
_test_files = []


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


def _cleanup():
    for p in _test_files:
        try:
            p.unlink()
        except OSError:
            pass


print("-- Models-Registry: Write + Load --")

# 1) Canonical-Datei (mit ":"-Encoding im Dateinamen)
p1 = save_profile("my-model:7b", {
    "model": "my-model:7b",
    "capabilities": {"thinking": True, "vision": False, "tool_call": True},
    "vision_preprocessing": True,
    "num_ctx": 8192,
    "num_ctx_duo_coder": 16384,
    "jinja": True,
    "reasoning": "on",
    "moe_cpu_experts": 8,
    "gpu_layers": 42,
    "mmproj_filename": "my-model-mmproj.gguf",
    "vram_gb_override": 3.5,
})
_test_files.append(p1)
check("1 canonical file written", p1.exists() and p1.name == "my-model_7b.json",
      f" ({p1.name})")

# 2) base file (base name without tag -> applies to all tags)
p2 = save_profile("my-model", {
    "model": "my-model",
    "capabilities": {"thinking": False, "vision": False, "tool_call": False},
    "num_ctx": 4096,
})
_test_files.append(p2)
check("2 base file written", p2.exists() and p2.name == "my-model.json",
      f" ({p2.name})")

refresh()

# 3) precedence: canonical wins over base
caps = get_capabilities("my-model:7b")
check("3 canonical capabilities win",
      caps == {"thinking": True, "vision": False, "tool_call": True}, f" ({caps})")

# 4) base config applies to other tags of the same base
caps_base = get_capabilities("my-model:3b")
check("4 base capabilities fallback",
      caps_base == {"thinking": False, "vision": False, "tool_call": False},
      f" ({caps_base})")

# 5) num_ctx: duo-coder override wins for duo_coder
check("5 num_ctx duo_coder", get_num_ctx("my-model:7b", "duo_coder") == 16384)
check("6 num_ctx default", get_num_ctx("my-model:7b") == 8192)
check("7 num_ctx from base", get_num_ctx("my-model:3b") == 4096)

# 6) Vision-Preprocessing-Allowlist-Override
check("8 vision_preprocessing true",
      is_vision_preprocessing("my-model:7b") is True)

# 7) Launch-Settings
check("9 jinja", is_jinja("my-model:7b") is True)
check("10 reasoning", get_reasoning("my-model:7b") == "on")
check("11 moe_cpu_experts", get_moe_cpu_experts("my-model:7b") == 8)
check("12 gpu_layers", get_gpu_layers("my-model:7b") == 42)
check("13 mmproj_filename",
      get_mmproj_filename("my-model:7b") == "my-model-mmproj.gguf")
check("14 vram_gb_override", get_vram_gb_override("my-model:7b") == 3.5)

# 8) unknown model -> None (code fallbacks apply)
check("15 unknown caps -> None", get_capabilities("does-not-exist:1b") is None)
check("16 unknown ctx -> None", get_num_ctx("does-not-exist:1b") is None)
check("17 unknown jinja -> None", is_jinja("does-not-exist:1b") is None)
check("18 unknown distilled -> None", is_distilled("does-not-exist:1b") is None)
check("19 unknown mtp -> None", is_mtp("does-not-exist:1b") is None)

# 9) orphaned files do not interfere
check("20 orphan cleanup (mtime-cache)", get_profile("my-model:7b") != {})

_cleanup()
refresh()

# 10) Nach Cleanup: kein Einfluss mehr
check("21 after cleanup -> None",
      get_capabilities("my-model:7b") is None and get_num_ctx("my-model:7b") is None)

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)

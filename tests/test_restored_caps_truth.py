"""Restored caps + test truth (b261de3 re-applied via f157631, 2026-10-16).

The commit was reverted from dev and re-applied via revert-of-the-revert.
This suite pins the restored state without any model and without
engine/process start (only short python subprocesses for the import-time
env values):
(a) CACHE_RAM_MIB=1024 / CTX_CHECKPOINTS=4 defaults,
(b) env overrides HIVEMIND_CACHE_RAM_MIB / HIVEMIND_CTX_CHECKPOINTS,
(c) _apply_test_truth_to_tc: green->passing, red->failing; nosuite and
    explicit statuses stay,
(d) is_moe_model: a profile pin (true/false) beats the name heuristic,
(e) moe_cfg_for: exact, -ud, prefix; the cmd flags are covered by a
    source guard (the manager builds --cache-ram/--ctx-checkpoints from
    the constants - no slot is ever started here)."""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


ROOT = Path(__file__).resolve().parent.parent
_PROBE = (
    "import sys; sys.path.insert(0, r'%s'); "
    "from backend.llama_config import CACHE_RAM_MIB, CTX_CHECKPOINTS; "
    "print(int(CACHE_RAM_MIB), int(CTX_CHECKPOINTS))" % str(ROOT)
)


def _probe(env_overrides: dict):
    env = {**os.environ, **env_overrides}
    env.pop("HIVEMIND_CACHE_RAM_MIB", None) if not env_overrides.get("HIVEMIND_CACHE_RAM_MIB") else None
    env.pop("HIVEMIND_CTX_CHECKPOINTS", None) if not env_overrides.get("HIVEMIND_CTX_CHECKPOINTS") else None
    r = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       env=env, cwd=str(ROOT), timeout=60)
    return r.stdout.strip()


def main():
    # (a) Defaults ohne Env
    out = _probe({})
    check("defaults: CACHE_RAM_MIB=1024 CTX_CHECKPOINTS=4", out == "1024 4",
          f" got {out!r}")
    # (b) Env-Override
    out = _probe({"HIVEMIND_CACHE_RAM_MIB": "8192", "HIVEMIND_CTX_CHECKPOINTS": "32"})
    check("env override: 8192 / 32", out == "8192 32", f" got {out!r}")

    # (c) task_complete test truth
    from core.tool_executor import _apply_test_truth_to_tc as _tt
    check("test truth: green->passing, red->failing",
          _tt("Build: untested", "green") == "Build: passing"
          and _tt("Build: untested", "red") == "Build: failing")
    check("test truth: nosuite + explicit status stay",
          _tt("Build: untested", "nosuite") == "Build: untested"
          and _tt("Build: blocked", "green") == "Build: blocked")

    # (d) Profil-Pin schlaegt Namens-Heuristik
    import backend.llama_config as _lc
    import model_configs.models_registry as _reg
    _orig = _reg.get_moe_pin
    try:
        _reg.get_moe_pin = lambda k: False   # pin "no" on a MoE-looking name
        dense_pin = _lc.is_moe_model("qwen3.6:35b-a3b")
        _reg.get_moe_pin = lambda k: True    # pin "yes" on a dense-looking name
        moe_pin = _lc.is_moe_model("ling-3.0-tiny")
    finally:
        _reg.get_moe_pin = _orig
    check("moe pin beats name heuristic (both directions)",
          dense_pin is False and moe_pin is True)

    # (e) moe_cfg_for: exact, -ud, prefix (base fields identical; single
    # table rows may carry extra nested keys)
    from backend.llama_vram_table import moe_cfg_for
    base = ("active_gpu_gb", "measured_bytes_per_token", "calibrated_n_cpu_moe")
    cfg_exact = moe_cfg_for("qwen3.6:35b-a3b")
    cfg_ud = moe_cfg_for("qwen3.6:35b-a3b-ud")
    cfg_prefix = moe_cfg_for("qwen3.6:35b-a3b-instant-quant")
    same = all(c is not None for c in (cfg_exact, cfg_ud, cfg_prefix)) and all(
        all(cfg_x.get(f) == cfg_exact.get(f) for f in base)
        for cfg_x in (cfg_ud, cfg_prefix))
    check("moe_cfg_for: exact, -ud, prefix resolve to same base row", same)

    # Source-Waechter: der Manager baut die Flags aus den Konstanten
    ml = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8",
                                                          errors="replace")
    check("manager_load: --cache-ram/--ctx-checkpoints from constants",
          '"--cache-ram", str(int(CACHE_RAM_MIB))' in ml
          and '"--ctx-checkpoints", str(int(CTX_CHECKPOINTS))' in ml)

    cfgs = sorted(p.name for p in (ROOT / "model_configs" / "models").glob(
        "*minicpm-v-4.7*")) + sorted(p.name for p in
        (ROOT / "model_configs" / "models").glob("*lfm2.5_24b*"))
    check("model profiles present (minicpm-v-4.7 + lfm2.5_24b x2)",
          len(cfgs) == 3, f" got {cfgs}")

    print()
    print(f"SUMMARY restored_caps_truth: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


main()

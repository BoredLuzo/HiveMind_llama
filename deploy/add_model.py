"""
add_model.py - interactive assistant: add your own model + config
==========================================================================
Asks the user for a GGUF source, detects the canonical name,
collects capability/context/launch settings and writes:

  1. models.json                     (name -> path, merge, nothing is deleted)
  2. model_configs/models/<name>.json (per-model config, via models_registry)
  3. optional: settings.json agents   (assign the model to an agent role)

Run (via setup_models.bat -> [C]ustom):
  python deploy\\add_model.py [models-dir]
  python deploy\\add_model.py --json path/to/config.json   (non-interactive)

Environment variables like fetch_models.py: HIVEMIND_MODELS_DIR.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.llama_models import _parse_gguf_filename, save_model_mapping  # noqa: E402
from model_configs.models_registry import save_profile  # noqa: E402

AGENT_ROLES = [
    "analyst", "refiner", "critic", "synthesizer",
    "direct", "judge", "duo_coder", "duo_critic",
]


def _ask(question: str, default: str = "") -> str:
    """Terminal prompt with a default; Enter = default."""
    try:
        if default:
            resp = input(f"{question} [{default}] ").strip()
        else:
            resp = input(f"{question} ").strip()
        return resp if resp else default
    except KeyboardInterrupt:
        print("\n[Aborted]")
        sys.exit(1)
    except EOFError:
        return default  # piped stdin / non-interactive


def _ask_bool(question: str, default: bool = False) -> bool:
    d = "y" if default else "n"
    resp = _ask(f"{question} (y/n)", d).lower()
    return resp.startswith(("y", "j"))


def _ask_int(question: str, default: int | None = None) -> int | None:
    d = str(default) if default else ""
    resp = _ask(question, d)
    if not resp.strip():
        return None
    try:
        return int(resp)
    except ValueError:
        print(f"    [WARN] '{resp}' is not a number - skipped.")
        return None


def find_models_dir(arg_dir: str) -> Path:
    if arg_dir:
        return Path(arg_dir).expanduser()
    env = os.environ.get("HIVEMIND_MODELS_DIR", "").strip()
    if env:
        return Path(env)
    # MODELS-DIR-SETTING (2026-09-01): persisted custom folder from settings.json.
    try:
        from settings import load_settings
        _cfg = str(load_settings().get("models_dir", "") or "").strip()
        if _cfg:
            return Path(_cfg)
    except Exception:
        pass
    return ROOT / "models"


def _persist_models_dir(mdir: Path) -> None:
    try:
        from settings import load_settings, save_settings
        s = load_settings()
        s["models_dir"] = str(mdir)
        save_settings(s)
        print(f"    models_dir persisted -> {mdir}")
    except Exception as _e:
        print(f"    [WARNING] could not persist models_dir: {_e}")


def scan_ggufs(mdir: Path) -> list[Path]:
    if not mdir.is_dir():
        return []
    return sorted(mdir.rglob("*.gguf"))


def detect_canonical(path: Path) -> str | None:
    try:
        names = _parse_gguf_filename(path.name)
        return names[0] if names else None
    except Exception:
        return None


def pick_gguf(mdir: Path) -> Path | None:
    print()
    print("  How do you want to specify the GGUF?")
    print("    [1] A single GGUF file by path")
    print("    [2] Choose from existing GGUFs in the models folder")
    choice = _ask("Choice", "1")
    if choice == "2":
        ggufs = scan_ggufs(mdir)
        if not ggufs:
            print(f"    [WARN] No .gguf files found in {mdir}.")
            return None
        print(f"  GGUFs in {mdir}:")
        for i, g in enumerate(ggufs, 1):
            canon = detect_canonical(g) or "?"
            rel = g.relative_to(mdir) if mdir in g.parents else g
            print(f"    [{i}] {rel}  ({canon})")
        idx = _ask_int("Number", None)
        if idx is None or not (1 <= idx <= len(ggufs)):
            print("    [WARN] Invalid number.")
            return None
        return ggufs[idx - 1]
    # Single file path
    raw = _ask("Path to the .gguf file", "")
    if not raw:
        return None
    p = Path(raw).expanduser()
    if not p.is_file():
        print(f"    [WARN] File not found: {p}")
        return None
    return p


def collect_config(gguf: Path, canonical: str, mdir: Path) -> dict:
    """Collect the per-model config interactively with sensible defaults."""
    # Capability defaults from the heuristic
    try:
        from routing.model_automap import get_model_capabilities
        caps = get_model_capabilities(canonical)
    except Exception:
        caps = {"thinking": False, "vision": False, "tool_call": True}

    print()
    print(f"  Configuration for '{canonical}' - Enter = default in parentheses.")
    thinking = _ask_bool("Thinking (reasoning tokens)", bool(caps.get("thinking")))
    vision = _ask_bool("Vision (process images directly)", bool(caps.get("vision")))
    tool_call = _ask_bool("Tool calls (function calling)", bool(caps.get("tool_call")))

    vision_pre = False
    if vision:
        vision_pre = _ask_bool("Also usable for vision preprocessing", True)

    num_ctx = _ask_int("Context size (num_ctx)", 8192)
    num_ctx_duo = _ask_int("Context as duo coder (optional)", None)

    cfg: dict = {
        "model": canonical,
        "capabilities": {
            "thinking": thinking,
            "vision": vision,
            "tool_call": tool_call,
        },
        "vision_preprocessing": vision_pre,
    }
    if num_ctx:
        cfg["num_ctx"] = num_ctx
    if num_ctx_duo:
        cfg["num_ctx_duo_coder"] = num_ctx_duo

    # Advanced
    print()
    print("  Optional launch settings (Enter = skip):")
    mmproj = _ask("mmproj filename (vision projector)", "")
    if mmproj:
        cfg["mmproj_filename"] = mmproj
    jinja = _ask_bool("--jinja (use chat template from GGUF)", False)
    if jinja:
        cfg["jinja"] = True
    reasoning = _ask("Reasoning (on/off/empty)", "")
    if reasoning in ("on", "off"):
        cfg["reasoning"] = reasoning
    moe = _ask_int("MoE CPU experts (n-cpu-moe)", None)
    if moe:
        cfg["moe_cpu_experts"] = moe
    gpu_layers = _ask_int("GPU layers (--n-gpu-layers)", None)
    if gpu_layers:
        cfg["gpu_layers"] = gpu_layers
    vram = _ask("VRAM override in GB (for display/planning)", "")
    if vram:
        try:
            cfg["vram_gb_override"] = float(vram)
        except ValueError:
            print("    [WARN] VRAM override skipped (not a number).")

    # Optional models.json alias? canonical gets registered automatically.
    return cfg


def write_models_json_entry(canonical: str, gguf: Path) -> None:
    """Register the canonical name -> GGUF path in models.json (merge)."""
    save_model_mapping(canonical, str(gguf))
    print(f"    models.json: '{canonical}' -> {gguf}")


def assign_agent(canonical: str) -> None:
    print()
    roles = " / ".join(AGENT_ROLES)
    ans = _ask(f"Assign the model to an agent? ({roles}) / empty = no", "")
    if not ans.strip():
        return
    role = ans.strip().lower()
    if role not in AGENT_ROLES:
        print(f"    [WARN] Unknown role '{role}' - skipped.")
        return
    try:
        from settings import load_settings, save_settings
        s = load_settings()
        agents = s.setdefault("agents", {})
        ag = agents.setdefault(role, {})
        if not isinstance(ag, dict):
            ag = {}
            agents[role] = ag
        ag["model"] = canonical
        save_settings(s)
        print(f"    settings.json: agents[{role}].model = '{canonical}'")
        print("    The model appears automatically in the agent cards (UI refresh).")
    except Exception as e:
        print(f"    [WARN] Agent assignment failed: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("models_dir", nargs="?", default="",
                    help="Models folder (default: HIVEMIND_MODELS_DIR or <repo>/models)")
    ap.add_argument("--json", default="",
                    help="Non-interactive: read the config from a JSON file "
                         "(field 'gguf' = path, 'model' = name)")
    args = ap.parse_args()

    mdir = find_models_dir(args.models_dir)
    mdir.mkdir(parents=True, exist_ok=True)
    _persist_models_dir(mdir)

    print("=" * 60)
    print("  HiveMind - add your own model")
    print("=" * 60)
    print(f"  Models folder: {mdir}")

    if args.json:
        try:
            raw = Path(args.json).read_bytes()
            if raw.startswith(b"\xef\xbb\xbf"):
                raw = raw[3:]
            data = json.loads(raw.decode("utf-8"))
        except Exception as e:
            print(f"[ERROR] JSON not readable: {e}")
            return 1
        gguf_raw = data.get("gguf") or ""
        canonical = data.get("model") or ""
        gguf = Path(gguf_raw).expanduser()
        if not gguf.is_file():
            print(f"[ERROR] GGUF not found: {gguf_raw}")
            return 1
        if not canonical:
            canonical = detect_canonical(gguf) or gguf.stem
        cfg = {k: v for k, v in data.items() if k not in ("gguf",)}
        cfg["model"] = canonical
        print(f"  Model: {canonical}")
        print(f"  GGUF:   {gguf}")
    else:
        gguf = pick_gguf(mdir)
        if gguf is None:
            print("[Aborted] No GGUF chosen.")
            return 1
        canonical = detect_canonical(gguf)
        if not canonical:
            canonical = gguf.stem
            print(f"  [INFO] Filename not parseable - using '{canonical}'.")
        print()
        confirm = _ask(f"Canonical name ('{canonical}') - Enter = OK, or change name:", canonical)
        canonical = confirm.strip() or canonical
        cfg = collect_config(gguf, canonical, mdir)

    # Write
    write_models_json_entry(canonical, gguf)
    path = save_profile(canonical, cfg)
    print(f"    model_configs/models/{path.name} written.")

    assign_agent(canonical)

    print()
    print("[OK] Done. Model registered. Reload the UI if needed (F5).")
    print("     Agent cards & model dropdowns show it automatically.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

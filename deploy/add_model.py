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
    d = str(default) if default is not None else ""
    resp = _ask(question, d)
    if not resp.strip():
        return None
    try:
        return int(resp)
    except ValueError:
        print(f"    [WARN] '{resp}' is not a number - skipped.")
        return None


def _ask_float(question: str, default: float) -> float:
    resp = _ask(question, str(default))
    try:
        return float(resp)
    except (ValueError, TypeError):
        print(f"    [WARN] '{resp}' is not a float - using {default}.")
        return float(default)


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


def _resolve_gguf_path(raw: str, *search_roots: Path) -> Path:
    """Resolve a user- or JSON-provided GGUF path to a canonical absolute Path."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        for root in search_roots:
            cand = (root / p).resolve()
            if cand.is_file():
                return cand
        p = Path.cwd() / p
    return p.resolve()


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
    p = _resolve_gguf_path(raw, mdir)
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

    # Thinking & Reasoning budget
    if thinking:
        t_budget = _ask_int("Thinking budget (tokens, default 2500)", 2500)
        if t_budget is not None:
            cfg["thinking_budget"] = t_budget

    # Sampling defaults
    print()
    print("  Sampling configuration (Enter = default):")
    temp_val = _ask_float("Default temperature", 0.6)
    top_p_val = _ask_float("Top-p", 0.95)
    top_k_val = _ask_int("Top-k", 20)
    if top_k_val is None:
        top_k_val = 20
    pres_val = _ask_float("Presence penalty (0.0 recommended for coding/tools)", 0.0)
    rep_val = _ask_float("Repeat/repetition penalty", 1.0)
    _base_samp = {
        "temperature": temp_val,
        "top_p": top_p_val,
        "top_k": top_k_val,
        "min_p": 0.0,
        "presence_penalty": pres_val,
        "repeat_penalty": rep_val,
        "repetition_penalty": rep_val,
    }
    cfg["sampling"] = {
        "thinking": dict(_base_samp),
        "non_thinking": dict(_base_samp),
        "sampling_thinking_code": {**_base_samp, "presence_penalty": 0.0},
    }

    # Advanced
    print()
    print("  Optional launch & acceleration settings (Enter = skip):")
    detected_mtp = "mtp" in canonical.lower()
    is_mtp_val = _ask_bool("MTP (Multi-Token Prediction / speculative decoding)", detected_mtp)
    if is_mtp_val:
        cfg["mtp"] = True
        n_max = _ask_int("MTP draft max tokens (mtp_draft_n_max, 1-4)", 2)
        if n_max is not None:
            cfg["mtp_draft_n_max"] = max(1, min(4, n_max))
        cfg["mtp_draft_p_min"] = max(0.0, min(0.95, _ask_float("MTP draft min prob (mtp_draft_p_min, 0.0-0.95)", 0.8)))

    load_mode = _ask("Load mode (none / mmap / mmap+mlock / Enter = auto)", "").strip().lower()
    if load_mode in ("none", "mmap", "mmap+mlock"):
        cfg["load_mode"] = load_mode

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
        mc = s.setdefault("agents_user_model_choice", {})
        if isinstance(mc, dict):
            mc[role] = canonical
        excl = s.setdefault("automap_excluded", [])
        if isinstance(excl, list) and role not in excl:
            excl.append(role)
        if role == "duo_coder":
            s["duo_coder_model"] = canonical
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
        gguf_raw = str(data.get("gguf") or "").strip()
        canonical = data.get("model") or ""
        if not gguf_raw:
            print("[ERROR] GGUF path missing in JSON ('gguf').")
            return 1
        gguf = _resolve_gguf_path(gguf_raw, Path(args.json).resolve().parent, mdir)
        if not gguf.is_file():
            print(f"[ERROR] GGUF not found: {gguf_raw} (resolved: {gguf})")
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

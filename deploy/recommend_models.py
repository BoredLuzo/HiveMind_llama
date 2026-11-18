"""Empfiehlt eine Start-Konfiguration fuer DIESE Maschine ("Default, der passt").

Warum: der groesste Einstiegshuerde ist die Modellwahl - Quant, Groesse, MoE-
Offload, ctx. Dieses Skript liest die vorhandenen GGUFs, schaetzt VRAM/RAM,
zieht die gemessenen Geschwindigkeiten aus `token_stats.json` (falls vorhanden)
und schlaegt eine konkrete Rollenbelegung vor.

    python deploy/recommend_models.py            # Bericht
    python deploy/recommend_models.py --json     # maschinenlesbar (fuer Setup-Skripte)

Es aendert nichts - die Ausgabe ist zum Abschreiben oder fuer ein spaeteres
Setup-Skript gedacht.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    _reconf = getattr(_s, "reconfigure", None)
    if callable(_reconf):
        try:
            _reconf(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

ROOT = Path(__file__).resolve().parent.parent
MODELS_JSON = ROOT / "models.json"
STATS_JSON = ROOT / "token_stats.json"

# Ab dieser Groesse gilt ein Modell als "muss teilweise in den RAM"
VRAM_FIT_RATIO = 0.80


def _total_ram_gb() -> float:
    """Delegate RAM probing to deploy.fetch_models.probe_hardware."""
    try:
        sys.path.insert(0, str(ROOT / "deploy"))
        import fetch_models as fm
        return float(fm.probe_hardware()[1] or 0.0)
    except (ImportError, OSError, ValueError, AttributeError):
        return 0.0


def _vram_gb() -> float:
    """Freier VRAM ueber die vorhandene Tabellen-Hilfe (AMD/NVIDIA/Intel)."""
    try:
        sys.path.insert(0, str(ROOT))
        from backend.llama_vram_table import get_live_gpu_free_mib
        mib = float(get_live_gpu_free_mib() or 0)
        return round(mib / 1024.0, 1)
    except (ImportError, OSError, ValueError, AttributeError):
        return 0.0


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _profile_for(tag: str) -> dict:
    """Modellprofil (capabilities, moe_cpu_experts, num_ctx) suchen."""
    try:
        sys.path.insert(0, str(ROOT))
        from model_configs.models_registry import get_profile
        prof = get_profile(tag)
        if prof:
            return prof
    except (ImportError, OSError, ValueError):
        pass
    safe = tag.replace(":", "_").replace("/", "_")
    for cand in (ROOT / "model_configs" / "models" / f"{safe}.json",):
        prof = _load_json(cand)
        if prof:
            return prof
    return {}


def _gguf_total_bytes(p: Path) -> int:
    try:
        from backend.llama_models import _SPLIT_SHARD_RE
        if _SPLIT_SHARD_RE.search(p.stem):
            prefix = _SPLIT_SHARD_RE.sub("", p.stem)
            shards = list(p.parent.glob(f"{prefix}-*-of-*.gguf"))
            if shards:
                return sum(s.stat().st_size for s in shards if s.is_file())
    except (ImportError, OSError):
        pass
    return p.stat().st_size


def collect(vram_gb: float, ram_gb: float) -> list[dict]:
    models = _load_json(MODELS_JSON)
    speeds = (_load_json(STATS_JSON) or {}).get("speeds") or {}
    try:
        sys.path.insert(0, str(ROOT))
        from backend.llama_vram_table import moe_cfg_for, vram_of_moe
    except (ImportError, OSError):
        moe_cfg_for = None
        vram_of_moe = None
    rows: list[dict] = []
    for tag, path in sorted(models.items()):
        p = Path(str(path))
        if not p.exists():
            continue
        # Projectors and DSpark/DFlash/MTP drafters are not standalone models
        _low = f"{tag} {p.name}".lower()
        if "mmproj" in _low or "dspark" in _low or "dflash" in _low or Path(p.name).name.lower().startswith("mtp-"):
            continue
        size_gb = round(_gguf_total_bytes(p) / (1024 ** 3), 2)
        prof = _profile_for(tag)
        moe = int(prof.get("moe_cpu_experts") or 0)
        ctx = int(prof.get("num_ctx") or 0)
        tps = float((speeds.get(tag) or {}).get("tps") or 0.0)
        vram_est = float(prof.get("vram_gb_override") or 0.0)
        if not vram_est and moe and vram_of_moe and moe_cfg_for and moe_cfg_for(tag):
            try:
                vram_est = float(vram_of_moe(tag, n_cpu_moe=moe) or size_gb)
            except (TypeError, ValueError):
                vram_est = size_gb
        if not vram_est:
            vram_est = size_gb
        has_gpu = vram_gb > 0
        fits_vram = has_gpu and (vram_est <= (vram_gb * VRAM_FIT_RATIO))
        fits_ram = size_gb <= max(2.0, ram_gb * 0.6) if ram_gb > 0 else (vram_est <= 4.0)
        if fits_vram and not moe:
            verdict = "fits completely in VRAM"
            score = 3
        elif (fits_ram and moe and has_gpu and vram_est <= max(1.0, vram_gb * VRAM_FIT_RATIO)):
            verdict = "fits via System RAM offload (CPU-MoE, needs 32+ GB RAM)"
            score = 2
        elif fits_ram:
            verdict = "CPU/RAM offload (slower, fits RAM)"
            score = 2
        else:
            verdict = "too large for this machine (still testable)"
            score = 1
        if tps >= 20:
            score += 1
        rows.append({
            "tag": tag, "gb": size_gb, "vram_gb": round(vram_est, 2),
            "moe_cpu_experts": moe, "num_ctx": ctx,
            "measured_tps": tps, "fits_vram": fits_vram, "verdict": verdict,
            "score": score, "path": str(p),
        })
    rows.sort(key=lambda r: (-r["score"], r["gb"]))
    return rows


def _hardware_profile() -> tuple[float, float, float, str]:
    """Return (vram_total_gb, vram_free_gb, ram_gb, gpu_name)."""
    vram_free = _vram_gb()
    ram = _total_ram_gb()
    vram_total = vram_free
    gpu_name = ""
    try:
        sys.path.insert(0, str(ROOT / "deploy"))
        import fetch_models as fm
        vt, rt, gn = fm.probe_hardware()
        if vt > 0:
            vram_total = vt
        if rt > 0:
            ram = rt
        gpu_name = gn
    except (ImportError, OSError, AttributeError):
        pass
    return vram_total, vram_free, ram, gpu_name


def _tier_plan(vram_total: float, ram: float, gpu_name: str) -> tuple[dict, str]:
    try:
        sys.path.insert(0, str(ROOT / "deploy"))
        import fetch_models as fm
        tier = fm.detect_hardware_tier(vram_total, ram)
        plan = fm.format_hardware_plan(vram_total, ram, gpu_name)
        return tier, plan
    except (ImportError, OSError, AttributeError):
        return {}, ""


def _pick_roles_from_rows(rows: list[dict], vram_total: float, vram_free: float,
                          ram: float, tier: dict) -> dict[str, str]:
    """Select curated/measured role winners rather than picking the smallest 0.5 GB GGUF."""
    usable = [r for r in rows if r["score"] >= 2] or rows
    usable_tags = {r["tag"]: r for r in usable}
    by_largest = sorted(usable, key=lambda r: (r["score"], r["gb"]), reverse=True)
    pick_desc = lambda pred: next((r["tag"] for r in by_largest if pred(r)), "")  # noqa: E731
    budget_ref = vram_total or vram_free
    max_direct_gb = max(6.0, budget_ref) if budget_ref > 0 else max(4.0, ram * 0.4)
    max_judge_gb = max(3.0, budget_ref * 0.5) if budget_ref > 0 else max(2.5, ram * 0.25)
    max_coder_gb = max(8.0, budget_ref) if budget_ref > 0 else max(6.0, ram * 0.5)

    try:
        sys.path.insert(0, str(ROOT / "deploy"))
        import apply_model_defaults as amd
        prefs = dict(amd.ROLE_PREFERENCE)
    except (ImportError, OSError, AttributeError):
        prefs = {}

    # Core recommended defaults stay fast and 100% GPU-resident; larger MoEs
    # (working via 32+ GB RAM offload) & 8 GB power dense models appear as optional upgrades.
    curated_orders = {
        "direct": prefs.get("direct") or ["gemma-4:e4b-it-qat", "nvidia-nemotron-3.5-bolt:e4b", "nemotron-3.5-bolt:e4b", "minicpm5:2b-sharp"],
        "judge": prefs.get("judge") or ["minicpm5:2b-sharp", "lfm3:1.7b-qad", "qwen3.5:4b-mtp"],
        "duo_coder": prefs.get("duo_coder") or ["qwen3.5:4b-mtp", "nvidia-nemotron-3.5-bolt:e4b", "nemotron-3.5-bolt:e4b", "gemma-4:e4b-it-qat"],
        "planner": prefs.get("duo_coder") or ["qwen3.5:4b-mtp", "nvidia-nemotron-3.5-bolt:e4b", "nemotron-3.5-bolt:e4b", "gemma-4:e4b-it-qat"],
    }
    pick_curated = lambda role: next((t for t in curated_orders.get(role, ()) if t in usable_tags), "")  # noqa: E731
    heavy_moe = ""
    if tier.get("id") in ("8gb_32gb", "12gb_16gb", "24gb_plus"):
        heavy_moe = next((t for t in ("qwen3.6:35b-a3b-mtp", "air:26b-a4b") if t in usable_tags), "")
    power_dense = next(
        (t for t in ("nvidia-nemotron-3.5-bolt:e4b", "nemotron-3.5-bolt:e4b", "twil:8b", "qwen3.5:9b-mtp") if t in usable_tags),
        "",
    )
    return {
        "direct": (
            next((r["tag"] for r in usable if r["measured_tps"] >= 20), "")
            or pick_curated("direct")
            or pick_desc(lambda r: r["gb"] <= max_direct_gb and r["gb"] >= 1.5)
            or usable[-1]["tag"]
        ),
        "judge": (
            pick_curated("judge")
            or next((r["tag"] for r in usable if r["gb"] <= max_judge_gb), "")
            or usable[0]["tag"]
        ),
        "duo_coder": (
            next((r["tag"] for r in by_largest if r["measured_tps"] >= 15 and not r.get("moe_cpu_experts") and r["gb"] <= max_coder_gb), "")
            or pick_curated("duo_coder")
            or pick_desc(lambda r: r["gb"] <= max_coder_gb)
            or by_largest[0]["tag"]
        ),
        "planner": (
            next((r["tag"] for r in by_largest if r["measured_tps"] >= 15 and r["gb"] <= max_direct_gb), "")
            or pick_curated("planner")
            or pick_desc(lambda r: r["gb"] <= max_direct_gb)
            or usable[0]["tag"]
        ),
        "duo_coder_heavy_moe": heavy_moe,
        "power_8gb": power_dense,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Recommend a hardware-matched HiveMind model setup")
    ap.add_argument("--json", action="store_true", help="JSON instead of a human report")
    ap.add_argument("--plan", action="store_true", help="Show hardware-tier recommendation plan only")
    args = ap.parse_args(argv)

    vram_total, vram_free, ram, gpu_name = _hardware_profile()
    tier, plan_txt = _tier_plan(vram_total, ram, gpu_name)
    if args.plan:
        if args.json:
            print(json.dumps({"detected": {"gpu": gpu_name, "vram_total_gb": vram_total, "vram_free_gb": vram_free, "ram_gb": ram}, "tier": tier}, indent=1))
        else:
            print(plan_txt or f"VRAM: {vram_total:.1f} GB · RAM: {ram:.1f} GB")
        return 0

    rows = collect(vram_total or vram_free, ram)
    if not rows:
        if args.json:
            print(json.dumps({"detected": {"gpu": gpu_name, "vram_total_gb": vram_total, "vram_free_gb": vram_free, "ram_gb": ram}, "tier": tier, "models": []}, indent=1))
        else:
            print(f"No models registered yet ({MODELS_JSON}).\n")
            if plan_txt:
                print(plan_txt)
            print("\nRun setup_models.bat to install your starter set or tier upgrades.")
        return 0 if tier else 1

    roles_rec = _pick_roles_from_rows(rows, vram_total, vram_free, ram, tier)
    rec = {
        "detected": {"gpu": gpu_name, "vram_total_gb": vram_total, "vram_free_gb": vram_free, "ram_gb": ram},
        "tier": tier.get("id", ""),
        **roles_rec,
        "models": rows,
    }
    if args.json:
        print(json.dumps(rec, indent=1, ensure_ascii=False))
        return 0

    if plan_txt:
        print(plan_txt)
        print()
    print(f"Installed ({vram_total:.1f} GB VRAM total / {vram_free:.1f} GB free · RAM {ram:.1f} GB · {len(rows)} models):\n")
    print(f"{'model':<46}{'GB':>6}{'MoE':>5}{'tok/s':>7}  assessment")
    for r in rows:
        print(f"{r['tag'][:45]:<46}{r['gb']:>6.1f}{r['moe_cpu_experts']:>5}"
              f"{(('%.1f' % r['measured_tps']) if r['measured_tps'] else '-'):>7}  {r['verdict']}")
    print("\nProposal from installed models (Recommended Default - 100% VRAM):")
    for role in ("direct", "judge", "duo_coder", "planner"):
        print(f"  {role:<10} {rec[role] or '(no matching model found)'}")
    if rec.get("duo_coder_heavy_moe") or rec.get("power_8gb"):
        print("\nOptional Installed Upgrades (switch in UI -> Agents anytime):")
        if rec.get("power_8gb"):
            print(f"  {'power_8gb':<20} {rec['power_8gb']} (strong 8 GB GPU-resident option)")
        if rec.get("duo_coder_heavy_moe"):
            print(f"  {'duo_coder (RAM-MoE)':<20} {rec['duo_coder_heavy_moe']} (works on 8 GB via 32+ GB RAM offload)")
    _roles = [rec[r] for r in ("direct", "judge", "duo_coder", "planner")]
    if len(set(_roles)) == 1:
        print("\nNote: only one model really fits - the coder usually benefits from a\n"
              "larger MoE with CPU experts (RAM offload), though it loads more slowly.")
    print("\nAll recommendations are visual guidance only — every installed model can be tried.")
    print("How to set it: UI -> Agents, or POST /settings/agent per role.")
    print("Measured tok/s come from token_stats.json (your own runs), not estimates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

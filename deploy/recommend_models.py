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
import ctypes
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
    """RAM ohne Zusatzpaket - Windows via ctypes, Linux via /proc."""
    try:
        if sys.platform == "win32":
            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            stat = _MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return round(stat.ullTotalPhys / (1024 ** 3), 1)
            return 0.0
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / (1024 ** 2), 1)
    except (OSError, ValueError, AttributeError):
        return 0.0
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
            verdict = "experts offloaded to RAM (fits VRAM + RAM)"
            score = 2
        elif fits_ram:
            verdict = "CPU/RAM offload (slower, fits RAM)"
            score = 2
        else:
            verdict = "too large for this machine"
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="propose a start configuration for this machine")
    ap.add_argument("--json", action="store_true", help="JSON instead of a report")
    args = ap.parse_args(argv)

    vram = _vram_gb()
    ram = _total_ram_gb()
    rows = collect(vram, ram)
    if not rows:
        print(f"no models found ({MODELS_JSON}) - run setup_models.bat first")
        return 1

    usable = [r for r in rows if r["score"] >= 2] or rows
    pick = lambda pred: next((r["tag"] for r in usable if pred(r)), "")  # noqa: E731
    max_direct_gb = max(6.0, vram) if vram > 0 else max(4.0, ram * 0.4)
    max_judge_gb = max(3.0, vram * 0.5) if vram > 0 else max(2.5, ram * 0.25)
    max_coder_gb = max(8.0, vram) if vram > 0 else max(6.0, ram * 0.5)
    rec = {
        "detected": {"vram_gb": vram, "ram_gb": ram},
        "direct": pick(lambda r: r["measured_tps"] >= 20) or pick(lambda r: r["gb"] <= max_direct_gb) or usable[0]["tag"],
        "judge": pick(lambda r: r["gb"] <= max_judge_gb) or usable[0]["tag"],
        "duo_coder": (pick(lambda r: r["measured_tps"] >= 15 and r["gb"] >= 8)
                      or pick(lambda r: r["measured_tps"] >= 15)
                      or pick(lambda r: r["gb"] <= max_coder_gb)
                      or usable[-1]["tag"]),
        "planner": (pick(lambda r: r["measured_tps"] >= 15 and r["gb"] <= max_direct_gb)
                    or pick(lambda r: r["gb"] <= max_direct_gb)
                    or usable[0]["tag"]),
        "models": rows,
    }
    if args.json:
        print(json.dumps(rec, indent=1, ensure_ascii=False))
        return 0

    print(f"detected: VRAM {vram:.1f} GB (free) · RAM {ram:.1f} GB · {len(rows)} models\n")
    print(f"{'model':<46}{'GB':>6}{'MoE':>5}{'tok/s':>7}  assessment")
    for r in rows[:14]:
        print(f"{r['tag'][:45]:<46}{r['gb']:>6.1f}{r['moe_cpu_experts']:>5}"
              f"{(('%.1f' % r['measured_tps']) if r['measured_tps'] else '-'):>7}  {r['verdict']}")
    print("\nProposal for this machine:")
    for role in ("direct", "judge", "duo_coder", "planner"):
        print(f"  {role:<10} {rec[role] or '(no matching model found)'}")
    _roles = [rec[r] for r in ("direct", "judge", "duo_coder", "planner")]
    if len(set(_roles)) == 1:
        print("\nNote: only one model really fits - the coder usually benefits from a\n"
              "groesseren MoE mit CPU-Experten (RAM-Offload), auch wenn es langsamer laedt.")
    print("\nHow to set it: UI -> Agents, or POST /settings/agent per role.")
    print("Measured tok/s come from token_stats.json (your own runs), not estimates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

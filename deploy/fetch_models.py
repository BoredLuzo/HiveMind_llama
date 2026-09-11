"""
fetch_models.py
===============
Downloads the recommended HiveMind models from Hugging Face and
populates models.json with all GGUFs found in the models folder.

Recommended set (as of release, = default-settings alignment 2026-08-26):
  gemma-4:e4b-it          Q4_K_M        — Direct/Vision/Allrounder
  qwen3.6:35b-a3b-ud      UD-Q4_K_XL    — Coder/Planner (MoE, needs ~16GB RAM)
  qwen3.6:…-genesis-final-apex-compact     APEX-Compact  — Coder/Hermes agent (MoE+MTP)
  ling-3.0-tiny           Q4_K_L        — Low-resource Coder (hybrid MoE 7.9B/1.3B)
  lfm2.5:2.6b             Q4_K_M        — Subagent/Worker + Judge
  qwen3.5:0.8b-ud         UD-Q4_K_XL    — Subagent ladder (smallest tier)
  qwen3.5:2b              Q4_K_M        — Refiner
  qwen3.5:4b-ud           UD-Q4_K_XL    — Analyst/Critic/Synthesizer/Speed/Fallback
  qwen3.5:9b-ud           UD-Q4_K_XL    — Direct/Duo-Coder/Quality

Usage:
  python deploy\\fetch_models.py --models-dir <path> [options]

  --models-dir     Target folder (default: env HIVEMIND_MODELS_DIR or <repo>/models)
  --only-missing   Do not re-download existing GGUFs
  --list-only      Only show what would be downloaded, download nothing
  --scan-only      Only scan local GGUFs + write models.json, NO download, NO network
  --yes            Skip confirmation for large-file warnings

Stdlib only — no huggingface_hub dependency.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HF_API = "https://huggingface.co/api/models"
HF_RESOLVE = "https://huggingface.co/{repo}/resolve/main/{path}"


def _persist_models_dir(mdir: Path) -> None:
    """Write the custom folder to settings.json "models_dir" so the server
    finds it even WITHOUT the HIVEMIND_MODELS_DIR env var (2026-09-01)."""
    try:
        from settings import load_settings, save_settings
        s = load_settings()
        s["models_dir"] = str(mdir)
        save_settings(s)
        print(f"    models_dir persisted -> {mdir}")
    except Exception as _e:
        print(f"    [WARNING] could not persist models_dir: {_e}")

# ── Download catalog (2026-09-11, user-curated) ──────────────────────────────
# Exactly the models the installer offers. file_regex: list of patterns, first
# priority first. "repo": PINNED repo (no HF fuzzy search — a search once
# pulled Jackrong/DeepSeek-V4-Pro... for the qwen3.5:4b-mtp spec because
# unsloth names its MTP files WITHOUT "mtp": unsloth/Qwen3.5-4B-MTP-GGUF →
# "Qwen3.5-4B-Q4_K_M.gguf". Pinned specs never fall back to search.)
SPECS: list[dict] = [
    {
        "key": "gemma-4:e4b-it-qat",
        "desc": "Gemma-4 E4B-IT QAT (All-rounder/Vision, ~4.2GB + mmproj, +MTP drafter)",
        "repo": "unsloth/gemma-4-E4B-it-qat-GGUF",
        "file_regex": [
            r"(?i)^gemma-4-e4b-it-qat-ud-q4_k_xl\.gguf$",
        ],
        "mmproj_regex": [
            r"(?i)^mmproj[-_.]bf16\.gguf$",
            r"(?i)^mmproj[-_.]f16\.gguf$",
        ],
        # MTP speculative-decoding drafter (~60MB, root of the repo)
        "sidecar": [
            {
                "repo": "unsloth/gemma-4-E4B-it-qat-GGUF",
                "path": "mtp-gemma-4-E4B-it.gguf",
                "note": "MTP spec-dec drafter (~60MB)",
            },
        ],
    },
    {
        "key": "gemma-4:e2b-it-qat",
        "desc": "Gemma-4 E2B-IT QAT (Small all-rounder/Vision, ~2.6GB + mmproj, +MTP drafter)",
        "repo": "unsloth/gemma-4-E2B-it-qat-GGUF",
        "file_regex": [
            r"(?i)^gemma-4-e2b-it-qat-ud-q4_k_xl\.gguf$",
        ],
        "mmproj_regex": [
            r"(?i)^mmproj[-_.]bf16\.gguf$",
            r"(?i)^mmproj[-_.]f16\.gguf$",
        ],
        "sidecar": [
            {
                "repo": "unsloth/gemma-4-E2B-it-qat-GGUF",
                "path": "mtp-gemma-4-E2B-it.gguf",
                "note": "MTP spec-dec drafter (~60MB)",
            },
        ],
    },
    {
        "key": "qwen3.6:35b-a3b-ud",
        "desc": "Qwen3.6 35B A3B unsloth UD (Coder/Planner MoE, ~20GB download)",
        "repo": "unsloth/Qwen3.6-35B-A3B-GGUF",
        "file_regex": [
            r"(?i)^qwen3\.6-35b-a3b-ud-q4_k_xl\.gguf$",
        ],
        "mmproj_regex": [r"(?i)^mmproj[-._]bf16\.gguf$", r"(?i)mmproj.*(f16|bf16)\.gguf$"],
    },
    {
        "key": "lfm2.5:2.6b",
        "desc": "LFM2.5 2.6B (Subagent/Worker, ~2GB VRAM)",
        "repo": "LiquidAI/LFM2.5-2.6B-GGUF",
        "file_regex": [
            r"(?i)^lfm2\.5-2\.6b-q4_k_m\.gguf$",
        ],
        "mmproj_regex": [],
        # DSpark speculative-decoding drafter (sidecar GGUF, paired with the
        # target model). It is NOT registered as a standalone model (see
        # write_models_json / llama_models dspark exclusion).
        "sidecar": [
            {
                "repo": "LiquidAI/LFM2.5-2.6B-DSpark-GGUF",
                "path": "LFM2.5-2.6B-DSpark-Q4_K_M.gguf",
                "note": "DSpark spec-dec drafter (Q4_K_M, ~190MB)",
            },
        ],
    },
    # ── Hermes3.6 Genesis V13 "MTP-APEX-Compact" (2026-09-11) ─────────────
    # Coder/Hermes agent: MoE 35B-A3B with MTP head, quantized as
    # "MTP-APEX-Compact" (~18 GB, experts offloaded to CPU). The per-model
    # config (model_configs/models/hermes3.6_..._v13-mtp-apex-compact.json)
    # enables the MTP head + CPU experts, so the DOWNLOAD must be the MTP
    # variant (registers as hermes3.6:35b-a3b-uncensored-genesis-v13-mtp-
    # apex-compact). Files verified 2026-09-11.
    {
        "key": "hermes3.6:35b-a3b-uncensored-genesis-v13-mtp-apex-compact",
        "desc": "Hermes3.6 Genesis V13 MTP-APEX-Compact (Coder/Hermes agent, MoE+MTP, ~18GB download)",
        "repo": "LuffyTheFox/Qwen3.6-35B-A3B-Uncensored-Genesis-Hermes-V13-GGUF",
        "file_regex": [
            r"(?i)^hermes3\.6-35b-a3b-uncensored-genesis-v13-mtp-apex-compact\.gguf$",
        ],
        "mmproj_regex": [
            r"(?i)^mmproj-hermes3\.6-35b-a3b-uncensored-genesis-f16\.gguf$",
        ],
    },
    # ── Qwen3.5 MTP pair (duo coder/planner + refiner) ────────────────────
    # unsloth MTP repos name files WITHOUT "mtp" (verified 2026-09-11) — the
    # regexes are anchored to the exact filenames and the repos are pinned,
    # so no third-party repo (Jackrong etc.) can ever match.
    {
        "key": "qwen3.5:4b-mtp",
        "desc": "Qwen3.5 4B MTP (Duo Coder/Planner default, MTP spec-decode, ~3.3GB VRAM)",
        "repo": "unsloth/Qwen3.5-4B-MTP-GGUF",
        "file_regex": [
            r"(?i)^qwen3\.5-4b-q4_k_m\.gguf$",
        ],
        "mmproj_regex": [],
    },
    {
        "key": "qwen3.5:2b-mtp",
        "desc": "Qwen3.5 2B MTP (Refiner, MTP spec-decode, ~1.3GB VRAM)",
        "repo": "unsloth/Qwen3.5-2B-MTP-GGUF",
        "file_regex": [
            r"(?i)^qwen3\.5-2b-q4_k_m\.gguf$",
        ],
        "mmproj_regex": [],
    },
]


def http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "HiveMind-Installer"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def pick_repos(spec: dict, max_candidates: int = 8) -> list[str]:
    """Candidate repos for a spec, sorted by author preference + downloads.
    Multiple candidates, so a top repo without a matching GGUF (e.g. google/QAT)
    can be skipped."""
    from urllib.parse import quote
    url = f"{HF_API}?search={quote(spec['search'])}&limit=50"
    try:
        results = http_json(url)
    except Exception as e:
        print(f"    [WARNING] HF search failed ({e})")
        return []

    gguf_repos = [r for r in results if "gguf" in r.get("id", "").lower()]
    if not gguf_repos:
        gguf_repos = results

    def rank(r: dict) -> tuple:
        author = r.get("id", "").split("/")[0].lower()
        pref = spec["author_pref"].index(author) if author in spec["author_pref"] else len(spec["author_pref"])
        return (pref, -int(r.get("downloads") or 0))

    gguf_repos.sort(key=rank)
    return [r["id"] for r in gguf_repos[:max_candidates]]


def list_repo_files(repo: str) -> list[dict]:
    return http_json(f"{HF_API}/{repo}/tree/main?recursive=true")


def pick_file(files: list[dict], regexes: list[str], exclude_mmproj: bool = True) -> dict | None:
    for rx in regexes:
        pat = re.compile(rx)
        for f in files:
            if f.get("type") != "file":
                continue
            name = Path(f["path"]).name
            if exclude_mmproj and "mmproj" in name.lower():
                continue
            if pat.search(name):
                return f
    return None


def disk_space_free_gb(path: Path) -> float:
    """Free space on the drive of `path` (in GB)."""
    try:
        return shutil.disk_usage(str(path)).free / (1024 ** 3)
    except OSError:
        return -1.0


def download_file(repo: str, f: dict, dest_dir: Path, auto_yes: bool) -> bool:
    url = HF_RESOLVE.format(repo=repo, path=f["path"])
    size_gb = int(f.get("size") or 0) / (1024 ** 3)
    dest = dest_dir / Path(f["path"]).name
    if size_gb > 8.0 and not auto_yes:
        ans = input(f"    {Path(f['path']).name} is {size_gb:.1f} GB large. Download? [Y/n] ").strip().lower()
        if ans in ("n", "no"):
            return False
    # DISK-SPACE-CHECK (2026-08-26): check free space before every download
    # (download + 1GB buffer). Live finding: a 25GB set on a nearly full disk
    # only failed mid-write and left partial files behind.
    free_gb = disk_space_free_gb(dest_dir)
    if 0 <= free_gb < size_gb + 1.0:
        print(f"    [ERROR] Not enough disk space: {size_gb:.1f} GB needed, only {free_gb:.1f} GB free on {dest_dir}.")
        return False
    print(f"    Downloading -> {dest} ({size_gb:.1f} GB)...")
    req = urllib.request.Request(url, headers={"User-Agent": "HiveMind-Installer"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as out:
        total = int(r.headers.get("Content-Length") or f.get("size") or 0)
        done = 0
        while True:
            chunk = r.read(1024 * 512)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if total:
                sys.stdout.write(f"\r    {done / (1024**3):.2f} / {total / (1024**3):.2f} GB ({done * 100 // max(total, 1)}%)")
                sys.stdout.flush()
    sys.stdout.write("\n")
    return True


def write_models_json(models_dir: Path) -> int:
    """Populate models.json from all GGUFs in the folder."""
    from backend.llama_models import _parse_gguf_filename  # noqa: E402

    ggufs = sorted(models_dir.rglob("*.gguf"))
    mapping: dict[str, str] = {}
    families: set[str] = set()

    # Normal models first
    for g in ggufs:
        if "mmproj" in g.name.lower():
            continue
        if "dspark" in g.name.lower():
            continue
        names = _parse_gguf_filename(g.name)
        if names:
            mapping[names[0]] = str(g)
            families.add(names[0].split(":")[0])

    # Assign mmproj entries to the matching family
    for g in ggufs:
        if "mmproj" not in g.name.lower():
            continue
        low = g.name.lower().replace(".", "")
        family = next((fam for fam in families if fam.replace(".", "") in low), None)
        if family is None and families:
            family = sorted(families)[0]
        if family:
            mapping[f"{family}_mmproj"] = str(g)

    out = ROOT / "models.json"
    out.write_text(json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(mapping)


def _resolve_only(args_only: str) -> list[dict] | None:
    """--only: comma-separated selection of spec keys OR 1-based numbers
    (as shown by setup_models.bat). Returns the filtered specs."""
    sel = [s.strip() for s in args_only.split(",") if s.strip()]
    if not sel:
        return None
    by_idx = {str(i + 1): s["key"] for i, s in enumerate(SPECS)}
    keys = {s["key"] for s in SPECS}
    chosen = set()
    for item in sel:
        if item in keys:
            chosen.add(item)
        elif item in by_idx:
            chosen.add(by_idx[item])
        else:
            print(f"    [WARNING] Unknown selection skipped: {item}")
    if not chosen:
        print("[ERROR] --only: no valid selection (keys or numbers 1-"
              + str(len(SPECS)) + ").")
        return None
    return [s for s in SPECS if s["key"] in chosen]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-dir", default="")
    ap.add_argument("--only-missing", action="store_true")
    ap.add_argument("--list-only", action="store_true")
    ap.add_argument("--scan-only", action="store_true",
                    help="Only scan local GGUFs + write models.json, NO download, NO network")
    ap.add_argument("--print-catalog", action="store_true",
                    help="Print the numbered download catalog and exit (no network, "
                         "no folder touch) — setup_models.bat renders its menu from this")
    ap.add_argument("--only", default="",
                    help="Only these models: keys or numbers, comma-separated (e.g. 1,4)")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    if args.print_catalog:
        for _i, _s in enumerate(SPECS):
            print(f"  {_i + 1}. {_s['key']:<45s} {_s['desc']}")
        return 0

    mdir = Path(args.models_dir).expanduser() if args.models_dir else \
        Path(os.environ.get("HIVEMIND_MODELS_DIR", "") or (ROOT / "models"))
    mdir.mkdir(parents=True, exist_ok=True)
    _persist_models_dir(mdir)
    local_files = {g.name.lower(): g for g in mdir.rglob("*.gguf")}
    print(f"Models folder: {mdir}")
    if local_files:
        print(f"Existing GGUFs: {len(local_files)}")

    # SCAN-ONLY (2026-08-27, user request): auto-detect own models and register
    # them in models.json - no download, no HF API. Previously --list-only was
    # abused for this, which does NOT register (only "would load").
    if args.scan_only:
        ggufs = sorted(mdir.rglob("*.gguf"))
        if not ggufs:
            print("[WARNING] No .gguf files found in the folder.")
        else:
            for g in ggufs:
                print(f"    found: {g.name}")
        n = write_models_json(mdir)
        print(f"[OK] models.json written: {n} entries -> {ROOT / 'models.json'}")
        return 0

    specs = SPECS
    if args.only:
        specs = _resolve_only(args.only)
        if specs is None:
            return 1
        print(f"Only {len(specs)} model(s): {', '.join(s['key'] for s in specs)}")

    # PREFLIGHT (2026-08-26): check disk space before starting — the complete
    # set is ~30 GB. Below 3 GB free the run makes no sense.
    if not args.list_only:
        free_gb = disk_space_free_gb(mdir)
        print(f"Free disk space: {free_gb:.1f} GB")
        if 0 <= free_gb < 3.0:
            print("[ERROR] Too little free disk space for the model download.")
            print("         Free up space or pick a different models folder.")
            return 1

    for spec in specs:
        print()
        print(f"== {spec['key']} — {spec['desc']}")

        # Already present?
        have = [n for n in local_files if re.search(spec["file_regex"][0], n)]
        if args.only_missing and have:
            print(f"    Already present: {have[0]} — skipping (--only-missing)")
            continue

        if spec.get("repo"):
            # PINNED REPO (2026-09-11): no HF search at all — the catalog pins
            # the exact repo, so third-party lookalikes can never be picked.
            repos = [spec["repo"]]
            print(f"    Repo (pinned): {spec['repo']}")
        else:
            print(f"    Searching Hugging Face: '{spec['search']}'...")
            repos = pick_repos(spec)
            if not repos:
                print("    [ERROR] No matching repo found — load manually from huggingface.co.")
                continue

        # The top repo can contain a GGUF that does not match file_regex
        # (e.g. google/gemma-4-E4B-it-qat-q4_0-gguf) — then try the next candidates.
        repo = None
        main_file = None
        files = []
        first_files = []
        for cand in repos:
            if not spec.get("repo"):
                print(f"    Repo candidate: {cand}")
            try:
                cand_files = list_repo_files(cand)
            except Exception as e:
                print(f"      [WARNING] Repo not fetchable ({e})")
                continue
            if not first_files:
                first_files = cand_files
            cand_file = pick_file(cand_files, spec["file_regex"], exclude_mmproj=True)
            if cand_file:
                repo, files, main_file = cand, cand_files, cand_file
                break
        if not main_file:
            print("    [ERROR] No matching GGUF in the candidate repos:")
            for f in first_files[:15]:
                if f.get("type") == "file":
                    print(f"      - {f['path']}")
            continue
        print(f"    Repo: {repo}")

        if args.list_only:
            print(f"    Would download: {main_file['path']}")
        else:
            download_file(repo, main_file, mdir, args.yes)
            local_files[main_file["path"].lower()] = mdir / Path(main_file["path"]).name

        if spec["mmproj_regex"]:
            mm = pick_file(files, spec["mmproj_regex"], exclude_mmproj=False)
            if mm and ("mmproj" not in " ".join(local_files) or not args.list_only):
                if args.list_only:
                    print(f"    Would download (Vision): {mm['path']}")
                else:
                    download_file(repo, mm, mdir, args.yes)

        for _sc in (spec.get("sidecar") or []):
            _sc_name = Path(_sc["path"]).name.lower()
            if args.only_missing and _sc_name in local_files:
                print(f"    Sidecar present: {Path(_sc['path']).name} — skipping (--only-missing)")
                continue
            if args.list_only:
                print(f"    Would download (sidecar): {_sc['path']} [{_sc.get('note', '')}]")
                continue
            try:
                if download_file(_sc["repo"], {"path": _sc["path"], "size": 0}, mdir, args.yes):
                    local_files[_sc_name] = mdir / Path(_sc["path"]).name
            except Exception as e:
                print(f"    [WARNING] Sidecar download failed ({e})")

    if args.list_only:
        return 0

    n = write_models_json(mdir)
    print()
    print(f"[OK] models.json written: {n} entries -> {ROOT / 'models.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

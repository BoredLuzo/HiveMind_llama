"""
fetch_llamacpp.py
=================
Downloads the latest llama.cpp release (Windows) from GitHub and
extracts it to <HiveMind>/llama/.

Usage:
  python deploy\\fetch_llamacpp.py --backend vulkan          # AMD/Intel
  python deploy\\fetch_llamacpp.py --backend cuda            # NVIDIA
  python deploy\\fetch_llamacpp.py --backend vulkan --force  # re-download even if a build exists

Stdlib only (urllib/zipfile) — no extra dependencies needed.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LLAMA_DIR = ROOT / "llama"

API_LATEST = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"

ASSET_REGEX = {
    "vulkan": re.compile(r"^llama-b(\d+)-bin-win-vulkan-x64\.zip$", re.IGNORECASE),
    # CUDA assets are named e.g. llama-b1234-bin-win-cuda-12.4-x64.zip
    # or llama-b1234-bin-win-cuda-13.3-x64.zip — the version is captured,
    # so that with equal build numbers the NEWER CUDA runtime wins
    # (CUDA-VERSION-FIX 2026-08-27: previously cuda-12.4 was always pulled,
    # even when the driver supports 13.x — live finding on RTX,
    # "--list-devices empty").
    "cuda": re.compile(r"^llama-b(\d+)-bin-win-cuda-([\d.]+)-x64\.zip$", re.IGNORECASE),
}


def _cuda_version_of(name: str, rx) -> tuple[int, ...]:
    """CUDA version from the asset name (e.g. 13.3 -> (13, 3)); (0,) otherwise."""
    m = rx.match(name or "")
    if not m or len(m.groups()) < 2:
        return (0,)
    try:
        return tuple(int(x) for x in m.group(2).split("."))
    except Exception:
        return (0,)


def _pick_asset(releases: list[dict], rx, cuda_version: str = "") -> tuple[dict | None, str]:
    """Highest build wins; with equal build the higher CUDA version.

    cuda_version: optional "major.minor" — filters assets exactly to this
    version (for old drivers without 13.x support).
    """
    want = None
    if cuda_version:
        try:
            want = tuple(int(x) for x in cuda_version.split(".")[:2])
        except Exception:
            want = None
    best = None
    best_key = (0, (0,))
    best_tag = ""
    for rel in releases:
        for a in rel.get("assets", []):
            m = rx.match(a.get("name", ""))
            if not m:
                continue
            build = int(m.group(1))
            cv = _cuda_version_of(a.get("name", ""), rx)
            if want is not None and cv[:2] != want:
                continue
            key = (build, cv)
            if key > best_key:
                best, best_key, best_tag = a, key, rel.get("tag_name", "?")
    return best, best_tag


def _detect_cuda_version_from_driver() -> str:
    """Read 'CUDA Version: 13.1' from nvidia-smi ('' if not detectable)."""
    try:
        out = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=30, encoding="utf-8", errors="replace")
        m = re.search(r"CUDA Version:\s*(\d+\.\d+)", out.stdout)
        return m.group(1) if m else ""
    except Exception:
        return ""


def http_json(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": "HiveMind-Installer",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def http_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "HiveMind-Installer"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "HiveMind-Installer"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                pct = done * 100 // total
                sys.stdout.write(f"\r    {done / (1024*1024):.0f} / {total / (1024*1024):.0f} MB ({pct}%)")
                sys.stdout.flush()
    sys.stdout.write("\n")


def existing_build() -> tuple[int, Path] | None:
    """Find the highest already installed build."""
    best: tuple[int, Path] | None = None
    if LLAMA_DIR.is_dir():
        for exe in LLAMA_DIR.glob("*/llama-server.exe"):
            m = re.search(r"b(\d{4,})", exe.parent.name)
            b = int(m.group(1)) if m else 0
            if best is None or b > best[0]:
                best = (b, exe.parent)
    return best


def _verify_backend_dlls(exe: Path, backend: str) -> list[str]:
    """Check that the backend runtime DLLs sit next to llama-server.exe.

    CUDA builds need ggml-cuda.dll + the bundled CUDA runtime DLLs
    (cudart64_*, cublas64_*, cublasLt64_*); Vulkan builds need ggml-vulkan.dll.
    The official llama.cpp Windows ZIPs bundle these — if they are missing,
    llama-server finds no devices ("--device CUDA0" needs them). Returns the
    list of missing DLL names (empty = complete).
    """
    dll_dir = Path(exe).parent
    missing: list[str] = []
    if backend == "cuda":
        for name in ("ggml-cuda.dll",):
            if not (dll_dir / name).exists():
                missing.append(name)
        for base in ("cudart64", "cublas64", "cublasLt64"):
            if not list(dll_dir.glob(f"{base}*.dll")):
                missing.append(f"{base}*.dll")
    else:
        if not (dll_dir / "ggml-vulkan.dll").exists():
            missing.append("ggml-vulkan.dll")
    return missing


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["vulkan", "cuda"], default="vulkan")
    ap.add_argument("--cuda-version", default="",
                    help="Pick the CUDA runtime exactly (e.g. 12.4 or 13.3). Default: "
                         "driver version via nvidia-smi, otherwise the newest available.")
    ap.add_argument("--force", action="store_true", help="Re-download even if a build already exists")
    args = ap.parse_args()

    have = existing_build()
    if have and not args.force:
        print(f"[OK] llama.cpp b{have[0]} already installed: {have[1]}")
        print("     (use --force to re-download)")
        return 0

    print(f"[1/3] Fetching latest llama.cpp release info ({args.backend})...")
    # NIGHTLY-FIRST (2026-08-31): the installer should install the nightly
    # bXXXX build, not the stable semver release. The stable release v0.x only
    # contains 'nightly-tag.txt' (pointer to the bXXXX nightly); the real
    # binaries live in the nightly release. Nightly is the primary source,
    # stable only serves as fallback if the nightly tag cannot be resolved.
    main_rel = http_json(API_LATEST)
    nightly_release = None
    nt = next((a for a in main_rel.get("assets", []) if a.get("name") == "nightly-tag.txt"), None)
    if nt:
        try:
            _tag = http_text(nt["browser_download_url"]).strip()
            if _tag:
                nightly_release = http_json(f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{_tag}")
                print(f"       Nightly release resolved: {_tag}")
        except Exception as e:
            print(f"       [WARNING] Nightly tag could not be resolved: {e}")
    releases = [nightly_release] if nightly_release else [main_rel]

    rx = ASSET_REGEX[args.backend]

    # CUDA-VERSION-SELECTION (2026-08-27, FIX): prefer exactly the driver
    # version (nvidia-smi), otherwise the newest available CUDA runtime.
    # Previously cuda-12.4 was always chosen even though cuda-13.3 exists.
    if args.backend == "cuda" and not args.cuda_version:
        _det = _detect_cuda_version_from_driver()
        if _det:
            print(f"       Driver CUDA version: {_det}")
            asset, asset_tag = _pick_asset(releases, rx, _det)
            if asset is None:
                print(f"       No asset exactly for CUDA {_det} — taking the newest version.")
                asset, asset_tag = _pick_asset(releases, rx)
        else:
            print("       nvidia-smi not available — taking the newest CUDA version.")
            asset, asset_tag = _pick_asset(releases, rx)
    else:
        asset, asset_tag = _pick_asset(releases, rx, args.cuda_version or "")

    if asset is None:
        print(f"[ERROR] No asset for '{args.backend}' found in release {main_rel.get('tag_name', '?')}.")
        print(f"         Manual: {main_rel.get('html_url', 'https://github.com/ggml-org/llama.cpp/releases')}")
        return 1

    tag = asset_tag
    build_num = int(rx.match(asset["name"]).group(1))
    _cv = _cuda_version_of(asset["name"], rx)
    _cv_txt = f" (CUDA {'.'.join(str(x) for x in _cv)})" if _cv != (0,) else ""
    print(f"[2/3] Downloading {asset['name']}{_cv_txt} (release {tag}, build b{build_num})...")
    print(f"       Install target: {LLAMA_DIR}")
    LLAMA_DIR.mkdir(parents=True, exist_ok=True)

    tmp_zip = Path(tempfile.gettempdir()) / asset["name"]
    url = asset.get("browser_download_url")
    if not url:
        print("[ERROR] Asset without download URL.")
        return 1
    download(url, tmp_zip)

    target = LLAMA_DIR / tmp_zip.stem
    if target.exists():
        print(f"    Removing old version: {target.name}")
        shutil.rmtree(target, ignore_errors=True)
    print(f"[3/3] Extracting to {target}...")
    with zipfile.ZipFile(tmp_zip) as zf:
        zf.extractall(target)
    tmp_zip.unlink(missing_ok=True)

    exe = target / "llama-server.exe"
    if not exe.exists():
        # Some archives extract a subfolder
        nested = list(target.glob("*/llama-server.exe"))
        exe = nested[0] if nested else exe

    # DLL-VERIFY (2026-08-27): `--device CUDA0` needs the bundled CUDA runtime
    # DLLs next to the exe. A ZIP without them would install a build that
    # "finds no devices" — fail here with a clear message instead.
    _missing = _verify_backend_dlls(exe, args.backend)
    if _missing:
        print()
        print(f"[ERROR] {args.backend} runtime DLLs missing in the downloaded build:")
        for _m in _missing:
            print(f"    - {_m}")
        print(f"  Expected next to: {exe}")
        print(f"  The ZIP seems incomplete. Try again (--force) or download")
        print(f"  the {args.backend} asset manually from:")
        print(f"  https://github.com/ggml-org/llama.cpp/releases")
        shutil.rmtree(target, ignore_errors=True)
        return 1

    print()
    print(f"[OK] llama.cpp b{build_num} installed.")
    print(f"     llama-server: {exe}")
    if have and build_num < have[0]:
        print(f"     NOTE: b{have[0]} is newer and stays in place (auto-discovery picks the highest build).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

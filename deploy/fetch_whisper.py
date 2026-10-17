"""
fetch_whisper.py
================
Downloads the whisper.cpp release binaries and the speech-to-text models into
``<HiveMind>/stt/`` (binaries in ``stt/bin/``, models in ``stt/models/``).

The same release carries every CLI the voice feature uses:

* ``whisper-cli`` / ``whisper-server`` - Whisper models (ggml-base/tiny/small)
* ``parakeet-cli`` - Parakeet TDT 0.6B v3 (default, best English accuracy)

No Python dependencies: stdlib urllib/zipfile/tarfile, exactly like
``fetch_llamacpp.py``. CPU/BLAS builds only - there is no Vulkan or CUDA asset
that matches an AMD card today (upstream issue #3673), and the measured cost is
1.4-1.9 s per voice note anyway.

Usage:
  python deploy/fetch_whisper.py --list             # show the models
  python deploy/fetch_whisper.py                    # binaries + default model
  python deploy/fetch_whisper.py --model base,tiny  # extra tiers
  python deploy/fetch_whisper.py --backend blas     # ~15 % faster, +12 MB (win)
  python deploy/fetch_whisper.py --check            # what is installed?
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.stt_engine import (  # noqa: E402
    MODELS, binary_path, model_path, resolve_model, stt_root,
)

RELEASE_API = "https://api.github.com/repos/ggml-org/whisper.cpp/releases?per_page=20"
HF_BASE = "https://huggingface.co/{repo}/resolve/main/{name}"
UA = {"User-Agent": "HiveMind-FetchWhisper"}

_IS_WIN = platform.system() == "Windows"
os_tag = "win" if _IS_WIN else "ubuntu"
ASSETS = {
    # (platform, backend) -> regex over the release asset names
    ("win", "cpu"): re.compile(r"^whisper-bin-x64\.zip$", re.IGNORECASE),
    ("win", "blas"): re.compile(r"^whisper-blas-bin-x64\.zip$", re.IGNORECASE),
    ("linux", "cpu"): re.compile(r"^whisper-bin-ubuntu-x64\.tar\.gz$", re.IGNORECASE),
    ("linux", "blas"): re.compile(r"^whisper-blas-bin-x64\.tar\.gz$", re.IGNORECASE),
}
MIN_BINARY_BYTES = 1 * 1024 * 1024      # a whisper.cpp zip is 8-21 MB
MIN_MODEL_BYTES = 50 * 1024 * 1024      # the smallest model is 74 MB


def _build_number(tag: str) -> int:
    """Build number from a release tag like ``b5454`` (0 when it has none)."""
    m = re.match(r"^b(\d+)$", (tag or "").strip(), re.IGNORECASE)
    return int(m.group(1)) if m else 0


def pick_release(releases: list[dict], rx) -> tuple[dict, dict] | None:
    """Release + asset for this platform, newest BUILD first.

    ``/releases/latest`` is not enough: the newest tag (v1.9.x) carries no
    binaries - the Windows/Linux assets hang off the ``b####`` build tags. So
    scan the release list and take the highest build number that actually has a
    matching asset.
    """
    best: tuple[int, dict, dict] | None = None
    for rel in releases or []:
        asset = next((a for a in (rel.get("assets") or [])
                      if rx.match(a.get("name") or "")), None)
        if not asset:
            continue
        build = _build_number(rel.get("tag_name") or "")
        if best is None or build > best[0]:
            best = (build, rel, asset)
    if best is None:
        return None
    return best[1], best[2]


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download(url: str, dest: Path, min_bytes: int = 0) -> bool:
    """Stream a URL to ``dest``. Returns True when the file arrived complete."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    got = 0
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                if total:
                    pct = 100.0 * got / total
                    print(f"\r  {dest.name}: {got / 1048576:6.1f} / "
                          f"{total / 1048576:6.1f} MB ({pct:5.1f} %)", end="")
                elif got % (10 * 1048576) < 262144:
                    print(f"\r  {dest.name}: {got / 1048576:6.1f} MB", end="")
        print()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print(f"\n  [ERROR] download failed: {exc}")
        tmp.unlink(missing_ok=True)
        return False
    if got < min_bytes:
        print(f"  [ERROR] {dest.name} is only {got} bytes - wrong asset?")
        tmp.unlink(missing_ok=True)
        return False
    tmp.replace(dest)
    return True


def install_binaries(backend: str, force: bool = False) -> bool:
    """Fetch the release asset for this platform and unpack it into stt/bin/."""
    dest = stt_root() / "bin"
    if binary_path("whisper-cli") and not force:
        print(f"[OK] binaries already present: {dest}")
        return True
    rx = ASSETS.get((os_tag, backend)) or ASSETS.get((os_tag, "cpu"))
    try:
        releases = _http_json(RELEASE_API)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[ERROR] could not read the whisper.cpp releases: {exc}")
        return False
    if isinstance(releases, dict):
        releases = [releases]
    picked = pick_release(releases, rx)
    if not picked:
        have = ", ".join(a.get("name", "?") for r in releases
                         for a in (r.get("assets") or [])[:4])[:400]
        print(f"[ERROR] no asset for {os_tag}/{backend}. Seen assets: {have}")
        return False
    rel, asset = picked
    print(f"[..] {rel.get('tag_name')} -> {asset['name']}")
    with tempfile.TemporaryDirectory() as tmp:
        arc = Path(tmp) / asset["name"]
        if not _download(asset["browser_download_url"], arc, MIN_BINARY_BYTES):
            return False
        dest.mkdir(parents=True, exist_ok=True)
        if arc.suffix == ".zip":
            with zipfile.ZipFile(arc) as zf:
                zf.extractall(tmp)
        else:
            with tarfile.open(arc) as tf:
                tf.extractall(tmp)  # noqa: S202 - archive from the official release
        # The zip nests everything under Release/ (win) or build/bin (linux).
        src = None
        for cand in Path(tmp).rglob("whisper-cli*"):
            if cand.parent.name in ("Release", "bin"):
                src = cand.parent
                break
        if src is None:
            print("[ERROR] unpacked archive has no whisper-cli")
            return False
        for item in src.iterdir():
            if item.is_file():
                shutil.copy2(item, dest / item.name)
    ok = bool(binary_path("whisper-cli"))
    print(f"[{'OK' if ok else 'ERROR'}] binaries in {dest}")
    return ok


def install_models(keys: list[str], force: bool = False) -> bool:
    ok = True
    for key in keys:
        m = MODELS.get(key)
        if not m:
            print(f"[ERROR] unknown model {key!r} (see --list)")
            ok = False
            continue
        dest = stt_root() / "models" / m.filename
        if dest.is_file() and not force:
            print(f"[OK] {key}: {m.filename} already present")
            continue
        url = HF_BASE.format(repo=m.repo, name=m.filename)
        print(f"[..] {key}: {m.filename} ({m.size_mb} MB)")
        if not _download(url, dest, MIN_MODEL_BYTES):
            ok = False
    return ok


def check() -> int:
    root = stt_root()
    print(f"STT root: {root}")
    print(f"config default model: {resolve_model({})}")
    print()
    print(f"{'engine':<14} {'installed':<10} path")
    for engine in ("whisper-cli", "parakeet-cli", "whisper-server"):
        p = binary_path(engine)
        print(f"{engine:<14} {'yes' if p else 'NO':<10} {p or '-'}")
    print()
    print(f"{'model':<14} {'disk':<8} {'installed':<10} {'WER':<7} file")
    for key, m in MODELS.items():
        p = model_path(key)
        print(f"{key:<14} {m.size_mb:>5} MB {'yes' if p else 'NO':<10} "
              f"{m.wer * 100:4.1f} %  {m.filename}")
    missing = [k for k in MODELS if not model_path(k)]
    print()
    if binary_path("whisper-cli") and model_path(resolve_model({})):
        print("[OK] speech-to-text can run")
        return 0
    print(f"[INFO] run: python deploy/fetch_whisper.py"
          f"{' --model ' + missing[0] if missing else ''}")
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch whisper.cpp binaries + STT models")
    ap.add_argument("--backend", choices=("cpu", "blas"), default="blas",
                    help="CPU build (default blas: ~15 %% faster on Windows)")
    ap.add_argument("--model", default="", help="comma list, or 'all' (default: the RAM default)")
    ap.add_argument("--list", action="store_true", help="show the model tiers")
    ap.add_argument("--check", action="store_true", help="report what is installed")
    ap.add_argument("--force", action="store_true", help="re-download")
    args = ap.parse_args(argv)

    if args.list:
        print(f"{'key':<14} {'disk':>6} {'RAM':>6} {'WER':>6} {'RTFx':>6}  label")
        for key, m in MODELS.items():
            print(f"{key:<14} {m.size_mb:>4} MB {m.ram_mb:>4} MB "
                  f"{m.wer * 100:5.1f} % {m.rtfx:6.1f}  {m.label} - {m.notes}")
        return 0
    if args.check:
        return check()

    keys = [k.strip() for k in args.model.split(",") if k.strip()]
    if not keys or keys == ["all"]:
        keys = list(MODELS) if args.model == "all" else [resolve_model({})]
    good = install_binaries(args.backend, force=args.force)
    good = install_models(keys, force=args.force) and good
    print()
    check()
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())

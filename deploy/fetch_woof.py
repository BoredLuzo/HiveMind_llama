"""Download ConwayResearch/Underdog-Woof into <HiveMind>/voice_models/underdog_woof/.

Underdog Woof is an English CPU text-to-speech model (~40.6 MB int8 weights +
pure-NumPy runtime) with two voices: Juno and Mira. It is ungated on Hugging
Face (no token required) and licensed under the Underdog Non-Commercial License
1.0 (free for non-commercial use with attribution; commercial use requires a
license from Underdog). LICENSE and NOTICE are always kept verbatim alongside
the downloaded weights.

Usage:
  python deploy/fetch_woof.py            # download weights + runtime files
  python deploy/fetch_woof.py --check    # print install status (JSON)
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

for _s in (sys.stdout, sys.stderr):
    _reconf = getattr(_s, "reconfigure", None)
    if callable(_reconf):
        try:
            _reconf(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO = "ConwayResearch/Underdog-Woof"
HF_RAW = f"https://huggingface.co/{REPO}/resolve/main/{{path}}"
UA = {"User-Agent": "HiveMind-FetchWoof/1.0"}

# Exact runtime + legal files required for standalone non-commercial execution.
REQUIRED_FILES: tuple[str, ...] = (
    "LICENSE",
    "NOTICE",
    "underdog_woof/__init__.py",
    "underdog_woof/normalize.py",
    "underdog_woof/respell.py",
    "underdog_woof/respell.tsv",
    "underdog_woof/runtime.py",
    "underdog_woof/textsplit.py",
    "woof_int8.npz",
)
MIN_WEIGHTS_BYTES = 30 * 1024 * 1024  # woof_int8.npz is ~40.6 MB


def woof_root() -> Path:
    """Return the local directory holding the downloaded Underdog Woof files."""
    env = (os.environ.get("HIVEMIND_WOOF_DIR") or "").strip()
    if env:
        return Path(env).resolve()
    return ROOT / "voice_models" / "underdog_woof"


def is_installed(base: Path | None = None) -> bool:
    """True when all required runtime, license, and weight files are present."""
    target = base or woof_root()
    for rel in REQUIRED_FILES:
        p = target / rel
        if not p.is_file():
            return False
        if rel == "woof_int8.npz":
            try:
                if p.stat().st_size < MIN_WEIGHTS_BYTES:
                    return False
            except OSError:
                return False
        elif p.stat().st_size <= 0:
            return False
    return True


def fetch_state(base: Path | None = None) -> dict:
    """Read the progress state file written during UI/background downloads."""
    target = base or woof_root()
    sf = target / ".fetch_state.json"
    try:
        if sf.is_file():
            data = json.loads(sf.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {"state": "ready" if is_installed(target) else "idle", "percent": 100.0 if is_installed(target) else 0.0}


def _write_state(target: Path, state: str, percent: float, message: str = "") -> None:
    try:
        target.mkdir(parents=True, exist_ok=True)
        tmp = target / ".fetch_state.part"
        tmp.write_text(
            json.dumps({"state": state, "percent": round(percent, 1), "message": message}),
            encoding="utf-8",
        )
        tmp.replace(target / ".fetch_state.json")
    except OSError:
        pass


def _download_file(url: str, dst: Path, *, progress: Callable[[int, int], None] | None = None,
                   max_attempts: int = 3) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".part")
    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, headers=UA)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                with open(part, "wb") as out:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        out.write(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress(done, total)
            if total and done != total:
                raise IOError(f"truncated download ({done}/{total} bytes) for {dst.name}")
            part.replace(dst)
            return
        except (OSError, urllib.error.URLError, http.client.HTTPException):
            part.unlink(missing_ok=True)
            if attempt >= max_attempts:
                raise
            time.sleep(min(8, 2 ** attempt))
        finally:
            if part.exists():
                part.unlink(missing_ok=True)


def download(base: Path | None = None, *, force: bool = False) -> Path:
    """Fetch ConwayResearch/Underdog-Woof files atomically into ``base``."""
    target = base or woof_root()
    if not force and is_installed(target):
        _write_state(target, "done", 100.0, "Already installed")
        return target
    target.mkdir(parents=True, exist_ok=True)
    _write_state(target, "running", 0.0, f"Downloading {REPO}...")
    n = len(REQUIRED_FILES)
    try:
        for idx, rel in enumerate(REQUIRED_FILES):
            dst = target / rel
            if not force and dst.is_file():
                ok = dst.stat().st_size >= (MIN_WEIGHTS_BYTES if rel == "woof_int8.npz" else 1)
                if ok:
                    continue
            url = HF_RAW.format(path=rel)
            base_pct = (idx / n) * 100.0
            span_pct = (1.0 / n) * 100.0

            def _cb(done: int, total: int, _bp: float = base_pct, _sp: float = span_pct, _rel: str = rel) -> None:
                frac = min(1.0, done / total) if total > 0 else 0.5
                _write_state(target, "running", _bp + frac * _sp, f"Downloading {_rel}...")

            _download_file(url, dst, progress=_cb)
        if not is_installed(target):
            raise RuntimeError("Underdog Woof download finished but woof_int8.npz failed size verification")
        _write_state(target, "done", 100.0, "Underdog Woof ready")
        return target
    except (OSError, urllib.error.URLError, http.client.HTTPException, RuntimeError) as exc:
        _write_state(target, "error", 0.0, str(exc)[:200])
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Download ConwayResearch/Underdog-Woof TTS model")
    ap.add_argument("--check", action="store_true", help="Print JSON status and exit")
    ap.add_argument("--force", action="store_true", help="Re-download existing files")
    args = ap.parse_args(argv)
    target = woof_root()
    if args.check:
        print(json.dumps({
            "installed": is_installed(target),
            "path": str(target),
            "fetch": fetch_state(target),
        }, indent=2))
        return 0
    try:
        out = download(target, force=args.force)
        print(f"  [OK] Underdog Woof installed in {out}")
        return 0
    except (OSError, urllib.error.URLError, http.client.HTTPException, RuntimeError) as exc:
        print(f"  [ERROR] Underdog Woof download failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

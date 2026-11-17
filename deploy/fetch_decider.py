r"""Holt das optionale Decider-Modell (GLiNER2.5-Decide) in den models-Ordner.

Der Encoder ist **optional**: ohne ihn entscheidet der LLM-Judge (wie bisher).
Der Release-ZIP enthaelt keine Gewichte (er bleibt ~1,4 MB) - dieses Skript holt
den Checkpoint einmalig, danach laeuft der Decider offline.

Aufruf:
    python deploy/fetch_decider.py                # holt fastino/GLiNER2.5-Decide
    python deploy/fetch_decider.py --dry-run      # zeigt nur, was passieren wuerde
    python deploy/fetch_decider.py --model fastino/GLiNER2.5-multi-Decide

Danach in den Settings:
    routing_decider_model = "models/decider/gliner25-decide"     (relativ zum Install)
    routing_decider       = "shadow"                             (erst vergleichen)

Die Python-Abhaengigkeit (gliner2 + torch, ~2,5 GB) ist NICHT Teil der normalen
Installation und wird ueber uv installiert:
  uv pip install --python .venv\Scripts\python.exe -r requirements-decider.txt
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
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
MODELS_DIR = ROOT / "models" / "decider"
DEFAULT_MODEL = "fastino/GLiNER2.5-Decide"


def _hf_cli() -> list[str] | None:
    """Bevorzugt die hf-CLI, sonst uvx / uv tool run, sonst huggingface-cli."""
    for candidate in (["hf"], ["uvx", "--from", "huggingface_hub", "hf"],
                      ["huggingface-cli"]):
        exe = shutil.which(candidate[0])
        if exe:
            return [exe] + candidate[1:]
    # Probe well-known user uv/uvx install locations when PATH was not reloaded.
    user_dirs = [
        Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links",
    ]
    for d in user_dirs:
        if not d.is_dir():
            continue
        uvx = d / ("uvx.exe" if os.name == "nt" else "uvx")
        if uvx.is_file():
            return [str(uvx), "--from", "huggingface_hub", "hf"]
        uv = d / ("uv.exe" if os.name == "nt" else "uv")
        if uv.is_file():
            return [str(uv), "tool", "run", "--from", "huggingface_hub", "hf"]
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Download the decider checkpoint (optional)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"HF-Repo (Default: {DEFAULT_MODEL})")
    ap.add_argument("--dir", default="", help="Zielordner (Default: models/decider/gliner25-decide)")
    ap.add_argument("--dry-run", action="store_true", help="show only, download nothing")
    args = ap.parse_args(argv)

    target = Path(args.dir) if args.dir else (MODELS_DIR / "gliner25-decide")
    print(f"Modell : {args.model}")
    print(f"Ziel   : {target}")

    try:
        import torch  # noqa: F401
        from gliner2 import AutoExtractor  # noqa: F401
        print("gliner2: installed")
    except (ImportError, OSError):
        print("gliner2: MISSING - the model download alone is not enough.")
        print("         Install gliner2[local] + CPU PyTorch with uv:")
        print("           uv pip install --python .venv\\Scripts\\python.exe --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple --index-strategy unsafe-best-match -r requirements-decider.txt")
        print("         If uv is missing, run install.bat once to provision it.")

    cli = _hf_cli()
    if cli is None:
        print("No 'hf' / 'huggingface-cli' found on PATH. Alternatives:")
        print(f"  - uvx --from huggingface_hub hf download {args.model} --local-dir \"{target}\"")
        print(f"  - or keep the HF id as routing_decider_model in the UI: {args.model}")
        return 1

    cmd = cli + ["download", args.model, "--local-dir", str(target)]
    print("Command:", " ".join(cmd))
    if args.dry_run:
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    rc = subprocess.call(cmd)
    if rc != 0:
        print(f"[ERROR] Download failed (exit {rc})")
        return rc

    print()
    print("Done. Set these in the settings:")
    # CRASH-FIX (audit r4): target.relative_to(ROOT) raised ValueError for a
    # --dir outside the install root - AFTER the download had succeeded.
    # Fall back to the absolute path instead of crashing on the success path.
    try:
        _rel = target.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        _rel = str(target)
    print(f"  routing_decider_model = \"{_rel}\"")
    print("  routing_decider       = \"shadow\"    # erst vergleichen, dann \"on\"")
    print("Note: the encoder runs on the CPU (~0.5-1.2 s per decision) and uses")
    print("no VRAM. Without it the LLM judge decides as before.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Holt das optionale Decider-Modell (GLiNER2.5-Decide) in den models-Ordner.

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
Installation:  pip install -r requirements-decider.txt
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models" / "decider"
DEFAULT_MODEL = "fastino/GLiNER2.5-Decide"


def _hf_cli() -> list[str] | None:
    """Bevorzugt die hf-CLI, sonst uvx, sonst huggingface-cli."""
    for candidate in (["hf"], ["uvx", "--from", "huggingface_hub", "hf"],
                      ["huggingface-cli"]):
        exe = shutil.which(candidate[0])
        if exe:
            return [exe] + candidate[1:]
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Decider-Checkpoint herunterladen (optional)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"HF-Repo (Default: {DEFAULT_MODEL})")
    ap.add_argument("--dir", default="", help="Zielordner (Default: models/decider/gliner25-decide)")
    ap.add_argument("--dry-run", action="store_true", help="show only, download nothing")
    args = ap.parse_args(argv)

    target = Path(args.dir) if args.dir else (MODELS_DIR / "gliner25-decide")
    print(f"Modell : {args.model}")
    print(f"Ziel   : {target}")

    try:
        import gliner2  # noqa: F401
        print("gliner2: installiert")
    except ImportError:
        print("gliner2: MISSING - run this after the download:")
        print("         pip install -r requirements-decider.txt")

    cli = _hf_cli()
    if cli is None:
        print("Kein 'hf'/'huggingface-cli' gefunden. Alternativen:")
        print(f"  - uvx --from huggingface_hub hf download {args.model} --local-dir \"{target}\"")
        print(f"  - oder in der UI als routing_decider_model direkt die HF-ID lassen: {args.model}")
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
    print(f"  routing_decider_model = \"{target.relative_to(ROOT).as_posix()}\"")
    print("  routing_decider       = \"shadow\"    # erst vergleichen, dann \"on\"")
    print("Note: the encoder runs on the CPU (~0.5-1.2 s per decision) and uses")
    print("no VRAM. Without it the LLM judge decides as before.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

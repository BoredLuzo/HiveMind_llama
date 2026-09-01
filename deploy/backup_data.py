# -*- coding: utf-8 -*-
"""backup_data.py - copy HiveMind user data into backups/<timestamp>/.

Backs up the most important persistent data (sessions, memory, soul, presets,
settings, learned model-configs, learning_logs) into a timestamped backup
directory under backups/. Missing sources are skipped (soul.json/memory.json/
learning_logs/ are only created on demand).

Run:
  python deploy/backup_data.py
  python deploy/backup_data.py --dry-run     # show only, do not copy
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Sources relative to the repo root. Files via copy2, directories via copytree.
SOURCES: list[str] = [
    "sessions",
    "memory.json",
    "soul.json",
    "presets.json",
    "settings.json",
    "model_configs/learned",
    "learning_logs",
]


def _timestamp() -> str:
    return time.strftime("%Y-%m-%d_%H-%M-%S")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Backup HiveMind user data.")
    ap.add_argument("--dry-run", action="store_true", help="show only, do not copy")
    ap.add_argument("--out", default=str(ROOT / "backups"), help="target directory (default: backups/)")
    args = ap.parse_args(argv)

    dest_root = Path(args.out).resolve()
    dest = dest_root / _timestamp()
    if not args.dry_run:
        dest.mkdir(parents=True, exist_ok=False)

    copied: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    for rel in SOURCES:
        src = ROOT / rel
        if not src.exists():
            skipped.append(f"{rel} (missing)")
            continue
        target = dest / rel
        try:
            if args.dry_run:
                copied.append(rel)
            elif src.is_dir():
                shutil.copytree(src, target, dirs_exist_ok=True)
                copied.append(rel)
            else:
                shutil.copy2(src, target)
                copied.append(rel)
        except Exception as _e:
            errors.append(f"{rel}: {_e}")

    if args.dry_run:
        print("DRY-RUN — target: %s" % dest)
    else:
        print("Backup to: %s" % dest)
    for rel in copied:
        print(f"  [OK]     {rel}")
    for rel in skipped:
        print(f"  [SKIP]   {rel}")
    for rel in errors:
        print(f"  [ERROR]  {rel}")
    print(f"{len(copied)} copied, {len(skipped)} skipped, {len(errors)} errors.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

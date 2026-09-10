"""Sync dev repo -> Desktop install folders (code only, instance data stays).

Usage:  python tools/sync_to_installs.py

Copies every tracked-code file that differs (CR-insensitive) into
HiveMind_install and HiveMind_install_test. settings.json, models.json,
searxng secrets, logs, sessions etc. are deliberately NOT touched.
"""
from __future__ import annotations

import os
import shutil

DEV = r"C:\Users\NtheP\Desktop\HiveMind_dev"
TARGETS = [
    r"C:\Users\NtheP\Desktop\HiveMind_install",
    r"C:\Users\NtheP\Desktop\HiveMind_install_test",
]
SKIP_TOP = ("logs", "sessions", "models", "llama", "chats", ".git", "__pycache__",
            ".venv", "build", ".ruff_cache", ".pytest_cache", ".zcode", ".opencode")
SKIP_FILES = {"settings.json", "memory.json", "runtime_models.json", "token_stats.json",
              "run_counter.json", "models.json", "soul.json", "presets.json", "uv.lock",
              ".python-version"}


def norm(b: bytes) -> bytes:
    return b.replace(b"\r\n", b"\n")


def main() -> int:
    copied = 0
    for inst in TARGETS:
        n = 0
        for root, dirs, files in os.walk(DEV):
            dirs[:] = [d for d in dirs if d not in SKIP_TOP]
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), DEV)
                if rel.split(os.sep)[0] in SKIP_TOP or rel in SKIP_FILES \
                        or rel.startswith("searxng-config"):
                    continue
                dst = os.path.join(inst, rel)
                src = os.path.join(root, f)
                if not os.path.exists(dst) or norm(open(src, "rb").read()) != norm(open(dst, "rb").read()):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    n += 1
        print(f"{inst}: {n} file(s) updated")
        copied += n
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

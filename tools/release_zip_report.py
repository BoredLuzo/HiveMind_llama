#!/usr/bin/env python
"""Release-zip inventory: is every shipped entry needed?

Run from the repo root: python tools/release_zip_report.py [path-to-zip]

Prints the file count and size per top-level directory, the biggest files, and
the entries that look like development material (they should be export-ignore'd
in .gitattributes rather than shipped to users).
"""
from __future__ import annotations

import collections
import sys
import zipfile

DEV_HINTS = ("audit_", "RELEASE_NOTES_", "scripts/hooks", "ruff.toml",
             ".gitignore", ".gitattributes", "plan_", "roadmap_", "backlog")


def main(path: str) -> int:
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
    root = infos[0].filename.split("/")[0]
    per_dir: collections.Counter = collections.Counter()
    size: collections.Counter = collections.Counter()
    for i in infos:
        parts = i.filename.split("/")
        key = parts[1] if len(parts) > 2 else "(root)"
        per_dir[key] += 1
        size[key] += i.file_size
    print(f"{len(infos)} entries, "
          f"{sum(i.file_size for i in infos) / 1e6:.2f} MB uncompressed")
    print(f"{'dir':18} {'files':>5} {'MB':>8}")
    for k, n in per_dir.most_common():
        print(f"{k:18} {n:5} {size[k] / 1e6:8.2f}")
    print("\nlargest files:")
    for i in sorted(infos, key=lambda x: -x.file_size)[:10]:
        print(f"  {i.file_size / 1024:8.1f} KB  {i.filename[len(root) + 1:]}")
    dev = [i.filename[len(root) + 1:] for i in infos
           if any(h in i.filename for h in DEV_HINTS)]
    print(f"\ndevelopment material still shipped ({len(dev)}):")
    for n in dev:
        print("  ", n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1
                  else "../HiveMind_v1.4.0_release.zip"))

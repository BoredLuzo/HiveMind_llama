# -*- coding: utf-8 -*-
"""Bump the HiveMind version in every machine-read place at once (C4).

core/version.py is the executable source of truth. server.py carries a
TEXT-ONLY compatibility anchor (`# HIVEMIND_VERSION = "x.y.z"`) because the
update.bat shipped with <=1.5.5 reads the version from server.py (first
Select-String match). pyproject.toml carries the packaging version. The three
drifting apart is pinned by tests/test_split_audit_fixes.py (h2/h3); this
script keeps them in sync so the pin never has to fire.

    python deploy/bump_version.py 1.5.6             # write all files
    python deploy/bump_version.py 1.5.6 --dry-run   # show, write nothing
    python deploy/bump_version.py --check           # drift check only

The CHANGELOG heading stays manual (it needs prose); the script reminds you.
Exit codes: 0 ok, 1 drift (--check), 2 invalid input / missing anchor.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Strict semver core plus an optional LOWERCASE pre-release tag. Lowercase
# only: the .bat/.ps1 probes and the h2 pin use `[0-9][0-9.a-z-]*`.
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9a-z][0-9a-z.-]*)?$")

# (label, relative path, anchored regex with groups prefix/version/suffix)
TARGETS = (
    ("version", "core/version.py",
     re.compile(r'^(HIVEMIND_VERSION\s*=\s*")([^"]+)(")', re.M)),
    ("anchor", "server.py",
     re.compile(r'^(#\s*HIVEMIND_VERSION\s*=\s*")([^"]+)(")', re.M)),
    ("pyproject", "pyproject.toml",
     re.compile(r'^(version\s*=\s*")([^"]+)(")', re.M)),
)

# What update.bat <=1.5.5 / start_hivemind.bat match in server.py (first hit).
_LEGACY_PROBE_RE = re.compile(r"HIVEMIND_VERSION\s*=\s*.([0-9][0-9.a-z-]*)", re.I)


class BumpError(Exception):
    """Refusal: invalid version or a target file without its anchor."""


def _read(path: Path) -> str:
    # newline="" keeps CRLF/LF exactly as on disk.
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def validate(version: str) -> str:
    v = version.strip()
    if v.startswith("v"):
        v = v[1:]
    if not SEMVER_RE.match(v):
        raise BumpError(f"not a semver version (x.y.z[-pre], lowercase): {version!r}")
    return v


def read_versions(root: Path) -> dict[str, tuple[Path, str]]:
    """label -> (path, current version). Raises BumpError on a missing anchor."""
    out: dict[str, tuple[Path, str]] = {}
    for label, rel, rx in TARGETS:
        path = root / rel
        if not path.is_file():
            raise BumpError(f"{label}: file missing: {path}")
        text = _read(path)
        m = rx.search(text)
        if m is None:
            raise BumpError(f"{label}: no version anchor in {path} "
                            f"(expected a line matching {rx.pattern!r})")
        if label == "anchor":
            probe = _LEGACY_PROBE_RE.search(text)
            if probe is None or probe.start() < m.start():
                raise BumpError(f"anchor: another HIVEMIND_VERSION assignment precedes "
                                f"the anchor in {path}; the legacy updater would read it")
        out[label] = (path, m.group(2))
    return out


_UV_LOCK_PKG_RE = re.compile(
    r'(name\s*=\s*"hivemind-llama"\r?\nversion\s*=\s*")([^"]+)(")'
)


def _pep440_version(ver: str) -> str:
    """Translate semver pre-releases (-alpha/.N, -beta/.N, -rc/.N) to PEP 440."""
    return re.sub(
        r"-(alpha|beta|rc)(?:\.?(\d+))?$",
        lambda m: {"alpha": "a", "beta": "b", "rc": "rc"}[m.group(1)] + (m.group(2) or "0"),
        ver,
    )


def plan(root: Path, new: str) -> list[tuple[str, Path, str, str, str]]:
    """[(label, path, old, new, new_text)] - computed before anything is written."""
    current = read_versions(root)
    changes = []
    for label, rel, rx in TARGETS:
        path, old = current[label]
        text = _read(path)
        new_text = rx.sub(lambda m: m.group(1) + new + m.group(3), text, count=1)
        changes.append((label, path, old, new, new_text))
    lock_path = root / "uv.lock"
    if lock_path.is_file():
        lock_text = _read(lock_path)
        m_lock = _UV_LOCK_PKG_RE.search(lock_text)
        if m_lock is not None:
            new_pep = _pep440_version(new)
            new_lock = _UV_LOCK_PKG_RE.sub(
                lambda m: m.group(1) + new_pep + m.group(3), lock_text, count=1
            )
            changes.append(("uv.lock", lock_path, m_lock.group(2), new_pep, new_lock))
    return changes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("version", nargs="?", help="new version, e.g. 1.5.6")
    ap.add_argument("--dry-run", action="store_true", help="show the change, write nothing")
    ap.add_argument("--check", action="store_true", help="only report drift (exit 1 on drift)")
    ap.add_argument("--root", type=Path, default=ROOT, help="repo root (tests use a temp copy)")
    args = ap.parse_args(argv)
    root = args.root.resolve()

    try:
        if args.check:
            current = read_versions(root)
            for label, (path, ver) in current.items():
                print(f"  {label:9s} {ver:12s} {path}")
            if len({ver for _p, ver in current.values()}) != 1:
                print("[DRIFT] versions differ - run deploy/bump_version.py <x.y.z>")
                return 1
            print("[OK] all version anchors agree")
            return 0
        if not args.version:
            ap.error("version required (or --check)")
        new = validate(args.version)
        changes = plan(root, new)
    except BumpError as exc:
        print(f"[REFUSED] {exc}")
        return 2

    pending = [c for c in changes if c[2] != c[3]]
    for label, path, old, nv, _t in changes:
        mark = "=" if old == nv else "->"
        print(f"  {label:9s} {old} {mark} {nv}  {path}")
    if not pending:
        print(f"[OK] already at {new} - nothing to write")
        return 0
    if args.dry_run:
        print(f"[DRY-RUN] {len(pending)} file(s) would change; nothing written")
        return 0
    for _label, path, _old, _nv, new_text in pending:
        _write(path, new_text)
    print(f"[OK] bumped to {new} ({len(pending)} file(s)). "
          f"Remember: CHANGELOG.md heading '## [{new}]' and RELEASE_NOTES_{new}.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

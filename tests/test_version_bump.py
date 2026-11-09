# -*- coding: utf-8 -*-
"""deploy/bump_version.py - version anchors stay in sync (C4).

Every check runs against a TEMP COPY of core/version.py, server.py and
pyproject.toml (`--root <tmp>`); the real files are hashed before and after
and must be untouched.

Run: python tests/test_version_bump.py
"""
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

_h = Harness()
check = _h.check

SCRIPT = ROOT / "deploy" / "bump_version.py"
FILES = ("core/version.py", "server.py", "pyproject.toml")


def _digest():
    return {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in FILES}


def _copy_tree(dst: Path) -> Path:
    for rel in FILES:
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, dst / rel)
    return dst


def _run(root: Path, *args):
    p = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    return p.returncode, p.stdout + p.stderr


def _versions(root: Path):
    sys.path.insert(0, str(SCRIPT.parent))
    import bump_version  # noqa: E402
    return {k: v for k, (_p, v) in bump_version.read_versions(root).items()}


before = _digest()

with tempfile.TemporaryDirectory() as td:
    tmp = _copy_tree(Path(td))
    start = _versions(tmp)

    # V1: the real tree is in sync right now (same guard as h2/h3).
    rc, out = _run(tmp, "--check")
    check("V1 --check passes on a copy of the current tree", rc == 0 and len(set(start.values())) == 1,
          f" rc={rc} {start}")

    # V2: dry-run prints every path and writes nothing.
    snap = {f: (tmp / f).read_bytes() for f in FILES}
    rc, out = _run(tmp, "9.8.7", "--dry-run")
    check("V2 dry-run exits 0 and writes nothing",
          rc == 0 and all((tmp / f).read_bytes() == snap[f] for f in FILES), f" rc={rc}")
    check("V3 dry-run names version.py and the server.py anchor",
          "version.py" in out and "server.py" in out and "DRY-RUN" in out,
          f" rc={rc} lines={len(out.splitlines())}")

    # V4: a real bump moves all anchors together and keeps line endings.
    rc, out = _run(tmp, "9.8.7")
    after = _versions(tmp)
    crlf_kept = all(((b"\r\n" in snap[f]) == (b"\r\n" in (tmp / f).read_bytes())) for f in FILES)
    check("V4 bump updates version.py + server.py anchor + pyproject in sync",
          rc == 0 and set(after.values()) == {"9.8.7"} and crlf_kept, f" rc={rc} {after}")
    # The legacy updater probe (first match in server.py) now reads the new value.
    probe = re.search(r"HIVEMIND_VERSION\s*=\s*.([0-9][0-9.a-z-]*)",
                      (tmp / "server.py").read_text(encoding="utf-8"))
    check("V5 legacy server.py probe reads the bumped anchor",
          probe is not None and probe.group(1) == "9.8.7", f" hit={probe and probe.group(1)}")

    # V6: idempotent - bumping to the same version writes nothing.
    snap2 = {f: (tmp / f).read_bytes() for f in FILES}
    rc, out = _run(tmp, "9.8.7")
    check("V6 second identical bump is a no-op",
          rc == 0 and "nothing to write" in out
          and all((tmp / f).read_bytes() == snap2[f] for f in FILES), f" rc={rc}")

    # V7: drift detection - hand-edit only core/version.py.
    vp = tmp / "core/version.py"
    vp.write_bytes(vp.read_bytes().replace(b'"9.8.7"', b'"9.8.8"'))
    rc, out = _run(tmp, "--check")
    check("V7 --check flags drift between version.py and the anchor",
          rc == 1 and "DRIFT" in out, f" rc={rc}")

    # V8: invalid semver is refused without touching files.
    snap3 = {f: (tmp / f).read_bytes() for f in FILES}
    bad = [_run(tmp, v)[0] for v in ("1.5", "1.05.0", "1.5.0-RC1", "dev")]
    check("V8 non-semver versions refused (exit 2, no write)",
          bad == [2, 2, 2, 2] and all((tmp / f).read_bytes() == snap3[f] for f in FILES),
          f" codes={bad}")

    # V9: a server.py without the anchor is refused - and NOTHING is written
    # (version.py must not be bumped half-way).
    sp = tmp / "server.py"
    sp.write_text("\n".join(ln for ln in sp.read_text(encoding="utf-8").splitlines()
                            if not ln.lstrip().startswith("# HIVEMIND_VERSION")),
                  encoding="utf-8")
    snap4 = vp.read_bytes()
    rc, out = _run(tmp, "9.9.9")
    check("V9 missing server.py anchor -> refused, version.py untouched",
          rc == 2 and "REFUSED" in out and vp.read_bytes() == snap4, f" rc={rc}")

check("V10 real repo version files untouched", _digest() == before)

sys.exit(_h.finish("version_bump"))

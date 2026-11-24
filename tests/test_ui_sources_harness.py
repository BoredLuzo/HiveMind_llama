"""Regression checks for the modular UI test harness (`tests/ui_sources.py` & `.cjs`).

Verifies:
1. `ui_js_files()` returns `static/app.js` and any `static/js/*.js` in exact `index.html` order,
   with zero orphan `.js` files under `static/js/`.
2. Every `<script src="/static/js/...">` and `/static/app.js` tag in `index.html` carries a `?v=` cache-buster.
3. `tests/ui_sources.py` and `tests/ui_sources.cjs` produce identical file lists and SHA-256/byte length.
4. `node --check` passes on every file returned by `all_static_js_files()`.
5. No duplicate top-level `function <name>(` declarations exist across `read_all_ui_js()`.
6. `deploy/settings_ui_scan.py` `ui_files()` covers `index.html`, `static/app.js`, and every `static/js/*.js`.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from deploy.settings_ui_scan import ui_files as scan_ui_files  # noqa: E402
from tests._harness import Harness  # noqa: E402
from tests.ui_sources import (  # noqa: E402
    all_static_js_files,
    index_script_paths,
    orphan_static_js_files,
    read_all_ui_js,
    ui_js_files,
)

h = Harness(style="summary", pad=True)
check = h.check


def main() -> int:
    html = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")
    html_clean = re.sub(r"<!--[\s\S]*?-->", "", html)
    idx_scripts = index_script_paths()
    missing_disk = [p.relative_to(ROOT).as_posix() for p in idx_scripts if not p.is_file()]
    check("every /static script tag in index.html exists on disk", bool(idx_scripts) and not missing_disk, repr(missing_disk))

    ui_files = ui_js_files()
    ui_rels = [p.relative_to(ROOT).as_posix() for p in ui_files]

    check(
        "ui_js_files() includes static/app.js last and matches static/js/*.js",
        bool(ui_rels) and ui_rels[-1] == "static/app.js" and all(p.is_file() for p in ui_files),
        repr(ui_rels),
    )

    orphans = [p.relative_to(ROOT).as_posix() for p in orphan_static_js_files()]
    check("no orphan static/js/*.js files outside index.html", not orphans, repr(orphans))

    script_tags = re.findall(r'<script\b[^>]*\bsrc=["\']([^"\']+)["\']', html_clean, re.IGNORECASE)
    ui_tags = [
        s for s in script_tags
        if s.startswith("/static/js/") or s.startswith("/static/app.js")
    ]
    bad_v = [s for s in ui_tags if not re.search(r"\?v=\d{8}-\d+$", s)]
    check(
        "every static/js/*.js and static/app.js script tag has ?v=YYYYMMDD-N cache-buster",
        bool(ui_tags) and not bad_v,
        repr(bad_v),
    )

    py_text = read_all_ui_js()
    py_bytes = len(py_text.encode("utf-8"))
    py_sha = hashlib.sha256(py_text.encode("utf-8")).hexdigest()
    node = shutil.which("node")
    if node:
        js_probe = (
            "const crypto = require('crypto');"
            "const u = require('./tests/ui_sources.cjs');"
            "const txt = u.readAllUiJs();"
            "const sha = crypto.createHash('sha256').update(txt, 'utf8').digest('hex');"
            "console.log(JSON.stringify({"
            "files: u.uiJsFiles().map(p => p.replace(/\\\\/g, '/')),"
            "bytes: Buffer.byteLength(txt, 'utf8'),"
            "sha: sha"
            "}));"
        )
        res = subprocess.run(
            [node, "-e", js_probe],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        cjs_info = json.loads(res.stdout.strip())
        py_abs = [p.resolve().as_posix() for p in ui_files]
        check(
            "ui_sources.py and ui_sources.cjs produce identical file lists, byte lengths, and SHA-256",
            cjs_info["files"] == py_abs and cjs_info["bytes"] == py_bytes and cjs_info["sha"] == py_sha,
            repr((len(py_abs), len(cjs_info["files"]), py_bytes, cjs_info["bytes"], py_sha == cjs_info["sha"])),
        )

        syntax_errs = []
        for jf in all_static_js_files():
            sr = subprocess.run(
                [node, "--check", str(jf)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if sr.returncode != 0:
                syntax_errs.append(f"{jf.relative_to(ROOT).as_posix()}: {sr.stderr.strip()[:200]}")
        check("node --check passes on all local static JS files", not syntax_errs, "; ".join(syntax_errs))
    else:
        h.skip("ui_sources.cjs parity and node --check", "node binary unavailable")

    decl_names = re.findall(
        r"^(?:async\s+)?(?:function|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)",
        py_text,
        re.MULTILINE,
    )
    dups = sorted((n, c) for n, c in Counter(decl_names).items() if c > 1)
    check("no duplicate top-level declarations across UI JS sources", not dups, repr(dups))

    scanned = {p.resolve() for p in scan_ui_files()}
    expected_scan = {(ROOT / "index.html").resolve(), *(p.resolve() for p in ui_files)}
    check(
        "deploy/settings_ui_scan.ui_files() covers index.html and all UI JS files",
        expected_scan.issubset(scanned),
        repr(sorted(str(x.relative_to(ROOT)) for x in (expected_scan - scanned))),
    )

    return h.finish("ui_sources_harness")


if __name__ == "__main__":
    sys.exit(main())

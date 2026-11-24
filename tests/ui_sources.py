"""Shared UI script inventory for test suites.

Parses `<script src="/static/...">` declarations from `index.html` in exact
document order so standalone source checks and Node VM tests automatically work
across `static/app.js` and extracted classic scripts under `static/js/*.js`.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_SCRIPT_SRC_RE = re.compile(
    r'<script\b[^>]*\bsrc=["\'](/static/[^"\'?#]+\.js)(?:[?#][^"\']*)?["\']',
    re.IGNORECASE,
)


def index_script_paths() -> list[Path]:
    """Return local `/static/*.js` script paths referenced by `index.html` in load order."""
    idx_path = ROOT / "index.html"
    try:
        html = idx_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    html = _HTML_COMMENT_RE.sub("", html)
    out: list[Path] = []
    seen: set[Path] = set()
    for m in _SCRIPT_SRC_RE.finditer(html):
        rel = m.group(1).lstrip("/")
        p = ROOT / rel
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def ui_js_files() -> list[Path]:
    """Return modular UI scripts (`static/js/*.js` + `static/app.js`) in exact load order."""
    matched: list[Path] = []
    for p in index_script_paths():
        rel_posix = p.relative_to(ROOT).as_posix()
        if rel_posix == "static/app.js" or rel_posix.startswith("static/js/"):
            matched.append(p)
    if matched:
        return matched
    fallback = ROOT / "static" / "app.js"
    return [fallback] if fallback.is_file() else []


def all_static_js_files() -> list[Path]:
    """Return all local `.js` files referenced in `index.html` in load order."""
    files = [p for p in index_script_paths() if p.is_file()]
    app = ROOT / "static" / "app.js"
    if app.is_file() and app not in files:
        files.append(app)
    return files


def orphan_static_js_files() -> list[Path]:
    """Return any `.js` files under `static/js/` not included in `index.html`."""
    js_dir = ROOT / "static" / "js"
    if not js_dir.is_dir():
        return []
    loaded = {p.resolve() for p in ui_js_files()}
    return sorted(
        p for p in js_dir.glob("*.js")
        if p.resolve() not in loaded
    )


def read_all_ui_js(*, normalize_newlines: bool = True) -> str:
    """Read and concatenate all `ui_js_files()` in exact HTML load order."""
    files = ui_js_files()
    read_kw: dict = {"encoding": "utf-8", "errors": "replace"}
    if not normalize_newlines:
        read_kw["newline"] = ""
    chunks = []
    for p in files:
        with p.open("r", **read_kw) as f:
            chunks.append(f.read().lstrip("\ufeff"))
    text = "\n\n".join(chunks)
    if normalize_newlines:
        text = text.replace("\r\n", "\n")
    return text


def read_ui_module(rel_path: str, *, normalize_newlines: bool = False) -> str:
    """Read one specific UI module relative to repo root (e.g. `static/js/voice.js`)."""
    read_kw: dict = {"encoding": "utf-8", "errors": "replace"}
    if not normalize_newlines:
        read_kw["newline"] = ""
    with (ROOT / rel_path).open("r", **read_kw) as f:
        text = f.read().lstrip("\ufeff")
    if normalize_newlines:
        text = text.replace("\r\n", "\n")
    return text

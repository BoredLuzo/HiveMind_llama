# -*- coding: utf-8 -*-
"""gen_settings_docs.py - auto-generate docs/settings.md from settings.DEFAULT_SETTINGS.

Collects per key: type, default value and the inline comment from settings.py.
Run:
  python deploy/gen_settings_docs.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "settings.py"
OUT = ROOT / "docs" / "settings.md"

# Keys explicitly marked "hidden" in the audit (now in DEFAULT_SETTINGS).
HIDDEN_KEYS = {
    "duo_coder_model", "duo_critic_model", "duo_caps",
    "duo_pyright_path", "duo_autolint_python_engine",
    "read_guard_enabled", "plan_tracker_classifier",
}


def _ast_value(node):
    """Converts an AST value node into a Python value (best effort)."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return f"<ref {node.id}>"
    if isinstance(node, (ast.List, ast.Tuple, ast.Dict, ast.Set, ast.UnaryOp, ast.BinOp)):
        try:
            return ast.literal_eval(node)
        except Exception:
            return "<expr>"
    return "<expr>"


def _type_name(value) -> str:
    if value is None:
        return "null"
    t = type(value).__name__
    if t == "dict":
        return "object"
    if t == "bool":
        return "bool"
    if t in ("int", "float", "str", "list"):
        return t
    return t


def _default_repr(value) -> str:
    if isinstance(value, str):
        if len(value) > 60:
            return value[:57] + "..."
        return f'"{value}"'
    if isinstance(value, dict) and not value:
        return "{}"
    if value is None:
        return "null"
    r = repr(value)
    return r if len(r) <= 60 else r[:57] + "..."


def main() -> int:
    src = SETTINGS_FILE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    entries: dict[str, tuple[str, str, str]] = {}

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "DEFAULT_SETTINGS" and isinstance(node.value, ast.Dict):
                    for k, v in zip(node.value.keys, node.value.values):
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            _v = _ast_value(v)
                            entries[k.value] = (_type_name(_v), _default_repr(_v), "")

    # Pull the inline comment per key from the source.
    for key in entries:
        _m = re.search(
            r'^\s{4}"' + re.escape(key) + r'":.*?(#.*)?$',
            src,
            re.MULTILINE,
        )
        if _m and _m.group(1):
            entries[key] = (*entries[key][:2], _m.group(1).lstrip("# ").strip())

    lines: list[str] = []
    lines.append("# HiveMind Settings")
    lines.append("")
    lines.append(
        "Auto-generated from `settings.py` (`DEFAULT_SETTINGS`) via "
        "`python deploy/gen_settings_docs.py` — do not edit by hand."
    )
    lines.append("")
    lines.append(f"{len(entries)} settings keys, based on DEFAULT_SETTINGS.")
    lines.append("")
    lines.append("| Key | Type | Default | Note |")
    lines.append("|---|---|---|---|")
    for key in sorted(entries):
        t, d, c = entries[key]
        note = c if c else "—"
        lines.append(f"| `{key}` | {t} | `{d}` | {note} |")

    lines.append("")
    lines.append("## Audit disclosure")
    lines.append("")
    lines.append(
        "The following keys were previously only implicit read fallbacks (\"hidden\") "
        "and are now explicit in `DEFAULT_SETTINGS`:"
    )
    lines.append("")
    lines.append("`" + ", ".join(sorted(HIDDEN_KEYS)) + "`")
    lines.append("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {OUT} ({len(entries)} keys)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

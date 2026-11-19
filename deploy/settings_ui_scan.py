"""Which settings keys does the web UI actually expose?

A key that only lives in settings.json is easy to reset by accident: the engine
owns the file and rewrites it on the next save. This module is the shared truth
for two consumers:

  * `deploy/gen_settings_docs.py` - the "UI" column in docs/settings.md
  * `tests/test_settings_ui.py` - fails when a new key joins DEFAULT_SETTINGS
    without either a UI control (index.html/static/app.js or /settings/schema)
    or an entry in `settings_ui_exempt.txt`

A key counts as exposed when its exact name appears in index.html or
static/app.js OR when it is exposed by GET /settings/schema (i.e. a user-
configurable DEFAULT_SETTINGS key not excluded by SETTINGS_SCHEMA_EXCLUDED_KEYS
or `settings_ui_exempt.txt`).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI_FILES = (ROOT / "index.html", ROOT / "static" / "app.js")
EXEMPT_FILE = ROOT / "deploy" / "settings_ui_exempt.txt"


def ui_text() -> str:
    """Concatenated UI sources (missing files count as empty)."""
    parts = []
    for path in UI_FILES:
        try:
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def in_dom(keys) -> set[str]:
    """Subset of `keys` whose name appears directly in index.html or static/app.js."""
    text = ui_text()
    return {k for k in keys if k in text}


def in_schema(keys) -> set[str]:
    """Subset of `keys` exposed dynamically via GET /settings/schema."""
    from settings_defaults import DEFAULT_SETTINGS, SETTINGS_SCHEMA_EXCLUDED_KEYS
    exempt = exempt_keys()
    return {
        k for k in keys
        if k in DEFAULT_SETTINGS
        and k not in SETTINGS_SCHEMA_EXCLUDED_KEYS
        and k not in exempt
    }


def in_ui(keys) -> set[str]:
    """Subset of `keys` exposed by direct UI sources or /settings/schema."""
    return in_dom(keys) | in_schema(keys)


def exempt_keys() -> set[str]:
    """Explicitly accepted internal-only keys (`#` comments allowed)."""
    try:
        lines = EXEMPT_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    out = set()
    for line in lines:
        name = line.split("#", 1)[0].strip()
        if name:
            out.add(name)
    return out


def orphans(keys) -> list[str]:
    """Keys that are neither UI/schema-exposed nor explicitly exempt."""
    exposed = in_ui(keys)
    exempt = exempt_keys()
    return [k for k in keys if k not in exposed and k not in exempt]

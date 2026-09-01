"""Pre-Explore: Explore-Context-Aufbau (TOML, Fallback-Tree) (Teil von hive_functions/pre_explore)."""

from __future__ import annotations

from pathlib import Path
import re

_MAX_FALLBACK_FILES = 50

_RE_TREE_PATH = re.compile(
    r"(?:^|[│├└─ \t]+)([\w][\w.\-/\\]*\.(?:py|js|ts|jsx|tsx|java|go|rs|cpp|c|h|cs|kt|rb|"
    r"php|swift|vue|svelte|json|yaml|yml|toml|md|html|css|sh|sql|cfg|ini|xml|env"
    r"|scss|less|bat|ps1|r|lua|txt|rst|tex|csv))\b",
    re.MULTILINE | re.IGNORECASE,
)

_SKIP_FILE_PREFIXES = (".hivemind_ckpt_", ".hivemind_")

from .tooling import _SKIP_DIRS
from .contracts import logger

def _toml_str(value: str) -> str:
    if not isinstance(value, str):
        value = str(value)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "") + '"'


def _toml_arr(values: list, max_items: int = 15) -> str:
    if not values:
        return "[]"
    safe = []
    for v in values[:max_items]:
        if not isinstance(v, str):
            v = str(v)
        clean = v.replace("\n", " ").replace("\r", "").replace("\t", " ")
        safe.append(_toml_str(clean))
    if not safe:
        return "[]"
    return "[" + ", ".join(safe) + "]"


def _build_explore_ctx(results: list[dict]) -> str:


    if not results:
        return ""

    lines: list[str] = []
    sorted_results = sorted(results, key=lambda x: -x.get("importance", 3))

    lines.append("## Codebase Architecture Map\n")

    for r in sorted_results:
        label = r.get("label", "?")
        c = r.get("contract", {})
        if not c:
            continue

        imp = r.get("importance", 3)
        role = c.get("role", "")
        exports = c.get("exports", [])
        imports_int = c.get("imports_internal", [])
        imports_ext = c.get("imports_external", [])
        data_flow = c.get("data_flow", "")
        config = c.get("config", "")
        hint = c.get("hint", "")
        touched = c.get("touched_by_task", "unknown")
        cx = c.get("complexity_score", 0.5)
        files = c.get("files_read", [])

        # Partition-Header
        header = f"### [{imp}] {label}"
        if c.get("_fallback"):
            header += " \u26a0\ufe0f PROSE-FALLBACK"
        if touched == "yes":
            header += " \u26a1 TOUCHED"
        lines.append(header)

        # Rollen-Beschreibung
        if role:
            lines.append(f"  Role: {role}")

        # Exports (Schnittstellen)
        if exports:
            lines.append(f"  Exports: {', '.join(exports[:10])}")

        # Interne Dependencies
        if imports_int:
            lines.append(f"  Depends on: {', '.join(imports_int[:6])}")

        # Externe Dependencies
        if imports_ext:
            lines.append(f"  Libraries: {', '.join(imports_ext[:8])}")

        # Data Flow
        if data_flow:
            lines.append(f"  Data flow: {data_flow}")

        # Config
        if config:
            lines.append(f"  Config: {config}")

        # Architectural Hint
        if hint:
            lines.append(f"  Hint: {hint}")

        # Files + Complexity
        lines.append(f"  Files: {len(files)} | Complexity: {cx}")
        lines.append("")

    # ── TOML Contracts (maschinenlesbar) ─────────────────────────
    lines.append("---\n## Contracts (TOML)\n")

    for r in sorted_results:
        c = r.get("contract", {})
        if not c:
            continue

        lines.append("```toml")
        lines.append("[contract]")
        lines.append(f'partition = {_toml_str(c.get("partition", r.get("label", "")))}')
        if c.get("role"):
            lines.append(f'role = {_toml_str(c["role"])}')
        if c.get("exports"):
            lines.append(f'exports = {_toml_arr(c["exports"], 10)}')
        if c.get("imports_internal"):
            lines.append(f'imports_internal = {_toml_arr(c["imports_internal"], 8)}')
        if c.get("imports_external"):
            lines.append(f'imports_external = {_toml_arr(c["imports_external"], 8)}')
        if c.get("data_flow"):
            lines.append(f'data_flow = {_toml_str(c["data_flow"])}')
        if c.get("config"):
            lines.append(f'config = {_toml_str(c["config"])}')

        _tbt = c.get("touched_by_task", "unknown")
        if isinstance(_tbt, bool):
            _tbt = "yes" if _tbt else "unlikely"
        lines.append(f'touched_by_task = {_toml_str(str(_tbt))}')
        lines.append(f'complexity_score = {c.get("complexity_score", 0.5)}')

        if c.get("hint"):
            lines.append(f'hint = {_toml_str(c["hint"])}')
        if c.get("files_read"):
            lines.append(f'files_read = {_toml_arr(c["files_read"], 15)}')
        if c.get("_fallback"):
            lines.append('_fallback = true')

        lines.append("```")
        lines.append("")

    # ── Cross-Partition Conflict Detection ─────────────────────
    _conflict_fields = ("config", "imports_external")
    _field_values: dict[str, dict] = {}
    for r in sorted_results:
        c = r.get("contract", {})
        if not c:
            continue
        label = c.get("partition", r.get("label", ""))
        for _k in _conflict_fields:
            _v = c.get(_k)
            if _v is not None and _v != "":
                _field_values.setdefault(_k, {})[label] = _v

    _conflicts: list[str] = []
    for _field, _vals in _field_values.items():
        _unique = set(str(v) for v in _vals.values())
        if len(_unique) > 1:
            _details = "; ".join(f"{p}: {str(_vals[p])[:80]}" for p in _vals)
            _conflicts.append(f"  {_field} — {_details}")

    if _conflicts:
        lines.append("---\n## ⚠ Cross-Partition Conflicts\n")
        for _c in _conflicts:
            lines.append(_c)
        lines.append("")
        logger.warning("[PRE-EXPLORE] %d cross-partition field conflict(s) detected: %s",
                       len(_conflicts), ", ".join(_conflicts))

    # ── Confirmed Directory Structure ─────────────────────────
    _any_files = any((r.get("contract") or {}).get("files_read") for r in sorted_results)
    if _any_files:
        lines.append("---\n## Confirmed Directory Structure\n")
        for r in sorted_results:
            c = r.get("contract", {})
            if not c:
                continue
            files = c.get("files_read", [])
            if not files:
                continue
            label = r.get("label", "?")
            lines.append(f"\n### {label}/")
            for f in files[:30]:
                lines.append(f"  - {f}")
            if len(files) > 30:
                lines.append(f"  ... ({len(files) - 30} more files)")
        lines.append("")

    return "\n".join(lines)


def _extract_paths_from_tree(tree_ctx: str, workspace: str) -> list[str]:
    """Extrahiert relative Dateipfade aus einem tree_ctx-String."""
    ws = Path(workspace)
    seen: set[str] = set()
    paths: list[str] = []
    for m in _RE_TREE_PATH.finditer(tree_ctx):
        raw = m.group(1).replace("\\", "/").strip()
        try:
            if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
                rel = str(Path(raw).relative_to(ws))
            else:
                rel = raw
        except ValueError:
            rel = raw
        norm = rel.lower()
        if norm not in seen and not any(s in (p.lower() for p in Path(rel).parts) for s in _SKIP_DIRS) and not Path(rel).name.startswith(_SKIP_FILE_PREFIXES):
            seen.add(norm)
            paths.append(rel)
        if len(paths) >= _MAX_FALLBACK_FILES:
            break
    return paths


def _merge_partition_messages(results: list[dict]) -> list[dict]:


    all_msgs: list[dict] = []
    for r in results:
        _wm = r.get("messages") or []
        all_msgs.extend(m for m in _wm if m.get("role") != "system")
    return all_msgs

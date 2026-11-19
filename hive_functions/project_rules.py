# -*- coding: utf-8 -*-
"""Workspace project rules loader (HIVEMIND.md / AGENTS.md / CLAUDE.md).

Reads workspace-local instruction files so HiveMind respects per-repo
conventions (test commands, style rules, venv/tooling notes, boundaries).

Discovery order inside the workspace root (never walks above workspace root):
  1. HIVEMIND.md
  2. .hivemind/HIVEMIND.md
  3. AGENTS.md
  4. CLAUDE.md  (fallback only if none of the primary files exist)

All resolved paths must remain strictly inside the workspace root (no symlink
escape) and total injected text is capped cleanly for small local models.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("hivemind.project_rules")

PRIMARY_RULE_FILES: tuple[str, ...] = (
    "HIVEMIND.md",
    ".hivemind/HIVEMIND.md",
    "AGENTS.md",
)
FALLBACK_RULE_FILES: tuple[str, ...] = (
    "CLAUDE.md",
)
DEFAULT_MAX_CHARS = 6000


def _resolve_inside_workspace(ws_root: Path, rel_path: str) -> Path | None:
    """Resolve *rel_path* inside *ws_root* and reject symlink escapes."""
    try:
        ws_res = ws_root.resolve()
        raw = ws_root / rel_path
        if not raw.exists():
            # Case-insensitive fallback for lowercase filenames (e.g. hivemind.md on Linux)
            parent = raw.parent
            if parent.is_dir():
                want = raw.name.lower()
                match = next((p for p in parent.iterdir() if p.name.lower() == want), None)
                if match is None:
                    return None
                raw = match
            else:
                return None
        cand = raw.resolve()
        if not cand.is_file():
            return None
        if not cand.is_relative_to(ws_res):
            logger.warning("[PROJECT-RULES] Refused symlink outside workspace: %s", rel_path)
            return None
        return cand
    except (OSError, ValueError, RuntimeError):
        return None


def _trim_at_line_boundary(text: str, budget: int, label: str) -> str:
    if len(text) <= budget:
        return text
    suffix = f"\n...[truncated — read {label} for full rules]"
    keep = max(0, budget - len(suffix))
    if keep <= 0:
        return ""
    head = text[:keep]
    nl = head.rfind("\n")
    if nl >= keep // 2:
        head = head[:nl]
    return head.rstrip() + suffix


def load_project_rules(
    workspace_root: str | Path | None,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[str, list[str]]:
    """Return ``(combined_text, loaded_labels)`` from workspace rule files."""
    if not workspace_root or max_chars <= 0:
        return "", []
    try:
        ws = Path(workspace_root)
        if not ws.is_dir():
            return "", []
    except (OSError, ValueError, TypeError):
        return "", []

    seen_real: set[str] = set()
    found: list[tuple[str, Path]] = []

    for rel in PRIMARY_RULE_FILES:
        resolved = _resolve_inside_workspace(ws, rel)
        if resolved is None:
            continue
        key = str(resolved).lower()
        if key not in seen_real:
            seen_real.add(key)
            found.append((rel, resolved))

    if not found:
        for rel in FALLBACK_RULE_FILES:
            resolved = _resolve_inside_workspace(ws, rel)
            if resolved is None:
                continue
            key = str(resolved).lower()
            if key not in seen_real:
                seen_real.add(key)
                found.append((rel, resolved))

    sections: list[str] = []
    loaded: list[str] = []
    remaining = int(max_chars)

    for rel, path in found:
        if remaining < 80:
            break
        try:
            raw = path.read_text(encoding="utf-8-sig", errors="replace").strip()
        except OSError as exc:
            logger.debug("[PROJECT-RULES] Failed to read %s: %s", rel, exc)
            continue
        if not raw:
            continue
        header = f"--- {rel} ---\n" if len(found) > 1 else ""
        body_budget = remaining - len(header)
        chunk = _trim_at_line_boundary(raw, body_budget, rel)
        if not chunk:
            continue
        section = header + chunk
        sections.append(section)
        loaded.append(rel)
        remaining -= len(section) + 2

    return "\n\n".join(sections), loaded


def format_project_rules(
    workspace_root: str | Path | None,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[str, list[str]]:
    """Return a prompt-ready project rules block plus the list of loaded files."""
    text, loaded = load_project_rules(workspace_root, max_chars=max_chars)
    if not text or not loaded:
        return "", []
    names = ", ".join(loaded)
    block = (
        f"[Project Rules — loaded from {names}]\n"
        "Follow these workspace-specific rules and conventions strictly:\n"
        f"{text}\n"
        "[End Project Rules]"
    )
    return block, loaded

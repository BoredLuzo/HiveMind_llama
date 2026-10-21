"""Editor-Kontext: was die IDE gerade sieht (Datei, Selektion, Diagnostics).

Warum: ein IDE-Agent wirkt nicht wegen des Modells besser, sondern weil er den
Kontext hat - offene Datei, markierter Code, Fehlermarkierungen. HiveMind laeuft
neben der IDE und hat diesen Kontext bisher nicht. Dieses Modul nimmt ihn entgegen
(HTTP, lokal), haelt ihn kurz (TTL) und kann ihn als kompakten Block in einen Run
geben.

Bewusst schlank und ohne Prompt-Magie: der Aufrufer entscheidet, ob/wie er den
Block in den Run haengt.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TTL_S = 300.0          # Kontext ist frisch oder weg - nie "irgendwann alt"
MAX_SELECTION_CHARS = 4000     # mehr Selektion hilft nicht, kostet ctx
MAX_DIAGNOSTICS = 20
MAX_OPEN_FILES = 20


@dataclass
class EditorContext:
    """Momentaufnahme aus der IDE."""
    workspace: str = ""
    file: str = ""
    cursor_line: int = 0
    selection: str = ""
    open_files: list[str] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)
    source: str = "ide"
    updated_at: float = field(default_factory=time.time)

    def normalized_workspace(self) -> str:
        return _norm_workspace(self.workspace)

    def to_dict(self) -> dict:
        return {
            "workspace": self.workspace,
            "file": self.file,
            "cursor_line": self.cursor_line,
            "selection": self.selection,
            "open_files": list(self.open_files),
            "diagnostics": list(self.diagnostics),
            "source": self.source,
            "updated_at": self.updated_at,
        }

    def summary(self) -> dict:
        """Kurzfassung fuer Logs/Statistiken - ohne den Code selbst."""
        return {
            "file": self.file,
            "selection_chars": len(self.selection or ""),
            "open_files": len(self.open_files or []),
            "diagnostics": len(self.diagnostics or []),
            "age_s": round(time.time() - self.updated_at, 1),
        }


def _clean_text(value, limit: int) -> str:
    text = str(value or "")
    text = text.replace("\x00", "")
    if len(text) > limit:
        text = text[:limit] + "\n… [truncated]"
    return text


def _clean_paths(values, limit: int) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        p = str(raw or "").strip()
        if p and p not in out:
            out.append(p)
        if len(out) >= limit:
            break
    return out


def _clean_diagnostics(values) -> list[dict]:
    out: list[dict] = []
    if not isinstance(values, list):
        return out
    for item in values:
        if not isinstance(item, dict):
            continue
        entry = {
            "file": str(item.get("file") or "")[:400],
            "line": _to_int(item.get("line")),
            "severity": str(item.get("severity") or "info")[:20],
            "message": _clean_text(item.get("message"), 300),
        }
        if entry["message"]:
            out.append(entry)
        if len(out) >= MAX_DIAGNOSTICS:
            break
    return out


def _norm_workspace(workspace) -> str:
    """Workspace-Key. Leer bleibt leer - Path("").resolve() waere das CWD."""
    raw = str(workspace or "").strip()
    if not raw:
        return ""
    try:
        return str(Path(raw).resolve()).lower()
    except (OSError, ValueError):
        return raw.lower()


def _to_int(value) -> int:
    """Tolerant: die IDE schickt mal eine Zahl, mal einen String, mal Unsinn."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def parse_context(payload: dict) -> EditorContext:
    """JSON der IDE -> EditorContext (defensiv, ohne Ausnahmen nach aussen)."""
    data = payload if isinstance(payload, dict) else {}
    return EditorContext(
        workspace=str(data.get("workspace") or "")[:500],
        file=str(data.get("file") or "")[:400],
        cursor_line=_to_int(data.get("cursor_line")),
        selection=_clean_text(data.get("selection"), MAX_SELECTION_CHARS),
        open_files=_clean_paths(data.get("open_files"), MAX_OPEN_FILES),
        diagnostics=_clean_diagnostics(data.get("diagnostics")),
        source=str(data.get("source") or "ide")[:40],
    )


class EditorContextStore:
    """Ein Slot pro Workspace, mit TTL. Thread-safe, absichtlich simpel."""

    def __init__(self, ttl_s: float = DEFAULT_TTL_S) -> None:
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._items: dict[str, EditorContext] = {}

    def put(self, ctx: EditorContext) -> None:
        key = ctx.normalized_workspace()
        if not key:
            return
        with self._lock:
            self._items[key] = ctx

    def get(self, workspace: str) -> EditorContext | None:
        key = _norm_workspace(workspace)
        if not key:
            return None
        with self._lock:
            ctx = self._items.get(key)
            if ctx is None:
                return None
            if (time.time() - ctx.updated_at) > self.ttl_s:
                self._items.pop(key, None)
                return None
            return ctx

    def clear(self, workspace: str) -> bool:
        key = _norm_workspace(workspace)
        with self._lock:
            return self._items.pop(key, None) is not None

    def count(self) -> int:
        with self._lock:
            return len(self._items)


_STORE = EditorContextStore()


def store() -> EditorContextStore:
    return _STORE


def format_block(ctx: EditorContext) -> str:
    """Kompakter Prompt-Block. Leer, wenn nichts Brauchbares da ist."""
    if ctx is None:
        return ""
    lines: list[str] = []
    if ctx.file:
        extra = f", cursor line {ctx.cursor_line}" if ctx.cursor_line else ""
        lines.append(f"Open file: {ctx.file}{extra}")
    if ctx.selection:
        lines.append("Selected code:")
        lines.append("```")
        lines.append(ctx.selection.rstrip())
        lines.append("```")
    if ctx.open_files:
        others = [p for p in ctx.open_files if p != ctx.file][:10]
        if others:
            lines.append("Also open: " + ", ".join(others))
    if ctx.diagnostics:
        lines.append("Editor diagnostics:")
        for d in ctx.diagnostics[:10]:
            loc = f"{d['file']}:{d['line']}" if d.get("line") else str(d.get("file") or "")
            lines.append(f"- [{d['severity']}] {loc} {d['message']}".strip())
    if not lines:
        return ""
    return "[EDITOR CONTEXT — from the IDE, current state]\n" + "\n".join(lines) + "\n[/EDITOR CONTEXT]"


def compact_workspace(workspace: str) -> str:
    """Nur fuer Logs: letzter Pfadteil, damit Logs keine langen Pfade tragen."""
    m = re.search(r"[\\\\/]([^\\\\/]+)$", str(workspace or ""))
    return m.group(1) if m else str(workspace or "")


def _workspace_from_settings() -> str:
    """Aktiver Workspace, ohne harte Abhaengigkeit auf core.state."""
    try:
        from core.state import settings as _st_settings
        return str(_st_settings.get("workspace") or "")
    except (ImportError, AttributeError, TypeError, ValueError):
        return ""


def context_block_for(workspace: str | None = None) -> str:
    """Fertiger Prompt-Block (leer, wenn im Workspace nichts anliegt)."""
    try:
        key = workspace if workspace is not None else _workspace_from_settings()
        return format_block(store().get(key))
    except (OSError, ValueError, TypeError, AttributeError):
        return ""


def context_meta_for(workspace: str | None = None) -> dict | None:
    """Kurzfassung fuer Run-Records (ohne den Code selbst zu speichern)."""
    try:
        key = workspace if workspace is not None else _workspace_from_settings()
        ctx = store().get(key)
        return ctx.summary() if ctx is not None else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None

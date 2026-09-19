"""Workspace snapshot system for fail->restore loops (ContextVar-isolated)."""
from __future__ import annotations
from contextvars import ContextVar
from pathlib import Path


class WorkspaceTransaction:


    def __init__(self):
        self._snapshots: dict[str, str | None] = {}
        self._active = False

    def begin(self):
        self._active = True
        self._snapshots.clear()

    def capture_before(self, filepath: str | Path):
        if not self._active:
            return
        p = Path(filepath).resolve()
        p_str = str(p)
        try:
            if p.exists():
                _cur: str | None = p.read_text(encoding="utf-8", errors="replace")
            else:
                _cur = None
        except Exception:
            return
        if p_str in self._snapshots and self._snapshots[p_str] == _cur:
            return
        # RE-CAPTURE (2026-09-19): a second write/edit to the same file in a
        # later round previously kept the round-1 baseline (for a new file:
        # None), so its diff showed the whole file as new (+N -0). The
        # snapshot now always holds the content right before the LATEST
        # modification; rollback and undo_path restore to that point.
        self._snapshots[p_str] = _cur

    def rollback(self) -> list[str]:
        if not self._active:
            return []
        restored = []
        for p_str, content in self._snapshots.items():
            p = Path(p_str)
            try:
                if content is None:
                    if p.exists():
                        p.unlink()
                        restored.append(p.name)
                else:
                    p.write_text(content, encoding="utf-8")
                    restored.append(p.name)
            except Exception:
                pass
        self._snapshots.clear()
        self._active = False
        return restored

    def undo_path(self, filepath: str | Path) -> tuple[bool, str]:


        if not self._active:
            return False, "no active workspace transaction"
        p = Path(filepath).resolve()
        p_str = str(p)
        if p_str not in self._snapshots:
            return False, f"no snapshot for '{p}' (file was not changed in this round)"
        content = self._snapshots.pop(p_str)
        try:
            if content is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
        except Exception as _e:
            return False, f"Wiederherstellung fehlgeschlagen: {_e}"
        return True, str(p)

    def commit(self):
        self._snapshots.clear()
        self._active = False

    @property
    def has_changes(self) -> bool:
        return len(self._snapshots) > 0

    def diff_for(self, filepath: str | Path, max_chars: int = 3000,
                 call_chars: int | None = None) -> str:


        import difflib
        p = Path(filepath).resolve()
        p_str = str(p)
        if p_str not in self._snapshots:
            return ""
        before = self._snapshots[p_str]
        try:
            after = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        except Exception:
            after = ""
        before_lines = (before or "").splitlines()
        after_lines = after.splitlines()
        try:
            _diff_lines = list(difflib.unified_diff(
                before_lines, after_lines,
                fromfile=f"a/{p.name}", tofile=f"b/{p.name}",
                lineterm="",
            ))
        except Exception:
            return ""
        _added = sum(1 for _l in _diff_lines[2:] if _l.startswith("+"))
        _removed = sum(1 for _l in _diff_lines[2:] if _l.startswith("-"))
        _hunks = sum(1 for _l in _diff_lines if _l.startswith("@@"))
        _truncated = 1 if len("\n".join(_diff_lines)) > max_chars else 0
        # TOOL-TOKENS (2026-09-19): estimated size of the whole call
        # (serialized args, ~4 chars per token, same factor the context
        # estimator uses) — shown in the result metrics instead of a line
        # count on the chip.
        _tokens = max(1, round(call_chars / 4)) if call_chars else 0
        _head = (f"[DIFFSTAT] added={_added} removed={_removed} hunks={_hunks} "
                 f"tokens={_tokens} truncated={_truncated}")
        _diff = "\n".join(_diff_lines)
        if len(_diff) > max_chars:
            _diff = _diff[:max_chars] + "\n... [diff truncated]"
        return _head + "\n" + _diff


_tx_context: ContextVar[WorkspaceTransaction] = ContextVar("workspace_tx")


def get_transaction() -> WorkspaceTransaction:
    """Return the WorkspaceTransaction for the current async context.
    Creates a new one if none exists yet (backward-compat for legacy paths)."""
    try:
        return _tx_context.get()
    except LookupError:
        tx = WorkspaceTransaction()
        _tx_context.set(tx)
        return tx


def new_transaction() -> WorkspaceTransaction:
    """Create a fresh WorkspaceTransaction and set it as the current context's tx."""
    tx = WorkspaceTransaction()
    _tx_context.set(tx)
    return tx


# Module-level alias for backward compat — resolves via ContextVar
_transaction_manager = get_transaction()

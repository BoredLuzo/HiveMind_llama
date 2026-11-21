"""Pre-Explore: Tool-Ausfuehrung (exec_tool, FileTransaction) (Teil von hive_functions/pre_explore)."""

from __future__ import annotations

from pathlib import Path
import asyncio
import os
import re

from utils.file import is_secret_path, _is_junction

_EXCLUDE_DIRS = {"node_modules", ".git", "__pycache__", ".cache",
                 "dist", "build", ".astro", "target"}

_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", "target",
}


def _tool_err(code, msg, tool=""):
    return f"[ERROR:{code}] {tool}: {msg}"


def _path_has_excluded_dir(path_str: str) -> bool:
    """True if path passes through any excluded directory or is a HiveMind artifact."""
    parts = Path(path_str).parts
    _name = Path(path_str).name
    if _name.startswith('.hivemind') or _name.startswith('.hive'):
        return True
    return any(p in _EXCLUDE_DIRS for p in parts)


class _DummyTx:
    def capture(self, *a): pass
    def rollback(self): return []
    def commit(self): pass
    def begin(self): pass


def _apply_patch(original, patch):
    rx = re.compile(r"<<<< SEARCH\s*\n(.*?)\n==== REPLACE\s*\n(.*?)\n>>>> END", re.DOTALL)
    out = original
    for m in rx.finditer(patch):
        old, new = m.group(1), m.group(2)
        if old in out:
            out = out.replace(old, new, 1)
    return out


class FileTransaction:
    """Pre-explore snapshot/rollback.

    CAPTURE SEMANTICS: FIRST-WINS on purpose - rollback must restore the
    file as it was BEFORE pre-explore started, not an intermediate write.
    `tools/workspace.py:WorkspaceTransaction` is the re-capturing sibling for
    fix loops; do not merge them without the pre-explore failure-path tests
    (deep audit 2026-11-07).
    """
    def __init__(self):
        self._snap: dict[str, str | None] = {}
        self._on = False

    def begin(self):
        self._on = True
        self._snap.clear()

    def capture(self, filepath):
        if not self._on:
            return
        p = Path(filepath).resolve()
        k = str(p)
        if k in self._snap:
            return
        try:
            self._snap[k] = p.read_text("utf-8", errors="replace") if p.exists() else None
        except Exception:
            pass

    def rollback(self):
        if not self._on:
            return []
        done = []
        for k, c in self._snap.items():
            p = Path(k)
            try:
                if c is None:
                    p.exists() and p.unlink()
                    done.append(p.name)
                else:
                    p.write_text(c, "utf-8")
                    done.append(p.name)
            except Exception:
                pass
        self._snap.clear()
        self._on = False
        return done

    def commit(self):
        self._snap.clear()
        self._on = False


def _check_contained(
    fp: Path,
    workspace: str | Path,
    *,
    allow_secret_dir: bool = False,
    tool_name: str = "",
) -> str | None:
    """Enforce workspace containment, non-junction/symlink path, and secret denylist."""
    try:
        abs_ws = Path(workspace).resolve()
        abs_fp = fp.resolve()
        abs_fp.relative_to(abs_ws)
    except (OSError, ValueError):
        return _tool_err("CONTAINMENT", f"Path '{fp}' is outside workspace", tool_name)

    try:
        cur = Path(os.path.abspath(str(fp)))
        stop = Path(os.path.abspath(str(workspace)))
        while True:
            if cur == stop:
                break
            if cur.is_symlink() or _is_junction(cur):
                return _tool_err("JUNCTION", f"Symlink/junction path denied: '{cur}'", tool_name)
            parent = cur.parent
            if parent == cur:
                break
            cur = parent
    except (OSError, ValueError, TypeError):
        return _tool_err("CONTAINMENT", f"Cannot validate path '{fp}'", tool_name)

    if not allow_secret_dir and (is_secret_path(abs_fp, abs_ws) or is_secret_path(fp, workspace)):
        return _tool_err("SECRET", f"Protected secret path denied: '{fp}'", tool_name)
    return None


async def _exec_tool(name, args, workspace, tx, max_read=30000, bash_to=60.0):
    def _r(p):
        pp = Path(p)
        return pp if pp.is_absolute() else Path(workspace) / pp

    if name == "read_file":
        p = args.get("path", "")
        if not p:
            return _tool_err("ARG", "path required", name)
        try:
            fp = _r(p)
            if err := _check_contained(fp, workspace, tool_name=name):
                return err
            c = await asyncio.to_thread(fp.read_text, "utf-8", errors="replace")
            return c[:max_read] + f"\n... [{len(c)} chars]" if len(c) > max_read else c
        except Exception as e:
            return _tool_err("READ", str(e), name)

    if name == "edit_file":
        p, c = args.get("path", ""), args.get("edits") or ""
        if not p:
            return _tool_err("ARG", "path required", name)
        try:
            fp = _r(p)
            tx.capture(fp)
            fp.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(fp.write_text, c, "utf-8")
            return f"OK: {len(c)} chars -> {p}"
        except Exception as e:
            return _tool_err("WRITE", str(e), name)

    if name == "patch_file":
        p, patch = args.get("path", ""), args.get("patch", "")
        if not p or not patch:
            return _tool_err("ARG", "path+patch required", name)
        try:
            fp = _r(p)
            tx.capture(fp)
            orig = await asyncio.to_thread(fp.read_text, "utf-8", errors="replace")
            await asyncio.to_thread(fp.write_text, _apply_patch(orig, patch), "utf-8")
            return f"OK: patched {p}"
        except Exception as e:
            return _tool_err("PATCH", str(e), name)

    if name == "run_bash":
        cmd = args.get("command", args.get("cmd", ""))
        if not cmd:
            return _tool_err("ARG", "command required", name)
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=workspace)
            o, e = await asyncio.wait_for(proc.communicate(), timeout=bash_to)
            r = o.decode(errors="replace")
            if e:
                r += f"\n[stderr]: {e.decode(errors='replace')}"
            return r[:10000] + "..." if len(r) > 10000 else (r or "(no output)")
        except asyncio.TimeoutError:
            return f"[TIMEOUT] >{bash_to}s"
        except Exception as e:
            return _tool_err("BASH", str(e), name)

    if name == "list_dir":
        p = args.get("path", workspace)
        try:
            fp = _r(p)
            if err := _check_contained(fp, workspace, allow_secret_dir=True, tool_name=name):
                return err
            abs_ws = Path(workspace).resolve()
            entries = sorted(
                await asyncio.to_thread(lambda: list(fp.iterdir())),
                key=lambda e: (not e.is_dir(), e.name.lower()),
            )
            lines_out = []
            for e in entries:
                if e.name.lower() in _SKIP_DIRS:
                    continue
                if e.is_symlink() or _is_junction(e) or is_secret_path(e, workspace):
                    continue
                try:
                    e.resolve().relative_to(abs_ws)
                except (OSError, ValueError):
                    continue
                lines_out.append(f"{'dir ' if e.is_dir() else '    '}{e.name}")
                if len(lines_out) >= 100:
                    break
            return "\n".join(lines_out) or "(empty)"
        except Exception as e:
            return _tool_err("LS", str(e), name)

    if name == "find_files":
        pat, p = args.get("pattern", "*"), args.get("path", workspace)
        try:
            fp = _r(p)
            if err := _check_contained(fp, workspace, allow_secret_dir=True, tool_name=name):
                return err
            found = await asyncio.to_thread(lambda: list(fp.rglob(pat)))
            kept = []
            for f in found:
                if any(s in (part.lower() for part in f.parts) for s in _SKIP_DIRS):
                    continue
                if _check_contained(f, workspace, tool_name=name) is not None:
                    continue
                kept.append(str(f.relative_to(fp)))
                if len(kept) >= 50:
                    break
            return "\n".join(kept) or "(none)"
        except Exception as e:
            return _tool_err("FIND", str(e), name)

    return f"[UNKNOWN] {name}"

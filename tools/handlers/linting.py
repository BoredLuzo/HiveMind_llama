"""Tool handlers: linting helpers (pyright) (part of tools/handlers, extracted from tools/handlers.py)."""

from __future__ import annotations

from pathlib import Path
import asyncio
import json
import logging
import shutil
import sys

from infra.child_env import resolve_workspace_python, tool_env
from tools.sandbox import kill_tree_and_reap as _lint_kill_reap

_LINT_ARGVS: dict[str, tuple[str, ...]] = {
    "python": ("python", "-m", "py_compile"),
    "javascript": ("node", "--check"),
    "java": ("javac",),
    "cpp": ("g++", "-fsyntax-only"),
}


def _build_lint_argv(lang: str, p: Path, workspace: Path | None = None) -> list[str] | None:
    """Return an argv list for a built-in per-file linter, or None."""
    base = _LINT_ARGVS.get(lang)
    if not base:
        return None
    argv = list(base)
    if lang == "python":
        resolved_py = resolve_workspace_python(workspace) if workspace is not None else None
        argv[0] = str(resolved_py) if resolved_py is not None else (
            sys.executable or ("python" if sys.platform == "win32" else "python3")
        )
    else:
        argv[0] = shutil.which(argv[0]) or argv[0]
    argv.append(str(p))
    return argv

def _resolve_pyright_cmd() -> list[str] | None:


    try:
        from core.state import settings as _py_settings
        _custom = str((_py_settings.get("duo_pyright_path") or "")).strip()
    except Exception:
        _custom = ""
    if _custom:
        _cp = Path(_custom)
        if _cp.is_file():
            return [str(_cp)]
    _which = shutil.which("pyright")
    if _which:
        return [_which]
    return None


async def _pyright_lint_result(cmd_prefix: list[str], p: "Path", workspace: "Path") -> str:

    r = None
    try:
        r = await asyncio.create_subprocess_exec(
            *cmd_prefix, "--outputjson", str(p),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace), env=tool_env(workspace=workspace))
        out, err = await asyncio.wait_for(r.communicate(), 12)
    except asyncio.TimeoutError:
        if r is not None:
            await _lint_kill_reap(r)
        return ("\n[lint/python WARNING: pyright timed out after 12s — type errors may be "
                "unreported. Check manually or run tests to verify.]")
    except Exception as _pe:
        return (f"\n[lint/python WARNING: pyright crashed ({type(_pe).__name__}) — type errors "
                "may be unreported. Verify manually or run tests.]")
    finally:
        if r is not None:
            _tr_p = getattr(r, "_transport", None)
            if _tr_p is not None:
                _tr_p.close()
    raw = out.decode(errors="replace")
    try:
        data = json.loads(raw)
    except Exception:
        combined = (raw + "\n" + err.decode(errors="replace")).strip()
        if r.returncode == 0:
            return ""
        msg = combined[-800:] if len(combined) > 800 else (combined or "(no output)")
        return f"\n[lint/python ERROR — fix before continuing]:\n{msg}"
    diags = data.get("generalDiagnostics") or []
    errors = [d for d in diags if d.get("severity") == "error"]
    warnings = [d for d in diags if d.get("severity") == "warning"]
    if not errors and not warnings:
        return ""
    lines = []
    for d in (errors + warnings)[:10]:
        _st = ((d.get("range") or {}).get("start") or {})
        ln = int(_st.get("line", 0)) + 1
        col = int(_st.get("character", 0)) + 1
        sev = "error" if d.get("severity") == "error" else "warning"
        msg1 = (str(d.get("message") or "").splitlines() or [""])[0][:160]
        rule = d.get("rule") or ""
        lines.append(f"{p.name}:{ln}:{col} — {sev}: {msg1}" + (f" [{rule}]" if rule else ""))
    body = "\n".join(lines)
    if len(body) > 800:
        body = f"[...{len(body) - 800} chars omitted...]\n" + body[-800:]
    head = ("[lint/python ERROR (pyright) — fix before continuing]"
            if errors else "[lint/python WARNINGS (pyright)]")
    return f"\n{head}:\n{body}"


async def _auto_lint_result(p: "Path", workspace: "Path") -> str:


    try:
        from hive_functions.language_config import LANGUAGE_RUNNERS, detect_language
        lang = detect_language(str(p))
        if not lang:
            return ""
        if lang == "python":
            # ONE LINTER (2026-11-04): pyright when installed, py_compile only
            # as an environment fallback. The user-facing engine selector is
            # gone - there is nothing to choose.
            _pyr = _resolve_pyright_cmd()
            if _pyr is not None:
                return await _pyright_lint_result(_pyr, p, workspace)
            logging.getLogger("hivemind.tools").debug(
                "[AUTO-LINT] pyright not found — falling back to py_compile for %s", p.name)
        cfg      = LANGUAGE_RUNNERS.get(lang, {})
        lint_tpl = cfg.get("lint_cmd")
        if not lint_tpl or "{file}" not in lint_tpl:
            return ""
        argv = _build_lint_argv(lang, p, workspace=workspace)
        if not argv:
            return ""
        r = None
        try:
            r = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=str(workspace), env=tool_env(workspace=workspace))
            out, err = await asyncio.wait_for(r.communicate(), 8)
            stdout = out.decode(errors="replace").strip()
            stderr = err.decode(errors="replace").strip()
            combined = (stdout + ("\n" + stderr if stderr else "")).strip()
            if r.returncode == 0:
                ok_noise = {"ok", "ok.", "", "no issues found"}
                if combined.lower() in ok_noise or not combined:
                    return ""
                if len(combined) < 80 and r.returncode == 0:
                    return f"\n[lint/{lang}: OK — {combined}]"
                return ""
            if combined and len(combined) > 800:
                msg = f"[...{len(combined)-800} chars omitted...]\n" + combined[-800:]
            else:
                msg = combined if combined else "(no output)"
            return f"\n[lint/{lang} ERROR — fix before continuing]:\n{msg}"
        except asyncio.TimeoutError:
            if r is not None:
                await _lint_kill_reap(r)
            return f"\n[lint/{lang} WARNING: lint timed out after 8s — syntax may have errors that were NOT detected.\nCheck manually or run build commands to verify.]"
        except Exception as _le:
            return f"\n[lint/{lang} WARNING: lint crashed ({type(_le).__name__}) — syntax may have unreported errors.\nVerify manually or run build commands.]"
        finally:
            if r is not None:
                _tr_l = getattr(r, "_transport", None)
                if _tr_l is not None:
                    _tr_l.close()
    except ImportError:
        return ""
    except Exception:
        return ""

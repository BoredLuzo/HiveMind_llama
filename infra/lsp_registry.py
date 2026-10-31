# -*- coding: utf-8 -*-
"""LSP server registry: file extension -> language id -> candidate commands.

The LSP client (`infra/lsp_client.py`) wants to be broad and generic: it
should drive ANY installed language server, with no per-language feature
flags. This module is the only place that knows file extensions, language
ids and default command candidates, so the client itself stays
registry-agnostic.

Rules (2026-10-31):
  - a server is auto-detected with `shutil.which`, candidates in order of
    preference; the first found command wins;
  - `settings["lsp_servers"]` maps a language id to a command (string or
    list) and REPLACES the default candidates for that language. An
    explicit override that does not resolve must fail as "not found"
    (available() -> found=False, start_language() -> False) instead of
    silently starting a different server than the user configured;
  - there are no on/off flags per language: a language is usable when a
    command for it was found, otherwise it is simply reported as missing.
"""
from __future__ import annotations

import logging
import shlex
import shutil
import sys
from pathlib import Path

logger = logging.getLogger("hivemind.lsp_registry")

# Default command candidates per language id, in preference order. All of
# these speak LSP over stdio (2026-10-31: verified against the servers'
# own docs - the "--stdio"/"start" arguments are part of the command).
SERVER_CANDIDATES: dict[str, list[list[str]]] = {
    "python": [
        ["pyright-langserver", "--stdio"],
        ["pylsp"],
        ["jedi-language-server"],
    ],
    "typescript": [["typescript-language-server", "--stdio"]],
    "javascript": [["typescript-language-server", "--stdio"]],
    "go": [["gopls"]],
    "rust": [["rust-analyzer"]],
    "c": [["clangd"]],
    "cpp": [["clangd"]],
    "bash": [["bash-language-server", "start"]],
    "lua": [["lua-language-server"]],
    "json": [["vscode-json-language-server", "--stdio"]],
    "yaml": [["yaml-language-server", "--stdio"]],
    "html": [["vscode-html-language-server", "--stdio"]],
    "css": [["vscode-css-language-server", "--stdio"]],
}

# File extension -> language id. Language ids follow the LSP convention
# (lowercase, e.g. "cpp" not "C++"), so they can be echoed back to servers
# as the languageId of textDocument/didOpen.
EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python", ".pyi": "python", ".pyw": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".mts": "typescript", ".cts": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".sh": "bash", ".bash": "bash",
    ".lua": "lua",
    ".json": "json", ".jsonc": "json",
    ".yaml": "yaml", ".yml": "yaml",
    ".html": "html", ".htm": "html",
    ".css": "css",
}


def language_for_path(path: str | Path) -> str | None:
    """The language id for a file path (by extension), or None if unknown."""
    return EXTENSION_LANGUAGE.get(Path(str(path)).suffix.lower())


def split_command(text: str) -> list[str]:
    """One command string -> argv list.

    shlex runs with posix=False so Windows backslashes survive
    (C:\\tools\\server.exe would lose them under posix rules); posix=False
    keeps the quote characters around tokens, so they are stripped after.
    """
    parts = shlex.split(str(text).strip(), posix=False)
    return [p for p in (t.strip('"').strip("'") for t in parts) if p]


def normalize_override(raw) -> dict[str, list[str]]:
    """settings["lsp_servers"] -> {language id: argv}.

    Accepts a string ("command --args") or a list of strings per language.
    Anything else is warned about and ignored - a broken override must not
    disable the auto-detected defaults.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        logger.warning("[LSP] lsp_servers is %s, expected an object - "
                       "ignored, default candidates stay active",
                       type(raw).__name__)
        return {}
    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        lang = str(key).strip().lower()
        if not lang:
            continue
        if isinstance(value, str):
            argv = split_command(value)
        elif isinstance(value, list):
            argv = [str(v) for v in value]
        else:
            logger.warning("[LSP] lsp_servers[%s] is %s, expected a string "
                           "or list - ignored", lang, type(value).__name__)
            continue
        if not argv:
            logger.warning("[LSP] lsp_servers[%s] is empty - ignored", lang)
            continue
        out[lang] = argv
    return out


def candidates(language_id: str, override=None) -> list[list[str]]:
    """The command candidates that would be tried for a language, in order
    (an override replaces the defaults entirely). Used for error messages
    and the available() status."""
    lang = str(language_id).strip().lower()
    ov = normalize_override(override)
    if lang in ov:
        return [ov[lang]]
    return [list(c) for c in SERVER_CANDIDATES.get(lang, [])]


def _find_exe(name: str) -> str | None:
    """Resolve one executable: absolute paths are used as-is, anything else
    goes through shutil.which (PATH + PATHEXT on Windows)."""
    p = Path(name)
    if p.is_absolute():
        return str(p) if p.exists() else None
    return shutil.which(name)


def _wrap_windows_shell(argv: list[str]) -> list[str]:
    """Windows cannot exec .cmd/.bat directly - and npm-installed language
    servers are almost always .cmd shims. Wrap them in `cmd /d /c` with
    metacharacter-safe quoting, mirroring infra/mcp_client.ServerSpec.argv
    (2026-10-31)."""
    if sys.platform != "win32":
        return argv
    exe = argv[0]
    if not exe.lower().endswith((".cmd", ".bat")):
        return argv
    _META = set('&|<>^%!"()')
    args = []
    for arg in argv[1:]:
        if any(c in _META or c.isspace() for c in arg):
            # cmd.exe strips quote pairs and does not know the backslash
            # escape - double the quotes (see mcp_client audit C13)
            arg = '"' + arg.replace('"', '""') + '"'
        args.append(arg)
    return ["cmd", "/d", "/c", exe, *args]


def resolve(language_id: str, override=None) -> list[str] | None:
    """An exec-ready argv for a language's server, or None when no command
    was found. The argv[0] is which()-resolved to an absolute path, so the
    caller can start it without a shell."""
    lang = str(language_id).strip().lower()
    ov = normalize_override(override)
    if lang in ov:
        argv = ov[lang]
        exe = _find_exe(argv[0])
        if exe is None:
            # the override REPLACES the defaults: an explicit config that
            # does not resolve must be reported as missing, not silently
            # replaced by a different server (2026-10-31)
            logger.info("[LSP] configured server for %s not found: %s",
                        lang, argv[0])
            return None
        return _wrap_windows_shell([exe, *argv[1:]])
    for cand in SERVER_CANDIDATES.get(lang, []):
        exe = _find_exe(cand[0])
        if exe is not None:
            return _wrap_windows_shell([exe, *cand[1:]])
    return None


def _is_cmd_wrapped(argv: list[str]) -> bool:
    return (sys.platform == "win32" and len(argv) >= 4
            and argv[0] == "cmd" and argv[1] == "/d" and argv[2] == "/c")


def available(override=None) -> list[dict]:
    """The status view for one tool: every known language (plus any
    override-only languages) with the command that would run and whether it
    was found. Never starts a process."""
    ov = normalize_override(override)
    out: list[dict] = []
    for lang in sorted(set(SERVER_CANDIDATES) | set(ov)):
        resolved = resolve(lang, ov)
        if resolved is not None:
            # display the logical command, not the cmd /d /c wrapper
            display = resolved[3:] if _is_cmd_wrapped(resolved) else resolved
            out.append({"language": lang, "command": " ".join(display),
                        "found": True})
            continue
        # what the status shows for a missing server: the override argv if
        # one was configured, else the first default candidate
        fallback = ([ov[lang]] if lang in ov
                    else [list(c) for c in SERVER_CANDIDATES.get(lang, [])])
        command = " ".join(fallback[0]) if fallback else "(no candidates)"
        out.append({"language": lang, "command": command, "found": False})
    return out

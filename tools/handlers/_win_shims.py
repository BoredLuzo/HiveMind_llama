"""Windows POSIX-path, drive, and shell shim helpers for run_bash."""
from __future__ import annotations

import os
import re
import sys
import tempfile

def _tail_slice(text: str, budget: int) -> str:
    """Return at most *budget* trailing chars, aligned cleanly on a newline when nearby."""
    if len(text) <= budget:
        return text
    raw = text[-budget:]
    nl = raw.find("\n")
    return ("...\n" + raw[nl + 1:]) if 0 <= nl < 200 else ("..." + raw)


_WIN_BASH_VENV_PATH_RE = re.compile(
    r"(?:^|(?<=[\s;&|(]))(\.?\\?(?:\.venv|venv)\\Scripts\\[\w.\-]+)",
    re.IGNORECASE,
)


def _normalize_win_bash_venv_paths(cmd: str) -> str:
    """Convert unquoted Windows backslash .venv\\Scripts\\... paths for Git Bash."""
    return _WIN_BASH_VENV_PATH_RE.sub(lambda m: m.group(1).replace("\\", "/"), str(cmd or ""))


_POSIX_DRIVE_PATH_RE = re.compile(
    r'(?<![^\s=:\x27"])/([a-zA-Z])/(?=[A-Za-z0-9._~-]{2}|[A-Za-z0-9._~-]/[A-Za-z0-9._~-])'
)
_SEARCH_OR_AWK_VERBS = frozenset({"sed", "awk", "gawk", "mawk", "grep", "egrep", "fgrep", "rg"})
_SED_AWK_VERBS = frozenset({"sed", "awk", "gawk", "mawk"})
_SEARCH_NO_MATCH_VERBS = frozenset({"grep", "egrep", "fgrep", "rg"})
_WIN_NATIVE_DEV_NULL_VERBS = frozenset({"curl", "wget", "ffmpeg", "ffprobe", "openssl", "ssh", "scp"})


def _extract_cmd_verb(segment: str) -> str:
    """Return lowercased base command verb (without .exe) from a shell segment."""
    for raw_tok in str(segment or "").lstrip().split():
        tok = raw_tok.strip("'\"")
        if not tok:
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok) or tok == "env":
            continue
        base = tok.replace("\\", "/").rsplit("/", 1)[-1].lower()
        return base[:-4] if base.endswith(".exe") else base
    return ""


def _command_verb_before(cmd: str, pos: int) -> tuple[str, str | None, bool, bool]:
    """Return (command_verb, open_quote_char, inside_slash_prefixed_quote, in_path_assign)."""
    quote: str | None = None
    quote_start = -1
    seg_start = 0
    i = 0
    while i < pos:
        ch = cmd[i]
        if quote is None:
            if ch == "\\" and i + 1 < pos:
                i += 2
                continue
            if ch in ("'", '"'):
                quote = ch
                quote_start = i
            elif ch in "|&;\n\r(":
                seg_start = i + 1
        elif quote == "'":
            if ch == "'":
                quote = None
                quote_start = -1
        else:
            if ch == "\\" and i + 1 < pos:
                i += 2
                continue
            if ch == '"':
                quote = None
                quote_start = -1
        i += 1

    inside_slash_quote = (
        quote is not None
        and pos > quote_start + 1
        and "/" in cmd[quote_start + 1:pos]
    )
    in_path_assign = bool(
        re.search(r"(?:^|[\s;|&(])PATH=['\"]?(?:[^'\"\s;:]+:)*$", cmd[seg_start:pos])
    )
    verb = _extract_cmd_verb(cmd[seg_start:pos])
    return verb, quote, inside_slash_quote, in_path_assign


def _should_rewrite_posix_drive(
    cmd: str, m: re.Match, existing_drives: set[str] | None = None
) -> bool:
    """Return True only when *m* is a genuine MSYS/Git-Bash drive mount path."""
    drv = m.group(1).upper()
    if existing_drives is not None:
        if drv.lower() not in {d.lower() for d in existing_drives}:
            return False
    elif sys.platform == "win32":
        if not os.path.isdir(f"{drv}:/"):
            return False

    verb, open_quote, inside_slash_quote, in_path_assign = _command_verb_before(cmd, m.start())
    if inside_slash_quote or in_path_assign:
        return False

    if open_quote is not None:
        close_q = cmd.find(open_quote, m.end())
        tail = cmd[m.end():close_q] if close_q != -1 else cmd[m.end():]
        seg = tail.split("/", 1)[0].strip()
    else:
        seg_m = re.match(r"([A-Za-z0-9._~-]+)", cmd[m.end():])
        seg = seg_m.group(1) if seg_m else ""
    if not seg:
        return False

    if verb in _SED_AWK_VERBS and open_quote is not None:
        close_q = cmd.find(open_quote, m.end())
        if close_q != -1:
            quoted_tail = cmd[m.end():close_q]
            if re.search(r"/(?:[dpg!]|s/|$)", quoted_tail):
                if sys.platform != "win32" or not os.path.exists(f"{drv}:/{quoted_tail}"):
                    return False

    if existing_drives is None and sys.platform == "win32":
        if not os.path.exists(f"{drv}:/{seg}"):
            return False
    return True


def _rewrite_posix_drive_paths(cmd: str, existing_drives: set[str] | None = None) -> str:
    """Rewrite genuine `/c/Users/...` POSIX drive mounts to `C:/Users/...`."""
    s = str(cmd or "")
    return _POSIX_DRIVE_PATH_RE.sub(
        lambda _m: (_m.group(1).upper() + ":/")
        if _should_rewrite_posix_drive(s, _m, existing_drives=existing_drives)
        else _m.group(0),
        s,
    )


_POSIX_TMP_PATH_RE = re.compile(r'(?<![^\s=:\x27"])/tmp/([A-Za-z0-9._~/-]+)?')
_WIN_NATIVE_DEV_NULL_RE = re.compile(
    r'(?:(?<=[\s=:\x27"])|(?<=-[oODcbKF])|^)/dev/null(?=$|[\s;|&)\x27"])'
)


def _rewrite_win_tmp_paths(cmd: str) -> str:
    """Rewrite `/tmp/<name>` to the Windows temp directory (forward slashes)."""
    s = str(cmd or "")
    win_tmp = tempfile.gettempdir().replace("\\", "/").rstrip("/") + "/"

    def _repl(m: re.Match) -> str:
        verb, open_quote, inside_slash_quote, in_path_assign = _command_verb_before(s, m.start())
        if inside_slash_quote or in_path_assign or verb in _SED_AWK_VERBS:
            return m.group(0)
        suffix = m.group(1) or ""
        if not suffix:
            return m.group(0)
        if " " in win_tmp and open_quote is None:
            return f'"{win_tmp}{suffix}"'
        return win_tmp + suffix

    return _POSIX_TMP_PATH_RE.sub(_repl, s)


def _rewrite_win_native_dev_null(cmd: str) -> str:
    """Rewrite `/dev/null` to `NUL` when passed as a path arg to native Windows tools."""
    s = str(cmd or "")

    def _repl(m: re.Match) -> str:
        if s[:m.start()].rstrip().endswith((">", "<")):
            return m.group(0)
        verb, _, inside_slash_quote, in_path_assign = _command_verb_before(s, m.start())
        if inside_slash_quote or in_path_assign or verb not in _WIN_NATIVE_DEV_NULL_VERBS:
            return m.group(0)
        return "NUL"

    return _WIN_NATIVE_DEV_NULL_RE.sub(_repl, s)


def _split_shim_args_quoted(raw: str) -> list[tuple[str, bool]]:
    """Tokenize Windows shim arguments as ``(token, was_quoted)`` pairs."""
    s = str(raw or "")
    out: list[tuple[str, bool]] = []
    cur: list[str] = []
    in_tok = False
    was_quoted = False
    quote: str | None = None
    for ch in s:
        if quote is None:
            if ch.isspace():
                if in_tok:
                    out.append(("".join(cur), was_quoted))
                    cur.clear()
                    in_tok = False
                    was_quoted = False
                continue
            in_tok = True
            if ch in ("'", '"'):
                quote = ch
                was_quoted = True
                continue
            cur.append(ch)
        elif ch == quote:
            quote = None
        else:
            cur.append(ch)
    if in_tok or cur:
        out.append(("".join(cur), was_quoted))
    return out


def _split_shim_args(raw: str) -> list[str]:
    """Tokenize Windows shim arguments supporting quote concatenation (e.g. `"a b".bak`)."""
    return [tok for tok, _ in _split_shim_args_quoted(raw)]


def _parse_shim_tokens_quoted(
    tokens: list[tuple[str, bool]],
    *,
    value_flags: tuple[str, ...] = (),
) -> tuple[list[str], list[tuple[str, bool]]]:
    """Split ``(token, was_quoted)`` pairs into ``(flags, operands)`` honoring `--`."""
    flags: list[str] = []
    operands: list[tuple[str, bool]] = []
    i = 0
    after_ddash = False
    while i < len(tokens):
        tok, was_q = tokens[i]
        if not after_ddash and tok == "--":
            after_ddash = True
            i += 1
        elif not after_ddash and not was_q and tok.startswith("-") and len(tok) > 1:
            flags.append(tok)
            if tok in value_flags and i + 1 < len(tokens):
                i += 2
            else:
                i += 1
        else:
            operands.append((tok, was_q))
            i += 1
    return flags, operands


def _parse_shim_tokens(
    tokens: list[str], *, value_flags: tuple[str, ...] = ()
) -> tuple[list[str], list[str]]:
    """Split shim tokens into (flags, operands), honoring `--` and value-taking flags."""
    flags, q_ops = _parse_shim_tokens_quoted(
        [(tok, False) for tok in tokens], value_flags=value_flags,
    )
    return flags, [tok for tok, _ in q_ops]


def _iter_ps_unquoted(cmd: str):
    """Yield ``(index, char)`` outside quotes (honoring PowerShell ``''`` & `` ` `` escapes)."""
    quote: str | None = None
    i = 0
    n = len(cmd)
    while i < n:
        ch = cmd[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
                i += 1
                continue
            yield i, ch
            i += 1
        elif quote == "'":
            if ch == "'" and i + 1 < n and cmd[i + 1] == "'":
                i += 2
                continue
            if ch == "'":
                quote = None
            i += 1
        else:
            if ch == "`" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1


def _split_unquoted_and(cmd: str) -> list[str]:
    """Split *cmd* on unquoted `&&` operators (PowerShell quoting: `` ` `` & `''`)."""
    parts: list[str] = []
    start = 0
    skip_until = -1
    for i, _ch in _iter_ps_unquoted(cmd):
        if i < skip_until:
            continue
        if cmd.startswith("&&", i):
            parts.append(cmd[start:i])
            start = i + 2
            skip_until = i + 2
    parts.append(cmd[start:])
    return parts


def _has_unquoted_token(cmd: str, token: str) -> bool:
    """Return True if *token* appears outside single/double quotes in *cmd* (PS escapes)."""
    if not token:
        return True
    return any(cmd.startswith(token, i) for i, _ in _iter_ps_unquoted(cmd))


def _format_timeout_partial(stdout_raw: str, stderr_raw: str) -> tuple[str, str, str]:
    """Render bounded partial stdout/stderr snippet for timeout errors."""
    partial_out = (stdout_raw or "").strip("\r\n")
    partial_err = (stderr_raw or "").strip("\r\n")
    parts: list[str] = []
    if partial_err:
        parts.append(f"[stderr]\n{_tail_slice(partial_err, 600)}")
    if partial_out:
        parts.append(f"[stdout]\n{_tail_slice(partial_out, 800)}")
    block = "\n\n[partial output before timeout]\n" + "\n---\n".join(parts) if parts else ""
    return partial_out, partial_err, block


def _build_win_shell_cmd(
    cmd: str, *, mode: str, win_bash: str = "", orig_cmd: str | None = None
) -> list[str]:
    """Build Windows Git-Bash or PowerShell argv with TMPDIR/UTF-8 and pip/python3 shims."""
    probe = orig_cmd if orig_cmd is not None else cmd
    needs_pip = bool(re.search(r"(?:^|[\s;&|(])pip3?\b", probe))
    needs_py3 = bool(re.search(r"(?:^|[\s;&|(])python3\b", probe))
    if mode == "bash" and win_bash:
        tmp_fwd = tempfile.gettempdir().replace("\\", "/")
        shims = (
            f'export TMPDIR="{tmp_fwd}"; '
            + ('pip() { python -m pip "$@"; }; pip3() { python -m pip "$@"; }; ' if needs_pip else "")
            + ('python3() { python "$@"; }; ' if needs_py3 else "")
        )
        return [win_bash, "-c", shims + cmd]
    ps_shims = (
        ("function pip { python -m pip @args }; function pip3 { python -m pip @args }; " if needs_pip else "")
        + ("function python3 { python @args }; " if needs_py3 else "")
    )
    ps_cmd = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + ps_shims + cmd
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd]

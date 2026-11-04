"""Workspace-scoped file delivery guards for Telegram sendDocument.

Both the engine tool (`send_file`) and the gateway command (`/send`) use
these helpers. The rules are deliberately boring and strict:

* the resolved file must live inside the workspace (symlinks included);
* it must be a regular file and at most 50 MB (Bot API document limit);
* obvious secret files are refused by name, and the gateway's central
  send() still runs its text redaction over UTF-8 documents.

No caller may pass an arbitrary path to Telegram.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Telegram Bot API: bots may send documents up to 50 MB.
MAX_SEND_BYTES = 50 * 1024 * 1024

_SECRET_EXACT = {
    ".env", ".env.local", ".env.production", ".env.development",
    "settings.json", "gateway_state.json", "secrets.json",
    "credentials.json", "auth.json", "id_rsa", "id_ed25519",
    ".git-credentials", ".npmrc", ".pypirc", ".netrc",
}
_SECRET_SUFFIXES = (
    ".pem", ".key", ".pfx", ".p12", ".keystore", ".jks", ".kdbx",
)
_SECRET_HINTS = (
    "secret", "token", "password", "passwd", "credential",
    "api_key", "apikey", "private_key", "access_key",
)
_BLOCKED_DIRS = {".git", ".ssh", ".aws", ".gnupg"}


class SendFileError(ValueError):
    """A file may not be delivered; the message is user/model facing."""


def _normalise_raw(raw: str) -> str:
    raw = str(raw or "").strip().strip('"').strip("'")
    # Small models emit MSYS-style "/C:/..." paths; Path() would treat that
    # as a root path. The containment check still runs afterwards.
    if re.match(r"^/[A-Za-z]:[/\\]", raw):
        raw = raw[1:]
    return raw


def canonical_workspace_path(path: str | Path,
                             workspace: str | Path | None = None) -> str:
    """One canonical spelling for a workspace path.

    Returns the resolved absolute path with forward slashes, lowercased on
    Windows, or "" when the path cannot be resolved. Relative paths are
    joined onto `workspace` when one is given; the same quote/MSYS fixes as
    `_normalise_raw` are applied first. This is pure normalisation so that
    equivalent spellings of the same file compare equal - it enforces
    nothing by itself, callers keep their guards.
    """
    text = _normalise_raw(path)
    if not text:
        return ""
    try:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute() and workspace:
            candidate = Path(workspace) / candidate
        target = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return ""
    out = str(target).replace("\\", "/")
    return out.lower() if os.name == "nt" else out


def _is_secret_path(p: Path) -> bool:
    name = p.name.rstrip(". ").lower()
    if name in _SECRET_EXACT:
        return True
    if name.startswith(".env."):
        return True
    if p.suffix.lower() in _SECRET_SUFFIXES:
        return True
    if any(h in name for h in _SECRET_HINTS):
        return True
    return any(part.lower() in _BLOCKED_DIRS for part in p.parts)


def resolve_workspace_file(workspace: str | Path, raw: str) -> Path:
    """Resolve `raw` under `workspace` or raise SendFileError."""
    ws_raw = str(workspace or "").strip()
    if not ws_raw:
        raise SendFileError("No workspace is set for this chat.")
    try:
        ws = Path(ws_raw).expanduser().resolve()
    except (OSError, RuntimeError):
        raise SendFileError("The workspace path is not usable.") from None
    if not ws.is_dir():
        raise SendFileError("The workspace folder does not exist.")

    text = _normalise_raw(raw)
    if not text:
        raise SendFileError("No file path was given.")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = ws / candidate
    try:
        target = candidate.resolve()
    except (OSError, RuntimeError):
        raise SendFileError("The file path cannot be resolved.") from None
    try:
        target.relative_to(ws)
    except ValueError:
        raise SendFileError(
            "Only files inside the workspace can be sent.") from None
    if _is_secret_path(target):
        raise SendFileError(
            "That file looks like a secret or credential and is blocked.")
    if not target.is_file():
        raise SendFileError("The file does not exist (or is not a file).")
    try:
        size = target.stat().st_size
    except OSError:
        raise SendFileError("The file cannot be read.") from None
    if size > MAX_SEND_BYTES:
        raise SendFileError(
            f"The file is too large ({size} bytes); Telegram allows at most "
            f"{MAX_SEND_BYTES} bytes.")
    return target


def read_sendable(workspace: str | Path, raw: str) -> tuple[bytes, str]:
    """Return (bytes, safe filename) for a workspace file.

    Reads at most MAX_SEND_BYTES + 1 so a file that grows after the stat
    check cannot slip past the limit.
    """
    target = resolve_workspace_file(workspace, raw)
    try:
        with target.open("rb") as fh:
            data = fh.read(MAX_SEND_BYTES + 1)
    except OSError:
        raise SendFileError("The file cannot be read.") from None
    if len(data) > MAX_SEND_BYTES:
        raise SendFileError(
            f"The file grew beyond the {MAX_SEND_BYTES}-byte send limit.")
    name = target.name
    # Telegram uses the name verbatim; strip control characters and any
    # accidental separators, keep it human-readable.
    name = re.sub(r"[\x00-\x1f\x7f/\\]+", "_", name).strip() or "file"
    return data, name[:120]


_DELIVERABLE_SUFFIXES = {
    ".html", ".htm", ".pdf", ".md", ".txt", ".csv",
    ".png", ".jpg", ".jpeg", ".webp", ".svg",
    ".docx", ".xlsx", ".pptx",
}


def choose_deliverables(pending: list[str] | None,
                        written: list[str] | None,
                        workspace: str | Path | None = None) -> list[str]:
    """Files a completed run should deliver, in order.

    Explicit `send_file` paths win. If the model queued none, exactly one
    deliverable-looking file written by the run is sent automatically; with
    several candidates the owner decides manually (no guessing). Paths are
    deduped via `canonical_workspace_path`, so two spellings of the same
    file queue once; the first spelling is kept for delivery.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in pending or []:
        p = str(raw or "").strip()
        if not p:
            continue
        key = canonical_workspace_path(p, workspace)
        if key:
            if key in seen:
                continue
            seen.add(key)
        elif p in out:
            # Unresolvable spelling: fall back to exact-string dedupe.
            continue
        out.append(p)
    if out:
        return out
    candidates: list[str] = []
    cand_seen: set[str] = set()
    for raw in written or []:
        p = str(raw or "").strip()
        if not p:
            continue
        try:
            suffix = Path(p).suffix.lower()
        except (TypeError, ValueError):
            continue
        if suffix not in _DELIVERABLE_SUFFIXES:
            continue
        key = canonical_workspace_path(p, workspace)
        if key:
            if key in cand_seen:
                continue
            cand_seen.add(key)
        elif p in candidates:
            continue
        candidates.append(p)
    return candidates if len(candidates) == 1 else []


def is_written_file(path: str | Path, workspace: str | Path,
                    written: set[str] | None = None,
                    run_started_at: float | None = None) -> bool:
    """True when `path` is one of the files this run wrote.

    `written` holds canonical workspace paths (the exact form
    `canonical_workspace_path` returns, which tools.runner registers).
    An empty/None set means the run has no write record, so the tool must
    refuse rather than guess. `run_started_at` additionally accepts a
    workspace file whose mtime falls inside this run - files produced by
    run_python/run_bash never pass the explicit write tools.
    """
    key = canonical_workspace_path(path, workspace)
    ws_key = canonical_workspace_path(workspace, workspace)
    if not key or not ws_key:
        return False
    # Containment stays mandatory: a path outside the workspace can never
    # count as run-written, not even via the mtime fallback below.
    prefix = ws_key if ws_key.endswith("/") else ws_key + "/"
    if not key.startswith(prefix):
        return False
    if written and key in written:
        return True
    if run_started_at is not None:
        try:
            st = Path(key).stat()
            start = float(run_started_at)
            if st.st_mtime < start:
                return False
            # Windows: st_ctime is the creation time. A file that existed
            # before the run (e.g. touched by an editor or another process)
            # must not become deliverable via the mtime fallback.
            if os.name == "nt" and st.st_ctime < start:
                return False
            return True
        except (OSError, TypeError, ValueError):
            return False
    return False

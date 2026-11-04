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
                        written: list[str] | None) -> list[str]:
    """Files a completed run should deliver, in order.

    Explicit `send_file` paths win. If the model queued none, exactly one
    deliverable-looking file written by the run is sent automatically; with
    several candidates the owner decides manually (no guessing).
    """
    out: list[str] = []
    for raw in pending or []:
        p = str(raw or "").strip()
        if p and p not in out:
            out.append(p)
    if out:
        return out
    candidates: list[str] = []
    for raw in written or []:
        p = str(raw or "").strip()
        if not p:
            continue
        try:
            suffix = Path(p).suffix.lower()
        except (TypeError, ValueError):
            continue
        if suffix in _DELIVERABLE_SUFFIXES and p not in candidates:
            candidates.append(p)
    return candidates if len(candidates) == 1 else []


def is_written_file(path: str | Path, workspace: str | Path,
                    written: set[str] | None = None) -> bool:
    """True when `path` is one of the files this run wrote.

    `written` holds normalised workspace-relative paths (the same form
    tools.runner uses). An empty/None set means the run has no write
    record, so the tool must refuse rather than guess.
    """
    if not written:
        return False
    try:
        target = Path(path).resolve()
        ws = Path(workspace).resolve()
        target.relative_to(ws)
    except (OSError, RuntimeError, ValueError):
        return False
    norm = str(target).replace("\\", "/")
    if os.name == "nt":
        norm = norm.lower()
    return norm in written

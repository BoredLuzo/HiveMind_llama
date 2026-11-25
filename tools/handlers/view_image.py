"""view_image: inspect a local image file in the workspace visually."""
from __future__ import annotations

import asyncio
import base64
import contextvars
import struct
from pathlib import Path

from tools.errors import tool_error_response as _tool_error_response
from tools.handlers._shared import coder_vision_truth
from utils.file import _inline_check_workspace, _inline_resolve_path

_VIEW_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"})
_VIEW_IMAGE_MAX_FILE_BYTES = 10 * 1024 * 1024

_pending_view_images: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
    "_pending_view_images", default=None
)


def reset_pending_view_images() -> contextvars.Token:
    """Initialize a fresh per-round view_image queue and return the reset token."""
    return _pending_view_images.set([])


def queue_viewed_image(item: dict) -> None:
    """Append one encoded image item to the active round queue."""
    cur = _pending_view_images.get(None)
    if cur is None:
        cur = []
        _pending_view_images.set(cur)
    cur.append(item)


def drain_viewed_images() -> list[dict]:
    """Return and clear all queued view_image items from the active round."""
    cur = _pending_view_images.get(None)
    if not cur:
        return []
    out = list(cur)
    cur.clear()
    return out


def _jpeg_dimensions_stdlib(raw: bytes) -> tuple[int, int]:
    """Extract (width, height) from JPEG SOF markers using pure stdlib."""
    pos = 2
    n = len(raw)
    while pos + 4 <= n:
        if raw[pos] != 0xFF:
            pos += 1
            continue
        while pos < n and raw[pos] == 0xFF:
            pos += 1
        if pos >= n:
            break
        marker = raw[pos]
        pos += 1
        if marker in (0xD8, 0xD9) or (0xD0 <= marker <= 0xD7) or marker == 0x01:
            continue
        if marker == 0xDA:  # Start of Scan — headers finished
            break
        if pos + 2 > n:
            break
        seg_len = struct.unpack(">H", raw[pos : pos + 2])[0]
        if seg_len < 2 or pos + seg_len > n:
            break
        if (0xC0 <= marker <= 0xCF) and marker not in (0xC4, 0xC8, 0xCC):
            if pos + 7 <= n:
                h, w = struct.unpack(">HH", raw[pos + 3 : pos + 7])
                return int(w), int(h)
            break
        pos += seg_len
    return 0, 0


def _sniff_image_stdlib(raw: bytes) -> tuple[str, str, int, int]:
    """Identify image MIME/format and extract dimensions without Pillow."""
    if raw.startswith(b"\x89PNG\r\n\x1a\n") and len(raw) >= 24 and raw[12:16] == b"IHDR":
        w, h = struct.unpack(">II", raw[16:24])
        return "image/png", "PNG", int(w), int(h)

    if raw.startswith(b"\xff\xd8") and len(raw) >= 4:
        w, h = _jpeg_dimensions_stdlib(raw)
        return "image/jpeg", "JPEG", w, h

    if raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) >= 10:
        w, h = struct.unpack("<HH", raw[6:10])
        return "image/gif", "GIF", int(w), int(h)

    if len(raw) >= 16 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        chunk = raw[12:16]
        w, h = 0, 0
        if chunk == b"VP8X" and len(raw) >= 30:
            w = 1 + int.from_bytes(raw[24:27], "little")
            h = 1 + int.from_bytes(raw[27:30], "little")
        elif chunk == b"VP8L" and len(raw) >= 25:
            bits = struct.unpack("<I", raw[21:25])[0]
            w = (bits & 0x3FFF) + 1
            h = ((bits >> 14) & 0x3FFF) + 1
        elif chunk == b"VP8 " and len(raw) >= 30 and raw[23:26] == b"\x9d\x01\x2a":
            w_raw, h_raw = struct.unpack("<HH", raw[26:30])
            w = w_raw & 0x3FFF
            h = h_raw & 0x3FFF
        return "image/webp", "WEBP", int(w), int(h)

    if len(raw) >= 26 and raw[:2] == b"BM":
        w, h = struct.unpack("<ii", raw[18:26])
        return "image/bmp", "BMP", abs(int(w)), abs(int(h))

    raise ValueError("unrecognized or corrupt image header")


def _encode_image_for_vision(
    target: Path,
) -> tuple[str, int, int, str, int]:
    """Validate and base64-encode an image file using pure Python stdlib."""
    raw = target.read_bytes()
    if len(raw) > _VIEW_IMAGE_MAX_FILE_BYTES:
        mb = round(len(raw) / (1024 * 1024), 1)
        raise ValueError(f"image exceeds 10.0 MB limit ({mb} MB)")
    mime, fmt_label, w, h = _sniff_image_stdlib(raw)
    b64 = base64.b64encode(raw).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    return data_url, w, h, fmt_label, len(raw)


async def _inline_tool_view_image(
    args: dict, workspace: Path, workspace_lock: str | None
) -> str:
    """Validate and queue a local workspace image for multimodal follow-up injection."""
    if coder_vision_truth() is False:
        return _tool_error_response(
            "VIEW_IMAGE_UNAVAILABLE",
            "The active model slot is running without vision/mmproj and cannot view images.",
            tool="view_image",
        )

    raw = str((args or {}).get("path") or "").strip()
    if not raw:
        return _tool_error_response(
            "INVALID_ARGUMENT",
            "view_image requires a non-empty 'path' argument.",
            tool="view_image",
        )

    target = _inline_resolve_path(workspace, raw)
    if _ws_err := _inline_check_workspace(target, workspace_lock, "view_image"):
        return _ws_err

    if not target.exists() or not target.is_file():
        return _tool_error_response(
            "FILE_NOT_FOUND",
            f"Image file not found: {raw}",
            tool="view_image",
            details={"path": raw[:300]},
        )

    if target.suffix.lower() not in _VIEW_IMAGE_EXTS:
        return _tool_error_response(
            "VIEW_IMAGE_INVALID_FORMAT",
            f"Unsupported image extension '{target.suffix or '(none)'}'. Supported: {', '.join(sorted(_VIEW_IMAGE_EXTS))}.",
            tool="view_image",
            details={"path": raw[:300]},
        )

    try:
        file_size = target.stat().st_size
    except OSError as exc:
        return _tool_error_response(
            "FILE_UNREADABLE",
            f"Cannot stat image file '{raw}': {exc}",
            tool="view_image",
            details={"path": raw[:300]},
        )

    if file_size <= 0:
        return _tool_error_response(
            "VIEW_IMAGE_INVALID_FORMAT",
            f"Image file '{raw}' is empty (0 bytes).",
            tool="view_image",
            details={"path": raw[:300]},
        )
    if file_size > _VIEW_IMAGE_MAX_FILE_BYTES:
        mb = round(file_size / (1024 * 1024), 1)
        return _tool_error_response(
            "VIEW_IMAGE_TOO_LARGE",
            f"Image file '{raw}' is {mb} MB (max 10.0 MB).",
            tool="view_image",
            details={"path": raw[:300], "bytes": file_size},
        )

    try:
        (
            data_url,
            out_w,
            out_h,
            fmt_label,
            enc_bytes,
        ) = await asyncio.to_thread(_encode_image_for_vision, target)
    except (OSError, ValueError, EOFError) as exc:
        return _tool_error_response(
            "VIEW_IMAGE_INVALID_FORMAT",
            f"Could not decode image '{raw}': {exc}",
            tool="view_image",
            details={"path": raw[:300]},
        )

    try:
        display_path = target.resolve().relative_to(Path(workspace).resolve()).as_posix()
    except (OSError, ValueError):
        display_path = target.name

    queue_viewed_image(
        {
            "path": display_path,
            "data_url": data_url,
            "width": out_w,
            "height": out_h,
            "format": fmt_label,
            "bytes": enc_bytes,
        }
    )

    dims_note = f"{out_w}x{out_h}" if (out_w > 0 and out_h > 0) else "unknown size"
    kb = max(1, int(round(enc_bytes / 1024.0)))
    return (
        f"[view_image] Loaded '{display_path}' ({dims_note}, "
        f"{kb} KB {fmt_label}). The image is attached immediately after this "
        "round's tool results — inspect it visually."
    )

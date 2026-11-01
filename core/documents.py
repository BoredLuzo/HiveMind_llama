# -*- coding: utf-8 -*-
"""Document text extraction for phone/desktop uploads (2026-11-01).

A PDF dropped into Telegram used to be rejected ("Text, voice notes and images
only"). The gateway now downloads it and posts the bytes to /documents/extract;
this module turns them into plain text.

Design notes
------------
- pypdf is a pure-Python dependency (no binaries, ~300 KB). It is optional at
  runtime: a fresh install that has not re-run the dependency step answers
  DOC_UNSUPPORTED instead of crashing the engine.
- Scanned PDFs carry no text layer. We do NOT rasterise pages here (that needs
  a PDF renderer); the caller tells the user to send photos instead.
- Extracted text is capped twice: per page count and in characters, and the
  result says when it was cut, so a model never mistakes a truncated document
  for a complete one.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("hivemind.documents")

# Hard caps - a 400-page manual must not become a 3 MB prompt.
DEFAULT_MAX_PAGES = 40
DEFAULT_MAX_CHARS = 120_000
# A page with less text than this counts as "no text layer" for that page
# (headers/footers still produce a few characters).
_MIN_PAGE_CHARS = 20


class DocumentError(RuntimeError):
    """Carries a code the caller can translate for the user."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code

    def as_dict(self) -> dict:
        return {"code": self.code, "error": self.message}


def _open_pdf(raw: bytes):
    from io import BytesIO

    from pypdf import PdfReader  # imported lazily: optional dependency
    return PdfReader(BytesIO(raw))


def extract_pdf_text(raw: bytes, *, max_pages: int = DEFAULT_MAX_PAGES,
                     max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """PDF bytes -> {text, pages, pages_read, truncated, empty_pages}.

    Raises DocumentError with DOC_* codes. Never raises a library exception.
    """
    if not raw:
        raise DocumentError("DOC_EMPTY", "the document arrived empty")
    # pypdf logs "invalid pdf header" for every junk upload - at warning level
    # that spams the engine log on each rejected file. Errors only.
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        reader = _open_pdf(raw)
    except ModuleNotFoundError:
        raise DocumentError(
            "DOC_UNSUPPORTED",
            "PDF support is not installed (pip install pypdf)",
        ) from None
    except Exception as exc:                      # noqa: BLE001 - pypdf raises many types
        logger.info("[DOC] not a readable PDF: %s", exc)
        raise DocumentError("DOC_UNREADABLE",
                            "the file is not a readable PDF") from None

    try:
        total = len(reader.pages)
    except Exception:                             # noqa: BLE001
        total = 0
    if total <= 0:
        raise DocumentError("DOC_UNREADABLE", "the PDF has no pages")

    limit = max(1, min(int(max_pages or DEFAULT_MAX_PAGES), total))
    chunks: list[str] = []
    empty_pages = 0
    used = 0
    truncated = False
    for idx in range(limit):
        try:
            page_text = reader.pages[idx].extract_text() or ""
        except Exception as exc:                  # noqa: BLE001 - one broken page
            logger.debug("[DOC] page %s unreadable: %s", idx + 1, exc)
            page_text = ""
        page_text = page_text.strip()
        if len(page_text) < _MIN_PAGE_CHARS:
            empty_pages += 1
        if not page_text:
            continue
        block = f"\n--- page {idx + 1} ---\n{page_text}"
        if used + len(block) > max_chars:
            room = max_chars - used
            if room > 200:
                chunks.append(block[:room])
                used = max_chars
            truncated = True
            break
        chunks.append(block)
        used += len(block)
    if limit < total:
        truncated = True

    text = "".join(chunks).strip()
    if not text:
        raise DocumentError(
            "DOC_NO_TEXT",
            "the PDF has no text layer (scanned?) - send the pages as photos",
        )
    return {
        "text": text,
        "pages": total,
        "pages_read": limit,
        "empty_pages": empty_pages,
        "truncated": truncated,
    }

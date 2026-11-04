# -*- coding: utf-8 -*-
"""Document uploads (2026-11-01): PDF text via /documents/extract.

Covers the extractor itself (real PDF bytes, no external fixture) and the
gateway wiring. Extraction needs pypdf; those checks skip when it is absent -
the engine answers DOC_UNSUPPORTED then, which is also verified.
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import documents as D  # noqa: E402

passed = failed = skipped = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def skip(name, why):
    global skipped
    skipped += 1
    print(f"  SKIP {name} ({why})")


HAS_PYPDF = importlib.util.find_spec("pypdf") is not None


def _pdf(text_lines, pages=1):
    """Minimal valid PDF (uncompressed) - no reporter library needed."""
    objs = ["1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj"]
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objs.append(f"2 0 obj<</Type/Pages/Kids[{kids}]/Count {pages}>>endobj")
    body = []
    for i in range(pages):
        content = ("BT /F1 12 Tf 40 760 Td 14 TL " +
                   " ".join(f"({ln}) Tj T*" for ln in text_lines) + " ET")
        body.append(f"{3 + i} 0 obj<</Type/Page/Parent 2 0 R"
                    f"/MediaBox[0 0 595 842]"
                    f"/Resources<</Font<</F1 {3 + pages} 0 R>>>>"
                    f"/Contents {3 + pages + 1 + i} 0 R>>endobj")
        body.append(f"{3 + pages + 1 + i} 0 obj<</Length {len(content)}>>"
                    f"stream\n{content}\nendstream endobj")
    objs.extend(body)
    objs.append(f"{3 + pages} 0 obj<</Type/Font/Subtype/Type1"
                f"/BaseFont/Helvetica>>endobj")
    out = "%PDF-1.4\n"
    offsets = []
    for o in objs:
        offsets.append(len(out))
        out += o + "\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n"
    out += (f"trailer<</Size {len(objs) + 1}/Root 1 0 R>>\n"
            f"startxref\n{xref}\n%%EOF\n")
    return out.encode("latin-1")


# ── 1. error mapping (no pypdf needed) ──────────────────────────────────────
try:
    D.extract_pdf_text(b"")
    check("1a empty input raises", False)
except D.DocumentError as exc:
    check("1a empty input -> DOC_EMPTY", exc.code == "DOC_EMPTY")
check("1b the error carries a user message",
      D.DocumentError("X", "text").as_dict()["error"] == "text")

if not HAS_PYPDF:
    skip("2 extraction", "pypdf not installed in this interpreter")
    try:
        D.extract_pdf_text(_pdf(["hello"]))
        check("2z without pypdf -> DOC_UNSUPPORTED", False)
    except D.DocumentError as exc:
        check("2z without pypdf -> DOC_UNSUPPORTED", exc.code == "DOC_UNSUPPORTED")
else:
    # ── 2. real extraction ─────────────────────────────────────────────────
    info = D.extract_pdf_text(_pdf(["HiveMind Testdokument", "Zeile zwei."]))
    check("2a one page is read", info["pages"] == 1 and info["pages_read"] == 1)
    check("2b the text layer is extracted",
          "HiveMind Testdokument" in info["text"] and "Zeile zwei" in info["text"])
    check("2c pages are marked", "--- page 1 ---" in info["text"])
    check("2d nothing was cut", info["truncated"] is False)

    # junk is a clean error, never a traceback (plain UTF-8 text is a
    # valid document now, so use real binary content here)
    try:
        D.extract_pdf_text(b"\x00\x01\x02not a pdf at all")
        check("2e junk -> DOC_UNREADABLE", False)
    except D.DocumentError as exc:
        check("2e junk -> DOC_UNREADABLE", exc.code == "DOC_UNREADABLE")

    # ── 3. caps ────────────────────────────────────────────────────────────
    big = D.extract_pdf_text(_pdf(["x" * 400]), max_chars=400)
    check("3a the char cap cuts the text", len(big["text"]) <= 420)
    check("3b the cut is announced", big["truncated"] is True)

    multi = D.extract_pdf_text(_pdf(["Seite mit Text."], pages=4), max_pages=2)
    check("3c the page cap limits pages_read", multi["pages_read"] == 2)
    check("3d ... and reports the full count", multi["pages"] == 4)
    check("3e ... and says it was cut", multi["truncated"] is True)

    # a page without a text layer (scanned PDF) -> honest error
    try:
        D.extract_pdf_text(_pdf(["ok"], pages=1).replace(b"(ok) Tj", b"() Tj"))
        check("3f empty text -> DOC_NO_TEXT", False)
    except D.DocumentError as exc:
        check("3f empty text -> DOC_NO_TEXT", exc.code == "DOC_NO_TEXT")

# ── 3g. plain-text uploads (no pypdf needed) ────────────────────────────────
_txt = D.extract_pdf_text("Hallo HiveMind\nZeile zwei".encode())
check("3g text file is accepted",
      "Hallo HiveMind" in _txt["text"] and _txt["pages"] == 1)
_utf16 = D.extract_pdf_text("Hallo UTF-16".encode("utf-16"))
check("3h UTF-16 text is decoded", "Hallo UTF-16" in _utf16["text"])
_latin = D.extract_pdf_text("Grüße aus Köln".encode("cp1252"))
check("3i legacy single-byte text is decoded", "Grüße" in _latin["text"])
_txt_cut = D.extract_pdf_text(("x" * 500).encode(), max_chars=100)
check("3j text files honour the char cap",
      len(_txt_cut["text"]) == 100 and _txt_cut["truncated"] is True)
try:
    D.extract_pdf_text(b"\x00\x01binary")
    check("3k binary without PDF header is refused", False)
except D.DocumentError as exc:
    check("3k binary without PDF header is refused", exc.code == "DOC_UNREADABLE")
try:
    D.extract_pdf_text(b"   \n\t  ")
    check("3l whitespace-only text -> DOC_EMPTY", False)
except D.DocumentError as exc:
    check("3l whitespace-only text -> DOC_EMPTY", exc.code == "DOC_EMPTY")

# ── 4. gateway wiring ───────────────────────────────────────────────────────
MAIN = (ROOT / "hivemind_gateway" / "main.py").read_text(encoding="utf-8")
AUTH = (ROOT / "hivemind_gateway" / "auth.py").read_text(encoding="utf-8")
CLIENT = (ROOT / "hivemind_gateway" / "hive_client.py").read_text(encoding="utf-8")
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")

check("4a the document branch exists", "p.document_file_id" in MAIN)
check("4b documents are parsed with mime + name",
      "document_file_id" in AUTH and "document_name" in AUTH)
check("4c pdf mime triggers the document path", '"application/pdf"' in AUTH)
check("4d text files are accepted too", '".txt"' in AUTH and '".md"' in AUTH)
check("4e the engine endpoint exists", '@app.post("/documents/extract")' in SERVER)
check("4f the client posts the raw body", "/documents/extract" in CLIENT)
check("4g scanned PDFs point at photos", "DOC_NO_TEXT" in MAIN)
check("4h a too-long document answers honestly",
      "more " in MAIN and "than one phone run accepts" in MAIN)
check("4i pypdf is declared in requirements and pyproject",
      "pypdf" in (ROOT / "requirements.txt").read_text(encoding="utf-8")
      and "pypdf" in (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
check("4j image and document are mutually exclusive",
      "if not _img_id:" in AUTH)
check("4k documents use the document limit, not the 4096-char message cap",
      "len(prompt) > self.cfg.document_limit_chars" in MAIN)
check("4l the document run carries the larger-limit flag",
      "document_prompt=True" in MAIN)
check("4m start_owner_run honors document_prompt",
      'getattr(p, "document_prompt", False)' in MAIN)
check("4n a document is not injected into an active run",
      "document was not injected" in MAIN)

print(f"\n=== Results: {passed} passed, {failed} failed, {skipped} skipped ===")
sys.exit(1 if failed else 0)

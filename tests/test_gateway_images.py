# -*- coding: utf-8 -*-
"""Telegram images (2026-11-01): a photo becomes a run with the caption.

The gateway rejected photos with "Text only in this build" while the engine's
/stream already accepted `images`. These checks pin the parsing (photo sizes,
image documents, caption as prompt) and the download/encode path guards.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hivemind_gateway import auth as A  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def _msg(**extra):
    base = {"message_id": 11, "date": 1700000000,
            "from": {"id": 7}, "chat": {"id": 7, "type": "private"}}
    base.update(extra)
    return {"update_id": 1, "message": base}


# ── 1. photo parsing ────────────────────────────────────────────────────────
p = A.parse_update(_msg(photo=[{"file_id": "small", "width": 90},
                               {"file_id": "big", "width": 1280}],
                        caption="was ist hier falsch?"))
check("1a the LARGEST photo size is taken", p and p.image_file_id == "big")
check("1b the caption becomes the prompt", p and p.text == "was ist hier falsch?")
check("1c jpeg is assumed for photos", p and p.image_mime == "image/jpeg")

p = A.parse_update(_msg(photo=[{"file_id": "only"}], caption=""))
check("1d photo without caption -> empty text (caller defaults)",
      p and p.image_file_id == "only" and p.text == "")

# ── 2. image documents ──────────────────────────────────────────────────────
p = A.parse_update(_msg(document={"file_id": "docpng", "mime_type": "image/png"},
                        caption="keep this"))
check("2a an image document is accepted", p and p.image_file_id == "docpng")
check("2b its mime type is kept", p and p.image_mime == "image/png")

p = A.parse_update(_msg(document={"file_id": "zippy", "mime_type": "application/zip"}))
check("2c a non-image document is NOT an image", p and p.image_file_id is None)
check("2d ... and its text stays empty", p and p.text == "")

# ── 3. no collision with voice / text ───────────────────────────────────────
p = A.parse_update(_msg(text="just text"))
check("3a plain text has no image", p and p.image_file_id is None)
p = A.parse_update(_msg(voice={"file_id": "v1", "duration": 3}))
check("3b voice notes keep voice semantics", p and p.voice_file_id == "v1"
      and p.image_file_id is None)
check("3c the frozen dataclass carries an images tuple",
      p is not None and p.images == ())

# ── 4. handler guards (static: the download path needs a live bot) ──────────
MAIN = (ROOT / "hivemind_gateway" / "main.py").read_text(encoding="utf-8")
check("4a the photo branch exists before the reject branch",
      MAIN.index("p.image_file_id") < MAIN.index("stickers and other files"))
check("4b the old text-only rejection is gone",
      '"Text only in this build (photos and "' not in MAIN)
check("4c the engine's 14 MB data-URL cap is enforced",
      "14_000_000" in MAIN)
check("4d an empty caption gets a default prompt", "Describe this image." in MAIN)
check("4e the setting wins over the gateway config",
      "_image_in_enabled" in MAIN and "telegram_image_in" in MAIN)
check("4f images are handed to the run", "images=(data_url,)" in MAIN)

CFG = (ROOT / "hivemind_gateway" / "config.py").read_text(encoding="utf-8")
check("4g image_in defaults to True", "image_in: bool = True" in CFG)
check("4h a byte cap exists", "image_max_bytes" in CFG)

BR = (ROOT / "hivemind_gateway" / "bridge.py").read_text(encoding="utf-8")
check("4i the bridge forwards images to hive.stream", "images=images" in BR)

ENG = (ROOT / "server.py").read_text(encoding="utf-8")
check("4j the engine still accepts images", 'images = body.get("images", [])' in ENG)
check("4k the engine keeps its own caps", "len(b) <= 14_000_000" in ENG and
      "if len(images) > 8:" in ENG)

# MEDIA-DISPATCH: the POLL path must route media to its handlers - it used to
# call start_owner_run, which only sees the caption (the image payload is
# filled by handle_image), so the model answered "I cannot see an image".
_dispatch = MAIN[MAIN.index("async def process_update"):]
_dispatch = _dispatch[:_dispatch.index("def ensure_enabled")]
check("4l the poll path routes photos to handle_image",
      "handle_image(p)" in _dispatch)
check("4m ... and documents to handle_document",
      "handle_document(p)" in _dispatch)
check("4n ... before the plain text run",
      _dispatch.index("handle_image(p)") < _dispatch.index("start_owner_run(p)"))
check("4o an open /command does not swallow a photo",
      "p.image_file_id or p.document_file_id" in MAIN)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)

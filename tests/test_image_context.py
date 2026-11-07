"""Chat image context: a text follow-up keeps the previous photo.

The transcript is text, so image bytes are stored per chat under
sessions/.images/<chat_id>/ and re-attached by chat_run on a text-only
direct turn. This suite pins the storage contract.

Run: python tests/test_image_context.py
"""
import base64
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import context.chat as C

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 32).decode()
JPG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"x" * 32).decode()


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        C.init_chat_context(td)
        C.save_chat_images("chat123", [PNG])
        got = C.load_chat_images("chat123")
        check("one image survives", len(got) == 1, f"({len(got)})")
        check("it is a png data URL",
              got and got[0].startswith("data:image/png;base64,"))
        raw = base64.b64decode(got[0].split(",", 1)[1])
        check("bytes round-trip", raw.startswith(b"\x89PNG"))

        C.save_chat_images("chat123", [PNG, JPG])
        got = C.load_chat_images("chat123")
        check("two images survive", len(got) == 2, f"({len(got)})")
        check("jpeg MIME is detected",
              any(x.startswith("data:image/jpeg") for x in got))

        C.save_chat_images("chat123", [JPG])
        got = C.load_chat_images("chat123")
        check("new image set replaces the old one", len(got) == 1, f"({len(got)})")
        check("the replacement is the jpeg",
              got and got[0].startswith("data:image/jpeg"))

        check("unknown chat has no images",
              C.load_chat_images("nope") == [])
        C.save_chat_images("chat123", ["not-base64!!"])
        check("garbage does not crash", True)
        check("garbage does not erase the previous image",
              len(C.load_chat_images("chat123")) == 1)
        C.clear_chat_images("chat123")
        check("chat deletion clears the image context",
              C.load_chat_images("chat123") == [])
        C.save_chat_images("chat123", [JPG])
        C.clear_all_image_context()
        check("a global shutdown wipes every chat image",
              C.load_chat_images("chat123") == [])

    return h.finish("image_context")


if __name__ == "__main__":
    raise SystemExit(main())

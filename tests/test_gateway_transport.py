# -*- coding: utf-8 -*-
"""Gateway: Telegram transport payload assertions — link previews OFF.

Zero-click exfiltration class (PromptArmor-style): a link preview in a
bot message quietly fetches an attacker URL that carries private data
as parameters. EVERY text path this gateway can take must therefore
send link_preview_options.is_disabled=true.

The suite drives the real TelegramApi with a fake httpx client and
asserts the outgoing JSON payloads — no network.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hivemind_gateway.telegram_api import TelegramApi

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


class CapturingHttpx:
    """Stands in for httpx.AsyncClient; records json POST bodies."""

    def __init__(self):
        self.posts = []
        self.trust_env = True  # not asserted here; hive client covered too

    async def post(self, url, json=None, data=None, files=None):
        self.posts.append((url.rsplit("/", 1)[-1], json or data))
        return FakeResp()

    async def aclose(self):
        pass


class FakeResp:
    def json(self):
        return {"ok": True, "result": {"message_id": 1}}


def _api():
    api = TelegramApi("123:FAKE")
    cap = CapturingHttpx()
    api._client = cap
    return api, cap


async def _main():
    api, cap = _api()
    await api.send_message("111", "answer with https://example.com/?d=SECRET")
    method, payload = cap.posts[-1]
    check("sendMessage disables previews",
          payload.get("link_preview_options", {}).get("is_disabled") is True)

    await api.edit_message_text("111", 5, "status https://example.com/?d=S")
    method, payload = cap.posts[-1]
    check("editMessageText disables previews",
          payload.get("link_preview_options", {}).get("is_disabled") is True)

    # default must be ON (disabled) even when callers forget the flag
    api2, cap2 = _api()
    await api2.send_message("111", "plain")
    _, payload2 = cap2.posts[-1]
    check("preview disable is the DEFAULT",
          payload2.get("link_preview_options", {}).get("is_disabled") is True)

    # document path: the gateway never sends captions (no preview surface);
    # pin that contract: send_document posts no 'caption' key via send.py
    from hivemind_gateway.send import send as gw_send

    recorded = {}

    class DocSpyApi:
        async def send_document(self, chat_id, data, filename,
                                caption=None, reply_to_message_id=None):
            recorded["caption"] = caption
            return {"ok": True}

    await gw_send(DocSpyApi(), "111", "111", document_bytes=b"x",
                  filename="ergebnis.txt")
    check("documents are sent WITHOUT captions (no preview surface)",
          recorded["caption"] is None)

    # send_document payload itself (multipart) carries no preview options
    api3, cap3 = _api()
    await api3.send_document("111", b"x", "a.txt", caption=None)
    method, form = cap3.posts[-1]
    check("sendDocument has no link preview params",
          "link_preview_options" not in (form or {}))

    print()
    print(f"passed={passed} failed={failed}")
    sys.exit(0 if failed == 0 else 1)


asyncio.run(_main())

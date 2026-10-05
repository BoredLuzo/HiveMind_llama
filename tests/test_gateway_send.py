"""Gateway WP1: THE send() choke point — central invariant.

Invariant: no code path sends to a chat_id other than the owner's. A
stranger's chat must raise PolicyViolation BEFORE any network call.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hivemind_gateway.send import PolicyViolation, send

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


class FakeApi:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, reply_to_message_id=None,
                           disable_web_page_preview=True):
        self.calls.append(("send_message", chat_id, text))
        return {"ok": True}

    async def edit_message_text(self, chat_id, message_id, text):
        self.calls.append(("edit_message_text", chat_id, message_id, text))
        return {"ok": True}

    async def send_document(self, chat_id, data, filename,
                            caption=None, reply_to_message_id=None):
        self.calls.append(("send_document", chat_id, filename))
        return {"ok": True}


OWNER = "111111"


async def _run():
    api = FakeApi()

    # owner text -> passes, api called once
    await send(api, OWNER, OWNER, text="hello")
    check("owner text sent", len(api.calls) == 1 and api.calls[0][0] == "send_message")

    # stranger -> raises BEFORE network
    try:
        await send(api, OWNER, "999999", text="evil")
        check("stranger blocked", False)
    except PolicyViolation:
        check("stranger blocked", True)
    check("stranger produced no call", len(api.calls) == 1)

    # unpaired gateway -> nothing goes out, ever
    try:
        await send(api, None, OWNER, text="hello")
        check("unpaired blocked", False)
    except PolicyViolation:
        check("unpaired blocked", True)

    # int/str chat_id equivalence
    await send(api, OWNER, int(OWNER), text="as int")
    check("int owner ok", len(api.calls) == 2)

    # edit path
    await send(api, OWNER, OWNER, new_text="edited", message_id=5)
    check("edit routed", api.calls[-1][0] == "edit_message_text")
    try:
        await send(api, OWNER, OWNER, new_text="edited", message_id=None)
        check("edit without id rejected", False)
    except ValueError:
        check("edit without id rejected", True)

    # document path
    await send(api, OWNER, OWNER, document_bytes=b"x", filename="a.txt")
    check("document routed", api.calls[-1][0] == "send_document")

    # empty call rejected
    try:
        await send(api, OWNER, OWNER)
        check("empty send rejected", False)
    except ValueError:
        check("empty send rejected", True)

    # the invariant across ALL api calls made in this run: only OWNER chats
    check("invariant: all calls to owner",
          all(str(c[1]) == OWNER for c in api.calls))


import asyncio

asyncio.run(_run())

print()
print(f"passed={passed} failed={failed}")
sys.exit(0 if failed == 0 else 1)

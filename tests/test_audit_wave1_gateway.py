# -*- coding: utf-8 -*-
"""Wave 1 Workstream A3 audit tests: gateway callbacks, /new busy guards,
follow_ui vs /new, deny-mode vs ask_user, mirrored decision_id + 64-byte cap,
config loopback SSRF validation, multipart reply_parameters JSON encoding,
exception traceback token redaction, and placeholder clear 409 lock guard.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hivemind_gateway import main as gw_main  # noqa: E402
from hivemind_gateway.auth import ParsedUpdate  # noqa: E402
from hivemind_gateway.bridge import RunBridge  # noqa: E402
from hivemind_gateway.config import GatewayConfig  # noqa: E402
from hivemind_gateway.redaction import TokenRedactionFilter  # noqa: E402
from hivemind_gateway.state import GatewayState  # noqa: E402
from hivemind_gateway.telegram_api import TelegramApi  # noqa: E402
from infra import run_control as RC  # noqa: E402
from routers import core as core_router  # noqa: E402
from utils import workspace_resolve as WSR  # noqa: E402

from tests._harness import Harness  # noqa: E402

h = Harness(style="counts")
check = h.check


class _FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "http://127.0.0.1:8001/test")
            resp = httpx.Response(self.status_code, json=self._body, request=req)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=req, response=resp)


class _FakeMessenger:
    def __init__(self):
        self.messages = []
        self.markups = []
        self.edits = []
        self.documents = []
        self._next_id = 10
        self.owner_id = "111"

    async def send_message(self, text, parse_mode=None, reply_markup=None):
        self._next_id += 1
        self.messages.append(text)
        self.markups.append(reply_markup)
        return {"message_id": self._next_id}

    async def edit_message(self, message_id, text):
        self.edits.append((message_id, text))

    async def send_document(self, data, filename):
        self.documents.append((bytes(data), filename))


class _AuditHive:
    def __init__(self):
        self.settings_body = {"telegram_mirror_enabled": True}
        self.journal_body = {"active": False}
        self.pending_body = {"active": False}
        self.chats_list = []
        self.created = []
        self.clear_calls = 0
        self.clear_status = 200
        self.decided = []
        self.events = []

    async def settings(self):
        return dict(self.settings_body)

    async def journal(self, run_id="", after=0):
        return dict(self.journal_body)

    async def pending_approval(self, run_id):
        return _FakeResp(200, dict(self.pending_body))

    async def decide_approval(self, run_id, answer, decision_id="", tool=""):
        self.decided.append((run_id, answer, decision_id, tool))
        return _FakeResp(200, {"routed": "pause"})

    async def get_chats(self):
        return list(self.chats_list)

    async def get_chat(self, chat_id):
        return _FakeResp(200, {"id": chat_id, "rev": 1, "messages": []})

    async def put_chat_messages(self, chat_id, messages, base_rev):
        return _FakeResp(200, {"ok": True, "rev": base_rev + 1})

    async def create_chat(self, title):
        self.created.append(title)
        return {
            "ok": True,
            "id": f"tg-{len(self.created)}",
            "rev": 1,
            "chat": {"created_at": "2026-10-05T12:00:00"},
        }

    async def placeholder_status(self):
        return {"ok": True, "path": "/tmp/ph", "files": ["a.txt"], "count": 1}

    async def clear_placeholder(self):
        self.clear_calls += 1
        if self.clear_status >= 400:
            req = httpx.Request("POST", "http://127.0.0.1:8001/workspace/placeholder/clear")
            resp = httpx.Response(
                self.clear_status,
                json={"ok": False, "error": "An active HiveMind run is in progress."},
                request=req,
            )
            raise httpx.HTTPStatusError("HTTP 409", request=req, response=resp)
        return {"ok": True, "cleared": 1}

    async def stream(self, q, chat_id, images=None, mode="", overrides=None, workspace=""):
        for ev in self.events:
            yield ev


def _mk_bridge(hive=None):
    tmp = Path(tempfile.mkdtemp(prefix="w1_a3_br_"))
    st = GatewayState(tmp / "state.json")
    ms = _FakeMessenger()
    br = RunBridge(hive or _AuditHive(), st, GatewayConfig(), ms)
    return br, st, ms


# ── 1. main.py: ask: callback dispatch + handle_newchat_callback busy check ──
async def t_main_callbacks():
    class _Api:
        def __init__(self):
            self.toasts = []
            self.edits = []
            self.sent = []

        async def send_message(self, chat_id, text, reply_to_message_id=None,
                               disable_web_page_preview=True, parse_mode=None,
                               reply_markup=None):
            self.sent.append((str(chat_id), text))
            return {"message_id": 100 + len(self.sent)}

        async def edit_message_text(self, chat_id, message_id, text):
            self.edits.append((message_id, text))
            return {"ok": True}

        async def answer_callback_query(self, callback_query_id, text=None):
            self.toasts.append((callback_query_id, text))
            return {"ok": True}

    tmp = Path(tempfile.mkdtemp(prefix="w1_a3_gw_"))
    st = GatewayState(tmp / "state.json")
    api = _Api()
    gw = gw_main.Gateway(api, GatewayConfig(), st)
    hive = _AuditHive()
    gw.hive = hive
    gw.owner_id = 111
    ms = _FakeMessenger()
    gw.bridge = RunBridge(hive, st, gw.cfg, ms)

    # Set up active mirrored ask card with decision_id
    m = gw.bridge._mirror()
    m["run_id"] = "ask-run-1"
    m["approval_sig"] = "d-ask-1|Confirm?"
    m["approval_decision_id"] = "d-ask-1"
    m["approval_card_did"] = "d-ask-1"
    m["approval_tool"] = "run_bash"
    m["approval_kind"] = "ask"

    now_ts = int(time.time()) - 2
    await gw_main.process_update(gw, {
        "update_id": 10,
        "callback_query": {
            "id": "cb-ask-later",
            "from": {"id": 111, "is_bot": False},
            "message": {"message_id": 50, "chat": {"id": 111, "type": "private"}, "date": now_ts},
            "data": "ask:ask-run-1:later:d-ask-1",
        },
    })
    check("ask: later dispatched and toasted",
          any(cid == "cb-ask-later" and "later" in (txt or "") for cid, txt in api.toasts)
          and hive.decided == [])

    await gw_main.process_update(gw, {
        "update_id": 11,
        "callback_query": {
            "id": "cb-ask-yes",
            "from": {"id": 111, "is_bot": False},
            "message": {"message_id": 50, "chat": {"id": 111, "type": "private"}, "date": now_ts},
            "data": "ask:ask-run-1:yes:d-ask-1",
        },
    })
    check("ask: yes dispatched through bridge + toasted",
          hive.decided == [("ask-run-1", "yes", "d-ask-1", "run_bash")]
          and any(cid == "cb-ask-yes" and "yes" in (txt or "") for cid, txt in api.toasts))

    # Unknown callback prefix still dropped cleanly
    toasts_before = len(api.toasts)
    await gw_main.process_update(gw, {
        "update_id": 12,
        "callback_query": {
            "id": "cb-unk",
            "from": {"id": 111, "is_bot": False},
            "message": {"message_id": 51, "chat": {"id": 111, "type": "private"}, "date": now_ts},
            "data": "bogus:1:2",
        },
    })
    check("unknown callback prefix dropped", len(api.toasts) == toasts_before)

    # handle_newchat_callback during active UI journal run: no misleading "Deleting default_workspace"
    hive.journal_body = {"active": True, "run_id": "ui-busy-1", "done": False, "aborted": False, "ts": time.time()}
    gw._pending_newchat = {"nonce": "n123", "ts": time.time(), "chat_id": "111"}
    api.edits.clear()
    api.sent.clear()
    api.toasts.clear()
    await gw.handle_newchat_callback(
        ParsedUpdate(13, "callback_query", "111", "111", "private",
                     "newchat:y:n123", now_ts, False, 88, "cb-nc-busy"))
    check("newchat confirm while UI busy rejects without Deleting edit",
          hive.clear_calls == 0
          and hive.created == []
          and not any("Deleting default_workspace" in txt for _, txt in api.edits)
          and any("ui-busy-1" in txt for _, txt in api.sent)
          and any("Run active" in (txt or "") for _, txt in api.toasts))

    # Race condition: idle at busy check, 409 from clear_placeholder -> card edit shows 409 note, not Deleting
    hive.journal_body = {"active": False}
    hive.clear_status = 409
    gw._pending_newchat = {"nonce": "n409", "ts": time.time(), "chat_id": "111"}
    api.edits.clear()
    api.sent.clear()
    await gw.handle_newchat_callback(
        ParsedUpdate(14, "callback_query", "111", "111", "private",
                     "newchat:y:n409", now_ts, False, 89, "cb-nc-409"))
    check("newchat confirm 409 race shows honest error instead of Deleting",
          hive.created == []
          and not any("Deleting default_workspace" in txt for _, txt in api.edits)
          and any("active HiveMind run" in txt for _, txt in api.sent))


# ── 2. bridge.py: new_chat busy, follow_ui vs /new, deny vs ask, C5c ─────
async def t_bridge_checks():
    # 2a. new_chat() checks _journal_busy(), _run_lock, and 409
    hive = _AuditHive()
    br, st, ms = _mk_bridge(hive)
    hive.journal_body = {"active": True, "run_id": "ui-run-z", "done": False, "aborted": False, "ts": time.time()}
    note_ui = await br.new_chat(clear_placeholder=True)
    check("new_chat rejects UI run via journal",
          "ui-run-z" in note_ui and hive.clear_calls == 0 and hive.created == [])

    hive.journal_body = {"active": False}
    async with br._run_lock:
        note_lock = await br.new_chat(clear_placeholder=True)
    check("new_chat rejects when _run_lock held",
          "already active" in note_lock and hive.clear_calls == 0)

    hive.clear_status = 409
    note_409 = await br.new_chat(clear_placeholder=True)
    check("new_chat handles 409 from clear_placeholder cleanly",
          "active HiveMind run" in note_409 and hive.created == [])

    # 2b. follow_ui=True vs newly created /new [TG] chat + tz-safe comparisons
    hive2 = _AuditHive()
    br2, st2, _ = _mk_bridge(hive2)
    now_local = dt.datetime.now()
    now_utc = dt.datetime.now(dt.timezone.utc)
    # UI chat was updated 5 minutes ago (aware UTC string)
    hive2.chats_list = [{
        "id": "ui-old",
        "title": "UI chat",
        "updated_at": (now_utc - dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "created_at": (now_utc - dt.timedelta(minutes=10)).isoformat(),
        "msg_count": 2,
    }]
    check("before /new, follow_ui selects fresh UI chat (aware UTC Z)",
          await br2._ui_chat_target() == "ui-old")

    await br2.new_chat(clear_placeholder=True)
    check("after /new, tg_chat stores new_chat_ts",
          isinstance((st2.data.get("tg_chat") or {}).get("new_chat_ts"), float))
    check("after /new, follow_ui keeps new [TG] chat over older UI chat",
          await br2._ui_chat_target() == "")
    ens = await br2._ensure_chat()
    check("_ensure_chat returns new [TG] chat instead of old UI chat",
          ens.get("hive_chat_id") == "tg-1" and not ens.get("followed"))

    # Simulate mixed naive + aware timestamps: UI chat gets NEW activity after new_chat_ts
    st2.data["tg_chat"]["new_chat_ts"] = (now_utc - dt.timedelta(seconds=30)).isoformat()
    hive2.chats_list = [
        {
            "id": "ui-naive-newer",
            "title": "UI naive",
            "updated_at": (now_local - dt.timedelta(seconds=5)).isoformat(timespec="seconds"),
            "msg_count": 3,
        },
        {
            "id": "ui-aware-older",
            "title": "UI aware",
            "updated_at": (now_utc - dt.timedelta(minutes=2)).isoformat(),
            "msg_count": 1,
        },
    ]
    check("newer UI chat activity resumes follow_ui without naive/aware TypeError",
          await br2._ui_chat_target() == "ui-naive-newer")

    # 2c. deny mode vs ask_user in _start_text_run_inner
    hive3 = _AuditHive()
    hive3.settings_body = {"telegram_approval_mode": "deny", "telegram_phone_restricted": False}
    hive3.events = [
        {"type": "run_id", "run_id": "deny-ask-1"},
        {"type": "approval_request", "run_id": "deny-ask-1", "kind": "ask",
         "tool": "ask_user", "decision_id": "d-q1", "preview": "Use SQLite?",
         "question": "Use SQLite?"},
        {"type": "approval_request", "run_id": "deny-ask-1", "kind": "approval",
         "tool": "run_bash", "decision_id": "d-b1", "preview": "ls"},
        {"type": "token", "content": "ok"},
        {"type": "done", "stop_reason": "completed"},
    ]
    br3, st3, ms3 = _mk_bridge(hive3)
    await br3.start_text_run("test question")
    check("deny mode does NOT auto-deny kind=='ask' with '3'",
          ("deny-ask-1", "3", "d-q1", "ask_user") not in hive3.decided)
    check("deny mode still auto-denies tool approvals with '3'",
          ("deny-ask-1", "3", "d-b1", "run_bash") in hive3.decided)
    check("deny mode relays kind=='ask' as question card",
          any("The agent asks" in m and "Use SQLite?" in m for m in ms3.messages))

    # 2d. Mirrored appr: & ask: decision_id + 64-byte cap + fail-closed
    hive4 = _AuditHive()
    br4, _, ms4 = _mk_bridge(hive4)
    long_rid = "run_" + ("x" * 34)
    long_did = "did_" + ("0123456789abcdef" * 3)
    hive4.journal_body = {
        "active": True, "run_id": long_rid, "done": False, "aborted": False,
        "ts": time.time(), "n": 1, "base": 0,
        "frames": [f'data: {{"type": "run_id", "run_id": "{long_rid}"}}'],
    }
    hive4.pending_body = {
        "active": True, "tool": "run_bash", "kind": "ask",
        "preview": "Confirm rm -rf? Reply with 'yes' to confirm or 'no' to decline.",
        "decision_id": long_did,
    }
    await br4.mirror_tick()  # adoption
    await br4.mirror_tick()  # ask card
    ask_btns = [b for row in (ms4.markups[-1] or {}).get("inline_keyboard", []) for b in row]
    ask_cbs = [b["callback_data"] for b in ask_btns]
    check("mirrored ask buttons carry decision_id token",
          all(len(cb.split(":")) == 4 for cb in ask_cbs))
    check("mirrored ask buttons fit 64-byte Telegram cap",
          all(len(cb.encode("utf-8")) <= 64 for cb in ask_cbs))
    dids_on_ask = {cb.split(":", 3)[3] for cb in ask_cbs}
    check("all ask buttons share identical truncated decision_id token",
          len(dids_on_ask) == 1)

    # Reject missing & wrong card_did on ask:
    t_miss_ask = await br4.approval_callback("cb1", f"ask:{long_rid}:yes")
    t_bad_ask = await br4.approval_callback("cb2", f"ask:{long_rid}:yes:wrong")
    t_bad_later = await br4.approval_callback("cb3", f"ask:{long_rid}:later:wrong")
    check("ask rejects missing/wrong card_did fail-closed and preserves sig",
          "outdated" in t_miss_ask and "outdated" in t_bad_ask
          and "outdated" in t_bad_later
          and hive4.decided == []
          and br4._mirror().get("approval_sig") is not None)

    # Matching truncated ask callback succeeds and forwards FULL decision_id
    yes_cb = next(cb for cb in ask_cbs if f":{long_rid}:yes:" in cb)
    t_ok_ask = await br4.approval_callback("cb4", yes_cb)
    check("truncated ask callback succeeds and sends full decision_id",
          "delivered" in t_ok_ask
          and hive4.decided[-1] == (long_rid, "yes", long_did, "run_bash"))

    # Mirrored appr: card
    hive4.pending_body = {
        "active": True, "tool": "write_file", "kind": "approval",
        "preview": "write foo.py", "decision_id": long_did,
    }
    await br4.mirror_tick()
    appr_btns = [b for row in (ms4.markups[-1] or {}).get("inline_keyboard", []) for b in row]
    appr_cbs = [b["callback_data"] for b in appr_btns]
    check("mirrored appr buttons carry decision_id token <= 64 bytes",
          len(appr_cbs) == 3
          and all(len(cb.split(":")) == 4 and len(cb.encode("utf-8")) <= 64 for cb in appr_cbs))
    t_miss_appr = await br4.approval_callback("cb5", f"appr:{long_rid}:1")
    t_bad_appr = await br4.approval_callback("cb6", f"appr:{long_rid}:1:wrong")
    check("appr rejects missing/wrong card_did fail-closed",
          "outdated" in t_miss_appr and "outdated" in t_bad_appr
          and len(hive4.decided) == 1
          and br4._mirror().get("approval_sig") is not None)
    allow_cb = next(cb for cb in appr_cbs if f":{long_rid}:1:" in cb)
    t_ok_appr = await br4.approval_callback("cb7", allow_cb)
    check("mirrored appr truncated callback sends full decision_id",
          "allowed" in t_ok_appr
          and hive4.decided[-1] == (long_rid, "1", long_did, "write_file"))

    # Legacy 3-part tolerance only when decision_id is empty
    m4 = br4._mirror()
    m4["run_id"] = "leg-1"
    m4["approval_sig"] = "|cmd"
    m4["approval_decision_id"] = ""
    m4["approval_card_did"] = ""
    m4["approval_tool"] = "run_bash"
    t_leg_ok = await br4.approval_callback("cb8", "appr:leg-1:3")
    check("legacy empty-decision_id card accepts 3-part callback",
          "denied" in t_leg_ok and hive4.decided[-1] == ("leg-1", "3", "", "run_bash"))


# ── 3. config.py: SSRF urlparse validation ────────────────────────────────
def t_config_ssrf():
    for good in (
        "http://127.0.0.1:8001",
        "https://127.0.0.1:8443/",
        "http://localhost:8001",
        "http://[::1]:8001",
    ):
        try:
            GatewayConfig(hive_base_url=good).validate()
            check(f"config accepts valid loopback {good}", True)
        except ValueError as exc:
            check(f"config accepts valid loopback {good}", False, f" ({exc})")

    for bad in (
        "http://localhost.evil.tld:8001",
        "http://127.0.0.1.evil.com:8001",
        "http://user@127.0.0.1:8001",
        "http://user:pass@localhost:8001",
        "http://@localhost:8001",
        "http://127.0.0.1@evil.com:8001",
        "ftp://127.0.0.1:8001",
        "http://[::1:8001",
    ):
        rejected = False
        try:
            GatewayConfig(hive_base_url=bad).validate()
        except ValueError:
            rejected = True
        check(f"config rejects {bad}", rejected)


# ── 4. telegram_api.py: JSON-encoded reply_parameters in multipart ────────
async def t_telegram_multipart():
    captured = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", errors="replace")
        captured.append((request.url.path.rsplit("/", 1)[-1], body))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77}})

    api = TelegramApi("123:TEST")
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    try:
        await api.send_document("111", b"hello", "f.txt", reply_to_message_id=42)
        await api.send_voice("111", b"OggS", duration_s=3, reply_to_message_id=99)
    finally:
        await api.close()

    doc_method, doc_body = captured[0]
    voice_method, voice_body = captured[1]
    expected_doc_rp = json.dumps({"message_id": 42, "allow_sending_without_reply": True})
    expected_voice_rp = json.dumps({"message_id": 99, "allow_sending_without_reply": True})
    check("sendDocument encodes reply_parameters as JSON string",
          doc_method == "sendDocument" and expected_doc_rp in doc_body)
    check("sendVoice encodes reply_parameters as JSON string",
          voice_method == "sendVoice" and expected_voice_rp in voice_body)


# ── 5. redaction.py: exc_info / exc_text / stack_info scrubbing ──────────
def t_redaction_exc():
    tok = "7488112345:AAH9x_SECRET_EXC_TOKEN_xyz123456"
    s1 = io.StringIO()
    s2 = io.StringIO()
    h1 = logging.StreamHandler(s1)
    h2 = logging.StreamHandler(s2)
    fmt = logging.Formatter("%(levelname)s %(message)s")
    h1.setFormatter(fmt)
    h2.setFormatter(fmt)
    h1.addFilter(TokenRedactionFilter([tok]))
    # h2 intentionally has NO filter to verify record.exc_info was cleared
    lg = logging.getLogger("gwtest.wave1.exc")
    lg.handlers.clear()
    lg.propagate = False
    lg.setLevel(logging.DEBUG)
    lg.addHandler(h1)
    lg.addHandler(h2)

    try:
        raise RuntimeError(f"POST https://api.telegram.org/bot{tok}/getUpdates failed")
    except RuntimeError:
        lg.exception("transport failure")

    out1 = s1.getvalue()
    out2 = s2.getvalue()
    check("exc_info traceback scrubbed on primary handler",
          tok not in out1 and "***REDACTED***" in out1 and "RuntimeError" in out1)
    check("record.exc_info cleared so downstream handler sees scrubbed exc_text",
          tok not in out2 and "***REDACTED***" in out2)

    # Pre-populated exc_text + stack_info also scrubbed
    rec = logging.LogRecord("gwtest", logging.ERROR, "f.py", 1, "boom", (), None)
    rec.exc_text = f"Traceback: https://api.telegram.org/bot{tok}/sendMessage"
    rec.stack_info = f"Stack: token={tok}"
    TokenRedactionFilter([tok]).filter(rec)
    check("pre-set exc_text and stack_info scrubbed",
          tok not in (rec.exc_text or "") and tok not in (rec.stack_info or "")
          and "***REDACTED***" in (rec.exc_text or "")
          and "***REDACTED***" in (rec.stack_info or ""))


# ── 6. routers/core.py: POST /workspace/placeholder/clear 409 when busy ──
async def t_placeholder_clear_busy():
    tmp_root = Path(tempfile.mkdtemp(prefix="w1_a3_ph_"))
    ph_dir = tmp_root / "default_workspace"
    ph_dir.mkdir(parents=True, exist_ok=True)
    (ph_dir / "keep.txt").write_text("important", encoding="utf-8")

    orig_ph = WSR._PLACEHOLDER_WS_DIR
    WSR._PLACEHOLDER_WS_DIR = ph_dir
    app = FastAPI()
    app.include_router(core_router.router)

    try:
        # 6a. Same-loop top_run_lock held (via ASGITransport)
        top_lock = RC.get_top_run_lock()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            async with top_lock:
                r1 = await client.post("/workspace/placeholder/clear", json={})
                check("clear returns 409 when top_run_lock held",
                      r1.status_code == 409
                      and r1.json() == {"ok": False, "error": "An active HiveMind run is in progress."}
                      and (ph_dir / "keep.txt").exists())

        # 6b. Active top run id via TestClient (cross-loop safe)
        RC.set_active_top_run_id("run-active-409")
        try:
            tc = TestClient(app)
            r2 = tc.post("/workspace/placeholder/clear", json={})
            check("clear returns 409 when active_top_run_id set",
                  r2.status_code == 409
                  and r2.json() == {"ok": False, "error": "An active HiveMind run is in progress."}
                  and (ph_dir / "keep.txt").exists())
        finally:
            RC.set_active_top_run_id("")

        # 6c. Live abort registry run via TestClient
        RC._register_abort("run-live-409")
        try:
            r3 = tc.post("/workspace/placeholder/clear", json={})
            check("clear returns 409 when live_run_ids non-empty",
                  r3.status_code == 409
                  and (ph_dir / "keep.txt").exists())
        finally:
            RC._unregister_abort("run-live-409")

        # 6d. Idle -> clears cleanly
        r4 = tc.post("/workspace/placeholder/clear", json={})
        check("clear succeeds (200) once no run is active",
              r4.status_code == 200
              and r4.json().get("cleared") == 1
              and not (ph_dir / "keep.txt").exists())
    finally:
        WSR._PLACEHOLDER_WS_DIR = orig_ph


async def _main():
    await t_main_callbacks()
    await t_bridge_checks()
    t_config_ssrf()
    await t_telegram_multipart()
    t_redaction_exc()
    await t_placeholder_clear_busy()


asyncio.run(_main())
sys.exit(h.finish())

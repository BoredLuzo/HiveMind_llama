# -*- coding: utf-8 -*-
"""Gateway WP2: command wiring + restart recovery at the Gateway level.

FakeApi records calls; the real HiveClient is swapped for a FakeHive.
Covers: /new, /stop, /status, /verbose, text-length limit, rate limit
with the /stop exemption, orphan-run recovery (still active -> owner
notice; finished -> cleared).
"""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hivemind_gateway.bridge import RunBridge
from hivemind_gateway.config import GatewayConfig
from hivemind_gateway.main import Gateway, process_update, recover_orphan_run
from hivemind_gateway.state import GatewayState

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


OWNER = 111


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


class FakeApi:
    def __init__(self):
        self.sent = []       # (chat_id, text)
        self.edits = []
        self.docs = []

    async def send_message(self, chat_id, text, reply_to_message_id=None,
                           disable_web_page_preview=True,
                           parse_mode=None, reply_markup=None):
        self.sent.append((str(chat_id), text))
        return {"message_id": 500 + len(self.sent)}

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((str(chat_id), message_id, text))
        return {"ok": True}

    async def send_document(self, chat_id, data, filename, caption=None,
                            reply_to_message_id=None):
        self.docs.append((str(chat_id), filename))
        return {"ok": True}

    async def answer_callback_query(self, callback_query_id, text=None):
        if not hasattr(self, "callback_answers"):
            self.callback_answers = []
        self.callback_answers.append((callback_query_id, text))
        return {"ok": True}


class FakeHive:
    def __init__(self):
        self.created = []
        self.journal_body = {"active": False}
        self.aborts = []

    async def create_chat(self, title):
        self.created.append(title)
        return {"ok": True, "id": f"chat{len(self.created)}", "rev": 1,
                "chat": {"created_at": "2026-10-05T12:00:00"}}

    async def journal(self):
        return dict(self.journal_body)

    async def decide_approval(self, run_id, answer, decision_id="",
                              tool=""):
        if not hasattr(self, "denied"):
            self.denied = []
        self.denied.append((run_id, answer))
        return FakeResponse(200, {"ok": True})

    async def abort_run(self, run_id):
        self.aborts.append(run_id)
        return FakeResponse(200, {"ok": True})


class FakeBridge(RunBridge):
    """Bridge stub: records requested runs, sends nothing."""

    async def start_text_run(self, q):
        if not hasattr(self, "runs"):
            self.runs = []
        self.runs.append(q)
        return None

    async def mirror_send(self, text):
        if not hasattr(self, "mirrored"):
            self.mirrored = []
        self.mirrored.append(text)
        return "🧭 test"

    async def approval_callback(self, callback_id, data):
        if not hasattr(self, "callbacks"):
            self.callbacks = []
        self.callbacks.append((callback_id, data))
        return "ok"


def _gw(tmp_name):
    tmp = Path(tempfile.mkdtemp(prefix=tmp_name))
    st = GatewayState(tmp / "gateway_state.json")
    api = FakeApi()
    gw = Gateway(api, GatewayConfig(), st)
    gw.hive = FakeHive()
    gw.bridge = FakeBridge(gw.hive, st, gw.cfg, gw)
    gw.bridge.runs = []
    gw.owner_id = OWNER
    return gw, st, api


def _msg(text, uid=1):
    import time as _t
    return {
        "update_id": uid,
        "message": {
            "message_id": 10 + uid,
            "from": {"id": OWNER, "is_bot": False},
            "chat": {"id": OWNER, "type": "private"},
            "text": text,
            "date": int(_t.time()) - 5,
        },
    }


def _p(text, uid=1):
    from hivemind_gateway.auth import parse_update
    return parse_update(_msg(text, uid))


# ── /new ────────────────────────────────────────────────────────────────
async def t_new():
    gw, st, api = _gw("gwcmd_new_")
    await gw.handle_owner_update(_p("/new"))
    check("/new creates chat [TG]", gw.hive.created
          and gw.hive.created[0].startswith("[TG]"))
    check("/new persists mapping",
          st.data["tg_chat"]["hive_chat_id"] == "chat1")
    check("/new replies", any("New HiveMind chat" in t
                              for _, t in api.sent))

# ── /status + /verbose ──────────────────────────────────────────────────
async def t_status_verbose():
    gw, st, api = _gw("gwcmd_sv_")
    await gw.handle_owner_update(_p("/status", 2))
    check("/status shows chat", any("workspace:" in t
                                    for _, t in api.sent))
    st.data["tg_chat"] = {"hive_chat_id": "c9", "created_at": ""}
    api.sent.clear()
    await gw.handle_owner_update(_p("/status", 3))
    check("/status shows mapping", any("c9" in t for _, t in api.sent))
    await gw.handle_owner_update(_p("/verbose", 4))
    check("/verbose toggles on", st.data["verbose"] is True
          and gw.bridge.verbose is True)
    await gw.handle_owner_update(_p("/verbose", 5))
    check("/verbose toggles off", st.data["verbose"] is False)

# ── text -> run, length limit ───────────────────────────────────────────
async def t_run_route():
    gw, st, api = _gw("gwcmd_run_")
    await process_update(gw, _msg("Hallo Lauf", 6))
    await asyncio.sleep(0)  # G2: the run executes as a task - yield to it
    check("plain text routed to bridge",
          gw.bridge.runs == ["Hallo Lauf"])
    long = "x" * (gw.cfg.max_text_chars + 1)
    api.sent.clear()
    await process_update(gw, _msg(long, 7))
    check("overlong text rejected", any("zu lang" in t for _, t in api.sent))
    await asyncio.sleep(0)  # let the first run task finish
    check("overlong text did not run",
          gw.bridge.runs == ["Hallo Lauf"])

# ── rate limit + /stop exemption ────────────────────────────────────────
async def t_rate_limit():
    gw, st, api = _gw("gwcmd_rate_")
    for _ in range(gw.cfg.rate_limit_per_min):
        gw.limiter.allow(str(OWNER))
    await process_update(gw, _msg("Hallo", 8))
    check("rate-limited message dropped with note",
          any("Rate limit" in t for _, t in api.sent))
    check("rate-limited text did not run", gw.bridge.runs == [])
    st.data["active_run"] = {"run_id": "r1", "chat_id": "c1"}
    await process_update(gw, _msg("/stop", 9))
    check("/stop exempt from rate limit", gw.hive.aborts == ["r1"])

# ── orphan recovery ─────────────────────────────────────────────────────
async def t_recover():
    gw, st, api = _gw("gwcmd_rec_")
    check("no orphan -> no-op", await recover_orphan_run(gw) is None)

    st.data["active_run"] = {"run_id": "orph-1", "chat_id": "c1"}
    gw.hive.journal_body = {"active": True, "run_id": "orph-1",
                            "done": False, "aborted": False, "ts": 1.0}
    await recover_orphan_run(gw)
    check("still-active orphan kept", st.data["active_run"]["run_id"] == "orph-1")
    check("owner warned about orphan",
          any("orph-1" in t and "stop" in t.lower() for _, t in api.sent))

    st.data["open_approvals"] = {"n1": {"x": 1}}
    gw.hive.journal_body = {"active": True, "run_id": "orph-1",
                            "done": True, "aborted": False, "ts": 1.0}
    await recover_orphan_run(gw)
    check("finished orphan cleared", st.data["active_run"] is None)
    check("open approvals denied at recovery",
          st.data["open_approvals"] == {})


# ── pair_window flow (integration: the path a real unpaired run hits) ──
async def t_pair_flow():
    gw, st, api = _gw("gwcmd_pair_")
    gw.owner_id = None                      # UNPAIRED — the real first start
    code = gw.pairing.start_window()

    # a non-pair message while unpaired: silently dropped, no answer
    await process_update(gw, _msg("hello bot", 20))
    check("unpaired non-pair dropped silently", api.sent == [])

    # /pair with the console code -> owner bound, pairing disabled
    await process_update(gw, _msg(f"/pair {code}", 21))
    check("pair_window /pair binds owner", gw.owner_id == OWNER)
    check("owner persisted in state", st.owner_telegram_id == OWNER)
    check("pairing disabled after success", gw.pairing.enabled is False)
    check("confirmation sent", any("Paired" in t for _, t in api.sent))

    # /pair again after binding: answered, nothing changes
    await process_update(gw, _msg("/pair WHATEVER", 22))
    check("post-pair /pair harmless", gw.owner_id == OWNER
          and any("Already paired" in t for _, t in api.sent))

    # wrong code from an unpaired gateway: attempt counts, NO answer goes
    # out (brief: no answer to strangers — even in the pair window)
    gw2, st2, api2 = _gw("gwcmd_pair2_")
    gw2.owner_id = None
    gw2.pairing.start_window()
    await process_update(gw2, _msg("/pair WRONGCODE1", 23))
    check("wrong code stays SILENT", api2.sent == [])
    check("wrong code counted", gw2.pairing.failed_attempts == 1)
    check("still unpaired", gw2.owner_id is None)


# ── instance lock: dead pid, recycled pid (realrun bug #3), live pid ───
async def t_lock():
    import os
    import hivemind_gateway.state as S
    import hivemind_gateway.main as M
    tmp = Path(tempfile.mkdtemp(prefix="gwcmd_lock_"))
    lock = tmp / "gateway.lock"
    orig_home = S.state_home
    S.state_home = lambda: tmp  # type: ignore[assignment]
    try:
        M.acquire_instance_lock()
        try:
            M.acquire_instance_lock()
            check("double start refused", False)
        except M.StartupError:
            check("double start refused", True)

        # dead pid -> stale -> silently replaced
        lock.write_text('{"pid": 999999999, "birth": 1}', encoding="utf-8")
        M.acquire_instance_lock()
        check("dead-pid lock replaced", True)

        # THE realrun bug: pid belongs to a LIVE but DIFFERENT process
        # (recycled pid) -> must NOT count as "gateway still running"
        live_pid = os.getpid()
        real_birth = M._process_birth(live_pid)
        assert real_birth is not None
        lock.write_text(
            f'{{"pid": {live_pid}, "birth": {real_birth - 1000}}}',
            encoding="utf-8")
        M.acquire_instance_lock()
        check("recycled-pid lock replaced", True)

        # same pid AND same birth -> a real other gateway -> refused
        lock.write_text(
            f'{{"pid": {live_pid}, "birth": {real_birth}}}',
            encoding="utf-8")
        try:
            M.acquire_instance_lock()
            check("matching live lock refused", False)
        except M.StartupError:
            check("matching live lock refused", True)

        # legacy plain-text lock (old format) -> stale -> replaced
        lock.write_text("12345", encoding="utf-8")
        M.acquire_instance_lock()
        check("legacy lock format replaced", True)

        # realrun bug #5: a TERMINATED process whose handle the parent
        # still holds stays openable via OpenProcess — tasklist lists
        # nothing, the old probe said "alive". Reproduced with a real
        # exited child: Popen keeps its handle, the pid must count as dead.
        import subprocess as sp
        child = sp.Popen([sys.executable, "-c", "pass"])
        child.wait()
        check("exited child with open handle counts as DEAD",
              M._pid_alive(child.pid) is False)
        lock.write_text(
            f'{{"pid": {child.pid}, "birth": 1}}', encoding="utf-8")
        M.acquire_instance_lock()
        check("terminated-child lock replaced", True)
    finally:
        S.state_home = orig_home  # type: ignore[assignment]
        if lock.exists():
            lock.unlink()


# ── UI veto semantics (N6): remote off switch, fail-safe on errors ─────
async def t_ui_veto():
    import hivemind_gateway.main as M

    class Gw:
        def __init__(self, hive):
            self.hive = hive

    class Hive:
        def __init__(self, body=None, boom=None):
            self.body = body
            self.boom = boom

        async def settings(self):
            if self.boom is not None:
                raise self.boom
            return self.body

    check("veto: key false shuts down",
          await M._ui_veto_active(Gw(Hive({"telegram_gateway_enabled": False}))) is True)
    check("veto: key true does NOT shut down",
          await M._ui_veto_active(Gw(Hive({"telegram_gateway_enabled": True}))) is False)
    check("veto: key ABSENT does not shut down (local switch rules)",
          await M._ui_veto_active(Gw(Hive({}))) is False)
    check("veto: engine offline keeps gateway up",
          await M._ui_veto_active(Gw(Hive(boom=M.HiveUnreachable("down")))) is False)
    check("veto: broken JSON keeps gateway up",
          await M._ui_veto_active(Gw(Hive(boom=ValueError("bad json")))) is False)

    # the UI can never OVERRIDE a locally disabled gateway (already
    # covered in the config suite; repeated here as the contract pair)
    from hivemind_gateway.config import GatewayConfig
    try:
        M.ensure_enabled(GatewayConfig())
        check("veto pair: local OFF stays OFF regardless of UI", False)
    except M.StartupError:
        check("veto pair: local OFF stays OFF regardless of UI", True)

    # N7: loopback client must ignore proxy environment variables
    from hivemind_gateway.hive_client import HiveClient
    hc = HiveClient("http://127.0.0.1:8001")
    check("hive client ignores proxy env (trust_env=False)",
          hc._client.trust_env is False)
    await hc.close()


# ── audit G8: mirror intercept beats a pending /setModel flow ──────────
async def t_g8_order():
    import time as _t
    gw, st, api = _gw("gwcmd_g8_")
    st.data["pending_setup"] = {"model": "m", "agentic": False,
                                "ts": _t.time()}
    gw.bridge._mirror()["run_id"] = "ui-x"
    await process_update(gw, _msg("1", 30))
    check("G8: open approval card beats pending setup",
          getattr(gw.bridge, "mirrored", []) == ["1"]
          and st.data.get("pending_setup") is not None)


# ── audit G9: an edited message never re-runs ──────────────────────────
async def t_g9_edits():
    gw, st, api = _gw("gwcmd_g9_")

    def _edit(text, uid):
        m = _msg(text, uid)
        m["edited_message"] = m.pop("message")
        return m

    await process_update(gw, _edit("zweiter Lauf", 40))
    check("G9: edited text starts no second run", gw.bridge.runs == [])
    await process_update(gw, _msg("echter Lauf", 41))
    await asyncio.sleep(0)  # G2: yield to the run task
    check("G9: normal text still starts a run",
          gw.bridge.runs == ["echter Lauf"])


# /help: a real instruction list (owner request), not a whitelist dump
async def t_help():
    gw, st, api = _gw("gwcmd_help_")
    await gw.handle_owner_update(_p("/help", 50))
    text = next((t for _, t in api.sent if "quick guide" in t), "")
    check("/help: instruction list title", bool(text))
    for needle in ("/stop", "/mode auto", "/setModel", "/workspace",
                   "auto-denied", "1 (allow once)"):
        check(f"/help: mentions {needle[:24]!r}", needle in text)


# setup-token: validate via getMe, store via injectable store
async def t_setup_token():
    import hivemind_gateway.main as M

    class FakeSetupApi:
        def __init__(self, token):
            self.token = token
            self.closed = False

        async def get_me(self):
            if not self.token.strip():
                raise M.TelegramApiError("getMe", "Unauthorized")
            return {"username": "fritz_hivemind_bot", "id": 42}

        async def close(self):
            self.closed = True

    stored = []
    orig_api = M.TelegramApi
    M.TelegramApi = FakeSetupApi
    try:
        user = await M.perform_token_setup(
            "  tok123  ", store=lambda *a: stored.append(a))
        check("setup: bot username returned",
              user == "fritz_hivemind_bot")
        check("setup: token stored under service/key",
              stored == [("hivemind_gateway", "bot_token", "tok123")])
        try:
            await M.perform_token_setup("   ", store=lambda *a: None)
            check("setup: empty token refused", False)
        except M.StartupError:
            check("setup: empty token refused", True)
    finally:
        M.TelegramApi = orig_api


# F4: owner callback_query routes into the bridge (tappable approvals)
async def t_f4_callback():
    import time as _t
    gw, st, api = _gw("gwcmd_cb_")
    gw.bridge._mirror()["run_id"] = "ui-x"
    gw.bridge._mirror()["approval_sig"] = "d|x"
    cb = {
        "update_id": 60,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": OWNER, "is_bot": False},
            "message": {"message_id": 77,
                        "chat": {"id": OWNER, "type": "private"},
                        "date": int(_t.time()) - 5},
            "data": "appr:ui-x:3",
        },
    }
    await process_update(gw, cb)
    check("F4: callback routed to bridge",
          getattr(gw.bridge, "callbacks", []) == [("cb-1", "appr:ui-x:3")])
    check("F4: toast answered via answerCallbackQuery",
          any(c == "cb-1" for c, *_ in getattr(api, "callback_answers", [])))


# stop verb: stale/recycled locks clear safely; a live match is killed
# via the injected killer (never a real kill in tests)
def t_stop_verb():
    import json
    import os
    import hivemind_gateway.main as M
    import hivemind_gateway.state as S
    tmp = Path(tempfile.mkdtemp(prefix="gwcmd_stopv_"))
    lock = tmp / "gateway.lock"
    orig = S.lock_path
    S.lock_path = lambda: lock  # type: ignore[assignment]
    try:
        out = M.stop_instance()
        check("stop: no lock -> nothing to do", "No gateway lock" in out)

        lock.write_text('{"pid": 999999999, "birth": 1}', encoding="utf-8")
        out = M.stop_instance()
        check("stop: dead pid -> stale cleared", "Stale lock" in out
              and not lock.exists())

        live_pid = os.getpid()
        real_birth = M._process_birth(live_pid)
        lock.write_text(json.dumps(
            {"pid": live_pid, "birth": (real_birth or 0) - 5,
             "cwd": "somewhere"}), encoding="utf-8")
        killed = []
        out = M.stop_instance(killer=lambda p: killed.append(p))
        check("stop: recycled pid NOT killed, cleared",
              killed == [] and not lock.exists())

        lock.write_text(json.dumps(
            {"pid": live_pid, "birth": real_birth, "cwd": "somewhere"}),
            encoding="utf-8")
        out = M.stop_instance(killer=lambda p: killed.append(p))
        check("stop: live match killed + lock removed",
              killed == [live_pid] and not lock.exists()
              and "somewhere" in out)
    finally:
        S.lock_path = orig  # type: ignore[assignment]


# ask mode restart-deny contract: persisted open approvals are denied
# on the engine when the gateway restarts (recover_orphan_run)
async def t_recover_deny():
    import time as _t
    gw, st, api = _gw("gwcmd_rcdeny_")
    gw.hive.journal_body = {"active": False}
    st.data["open_approvals"] = {
        "run-x": {"decision_id": "dx", "tool": "run_bash"}}
    st.data["active_run"] = {"run_id": "run-x", "chat_id": "c1"}
    await recover_orphan_run(gw)
    check("restart-deny: persisted approval denied on engine",
          getattr(gw.hive, "denied", []) == [("run-x", "3")])
    check("restart-deny: open_approvals cleared",
          st.data.get("open_approvals") == {})


async def _main():
    await t_new()
    await t_status_verbose()
    await t_run_route()
    await t_rate_limit()
    await t_recover()
    await t_pair_flow()
    await t_lock()
    await t_ui_veto()
    await t_g8_order()
    await t_g9_edits()
    await t_f4_callback()
    t_stop_verb()
    await t_recover_deny()
    await t_help()
    await t_setup_token()

asyncio.run(_main())
print()
print(f"passed={passed} failed={failed}")
sys.exit(0 if failed == 0 else 1)

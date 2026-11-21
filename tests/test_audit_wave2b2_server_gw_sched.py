# -*- coding: utf-8 -*-
"""Regression suite for Audit Wave 2B-2: server, routers, scheduler, learning, gateway & deploy.

Covers every Wave 2B-2 fix hermetically (no live LLM, network, or GPU):
  1. server.py: jr_pending bytes/n_total accounting, keep_awake inside _produce(), spawn() task ref, /v1 null params
  2. routers/chats.py + routers/config.py: post-write _chats_cache update & DEFAULT_AGENT_CFG deepcopy isolation
  3. routers/automations.py + scheduler/manager.py: toggle next_ts reset, PUT pending_fire clear on disable, unified busy gate
  4. learning/peer_ratings.py + insights.py: safe numeric coercion, missing rater_model guard, spawn(), _insight_lock
  5. hivemind_gateway/bridge.py + main.py: TelegramApiError handling, mirror latch on 5xx vs 200/404, /workspace folder check, _bg_tasks tracking
  6. deploy/updater.py + update_llama.bat: stage .update_new.bat only after verify, ROCm prompt branch
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


class _FakeReq:
    def __init__(self, body, headers=None):
        self._body = body
        self.headers = headers or {}

    async def json(self):
        return self._body


# ── 1. server.py journal accounting, keep_awake/spawn, /v1 null safety ─────

def test_server_stream_and_v1() -> None:
    import infra.keep_awake as ka
    import server as S

    ka_events: list[str] = []

    def _acq():
        ka_events.append("acq")
        return 1

    def _rel():
        ka_events.append("rel")
        return 0

    async def _fake_run_stream(*_a, **_kw):
        yield f"data: {json.dumps({'type': 'status', 'content': 'pre1'})}\n\n"
        yield f"data: {json.dumps({'type': 'status', 'content': 'pre2'})}\n\n"
        yield f"data: {json.dumps({'type': 'run_id', 'run_id': 'jr_pend_1'})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'stop_reason': 'completed'})}\n\n"

    async def _consume_stream() -> list[str]:
        resp = await S.stream(_FakeReq({"q": "hello", "mode": "direct"}))
        out = []
        async for chunk in resp.body_iterator:
            out.append(chunk)
        return out

    S._RUN_JOURNAL.pop("jr_pend_1", None)
    with mock.patch.object(S, "run_stream", _fake_run_stream), \
         mock.patch.object(ka, "acquire", _acq), \
         mock.patch.object(ka, "release", _rel):
        chunks = asyncio.run(_consume_stream())

    reg = S._RUN_JOURNAL.pop("jr_pend_1", {})
    expected_bytes = sum(len(f) for f in reg.get("frames", []))
    check("/stream jr_pending flushes accurate bytes & n_total into journal",
          len(chunks) == 4
          and len(reg.get("frames", [])) == 4
          and reg.get("n_total") == 4
          and reg.get("bytes") == expected_bytes
          and ka_events == ["acq", "rel"],
          f"n_total={reg.get('n_total')} bytes={reg.get('bytes')}/{expected_bytes} ka={ka_events}")

    srv_src = (ROOT / "server.py").read_text(encoding="utf-8")
    check("server.py uses spawn() for _producer and holds keep_awake inside _produce()",
          '_producer = spawn(_produce(), name=f"stream-producer-{chat_id or mode}")' in srv_src
          and "# Keep system awake for the full lifetime of the producer" in srv_src)

    # /v1/chat/completions null & bad-param validation
    captured_v1: dict = {}

    async def _fake_v1_direct(msgs, raw_model, temp, max_tok, chat_id, created, do_stream):
        captured_v1.update(model=raw_model, temp=temp, max_tok=max_tok, do_stream=do_stream)
        return {"ok": True, "model": raw_model, "temp": temp, "max_tok": max_tok}

    async def _run_v1() -> tuple[dict, int, int, int]:
        with mock.patch.object(S, "_v1_direct_mode", _fake_v1_direct):
            r_ok = await S.v1_chat_completions(_FakeReq({
                "model": None,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": None,
                "temperature": None,
                "stream": None,
            }))
            r_bad_tok = await S.v1_chat_completions(_FakeReq({
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": "not-an-int",
            }))
            r_bad_temp = await S.v1_chat_completions(_FakeReq({
                "messages": [{"role": "user", "content": "hi"}],
                "temperature": "not-a-float",
            }))
            r_not_obj = await S.v1_chat_completions(_FakeReq(["not-a-dict"]))
            return r_ok, r_bad_tok.status_code, r_bad_temp.status_code, r_not_obj.status_code

    r_ok, rc_tok, rc_temp, rc_obj = asyncio.run(_run_v1())
    check("/v1/chat/completions tolerates null params and returns 400 on bad param types",
          isinstance(r_ok, dict) and r_ok.get("ok") is True
          and captured_v1.get("max_tok") == 4096
          and abs(float(captured_v1.get("temp", 0)) - 0.2) < 1e-6
          and captured_v1.get("do_stream") is False
          and rc_tok == 400 and rc_temp == 400 and rc_obj == 400,
          f"captured={captured_v1} codes={(rc_tok, rc_temp, rc_obj)}")


# ── 2. routers/chats.py post-write cache + routers/config.py deepcopy ──────

def test_chats_cache_and_config_deepcopy() -> None:
    import core.state as CS
    import routers.chats as RC
    import routers.config as RCFG

    orig_sessions = CS._SESSIONS_DIR
    orig_cache = dict(CS._chats_cache)
    orig_loaded = CS._cache_loaded
    orig_limit = RC._CHAT_JSON_LIMIT
    with tempfile.TemporaryDirectory() as td:
        sdir = Path(td)
        CS._SESSIONS_DIR = sdir
        CS._chats_cache.clear()
        CS._cache_loaded = True
        try:
            chat_ok = {"id": "c_ok1", "title": "Good", "created_at": "2026-11-21T10:00:00",
                       "updated_at": "2026-11-21T10:00:00", "messages": [{"role": "user", "content": "hi"}]}
            res_ok = RC._save_chat("c_ok1", chat_ok)
            check("_save_chat valid write populates _chats_cache",
                  res_ok and not res_ok.get("oversize") and "c_ok1" in CS._chats_cache)

            # Refused oversize write must NOT clobber cache entry
            RC._CHAT_JSON_LIMIT = 50
            chat_big = {"id": "c_ok1", "title": "PoisonedTitle", "created_at": "2026-11-21T10:00:00",
                        "updated_at": "2026-11-21T10:05:00",
                        "messages": [{"role": "user", "content": "X" * 500}]}
            res_big = RC._save_chat("c_ok1", chat_big)
            check("_save_chat oversize refusal does NOT poison _chats_cache",
                  res_big and res_big.get("oversize") is True
                  and CS._chats_cache["c_ok1"]["title"] == "Good")

            # Disk IO failure must NOT populate cache for a new chat
            RC._CHAT_JSON_LIMIT = orig_limit
            chat_io = {"id": "c_io1", "title": "Unwritten", "created_at": "2026-11-21T10:00:00",
                       "updated_at": "2026-11-21T10:00:00", "messages": [{"role": "user", "content": "hi"}]}
            raised_io = False
            with mock.patch.object(RC, "_replace_with_retry", side_effect=PermissionError("locked")):
                try:
                    RC._save_chat("c_io1", chat_io)
                except PermissionError:
                    raised_io = True
            check("_save_chat disk IO error leaves _chats_cache unpolluted",
                  raised_io and "c_io1" not in CS._chats_cache)
        finally:
            RC._CHAT_JSON_LIMIT = orig_limit
            CS._SESSIONS_DIR = orig_sessions
            CS._chats_cache.clear()
            CS._chats_cache.update(orig_cache)
            CS._cache_loaded = orig_loaded

    # DEFAULT_AGENT_CFG isolation in _populate_default_agents
    baseline_analyst = copy.deepcopy(RCFG.DEFAULT_AGENT_CFG["analyst"])
    sa: dict = {}
    RCFG._populate_default_agents(sa)
    sa["analyst"]["model"] = "mutated:99b"
    check("_populate_default_agents uses deepcopy so mutating sa leaves DEFAULT_AGENT_CFG intact",
          RCFG.DEFAULT_AGENT_CFG["analyst"] == baseline_analyst)


# ── 3. routers/automations.py + scheduler/manager.py ───────────────────────

def test_automations_and_scheduler_busy() -> None:
    import routers.automations as RA
    from scheduler.manager import SchedulerManager

    with tempfile.TemporaryDirectory() as td:
        mgr = SchedulerManager(Path(td) / "auto.json", "http://127.0.0.1:1")
        past_ts = time.time() - 5000.0
        job = mgr.store.add({
            "name": "Morning",
            "prompt": "status check",
            "schedule": {"kind": "every", "interval_s": 600},
            "next_ts": past_ts,
            "enabled": True,
            "pending_fire": {"due_ts": past_ts, "queued_ts": past_ts},
        })
        jid = job["id"]

        with mock.patch.object(RA, "_mgr", return_value=mgr):
            # Disabling via PUT clears pending_fire
            upd_dis = asyncio.run(RA.update_automation(jid, _FakeReq({"enabled": False})))
            check("PUT /automations/{id} with enabled=False clears pending_fire",
                  upd_dis["enabled"] is False and upd_dis.get("pending_fire") is None)

            # Toggling back ON when missed refreshes next_ts into the future
            tog_on = asyncio.run(RA.toggle_automation(jid))
            check("POST /automations/{id}/toggle re-enabling missed job refreshes next_ts",
                  tog_on["enabled"] is True and float(tog_on["next_ts"]) > time.time())

        # SchedulerManager._scheduler_busy blocks double-fire while _in_flight_jobs non-empty
        mgr2 = SchedulerManager(Path(td) / "auto2.json", "http://127.0.0.1:1")
        now = time.time()
        j2 = mgr2.store.add({
            "name": "Second",
            "prompt": "check",
            "schedule": {"kind": "every", "interval_s": 600},
            "next_ts": now - 5,
            "policy": "skip",
        })
        mgr2.lane_busy = lambda: False
        mgr2._in_flight_jobs.add("other-job")
        spawned: list[str] = []
        mgr2._spawn_fire = lambda jb, ts, reason="": spawned.append(jb["id"])
        asyncio.run(mgr2.tick(now))
        check("_scheduler_busy prevents concurrent tick fire when another job is in flight",
              len(spawned) == 0 and mgr2.store.get(j2["id"])["last_status"] == "skipped_busy",
              f"spawned={spawned} status={mgr2.store.get(j2['id'])}")


# ── 4. learning/peer_ratings.py & learning/insights.py ─────────────────────

def test_learning_safe_parse_and_locks() -> None:
    import learning.insights as LI
    import learning.peer_ratings as LPR

    check("_safe_num coerces bad values cleanly",
          LPR._safe_num("bad", float, 0.5) == 0.5
          and LPR._safe_num(None, int, 0) == 0
          and LPR._safe_num("0.85", float, 0.5) == 0.85)

    # _maybe_adapt_config survives non-numeric delta strings without raising
    fake_ratings = [
        {"event": "peer_rating", "rated_agent": "coder", "score": 0.2},
        {"event": "peer_rating", "rated_agent": "coder", "score": 0.3},
    ]
    saved_cfgs: list[dict] = []
    with mock.patch.object(LPR, "read_learning_log", return_value=fake_ratings), \
         mock.patch.object(LPR, "get_learned_config", return_value={"temperature": 0.4, "max_tokens": 500}), \
         mock.patch.object(LPR, "get_base_config", return_value={}), \
         mock.patch.object(LPR, "save_learned_config", side_effect=lambda _f, _m, _a, cfg: saved_cfgs.append(cfg)), \
         mock.patch.object(LPR, "append_learning_log"):
        asyncio.run(LPR._maybe_adapt_config(
            "m:1b", "coder", 0.3,
            {"suggested_temp_delta": "N/A", "suggested_tokens_delta": None, "suggested_prompt_hint": ""},
        ))
    check("_maybe_adapt_config handles malformed LLM temp/token deltas gracefully",
          saved_cfgs == [])

    lpr_src = (ROOT / "learning" / "peer_ratings.py").read_text(encoding="utf-8")
    check("peer_ratings skips empty rater_model and uses spawn() for soul evolution",
          "if not rater_model:\n            continue" in lpr_src
          and 'spawn(_maybe_trigger_soul_evolution_forced(run_count), name="soul-evolution-low-score")' in lpr_src)

    # learning/insights._mark_insight_distilled uses _insight_lock + safe confidence
    class _LockTracker:
        def __init__(self):
            self.entered = 0
            self.exited = 0

        def __enter__(self):
            self.entered += 1
            return self

        def __exit__(self, *a):
            self.exited += 1
            return False

    lt = _LockTracker()
    persisted = [0]
    orig_mem = LI._memory
    try:
        LI._memory = SimpleNamespace(_insight_lock=lt, _persist_insights=lambda: persisted.__setitem__(0, persisted[0] + 1))
        ins = {"insight": "check tests"}
        LI._mark_insight_distilled(ins, "test-skill")
        check("_mark_insight_distilled acquires _insight_lock around mutation & persist",
              lt.entered == 1 and lt.exited == 1 and persisted[0] == 1 and ins["distilled_skill"] == "test-skill")
    finally:
        LI._memory = orig_mem

    li_src = (ROOT / "learning" / "insights.py").read_text(encoding="utf-8")
    check("insights guards float(confidence) against invalid strings",
          '_confidence = float(_ie.get("confidence", 0.5))\n                except (TypeError, ValueError):' in li_src)


# ── 5. hivemind_gateway/bridge.py & main.py ────────────────────────────────

def test_gateway_bridge_and_main() -> None:
    from hivemind_gateway import main as GM
    from hivemind_gateway.bridge import RunBridge
    from hivemind_gateway.config import GatewayConfig
    from hivemind_gateway.state import GatewayState
    from hivemind_gateway.telegram_api import TelegramApiError

    class _MockMs:
        def __init__(self, fail_send=False):
            self.fail_send = fail_send
            self.sent: list[str] = []

        async def send_message(self, text, **_kw):
            if self.fail_send:
                raise TelegramApiError(500, "telegram send down")
            self.sent.append(text)
            return {"message_id": 1}

        async def edit_message(self, *_a, **_kw):
            pass

        async def send_document(self, *_a, **_kw):
            pass

    class _MockHive:
        def __init__(self):
            self.resp_code = 500
            self.aborts = []
            self.denies = []

        async def settings(self):
            return {}

        async def journal(self, *_a, **_kw):
            return {"active": False}

        async def pending_approval(self, _rid):
            return SimpleNamespace(json=lambda: {"active": False})

        async def decide_approval(self, *_a, **_kw):
            return SimpleNamespace(status_code=self.resp_code, json=lambda: {"routed": "pause"})

        async def _progress_msgs(self):
            return False

    with tempfile.TemporaryDirectory() as td:
        st = GatewayState(Path(td) / "gw_state.json")
        st.data["tg_chat"] = {"hive_chat_id": "c1", "created_at": "t"}
        hive = _MockHive()
        ms = _MockMs(fail_send=True)
        br = RunBridge(hive, st, GatewayConfig(), ms)

        async def _boom_inner(*_a, **_kw):
            raise TelegramApiError(400, "bad markdown")

        br._start_text_run_inner = _boom_inner
        err_note = asyncio.run(br.start_text_run("hi"))
        fail_res = asyncio.run(br._fail("notice"))
        check("start_text_run catches TelegramApiError & _fail handles send failure safely",
              "Telegram delivery error" in err_note
              and st.data.get("active_run") is None
              and fail_res == "notice",
              f"note={err_note!r}")

        # Mirrored approval latch preserved on 500, cleared on 404 or 200
        m = br._mirror()
        m.update({"run_id": "r-mir", "approval_sig": "sig1", "approval_decision_id": "d1",
                  "approval_card_did": "d1", "approval_tool": "run_bash", "approval_kind": "approval"})
        hive.resp_code = 503
        cb_503 = asyncio.run(br.approval_callback("cb1", "appr:r-mir:1:d1"))
        sig_after_503 = br._mirror().get("approval_sig")
        ms_503 = asyncio.run(br.mirror_send("1"))
        sig_after_ms_503 = br._mirror().get("approval_sig")
        hive.resp_code = 404
        ms_404 = asyncio.run(br.mirror_send("1"))
        sig_after_404 = br._mirror().get("approval_sig")
        check("mirror approval_callback/mirror_send preserve latch on 5xx and clear on 404",
              "HTTP 503" in cb_503 and sig_after_503 == "sig1"
              and "HTTP 503" in ms_503 and sig_after_ms_503 == "sig1"
              and "HTTP 404" in ms_404 and sig_after_404 is None)

        # workspace_text rejects regular files
        f_not_dir = Path(td) / "file.txt"
        f_not_dir.write_text("x", encoding="utf-8")
        ws_file_res = asyncio.run(br.workspace_text(str(f_not_dir)))
        check("workspace_text rejects regular files (not a folder)",
              "not a folder" in ws_file_res.lower(), f"res={ws_file_res!r}")

        # Gateway.start_owner_run tracks _bg_tasks & discards cleanly
        class _DummyApi:
            async def close(self): pass

        gw = GM.Gateway(_DummyApi(), GatewayConfig(), st)
        ran = asyncio.Event()

        async def _fast_run(*_a, **_kw):
            await asyncio.sleep(0.01)
            ran.set()
            return ""

        gw.bridge.start_text_run = _fast_run
        gw.bridge.mirror_intercept = lambda _q: False
        gw.bridge.own_approval_pending = lambda: False

        async def _test_bg_task():
            p_upd = SimpleNamespace(text="hello", chat_id="1", message_id=10,
                                    document_prompt=False, from_voice=False, images=())
            await gw.start_owner_run(p_upd)
            check("Gateway tracks spawned owner run task in _bg_tasks",
                  len(gw._bg_tasks) == 1)
            await asyncio.wait_for(ran.wait(), timeout=2.0)
            await asyncio.sleep(0.02)
            check("Gateway discards finished owner run task from _bg_tasks",
                  len(gw._bg_tasks) == 0)

        asyncio.run(_test_bg_task())


# ── 6. deploy/updater.py staging after verify + update_llama.bat ROCm ───────

def test_updater_stage_and_rocm_bat() -> None:
    from deploy import updater as U

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        fake = tmp / "install"
        (fake / "core").mkdir(parents=True)
        (fake / "core" / "version.py").write_text('HIVEMIND_VERSION = "1.0.0"\n', encoding="utf-8")
        (fake / "utils").mkdir()
        (fake / "deploy").mkdir()
        (fake / "update.bat").write_text("@echo OLD\n", encoding="utf-8")

        zpath = tmp / "release.zip"
        with zipfile.ZipFile(zpath, "w") as z:
            base = "HiveMind_v2.0.0_release/"
            z.writestr(base + "core/version.py", 'HIVEMIND_VERSION = "2.0.0"\n')
            z.writestr(base + "utils/x.py", "# u\n")
            z.writestr(base + "update.bat", "@echo NEW\n")

        with mock.patch.object(U, "__file__", str(fake / "deploy" / "updater.py")), \
             mock.patch.object(U.shutil, "which", return_value="curl"), \
             mock.patch.object(U, "port_listener", return_value=False), \
             mock.patch.object(U, "stop_watchdog", return_value=False), \
             mock.patch.object(U, "_run_rc", side_effect=[1, 8]):  # backup ok, apply fails
            rc_fail = U.main(["--zip", str(zpath)])

        check("updater does NOT stage .update_new.bat when apply fails",
              rc_fail == 1 and not (fake / ".update_new.bat").exists())

    bat = (ROOT / "update_llama.bat").read_text(encoding="utf-8")
    check("update_llama.bat preserves ROCm option via explicit branch",
          'else if /i "%BACKEND%"=="rocm"' in bat and "choice /c RVC" in bat)


if __name__ == "__main__":
    test_server_stream_and_v1()
    test_chats_cache_and_config_deepcopy()
    test_automations_and_scheduler_busy()
    test_learning_safe_parse_and_locks()
    test_gateway_bridge_and_main()
    test_updater_stage_and_rocm_bat()
    sys.exit(h.finish("audit_wave2b2_server_gw_sched"))

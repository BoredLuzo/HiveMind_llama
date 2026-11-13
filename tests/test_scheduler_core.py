"""Pure schedule/store tests for the Automations scheduler."""
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scheduler.spec import (  # noqa: E402
    advance_fire_ts, classify_due, next_fire_ts, parse_schedule)
from scheduler.store import AutomationStore  # noqa: E402
from scheduler.manager import SchedulerManager  # noqa: E402

from tests._harness import Harness  # noqa: E402

_h = Harness(style="summary", pad=True)
check = _h.check


def main() -> int:
    check("parse every", parse_schedule("every 10m") ==
          {"kind": "every", "interval_s": 600})
    check("parse once", parse_schedule("in 30m") ==
          {"kind": "once", "interval_s": 1800})
    check("parse daily", parse_schedule("daily 09:00") ==
          {"kind": "daily", "at_hm": "09:00"})
    check("parse at alias", parse_schedule("at 07:05") ==
          {"kind": "daily", "at_hm": "07:05"})
    check("bad times rejected", parse_schedule("at 25:99") is None)
    check("nonsense rejected", parse_schedule("whenever") is None)

    # WEEKDAYS (2026-11-05): days = 0-6 (Mon..Sun)
    tzname = "Europe/Berlin"
    # 2026-11-05 is a Thursday
    thu = datetime(2026, 11, 5, 12, 0, tzinfo=ZoneInfo(tzname)).timestamp()
    mon_wed = next_fire_ts({"kind": "daily", "at_hm": "09:00", "days": [0, 2]},
                           thu, tzname)
    check("weekday daily: Thu 12:00 -> next Mon 09:00",
          mon_wed == datetime(2026, 11, 9, 9, 0,
                              tzinfo=ZoneInfo(tzname)).timestamp())
    before_slot = datetime(2026, 11, 5, 8, 0,
                           tzinfo=ZoneInfo(tzname)).timestamp()
    same_day = next_fire_ts({"kind": "daily", "at_hm": "09:00", "days": [3]},
                            before_slot, tzname)
    check("weekday daily: Thu 08:00 -> same Thu 09:00",
          same_day == datetime(2026, 11, 5, 9, 0,
                               tzinfo=ZoneInfo(tzname)).timestamp())
    check("weekday daily: empty days list = every day",
          next_fire_ts({"kind": "daily", "at_hm": "09:00", "days": []},
                       thu, tzname) is not None)
    check("weekday daily: all-invalid days -> None",
          next_fire_ts({"kind": "daily", "at_hm": "09:00", "days": [9, -1]},
                       thu, tzname) is None)

    tz = ZoneInfo("Europe/Berlin")
    base = datetime(2026, 7, 1, 8, 0, tzinfo=tz).timestamp()
    nxt = next_fire_ts({"kind": "daily", "at_hm": "09:00"}, base,
                       "Europe/Berlin")
    check("daily next is today 09:00", nxt ==
          datetime(2026, 7, 1, 9, 0, tzinfo=tz).timestamp())
    after = datetime(2026, 7, 1, 10, 0, tzinfo=tz).timestamp()
    nxt2 = next_fire_ts({"kind": "daily", "at_hm": "09:00"}, after,
                        "Europe/Berlin")
    check("daily after slot rolls to tomorrow", nxt2 ==
          datetime(2026, 7, 2, 9, 0, tzinfo=tz).timestamp())

    every = {"kind": "every", "interval_s": 600}
    check("every next", next_fire_ts(every, 1000.0) == 1600.0)
    check("every advances whole slots", advance_fire_ts(every, 1000.0, 2500.0)
          == 2800.0)
    check("once never repeats", advance_fire_ts(
        {"kind": "once", "interval_s": 60}, 1000.0, 2000.0) is None)
    check("due window", classify_due(1000.0, 1030.0, 60) == "due")
    check("missed window", classify_due(1000.0, 1200.0, 60) == "missed")
    check("future is not due", classify_due(1000.0, 900.0, 60) == "not_due")

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "automations.json"
        store = AutomationStore(path)
        job = store.add({
            "name": "daily report",
            "prompt": "summarize the repo",
            "schedule": {"kind": "daily", "at_hm": "09:00"},
            "next_ts": time.time() + 60,
        })
        check("store assigns an id", bool(job.get("id")))
        check("store persists jobs", path.exists())
        loaded = AutomationStore(path)
        check("store reloads the job",
              loaded.get(job["id"]) is not None)
        upd = loaded.update(job["id"], {"enabled": False})
        check("store updates a job", upd is not None
              and upd["enabled"] is False)
        loaded.append_history({"job_id": job["id"], "status": "ok"})
        check("store keeps history", len(loaded.history(job["id"])) == 1)
        check("store deletes a job", loaded.delete(job["id"]) is True
              and loaded.get(job["id"]) is None)

        bad = Path(td) / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        recovered = AutomationStore(bad)
        check("corrupt store recovers to empty",
              recovered.jobs() == [] and bad.with_suffix(".corrupt.json").exists())

        # lane policies (no HTTP fire)
        import asyncio
        now = time.time()
        mgr = SchedulerManager(Path(td) / "lane.json", "http://127.0.0.1:1")
        skip_job = mgr.store.add({
            "name": "skip", "prompt": "x",
            "schedule": {"kind": "every", "interval_s": 600},
            "next_ts": now - 5, "policy": "skip"})
        mgr.lane_busy = lambda: True
        asyncio.run(mgr.tick(now))
        check("busy lane skips a due job",
              mgr.store.get(skip_job["id"])["last_status"] == "skipped_busy")
        check("skip keeps the interval alignment",
              mgr.store.get(skip_job["id"])["next_ts"] > now)

        queue_job = mgr.store.add({
            "name": "queue", "prompt": "x",
            "schedule": {"kind": "every", "interval_s": 600},
            "next_ts": now - 5, "policy": "queue"})
        asyncio.run(mgr.tick(now + 1))
        q = mgr.store.get(queue_job["id"])
        check("busy lane queues one pending fire",
              q["last_status"] == "queued" and bool(q.get("pending_fire")))

        fired = []
        async def _fake_fire(job, ts, reason=""):
            fired.append((job["id"], reason))
        mgr._fire = _fake_fire
        mgr.lane_busy = lambda: False
        asyncio.run(mgr.tick(now + 2))
        check("queued fire runs when the lane frees",
              fired and fired[0][0] == queue_job["id"]
              and mgr.store.get(queue_job["id"]).get("pending_fire") is None)

        mgr.lane_busy = lambda: True
        ok, note = mgr.run_now(skip_job["id"])
        check("run-now still refuses a busy lane",
              ok is False and "busy" in note)

        # AUDIT-PIN (2026-11-05): a queued job must not collect a bogus
        # skipped_busy entry on later busy ticks.
        qjob = mgr.store.get(queue_job["id"])
        qjob["pending_fire"] = {"due_ts": now, "queued_ts": now}
        before_hist = len(mgr.store.history(queue_job["id"]))
        asyncio.run(mgr.tick(now + 3))
        check("queued job is not double-counted as skipped_busy",
              len(mgr.store.history(queue_job["id"])) == before_hist)

        # A disabled job with a pending fire must not run later.
        mgr.lane_busy = lambda: False
        fired2 = []
        async def _fake_fire2(job, ts, reason=""):
            fired2.append(job["id"])
        mgr._fire = _fake_fire2
        dis = mgr.store.add({
            "name": "disabled", "prompt": "x",
            "schedule": {"kind": "every", "interval_s": 600},
            "next_ts": now - 5, "policy": "queue", "enabled": False})
        dis = mgr.store.get(dis["id"])
        dis["pending_fire"] = {"due_ts": now, "queued_ts": now}
        mgr.store.update(dis["id"], {"pending_fire": dis["pending_fire"]})
        asyncio.run(mgr.tick(now + 4))
        check("disabled job never fires (even queued)",
              not fired2 or dis["id"] not in fired2)

        # Zombie once-job (next_ts None) is swept with an expired entry.
        zm = SchedulerManager(Path(td) / "zombie.json", "http://127.0.0.1:1")
        zomb = zm.store.add({
            "name": "zombie", "prompt": "x",
            "schedule": {"kind": "once", "interval_s": 60},
            "next_ts": None, "enabled": True})
        asyncio.run(zm.tick(now + 5))
        check("zombie once job is swept",
              zm.store.get(zomb["id"]) is None
              and any(h.get("status") == "expired"
                      for h in zm.store.history(zomb["id"])))

        # Store caps and unique ids.
        from scheduler.store import MAX_JOBS as _MJ
        small = AutomationStore(Path(td) / "cap.json")
        added = []
        for i in range(min(_MJ, 8)):
            added.append(small.add({
                "name": f"j{i}", "prompt": "x",
                "schedule": {"kind": "every", "interval_s": 600},
                "next_ts": now}))
        check("store ids are unique",
              len({a["id"] for a in added}) == len(added))

    html = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")
    srv = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
    check("Automap is gone from the mode row",
          'data-mode="automap"' not in html)
    check("Automations tab and panel exist",
          'data-p="automations"' in html and 'id="p-automations"' in html)
    check("UI loads automations",
          "loadAutomations" in js and "automationSave" in js)
    check("engine starts the scheduler and router",
          "automations_router" in srv and "init_manager" in srv
          and "scheduler started" in srv)

    # ── Agentic/MCP/Telegram routing & UI checks (2026-11-13) ───────────
    import tools.definitions as TD
    check("direct_mcp unlocks MCP tools while direct keeps them out",
          TD.mcp_allowed_in_mode("direct_mcp")
          and not TD.mcp_allowed_in_mode("direct"))
    from routers.automations import _validate as _val_auto
    j_ag, err_ag = _val_auto({
        "name": "Mail TG",
        "prompt": "Summarize mails",
        "schedule": {"kind": "every", "interval_s": 1800},
        "mode": "agentic",
        "tools_tier": "mcp",
        "notify_telegram": True,
    })
    check("automations validator accepts agentic + mcp + notify_telegram",
          j_ag is not None and not err_ag
          and j_ag.get("mode") == "agentic"
          and j_ag.get("tools_tier") == "mcp"
          and j_ag.get("notify_telegram") is True, f"({j_ag}/{err_ag})")

    with tempfile.TemporaryDirectory() as td2:
        import asyncio
        fm = SchedulerManager(Path(td2) / "fire.json", "http://127.0.0.1:1")
        fjob = fm.store.add({
            "name": "Agentic MCP", "prompt": "check mail",
            "schedule": {"kind": "every", "interval_s": 1800},
            "mode": "agentic", "tools_tier": "mcp",
            "notify_telegram": True, "next_ts": time.time() - 1,
        })
        captured = {}
        tg_delivered = []
        async def _fake_chat(j): return "chat-auto-1"
        async def _fake_append(c, r, t, j): pass
        async def _fake_stream(body, max_rt):
            captured["body"] = dict(body)
            return ("Mail summary ready", "completed", "run-1")
        async def _fake_tg(j, ans, st, sr, files=None):
            tg_delivered.append((j["id"], ans, st))
        fm._ensure_chat = _fake_chat
        fm._append_turn = _fake_append
        fm._run_stream = _fake_stream
        fm._deliver_telegram = _fake_tg
        asyncio.run(fm._fire(fjob, time.time(), "due"))
        b = captured.get("body", {})
        check("SchedulerManager routes agentic+mcp+tg with mcp coder tier & source=telegram",
              b.get("mode") == "code_duo"
              and b.get("duo_agentic_mode") is True
              and b.get("agentic_force") is True
              and b.get("duo_coder_tool_tier") == "mcp"
              and b.get("direct_tools_tier") == "mcp"
              and b.get("source") == "telegram"
              and len(tg_delivered) == 1
              and tg_delivered[0][1] == "Mail summary ready", f"({b} / {tg_delivered})")

        # Tier matrix + stale _last_ask_preview reset
        tier_cases = {}
        for tname in ("off", "readonly", "research", "python", "full"):
            tjob = fm.store.add({
                "name": f"Tier {tname}", "prompt": "check",
                "schedule": {"kind": "every", "interval_s": 1800},
                "mode": "agentic", "tools_tier": tname,
                "next_ts": time.time() - 1,
            })
            captured.clear()
            fm._last_ask_preview = "stale question from earlier run"
            async def _empty_ok_stream(body, max_rt):
                captured["body"] = dict(body)
                return ("", "completed", "run-tier")
            fm._run_stream = _empty_ok_stream
            asyncio.run(fm._fire(tjob, time.time(), "due"))
            tier_cases[tname] = (dict(captured["body"]), fm._last_ask_preview,
                                 fm.store.get(tjob["id"]).get("last_status"))
        check("tier matrix maps off/readonly/research->readonly, python->core, full->full & sets tool_rounds=0 for off",
              tier_cases["off"][0].get("duo_coder_tool_tier") == "readonly"
              and tier_cases["off"][0].get("duo_tool_rounds") == 0
              and tier_cases["off"][0].get("duo_pre_explore") is False
              and tier_cases["readonly"][0].get("duo_coder_tool_tier") == "readonly"
              and tier_cases["research"][0].get("duo_coder_tool_tier") == "readonly"
              and tier_cases["python"][0].get("duo_coder_tool_tier") == "core"
              and tier_cases["full"][0].get("duo_coder_tool_tier") == "full",
              f"({tier_cases})")
        check("_last_ask_preview is reset at start of _fire so empty ok run stays ok",
              tier_cases["full"][1] == "" and tier_cases["full"][2] == "ok",
              f"({tier_cases['full']})")

        # _deliver_telegram: split Markdown before HTML + send_file attachment
        from unittest import mock as _mock
        from hivemind_gateway import main as _gw_main, send as _gw_send, state as _gw_state
        ws_att = Path(td2) / "ws"
        ws_att.mkdir()
        (ws_att / "report.txt").write_text("hello report", encoding="utf-8")
        long_md = ("**bold item**\n" + ("word " * 30) + "\n") * 80
        sent_calls = []

        async def _fake_send(api, owner_chat_id, chat_id, **kw):
            sent_calls.append(dict(kw))
            return {}

        class _DummyApi:
            def __init__(self, tok): pass
            async def close(self): pass

        with _mock.patch.object(_gw_main, "resolve_token", side_effect=_gw_main.StartupError("no token")):
            from routers.automations import _automation_capabilities
            from infra import mcp_client as _mc
            with _mock.patch.object(_mc, "enabled", return_value=True), \
                 _mock.patch.object(_mc, "status", return_value=[{"name": "mail", "ok": True}]):
                caps = _automation_capabilities()
        check("_automation_capabilities handles list mcp status & StartupError token",
              caps.get("mcp_enabled") is True
              and caps.get("mcp_servers") == ["mail"]
              and caps.get("telegram_configured") is False, f"({caps})")

        class _FakeGwState:
            owner_telegram_id = "123456"

        fm_tg = SchedulerManager(Path(td2) / "tg_real.json", "http://127.0.0.1:1")
        with _mock.patch.object(_gw_main, "resolve_token", return_value="123:tok"), \
             _mock.patch.object(_gw_state, "GatewayState", _FakeGwState), \
             _mock.patch("hivemind_gateway.telegram_api.TelegramApi", _DummyApi), \
             _mock.patch.object(_gw_send, "send", _fake_send):
            asyncio.run(fm_tg._deliver_telegram(
                {"id": "j1", "name": "Digest", "workspace": str(ws_att)},
                long_md, "ok", "completed",
                [{"path": "report.txt", "workspace": str(ws_att)}],
            ))
        text_chunks = [c for c in sent_calls if c.get("text")]
        doc_calls = [c for c in sent_calls if c.get("document_bytes")]
        check("_deliver_telegram splits Markdown before HTML conversion and attaches send_file",
              len(text_chunks) >= 2
              and all(c.get("parse_mode") == "HTML" and "<b>bold item</b>" in c["text"] for c in text_chunks)
              and len(doc_calls) == 1
              and doc_calls[0].get("filename") == "report.txt"
              and doc_calls[0].get("document_bytes") == b"hello report",
              f"(chunks={len(text_chunks)}, docs={len(doc_calls)})")

        # PUT /automations/{job_id} preserves last_status, last_duration_s & pending_fire
        import routers.automations as _ra
        j_put = fm.store.add({
            "name": "Put Keep", "prompt": "hi",
            "schedule": {"kind": "every", "interval_s": 600},
            "last_status": "ok", "last_duration_s": 4.2, "pending_fire": True,
        })
        class _FakeReq:
            def __init__(self, d): self._d = d
            async def json(self): return self._d
        with _mock.patch.object(_ra, "_mgr", return_value=fm):
            upd = asyncio.run(_ra.update_automation(j_put["id"], _FakeReq({"name": "Put Renamed"})))
        check("PUT preserves last_status, last_duration_s & pending_fire",
              upd.get("name") == "Put Renamed"
              and upd.get("last_status") == "ok"
              and upd.get("last_duration_s") == 4.2
              and upd.get("pending_fire") is True, f"({upd})")

    check("Automations UI exposes MCP tier, Telegram push & Agentic naming",
          'value="agentic"' in html and 'value="mcp"' in html
          and 'id="auto-notify-tg"' in html
          and 'automationApplyTemplate' in js and 'Code+Critic' in js)

    return _h.finish("scheduler_core")


if __name__ == "__main__":
    raise SystemExit(main())

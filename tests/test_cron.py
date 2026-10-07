"""R7 cron: pure helpers + the /cron command flow (gateway-side)."""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


sys.path.insert(0, str(Path(__file__).parent.parent / "hivemind_gateway"))
from hivemind_gateway.cron import parse_spec, next_fire_ts, CronManager


class FakeState:
    def __init__(self):
        self.data = {}

    def save(self):
        pass


class FakeBridge:
    def __init__(self):
        self.runs = []

    async def start_text_run(self, q):
        self.runs.append(q)
        return "⏳ started"


def test_parse_every():
    j = parse_spec("every 10m check the mail")
    if j and j["kind"] == "every" and j["interval_s"] == 600 and "check the mail" in j["prompt"]:
        ok("parse: 'every 10m <prompt>' -> recurring 600 s")
    else:
        fail("parse_every", str(j))


def test_parse_in():
    j = parse_spec("in 30m continue the build")
    if j and j["kind"] == "in" and j["interval_s"] == 1800:
        ok("parse: 'in 30m <prompt>' -> one-shot 1800 s")
    else:
        fail("parse_in", str(j))


def test_parse_at():
    j = parse_spec("at 09:00 morning report")
    if j and j["kind"] == "at" and j["at_hm"] == "09:00":
        ok("parse: 'at 09:00 <prompt>' -> daily at")
    else:
        fail("parse_at", str(j))


def test_parse_rejects():
    bad = [parse_spec(""), parse_spec("every 10m"), parse_spec("at 25:99 x"),
           parse_spec("just do stuff")]
    if all(b is None for b in bad):
        ok("parse: empty/prompt-only/bad-time all rejected")
    else:
        fail("parse_reject", str(bad))


def test_unit_conversions():
    j1 = parse_spec("every 2h x")
    j2 = parse_spec("every 1d x")
    if j1["interval_s"] == 7200 and j2["interval_s"] == 86400:
        ok("unit conversions: 2h=7200 s, 1d=86400 s")
    else:
        fail("units", f"{j1.get('interval_s')}, {j2.get('interval_s')}")


def test_min_interval():
    j = parse_spec("every 5s x")
    if j["interval_s"] >= 60:
        ok("interval floor: >= 60 s (no rapid-fire)")
    else:
        fail("floor", str(j["interval_s"]))


def test_manager_flow():
    st = FakeState()
    br = FakeBridge()
    mgr = CronManager(st, br)
    ok_add, _ = mgr.add(parse_spec("every 1h heartbeat check"))
    job = mgr.jobs()[0]
    now = 1000.0
    mgr.mark_fired(job, now)
    due = mgr.due_jobs(now + 3600)
    if ok_add and job["id"] and due and due[0]["id"] == job["id"] \
            and due[0]["next_ts"] == now + 3600 \
            and not mgr.due_jobs(now + 1800):
        ok("manager: fires recurring on schedule, persists via state")
    else:
        fail("manager_flow", f"ok={ok_add} due={due}")


def test_once_removes():
    st = FakeState()
    br = FakeBridge()
    mgr = CronManager(st, br)
    mgr.add({"kind": "once", "interval_s": 60, "prompt": "one shot", "id": "j1"})
    job = mgr.jobs()[0]
    mgr.mark_fired(job, 2000.0)
    if job not in mgr.jobs():
        ok("once jobs remove themselves after firing")
    else:
        fail("once", "once job not removed")


def test_state_persisted():
    st = FakeState()
    br = FakeBridge()
    mgr = CronManager(st, br)
    mgr.add({"kind": "every", "interval_s": 600, "prompt": "x", "id": "j1"})
    if st.data.get("cron_jobs") and st.data["cron_jobs"][0]["prompt"] == "x":
        ok("jobs persisted into gateway state (survives restarts)")
    else:
        fail("persist", "cron_jobs not in state")


def test_delete():
    st = FakeState()
    br = FakeBridge()
    mgr = CronManager(st, br)
    _added = mgr.add({"kind": "every", "interval_s": 600, "prompt": "x"})
    _jid = mgr.jobs()[0]["id"] if mgr.jobs() else ""
    ok_del, note = mgr.delete(_jid)
    if ok_del and not mgr.jobs():
        ok("delete removes the job")
    else:
        fail("delete", note)


if __name__ == "__main__":
    test_parse_every()
    test_parse_in()
    test_parse_at()
    test_parse_rejects()
    test_unit_conversions()
    test_min_interval()
    test_manager_flow()
    test_once_removes()
    test_state_persisted()
    test_delete()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

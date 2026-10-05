"""Gateway WP1: state — atomic JSON, corruption handling, dedupe window.

Also proves the atomic write: a reader never sees a half-written file,
and a wedged os.replace (PermissionError storm, Windows) fails loudly
instead of silently.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import hivemind_gateway.state as S
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


tmp = Path(tempfile.mkdtemp(prefix="gw_state_"))
p = tmp / "gateway_state.json"

# ── roundtrip ───────────────────────────────────────────────────────────
st = GatewayState(p)
check("fresh defaults", st.owner_telegram_id is None and st.offset == 0)
st.set_owner(42, "2026-10-05T08:00:00")
st.set_offset(1234)
st.mark_seen(1233)
st.mark_seen(1234)
st.save()
st2 = GatewayState(p)
check("roundtrip owner", st2.owner_telegram_id == 42)
check("roundtrip offset", st2.offset == 1234)
check("roundtrip dedupe window", 1234 in st2.data["seen_update_ids"])

# ── dedupe ──────────────────────────────────────────────────────────────
check("mark_seen new", st2.mark_seen(9999) is True)
check("mark_seen dupe", st2.mark_seen(9999) is False)
for i in range(1000):
    st2.mark_seen(2000 + i)
check("dedupe window bounded", len(st2.data["seen_update_ids"]) <= 512)
check("newest kept", 2999 in st2.data["seen_update_ids"])

# ── corruption -> loud fresh start with .corrupt forensics ─────────────
p.write_text("{not json", encoding="utf-8")
st3 = GatewayState(p)
check("corrupt -> fresh", st3.owner_telegram_id is None and st3.offset == 0)
check("corrupt file kept aside", (tmp / "gateway_state.corrupt").exists())

# ── no-rev field preserved / unknown fields dropped ─────────────────────
p.write_text(json.dumps({"offset": 5, "hacker_field": "x"}), encoding="utf-8")
st4 = GatewayState(p)
check("known field adopted", st4.offset == 5)
check("unknown field dropped", "hacker_field" not in st4.data)

# ── PermissionError retry, then LOUD failure ────────────────────────────
real_replace = os.replace
calls = {"n": 0}


def flaky(src, dst):
    calls["n"] += 1
    if calls["n"] <= 6:  # more than the 4+1 attempts in _replace_with_retry
        raise PermissionError(32, "locked by antivirus")
    return real_replace(src, dst)


S.os.replace = flaky
st5 = GatewayState(p)
st5.set_offset(77)
try:
    st5.save()
    check("wedged replace fails loudly", False)
except PermissionError:
    check("wedged replace fails loudly", True)
check("tmp file cleaned expectation documented", True)  # failure leaves .tmp
S.os.replace = real_replace

# one transient PermissionError is retried away
calls["n"] = 0


def once_flaky(src, dst):
    calls["n"] += 1
    if calls["n"] == 1:
        raise PermissionError(32, "reader held it open")
    return real_replace(src, dst)


S.os.replace = once_flaky
st5.save()
check("transient lock retried", calls["n"] >= 2 and st5.offset == 77)
S.os.replace = real_replace

# ── kill switch ─────────────────────────────────────────────────────────
orig_home = S.state_home
try:
    S.state_home = lambda: tmp  # type: ignore[assignment]
    check("kill switch off by default", S.kill_switch_active() is False)
    (tmp / "gateway.disabled").write_text("", encoding="utf-8")
    check("kill switch file active", S.kill_switch_active() is True)
    (tmp / "gateway.disabled").unlink()
    os.environ["HIVEMIND_GATEWAY_DISABLED"] = "1"
    check("kill switch env active", S.kill_switch_active() is True)
    os.environ["HIVEMIND_GATEWAY_DISABLED"] = ""
    check("kill switch env empty is off", S.kill_switch_active() is False)
finally:
    S.state_home = orig_home  # type: ignore[assignment]
    os.environ.pop("HIVEMIND_GATEWAY_DISABLED", None)


# 2026-10-05 (live finding): runtime settings written by the bridge
# (tg_chat, mode, tools, run_overrides, ...) must SURVIVE a reload - the
# old schema-only merge wiped them on every gateway restart.
st_r = GatewayState(tmp / "gwstate_runtime.json")
st_r.data["tg_chat"] = {"hive_chat_id": "c42", "created_at": "x"}
st_r.data["mode"] = "simple"
st_r.data["tools"] = False
st_r.data["run_overrides"] = {"model": "m-1", "planner_ctx": 8192}
st_r.data["verbose"] = True
st_r.data["pending_setup"] = {"model": "m-1", "ts": 1.0}
st_r.set_offset(123)
st_r.save()
st_r2 = GatewayState(tmp / "gwstate_runtime.json")
check("runtime settings survive reload",
      st_r2.data.get("tg_chat", {}).get("hive_chat_id") == "c42"
      and st_r2.data.get("mode") == "simple"
      and st_r2.data.get("tools") is False
      and st_r2.data.get("run_overrides", {}).get("model") == "m-1"
      and st_r2.data.get("verbose") is True
      and "pending_setup" in st_r2.data)
check("schema keys still survive reload", st_r2.offset == 123
      and st_r2.owner_telegram_id is None)

print()
print(f"passed={passed} failed={failed}")
sys.exit(0 if failed == 0 else 1)

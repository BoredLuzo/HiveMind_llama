"""Gateway WP1: auth classification + pairing state machine (pure).

Security table rows covered here: unknown user, group chat, update types,
forwarded message, pairing with 5 failed attempts, expired/used code.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hivemind_gateway import auth as A

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


OWNER = 111111


def _msg(uid=1, from_id=OWNER, chat_type="private", text="/status",
         forwarded=False, date=None):
    m = {
        "update_id": uid,
        "message": {
            "message_id": 10 + uid,
            "from": {"id": from_id, "is_bot": False},
            "chat": {"id": from_id, "type": chat_type},
            "text": text,
        },
    }
    if forwarded:
        m["message"]["forward_date"] = 1234567890
    if date is not None:
        m["message"]["date"] = date
    return m


def _cb(uid=1, from_id=OWNER, chat_type="private", data="nonce123"):
    return {
        "update_id": uid,
        "callback_query": {
            "id": "cbq1",
            "from": {"id": from_id},
            "message": {"message_id": 42, "chat": {"id": 42, "type": chat_type},
                        "date": 1700000000},
            "data": data,
        },
    }


# ── code format ─────────────────────────────────────────────────────────
codes = {A.generate_pairing_code() for _ in range(20)}
check("code length >= 8", all(len(c) >= A.MIN_CODE_CHARS for c in codes))
check("code alphabet is base32", all(set(c) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567") for c in codes))
check("codes unique", len(codes) == 20)

# ── parse + classify ────────────────────────────────────────────────────
p = A.parse_update(_msg())
check("parse owner private", A.classify(p, OWNER) == "owner")
check("parse unknown user", A.classify(A.parse_update(_msg(from_id=999999)), OWNER) == "unknown_user")
check("parse group chat", A.classify(A.parse_update(_msg(chat_type="group")), OWNER) == "non_private")
check("unpaired -> pair_window", A.classify(p, None) == "pair_window")

pe = A.parse_update(_msg(2, forwarded=True))
check("forwarded flagged", pe.is_forwarded is True)

pc = A.parse_update(_cb())
check("callback parsed", pc is not None and pc.kind == "callback_query"
      and pc.callback_id == "cbq1" and pc.chat_type == "private")
check("callback classified", A.classify(pc, OWNER) == "owner")
check("callback from stranger", A.classify(A.parse_update(_cb(from_id=999999)), OWNER) == "unknown_user")

pem = A.parse_update({"update_id": 5, "edited_message": _msg()["message"] | {"date": 1700000000}})
check("edited_message handled", pem is not None and pem.kind == "edited_message")

check("unsupported type None", A.parse_update({"update_id": 6, "channel_post": {}}) is None)
check("no update_id None", A.parse_update({"message": {}}) is None)
check("malformed from None-safe", A.classify(A.parse_update(
    {"update_id": 7, "message": {"chat": {"id": 1, "type": "private"}}}), OWNER) == "unknown_user")

# ── pairing machine ─────────────────────────────────────────────────────
clock = {"t": 1000.0}
pm = A.PairingManager(now=lambda: clock["t"])
code = pm.start_window()
check("verify correct code", pm.verify(code) is True)
check("pairing disabled after success", pm.enabled is False)
try:
    pm.verify(code)
    check("second use raises", False)
except A.PairingDisabled:
    check("second use raises (PairingDisabled)", True)
except A.PairingError as e:
    check("second use raises", False, f" (wrong exc: {type(e).__name__})")

pm2 = A.PairingManager(now=lambda: clock["t"])
code2 = pm2.start_window()
try:
    pm2.verify("WRONG")
    check("wrong code raises", False)
except A.PairingError:
    check("wrong code raises", True)
check("failed counted", pm2.failed_attempts == 1)
check("not locked after 1 failure", pm2.enabled and pm2.failed_attempts < A.MAX_FAILED_ATTEMPTS)

for i in range(4):
    try:
        pm2.verify("WRONG")
    except A.PairingLocked:
        pass
    except A.PairingError:
        pass
check("locked after 5 failures", pm2.failed_attempts >= A.MAX_FAILED_ATTEMPTS)
try:
    pm2.verify(code2)
    check("locked rejects correct code", False)
except A.PairingLocked:
    check("locked rejects correct code", True)
except A.PairingError:
    check("locked rejects correct code", False, " (wrong exc)")

# expired window
pm3 = A.PairingManager(now=lambda: clock["t"])
code3 = pm3.start_window()
clock["t"] += A.PAIR_TTL_S + 1
try:
    pm3.verify(code3)
    check("expired code denied", False)
except A.PairingError:
    check("expired code denied", True)
check("expired did not disable", pm3.enabled is True)

# whitespace-tolerant constant-time path (compare_digest on stripped guess)
pm4 = A.PairingManager(now=lambda: clock["t"])
code4 = pm4.start_window()
check("strip tolerated", pm4.verify(" " + code4 + "\n") is True)

# deep audit N1: non-ASCII guesses must be a safe DENY, never a TypeError
# (hmac.compare_digest raises on non-ASCII strings - realrun crash class)
pm5 = A.PairingManager(now=lambda: clock["t"])
code5 = pm5.start_window()
try:
    pm5.verify("\U0001F600 emoji pair")
    check("non-ascii guess denied", False)
except A.PairingDenied:
    check("non-ascii guess denied", True)
except TypeError as exc:
    check("non-ascii guess denied", False, f" (TypeError: {exc})")
check("non-ascii counted as failure", pm5.failed_attempts == 1)

# ── approvals nonce store (pure) ────────────────────────────────────────
from hivemind_gateway.approvals import ApprovalStore

clock2 = {"t": 1000.0}
store = ApprovalStore(ttl_s=120, now=lambda: clock2["t"])
nonce = store.create("appr1", "111", "hash", 42)
check("nonce usable length", bool(nonce) and len(nonce) >= 12)
check("consume ok", (store.consume(nonce, "111") or {}).get("approval_id") == "appr1")
check("replay rejected", store.consume(nonce, "111") is None)
n2 = store.create("appr2", "111", "h2", 43)
check("wrong chat rejected", store.consume(n2, "222") is None)
n3 = store.create("appr3", "111", "h3", 44)
clock2["t"] += 121
check("expired rejected", store.consume(n3, "111") is None)
check("sweep removes dead", store.sweep() >= 2)

# ── command whitelist (pure) ────────────────────────────────────────────
from hivemind_gateway.commands import command_from_message, parse_command

check("whitelist /status", parse_command("/status@Bot hello") == ("status", "hello"))
check("non-command None", parse_command("hello /status") is None)
check("unknown command None", parse_command("/rm -rf") is None)
check("bare slash None", parse_command("/") is None)


class _P:
    text = "/stop"
    is_forwarded = False


check("own command ok", command_from_message(_P()) == ("stop", ""))
_P.is_forwarded = True
check("forwarded never a command", command_from_message(_P()) is None)

print()
print(f"passed={passed} failed={failed}")
sys.exit(0 if failed == 0 else 1)

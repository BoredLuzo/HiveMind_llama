"""Gateway entrypoint — config, token, state, pairing, poll loop.

WP1 scope: the bot runs, pairs the owner, and answers whitelisted
commands that do not touch HiveMind. HiveMind calls (/new, /stop, runs)
arrive with WP2 — non-pair commands answer honestly as "not in this
build".

Hard rules implemented here:
  - token ONLY from env (HIVEMIND_TG_TOKEN) or Windows Credential Manager;
    never from a file, never logged (redaction filter on every handler)
  - backlog drop at startup (offset to the newest update)
  - updates older than update_max_age_s are dropped
  - dedupe by update_id; offset persisted BEFORE an update is processed
    (at-most-once)
  - unknown users: silently dropped, rate-limited log
  - kill switch: gateway.disabled file or HIVEMIND_GATEWAY_DISABLED env
  - exactly one gateway process per token (lock file with liveness check)
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import os
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

from httpx import HTTPError

from . import auth as gw_auth
from . import commands as gw_commands
from . import send as gw_send
from . import state as gw_state
from .bridge import RunBridge
from .config import GatewayConfig, load_gateway_config, resolve_config_path
from .hive_client import HiveClient, HiveUnreachable
from .redaction import attach_redaction_to_root
from .send import PolicyViolation
from .state import GatewayState, kill_switch_active
from .telegram_api import TelegramApi, TelegramApiError

log = logging.getLogger("hivemind_gateway")

TOKEN_ENV_VAR = "HIVEMIND_TG_TOKEN"


class StartupError(RuntimeError):
    """Fatal, human-readable startup condition (fail fast)."""


# -- token ----------------------------------------------------------------


def resolve_token() -> str:
    """Environment first, then Windows Credential Manager (keyring).
    Never accepts a token from a file."""
    tok = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if tok:
        return tok
    try:
        import keyring  # optional dependency; guarded on purpose
        tok = (keyring.get_password("hivemind_gateway", "bot_token") or "").strip()
    except ImportError:
        tok = ""
    if tok:
        return tok
    raise StartupError(
        f"no bot token: set {TOKEN_ENV_VAR} or store it in the Windows "
        "Credential Manager under hivemind_gateway/bot_token")


# -- single instance ------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Liveness probe. Two Windows traps handled here:
    - os.kill(pid, 0) KILLS the target on Windows (TerminateProcess) —
      never use it.
    - OpenProcess SUCCEEDS for a TERMINATED process as long as anyone
      (e.g. the parent shell) still holds its handle — tasklist shows
      nothing, yet the lock check would refuse forever (realrun bug #5).
      GetExitCodeProcess must confirm STILL_ACTIVE (259)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                 pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(handle,
                                          ctypes.byref(exit_code)):
                return False
            return exit_code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_birth(pid: int) -> int | None:
    """Creation timestamp of the process (Windows FILETIME, 100 ns since
    1601 / posix boot-time ticks), or None if it cannot be read.

    Why: PIDs are recycled. A lock that only stores the pid claims the
    gateway is 'still running' when the pid now belongs to some random
    other process — the second gateway then refuses to start forever
    (realrun bug #3, 2026-10-05). Comparing the birth stamp makes
    pid-reuse detectable."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                 pid)
        if not handle:
            return None
        try:
            created, exited, kernel, user = (ctypes.c_ulonglong(),
                                             ctypes.c_ulonglong(),
                                             ctypes.c_ulonglong(),
                                             ctypes.c_ulonglong())
            if not k32.GetProcessTimes(handle,
                                       ctypes.byref(created),
                                       ctypes.byref(exited),
                                       ctypes.byref(kernel),
                                       ctypes.byref(user)):
                return None
            return created.value
        finally:
            k32.CloseHandle(handle)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # field 22 (1-based) = starttime, sits after the ')' of the comm
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _lock_holder_is_live(record: dict) -> bool:
    """True only when the recorded pid is alive AND was born before the
    lock was written — i.e. it is really the process that took the lock,
    not a recycled pid."""
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    if not _pid_alive(pid):
        return False
    birth = _process_birth(pid)
    if birth is None:
        # cannot verify identity; refuse to steal a live pid's lock
        return True
    stored = record.get("birth")
    if not isinstance(stored, int):
        return True  # unknown identity — conservative, keep refusing
    return stored == birth


def acquire_instance_lock() -> None:
    """One gateway process per bot token. A stale lock (dead pid, or a
    recycled pid belonging to someone else) is replaced, a live one is
    fatal."""
    import json
    p = gw_state.lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError
        except (OSError, ValueError):
            record = {}
        if _lock_holder_is_live(record):
            raise StartupError(
                f"another gateway instance seems to run (pid "
                f"{record.get('pid')}, started from "
                f"{record.get('cwd') or 'unknown folder'}).\n"
                f"  Stop it:  start_gateway.bat stop\n"
                f"  (or the HiveMind UI: Telegram Gateway card -> "
                f"Process -> Stop)\n"
                f"  Lock: {p}")
        log.info("[LOCK] stale gateway lock replaced (pid %s)",
                 record.get("pid"))
        try:
            p.unlink()
        except OSError:
            pass
    birth = _process_birth(os.getpid())
    payload = json.dumps({"pid": os.getpid(), "birth": birth,
                          "cwd": os.getcwd()})
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
    except FileExistsError:
        raise StartupError("gateway lock taken concurrently") from None


def stop_instance(killer=None) -> str:
    """CLI 'stop': stop the recorded instance and clear the lock. The
    birth-stamp check makes sure a recycled pid is never killed (same
    guard as acquire_instance_lock). `killer` is injectable for tests."""
    import json
    if killer is None:
        import subprocess

        def killer(pid: int) -> None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True)
    p = gw_state.lock_path()
    if not p.exists():
        return ("No gateway lock found — no instance is recorded as "
                "running. Nothing to stop.")
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(rec, dict):
            raise ValueError
    except (OSError, ValueError):
        try:
            p.unlink()
        except OSError:
            pass
        return "Lock file was unreadable — removed. You can start now."
    pid = rec.get("pid")
    if not isinstance(pid, int) or pid <= 0 or not _pid_alive(pid):
        try:
            p.unlink()
        except OSError:
            pass
        return (f"Stale lock (pid {pid} is gone) — removed. "
                "You can start now.")
    real = _process_birth(pid)
    stored = rec.get("birth")
    if stored is not None and real is not None and real != stored:
        try:
            p.unlink()
        except OSError:
            pass
        return (f"Stale lock (pid {pid} was recycled by another "
                "process) — removed. You can start now.")
    killer(pid)
    try:
        p.unlink()
    except OSError:
        pass
    return (f"Gateway instance stopped (pid {pid}, from "
            f"{rec.get('cwd') or 'unknown folder'}).")


# -- rate limiting --------------------------------------------------------


class RateLimiter:
    def __init__(self, per_min: int):
        self._per_min = per_min
        self._hits: dict[str, deque] = defaultdict(lambda: deque())

    def allow(self, key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        q = self._hits[key]
        while q and now - q[0] > 60.0:
            q.popleft()
        if len(q) >= self._per_min:
            return False
        q.append(now)
        return True

    def should_log_drop(self, key: str, min_gap_s: float = 30.0) -> bool:
        """Rate-limited logging for dropped strangers: at most one line
        per key per min_gap_s."""
        now = time.time()
        key = "log:" + key
        q = self._hits[key]
        if q and now - q[-1] < min_gap_s:
            return False
        q.append(now)
        return True


# -- update handling ------------------------------------------------------


class Gateway:
    def __init__(self, api: TelegramApi, cfg: GatewayConfig,
                 state: GatewayState):
        self.api = api
        self.cfg = cfg
        self.state = state
        self.pairing = gw_auth.PairingManager()
        self.limiter = RateLimiter(cfg.rate_limit_per_min)
        self.owner_id: int | None = state.owner_telegram_id
        self._redaction = attach_redaction_to_root([os.environ.get(
            TOKEN_ENV_VAR, "")])
        self.hive = HiveClient(cfg.hive_base_url)
        self.bridge = RunBridge(self.hive, state, cfg, self)
        # Set by the startup health probe: True only when the engine's
        # /health carries gateway_overrides=True, i.e. the engine lifts
        # duo_action_approval_enabled (and the other /stream overrides)
        # from the run body. False means VERSION SKEW (new gateway, old
        # engine): the body key would be silently ignored there and the
        # forced approval gate would be dead — /status says so, the owner
        # must update the engine or switch the global toggle on.
        self.engine_gate_support = True
        self.engine_version = "?"
        # phone restriction (UI toggle): boot default = restricted
        # (safe by default); the first settings fetch corrects it.
        self.phone_restricted = True

    # -- outbound (always via send()) ------------------------------------

    async def reply(self, chat_id: str | int, text: str,
                    reply_to_message_id: int | None = None) -> None:
        await gw_send.send(self.api, self.owner_id, chat_id, text=text,
                           reply_to_message_id=reply_to_message_id)

    # -- messenger interface for the bridge (owner-addressed by policy) --

    async def send_message(self, text: str, parse_mode: str | None = None,
                           reply_markup: dict | None = None) -> dict:
        return await gw_send.send(self.api, self.owner_id, self.owner_id,
                                  text=text, parse_mode=parse_mode,
                                  reply_markup=reply_markup)

    async def edit_message(self, message_id: int, text: str) -> None:
        await gw_send.send(self.api, self.owner_id, self.owner_id,
                           new_text=text, message_id=message_id)

    async def send_document(self, data: bytes, filename: str) -> None:
        await gw_send.send(self.api, self.owner_id, self.owner_id,
                           document_bytes=data, filename=filename)

    # -- commands ---------------------------------------------------------

    async def handle_owner_update(self, p: gw_auth.ParsedUpdate) -> None:
        cmd = gw_commands.command_from_message(p)
        if cmd is None:
            if p.kind == "message" and not p.is_forwarded:
                # non-text update (photo/document/voice/sticker): rejected
                # with a note until WP5 (brief: SECURITY/Inputs)
                await self.reply(p.chat_id,
                                 "Text only in this build (photos and "
                                 "steering arrive with WP5).",
                                 reply_to_message_id=p.message_id)
            return
        name, arg = cmd
        if name == "pair":
            await self.cmd_pair(p, arg)
        elif name == "start":
            await self.reply(p.chat_id,
                             "HiveMind gateway active. Send any text "
                             "to start a run. /help for commands.",
                             reply_to_message_id=p.message_id)
        elif name == "help":
            await self.reply(p.chat_id, gw_commands.HELP_TEXT,
                             reply_to_message_id=p.message_id)
        elif name == "new":
            await self._owner_bridge_call(p, self.bridge.new_chat())
        elif name == "stop":
            # never rate-limited: the safety valve must always work
            note = await self.bridge.stop()
            mirror_note = await self.bridge.stop_mirror()
            if mirror_note:
                note += "\n" + mirror_note
            await self.reply(p.chat_id, note,
                             reply_to_message_id=p.message_id)
        elif name == "workspace":
            await self._owner_bridge_call(p, self.bridge.workspace_text(arg))
        elif name == "tools":
            await self.reply(p.chat_id, self.bridge.tools_text(arg),
                             reply_to_message_id=p.message_id)
        elif name == "gate":
            await self.reply(p.chat_id,
                             await self.bridge.gate_text(arg),
                             reply_to_message_id=p.message_id)
        elif name == "status":
            await self.reply(p.chat_id, self.bridge.status_text(),
                             reply_to_message_id=p.message_id)
        elif name == "verbose":
            await self.reply(p.chat_id, self.bridge.toggle_verbose(),
                             reply_to_message_id=p.message_id)
        elif name == "mode":
            await self.reply(p.chat_id, self.bridge.mode_text(arg),
                             reply_to_message_id=p.message_id)
        elif name == "models":
            await self._owner_bridge_call(p, self.bridge.models_text())
        elif name == "setmodel":
            await self._owner_bridge_call(p, self.bridge.set_model(arg))
        elif name == "cancel":
            await self.reply(p.chat_id, self.bridge.cancel_setup(),
                             reply_to_message_id=p.message_id)
        elif name == "lock":
            await self.reply(p.chat_id,
                             "/lock kommt mit WP6 (Kill-Switch).",
                             reply_to_message_id=p.message_id)
        else:
            await self.reply(p.chat_id,
                             f"/{name} ist nicht in diesem Build.",
                             reply_to_message_id=p.message_id)

    async def _owner_bridge_call(self, p: gw_auth.ParsedUpdate, coro) -> None:
        try:
            note = await coro
        except (HiveUnreachable, HTTPError, OSError) as exc:
            note = f"🔌 HiveMind unreachable: {exc}"
        await self.reply(p.chat_id, note,
                         reply_to_message_id=p.message_id)

    async def start_owner_run(self, p: gw_auth.ParsedUpdate) -> None:
        """A plain owner text message = a run — unless a mirrored engine
        run consumes it (approval answer / steering, checked FIRST: audit
        G8 — an open approval card must never be eaten by a pending
        /setModel flow) or a /setModel flow is pending (numeric answer)."""
        q = p.text.strip()
        if not q:
            return
        # P10 takeover: while a mirrored engine run is active, phone
        # texts belong to that run (approval answers / steering), they
        # never start a second run.
        if self.bridge.mirror_intercept(q):
            note = await self.bridge.mirror_send(q)
            await self.reply(p.chat_id, note,
                             reply_to_message_id=p.message_id)
            return
        # ask mode: an OWN phone-run approval is waiting — 1/2/3 answers
        # it, anything else gets the waiting hint (the run is paused; a
        # second run would only get the busy note anyway).
        if self.bridge.own_approval_pending():
            note = await self.bridge.own_answer(q)
            await self.reply(p.chat_id, note,
                             reply_to_message_id=p.message_id)
            return
        setup_note = await self.bridge.consume_setup(q)
        if setup_note is not None:
            await self.reply(p.chat_id, setup_note,
                             reply_to_message_id=p.message_id)
            return
        if len(q) > self.cfg.max_text_chars:
            await self.reply(p.chat_id,
                             f"❌ Text zu lang ({len(q)} > "
                             f"{self.cfg.max_text_chars} Zeichen).",
                             reply_to_message_id=p.message_id)
            return
        note = await self.bridge.start_text_run(q)
        if note and note.startswith(("⏳", "🔌", "❌")):
            # busy/offline notes come back as messages; successful runs
            # already delivered their own output
            await self.reply(p.chat_id, note,
                             reply_to_message_id=p.message_id)

    async def cmd_pair(self, p: gw_auth.ParsedUpdate, arg: str) -> None:
        if self.owner_id is not None:
            await self.reply(p.chat_id, "Already paired.",
                             reply_to_message_id=p.message_id)
            return
        if p.chat_type != "private":
            log.info("[PAIR] rejected: non-private chat from=%s", p.from_id)
            return
        try:
            ok = self.pairing.verify(arg)
        except gw_auth.PairingLocked:
            # NO reply to an unpaired sender: until the owner is bound,
            # everyone is a stranger (brief: "No answer to strangers").
            # The console is the pairing interface — it shows attempts.
            log.warning("[PAIR] LOCKED — restart the gateway for a fresh "
                        "window (sender=%s)", _mask_tid(p.from_id))
            return
        except gw_auth.PairingDisabled:
            log.info("[PAIR] attempt while already paired (sender=%s)",
                     _mask_tid(p.from_id))
            return
        except gw_auth.PairingError:
            log.warning("[PAIR] failed attempt (sender=%s, %d/%d) — "
                        "code wrong or expired",
                        _mask_tid(p.from_id), self.pairing.failed_attempts,
                        gw_auth.MAX_FAILED_ATTEMPTS)
            return
        if not ok:  # pragma: no cover - verify returns True or raises
            return
        self.owner_id = p.from_id
        self.state.set_owner(
            p.from_id,
            _dt.datetime.now().astimezone().isoformat(timespec="seconds"))
        self.state.save()
        log.warning("[PAIR] owner bound (telegram id %s)", _mask_tid(p.from_id))
        await self.reply(p.chat_id,
                         "Paired. This Telegram account is now the owner.",
                         reply_to_message_id=p.message_id)


def _mask_tid(tid) -> str:
    """Telegram ids in log lines: last 3 digits only. Logs end up in
    backups; the full id lives in Telegram itself if the owner ever
    really needs it."""
    s = str(tid or "")
    return "…" + s[-3:] if len(s) > 3 else "…"


def _is_stale(p: gw_auth.ParsedUpdate, max_age_s: int) -> bool:
    if p.date is None:
        return False
    return time.time() - p.date > max_age_s


async def recover_orphan_run(gw: Gateway) -> None:
    """Restart with a running run (brief: SECURITY/Operations): check
    run_active against the server, deny persisted open approvals, and ask
    the owner about aborting. Never restart the run automatically."""
    run = gw.state.data.get("active_run")
    if not run:
        return
    rid = run.get("run_id")
    if gw.state.data.get("open_approvals"):
        # restart-deny contract (ask mode): a waiting approval must not
        # silently survive the gateway restart — deny it on the engine.
        for _rid, _oa in list(gw.state.data["open_approvals"].items()):
            try:
                await gw.hive.decide_approval(
                    _rid, "3",
                    decision_id=(_oa or {}).get("decision_id") or "",
                    tool=(_oa or {}).get("tool") or "")
                log.warning("[RECOVER] denied persisted approval for %s",
                            _rid)
            except (HiveUnreachable, OSError) as exc:
                log.warning("[RECOVER] deny failed for %s: %s", _rid, exc)
        gw.state.data["open_approvals"] = {}
        gw.state.save()
    try:
        j = await gw.hive.journal()
        still_active = (isinstance(j, dict) and bool(j.get("active"))
                        and not j.get("done") and not j.get("aborted")
                        and (not rid or str(j.get("run_id")) == str(rid)))
    except (HiveUnreachable, OSError):
        still_active = True  # assume the worst; do not silently clear
    if not still_active:
        gw.state.set_active_run(None)
        gw.state.save()
        log.info("[RECOVER] orphan run %s is no longer active — cleared",
                 rid)
        return
    log.warning("[RECOVER] orphan run %s still active", rid)
    if gw.owner_id is not None:
        try:
            await gw.reply(
                gw.owner_id,
                f"⚠️ Run {rid} was still active when the gateway "
                "restarted. /stop aborts it — otherwise it keeps running detached and holds VRAM.")
        except TelegramApiError as exc:
            log.error("[RECOVER] notice failed: %s", exc)


async def process_update(gw: Gateway, raw: dict) -> None:
    p = gw_auth.parse_update(raw)
    if p is None:
        return
    verdict = gw_auth.classify(p, gw.owner_id)
    if verdict == "owner":
        if _is_stale(p, gw.cfg.update_max_age_s):
            log.info("[DROP] stale update %s (%s)", p.update_id, p.kind)
            return
        cmd = gw_commands.command_from_message(p)
        if cmd and cmd[0] == "stop":
            # the safety valve is never rate-limited
            await gw.handle_owner_update(p)
            return
        if not gw.limiter.allow(str(p.from_id)):
            log.warning("[RATE] owner hit the rate limit (drop)")
            await gw.reply(p.chat_id,
                           "⏳ Rate limit reached — wait a moment.",
                           reply_to_message_id=p.message_id)
            return
        if cmd:
            await gw.handle_owner_update(p)
        elif p.kind == "callback_query":
            # 2026-10-05: tappable approval buttons (mirror cards) —
            # callback_data 'appr:<rid>:<1|2|3>'. Taps count toward the
            # rate limit (20/min is plenty); the toast answers via
            # answerCallbackQuery.
            if p.text.startswith("appr:"):
                note = await gw.bridge.approval_callback(p.callback_id,
                                                         p.text)
                if p.callback_id:
                    try:
                        await gw.api.answer_callback_query(p.callback_id,
                                                           text=note)
                    except TelegramApiError as exc:
                        log.warning("[APPR] toast failed: %s", exc)
            else:
                log.info("[DROP] owner callback %s", p.update_id)
        elif p.kind == "edited_message":
            # audit G9: an edit of a recent text arrives as a NEW update
            # (fresh update_id — dedupe passes) with the ORIGINAL date, so
            # an edit inside the 60 s window would start a second run for
            # text that already ran. Edits never re-run; commands in edits
            # still work (handled above).
            log.info("[DROP] edited_message %s (edits never re-run)",
                     p.update_id)
        else:
            await gw.start_owner_run(p)
        return
    if verdict == "pair_window":
        # Unpaired: the ONLY thing anyone may do here is /pair with the
        # one-time console code. Everything else is silently dropped —
        # no answers to strangers, even before an owner exists.
        if p.is_forwarded or _is_stale(p, gw.cfg.update_max_age_s):
            log.info("[PAIR] dropped (forwarded or stale) from=%s",
                     p.from_id)
            return
        cmd = gw_commands.command_from_message(p)
        if cmd and cmd[0] == "pair":
            log.info("[PAIR] attempt from=%s", p.from_id)
            await gw.handle_owner_update(p)
        elif gw.limiter.should_log_drop(str(p.from_id)):
            log.info("[DROP] %s from=%s (only /pair accepted while "
                     "unpaired)", verdict, p.from_id)
        return
    # everyone else: silent. rate-limited log only, NEVER an answer.
    if gw.limiter.should_log_drop(str(p.from_id)):
        log.info("[DROP] %s from=%s chat_type=%s kind=%s",
                 verdict, p.from_id, p.chat_type, p.kind)


def ensure_enabled(cfg: GatewayConfig) -> None:
    """MASTER SWITCH (fail-closed): the gateway must be turned on
    deliberately, in exactly one of two places, or it refuses to start.
    An attacker gains nothing from a feature that is off by default."""
    if cfg.telegram_enabled:
        return
    env = os.environ.get("HIVEMIND_GATEWAY_ENABLED", "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        log.warning("gateway enabled via HIVEMIND_GATEWAY_ENABLED env "
                    "(config telegram_enabled is false)")
        return
    raise StartupError(
        "gateway is DISABLED (fail-closed master switch). Turn it on in "
        "ONE of these places:\n"
        "  1. one-time setup (writes it for you):  start_gateway.bat setup\n"
        "  2. gateway.toml next to the gateway:  telegram_enabled = true\n"
        "  3. environment:  $env:HIVEMIND_GATEWAY_ENABLED = \"1\"\n"
        "Nothing polls Telegram while the switch is off.")


async def _ui_veto_active(gw: Gateway) -> bool:
    """True when the HiveMind settings EXPLICITLY switch the link off
    (telegram_gateway_enabled=false). Key absent = not configured = the
    local master switch decides. Any error reaching the server (offline,
    broken JSON) keeps the gateway up — the runs report engine problems
    readably, the control plane must not flap with them."""
    try:
        s = await gw.hive.settings()
    except (HiveUnreachable, OSError, ValueError):
        return False
    return "telegram_gateway_enabled" in s and not s["telegram_gateway_enabled"]


async def _mirror_loop(gw: Gateway) -> None:
    """P10: watch the engine journal for UI runs and mirror them to the
    phone (status, approvals relayed as 1/3, steering). Runs next to the
    Telegram poll loop; every error is contained — the mirror must
    never take the gateway down."""
    while True:
        if kill_switch_active():
            break
        # P8-lite supervisor heartbeat: the UI's start/stop/status reads
        # this liveness beacon (fire-and-forget, see hive_client). The
        # process birth (FILETIME) lets /gateway/stop verify the pid was
        # not recycled by another process inside the freshness window
        # (deep audit 2026-10-05).
        await gw.hive.heartbeat(os.getpid(), {
            "paired": gw.owner_id is not None,
            "mode": gw.state.data.get("mode") or "",
            "restricted": getattr(gw, "phone_restricted", None),
            "birth": _process_birth(os.getpid()),
        })
        await gw.bridge.mirror_tick()
        await asyncio.sleep(5.0)


async def run() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg_path = resolve_config_path(start=os.getcwd())
    cfg = GatewayConfig()
    if cfg_path is not None:
        cfg = load_gateway_config(cfg_path)
        log.info("config loaded from %s", cfg_path)
    ensure_enabled(cfg)
    token = resolve_token()
    acquire_instance_lock()
    state = GatewayState()
    api = TelegramApi(token)
    gw = Gateway(api, cfg, state)
    # 2026-10-05: the startup filter only carries the ENV token — a token
    # from the Credential Manager would never be in the literal scrub
    # list. Feed the RESOLVED token to the same filter (its docstring
    # promised exactly this).
    gw._redaction.add_secret(token)

    # audit F3 + version-skew detection: probe the engine once. Not fatal
    # (runs report engine problems readably), but the console should say
    # upfront when nothing is listening — and WHEN THE ENGINE IS TOO OLD
    # to honor the gateway's forced approval gate (gateway_overrides
    # marker missing): in that state the body key would be ignored and
    # phone runs would only be gated by the engine-global toggle.
    try:
        h = await gw.hive.health()
        gw.engine_gate_support = bool(h.get("gateway_overrides"))
        gw.engine_version = str(h.get("version") or "?")
        log.info("[HIVE] engine reachable at %s (gateway_overrides=%s)",
                 cfg.hive_base_url, gw.engine_gate_support)
        if not gw.engine_gate_support:
            log.warning(
                "[HIVE] ENGINE TOO OLD for the forced approval gate — "
                "duo_action_approval_enabled from the run body will be "
                "IGNORED. Update the engine, or switch the toggle on "
                "globally in the UI (/status shows this too).")
    except (HiveUnreachable, OSError, ValueError) as exc:
        log.warning("[HIVE] engine NOT reachable at startup (%s) — runs "
                    "will report offline until it is up", exc)

    if kill_switch_active():
        log.warning("kill switch active (gateway.disabled or env) — exiting")
        return 0

    if gw.owner_id is None:
        code = gw.pairing.start_window()
        print("=" * 60, file=sys.stderr)
        print("  PAIRING CODE (valid 5 min, one-time):", file=sys.stderr)
        print(f"      {code}", file=sys.stderr)
        print("  Send:  /pair <code>   from your Telegram account",
              file=sys.stderr)
        print("=" * 60, file=sys.stderr)
    else:
        log.warning("owner already bound (%s) — pairing disabled",
                    _mask_tid(gw.owner_id))

    # BACKLOG DROP: jump to the newest update, discard everything older.
    try:
        newest = await api.get_updates(offset=-1, timeout_s=0)
    except TelegramApiError as exc:
        log.error("getUpdates failed at startup: %s", exc)
        return 1
    if newest:
        state.set_offset(newest[-1]["update_id"] + 1)
        state.save()
        log.info("backlog dropped: starting at offset %s", state.offset)

    await recover_orphan_run(gw)

    mirror_task = asyncio.create_task(_mirror_loop(gw))

    try:
        while True:
            if kill_switch_active():
                log.warning("kill switch activated — stopping")
                break
            # UI VETO (the remote off switch): when the HiveMind settings
            # explicitly carry telegram_gateway_enabled=false, the whole
            # link shuts down from the browser. Key absent = the local
            # master switch decides; server errors keep the gateway up.
            if await _ui_veto_active(gw):
                log.warning("telegram_gateway_enabled=false in "
                            "HiveMind settings — shutting down")
                break
            try:
                updates = await api.get_updates(offset=state.offset,
                                                timeout_s=cfg.long_poll_timeout_s)
            except TelegramApiError as exc:
                if "conflict" in exc.description.lower():
                    # 409 from Telegram: a webhook or a SECOND poller is
                    # active on this token. Retrying forever would just
                    # fight the other poller — fail loudly instead.
                    log.error(
                        "getUpdates CONFLICT: another poller or a webhook "
                        "is active on this bot token. Stop the other "
                        "instance / delete the webhook, then restart. (%s)",
                        exc)
                    return 3
                if exc.retry_after:
                    await asyncio.sleep(exc.retry_after + 1)
                    continue
                log.error("getUpdates failed: %s — retrying in 5s", exc)
                await asyncio.sleep(5)
                continue
            for raw in updates:
                uid = raw.get("update_id")
                if not isinstance(uid, int):
                    continue
                # AT-MOST-ONCE: persist offset BEFORE processing.
                state.set_offset(uid + 1)
                if state.mark_seen(uid):
                    state.save()
                    try:
                        await process_update(gw, raw)
                    except (TelegramApiError, HiveUnreachable, HTTPError,
                            OSError, ValueError, PolicyViolation,
                            TypeError, KeyError, AttributeError) as exc:
                        # ONE broken update must never kill the poll loop
                        # (realrun bug #4: a raw ConnectError did exactly
                        # that). Log loudly, keep polling.
                        # audit G6: same containment breadth as the mirror
                        # loop (N3) — N1 was exactly this crash class with
                        # phone-controlled payload shapes.
                        log.error("update %s handling failed: %s: %s",
                                  uid, type(exc).__name__, exc)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.warning("gateway stopped")
    finally:
        mirror_task.cancel()
        await api.close()
        await gw.hive.close()
    return 0


async def perform_token_setup(token: str, store=None) -> str:
    """WP6 setup path: validate the token against Telegram (getMe) and
    store it in the Windows Credential Manager. Returns the bot username
    for the confirmation line. The token is never printed and never
    written to a file. Injectable `store` for tests."""
    token = (token or "").strip()
    if not token:
        raise StartupError("no token entered")
    api = TelegramApi(token)
    try:
        me = await api.get_me()
    finally:
        await api.close()
    username = str(me.get("username") or "?")
    if store is None:
        try:
            import keyring
        except ImportError:
            raise StartupError(
                "keyring not installed — install it into the SAME "
                "interpreter that runs the gateway:  python -m pip "
                "install keyring   (project-venv installs, which ship "
                "without pip:  uv pip install --python "
                ".venv\\Scripts\\python.exe keyring)") from None
        store = keyring.set_password
    store("hivemind_gateway", "bot_token", token)
    return username


def _setup_token_cli() -> int:
    import getpass
    print("Telegram bot token setup — validates the token and stores it "
          "in the Windows Credential Manager (hivemind_gateway / "
          "bot_token). Nothing is written to disk, history or logs.")
    tok = getpass.getpass("Bot token (input hidden): ")
    try:
        username = asyncio.run(perform_token_setup(tok))
    except TelegramApiError as exc:
        print(f"setup failed: the token was rejected by Telegram ({exc})",
              file=sys.stderr)
        return 2
    except StartupError as exc:
        print(f"setup failed: {exc}", file=sys.stderr)
        return 2
    from .config import ensure_enabled_config
    try:
        cfg_note = ensure_enabled_config(Path.cwd())
    except OSError as exc:
        cfg_note = f"gateway.toml write failed ({exc}) — enable it manually"
    print(f"OK — token validated and stored. Bot: @{username}")
    print(cfg_note)
    print("Start the gateway with start_gateway.bat (double-click) or:")
    print("  python -m hivemind_gateway.main")
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "setup-token":
        return _setup_token_cli()
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        print(stop_instance())
        return 0
    try:
        return asyncio.run(run())
    except StartupError as exc:
        print(f"startup error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("gateway stopped (Ctrl+C)", file=sys.stderr)
        return 0


if __name__ == "__main__":
    sys.exit(main())

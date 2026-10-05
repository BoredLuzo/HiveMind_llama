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

from . import auth as gw_auth
from . import commands as gw_commands
from . import send as gw_send
from . import state as gw_state
from .bridge import RunBridge
from .config import GatewayConfig, load_gateway_config, resolve_config_path
from .hive_client import HiveClient, HiveUnreachable
from .redaction import attach_redaction_to_root
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
    """Liveness probe. NOTE: os.kill(pid, 0) is NOT a probe on Windows —
    any sig other than CTRL_C_EVENT/CTRL_BREAK_EVENT calls
    TerminateProcess and would KILL the other gateway. Use OpenProcess."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False,
                                 pid)
        if not handle:
            return False
        k32.CloseHandle(handle)
        return True
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
                f"{record.get('pid')}, lock {p}). Close it first.")
        log.info("[LOCK] stale gateway lock replaced (pid %s)",
                 record.get("pid"))
        try:
            p.unlink()
        except OSError:
            pass
    birth = _process_birth(os.getpid())
    payload = json.dumps({"pid": os.getpid(), "birth": birth})
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
    except FileExistsError:
        raise StartupError("gateway lock taken concurrently") from None


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

    # -- outbound (always via send()) ------------------------------------

    async def reply(self, chat_id: str | int, text: str,
                    reply_to_message_id: int | None = None) -> None:
        await gw_send.send(self.api, self.owner_id, chat_id, text=text,
                           reply_to_message_id=reply_to_message_id)

    # -- messenger interface for the bridge (owner-addressed by policy) --

    async def send_message(self, text: str) -> dict:
        return await gw_send.send(self.api, self.owner_id, self.owner_id,
                                  text=text)

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
                                 "Nur Text in diesem Build (Fotos/Steering "
                                 "kommen mit WP5).",
                                 reply_to_message_id=p.message_id)
            return
        name, arg = cmd
        if name == "pair":
            await self.cmd_pair(p, arg)
        elif name == "start":
            await self.reply(p.chat_id,
                             "HiveMind-Gateway aktiv. Einfach Text senden "
                             "= Lauf starten. /help für Befehle.",
                             reply_to_message_id=p.message_id)
        elif name == "help":
            lines = [f"/{n} — {d}" for n, d in
                     gw_commands.COMMAND_WHITELIST.items()]
            await self.reply(p.chat_id, "Befehle:\n" + "\n".join(lines),
                             reply_to_message_id=p.message_id)
        elif name == "new":
            await self._owner_bridge_call(p, self.bridge.new_chat())
        elif name == "stop":
            # never rate-limited: the safety valve must always work
            note = await self.bridge.stop()
            await self.reply(p.chat_id, note,
                             reply_to_message_id=p.message_id)
        elif name == "status":
            await self.reply(p.chat_id, self.bridge.status_text(),
                             reply_to_message_id=p.message_id)
        elif name == "verbose":
            await self.reply(p.chat_id, self.bridge.toggle_verbose(),
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
        except (HiveUnreachable, OSError) as exc:
            note = f"🔌 HiveMind nicht erreichbar: {exc}"
        await self.reply(p.chat_id, note,
                         reply_to_message_id=p.message_id)

    async def start_owner_run(self, p: gw_auth.ParsedUpdate) -> None:
        """A plain owner text message = a run. Length-limited, then the
        bridge does the rest (busy check, transcript, stream, result)."""
        q = p.text.strip()
        if not q:
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
                        "window (sender=%s)", p.from_id)
            return
        except gw_auth.PairingDisabled:
            log.info("[PAIR] attempt while already paired (sender=%s)",
                     p.from_id)
            return
        except gw_auth.PairingError:
            log.warning("[PAIR] failed attempt (sender=%s, %d/%d) — "
                        "code wrong or expired",
                        p.from_id, self.pairing.failed_attempts,
                        gw_auth.MAX_FAILED_ATTEMPTS)
            return
        if not ok:  # pragma: no cover - verify returns True or raises
            return
        self.owner_id = p.from_id
        self.state.set_owner(
            p.from_id,
            _dt.datetime.now().astimezone().isoformat(timespec="seconds"))
        self.state.save()
        log.warning("[PAIR] owner bound (telegram id %s)", p.from_id)
        await self.reply(p.chat_id,
                         "Paired. This Telegram account is now the owner.",
                         reply_to_message_id=p.message_id)


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
                f"⚠️ Beim Neustart lief noch Run {rid}. /stop bricht ihn "
                "ab — sonst läuft er detached weiter und hält VRAM.")
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
                           "⏳ Rate-Limit erreicht — kurz warten.",
                           reply_to_message_id=p.message_id)
            return
        if cmd:
            await gw.handle_owner_update(p)
        elif p.kind == "callback_query":
            log.info("[DROP] owner callback (approvals arrive with WP4)")
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


async def run() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg_path = resolve_config_path(start=os.getcwd())
    cfg = GatewayConfig()
    if cfg_path is not None:
        cfg = load_gateway_config(cfg_path)
        log.info("config loaded from %s", cfg_path)
    token = resolve_token()
    acquire_instance_lock()
    state = GatewayState()
    api = TelegramApi(token)
    gw = Gateway(api, cfg, state)

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
                    gw.owner_id)

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

    try:
        while True:
            if kill_switch_active():
                log.warning("kill switch activated — stopping")
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
                    except TelegramApiError as exc:
                        # a failed reply must not kill the poll loop
                        log.error("update %s handling failed: %s", uid, exc)
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.warning("gateway stopped")
    finally:
        await api.close()
        await gw.hive.close()
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except StartupError as exc:
        print(f"startup error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

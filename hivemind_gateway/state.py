"""Gateway state — atomic JSON in a directory OUTSIDE every workspace.

State lives in %LOCALAPPDATA%\\HiveMindGateway (overridable via
HIVEMIND_GATEWAY_HOME) so neither the HiveMind workspace tools nor the
updater's tree sync can see or clobber it. The kill-switch file
gateway.disabled and (later, WP6) the audit log share this directory.

Windows: os.replace over a file another process still holds open raises
PermissionError — same situation as the chat saves, so the same retry
pattern applies (routers/chats.py:_replace_with_retry), but fail loudly
after the retries: a lost state write must stop the gateway, not corrupt
the offset bookkeeping.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

HOME_ENV_VAR = "HIVEMIND_GATEWAY_HOME"
DEFAULT_HOME_SUBDIR = "HiveMindGateway"
STATE_FILE_NAME = "gateway_state.json"
DISABLE_FILE_NAME = "gateway.disabled"
LOCK_FILE_NAME = "gateway.lock"
CORRUPT_SUFFIX = ".corrupt"

_SEEN_CAP = 512  # bounded dedupe window (update_ids), newest kept

_REPLACE_RETRIES = (0.02, 0.05, 0.1, 0.2)


def state_home() -> Path:
    env = os.environ.get(HOME_ENV_VAR, "").strip()
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA", "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / DEFAULT_HOME_SUBDIR


def state_path() -> Path:
    return state_home() / STATE_FILE_NAME


def disabled_path() -> Path:
    return state_home() / DISABLE_FILE_NAME


def lock_path() -> Path:
    return state_home() / LOCK_FILE_NAME


def kill_switch_active() -> bool:
    """File OR environment variable — the owner can lock the gateway from
    the PC even when the bot itself is wedged."""
    if disabled_path().exists():
        return True
    return os.environ.get("HIVEMIND_GATEWAY_DISABLED", "").strip() \
        not in ("", "0", "false")


def _replace_with_retry(src: Path, dst: Path) -> None:
    import time
    last_err: PermissionError | None = None
    for delay in _REPLACE_RETRIES:
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_err = exc
            time.sleep(delay)
    try:
        os.replace(src, dst)
    except PermissionError as exc:
        raise last_err if last_err is not None else exc


class GatewayState:
    """Persisted before every side effect that must be at-most-once.

    Fields (all owner-relevant, none secret):
      owner_telegram_id — set by pairing, None until then
      offset            — Telegram getUpdates offset (persisted BEFORE a
                          received update is processed: crash => update is
                          lost, never run twice)
      seen_update_ids   — dedupe window
      active_run        — {run_id, chat_id, started} while a run is live
      open_approvals    — nonce -> payload snapshot for restart-deny (WP4)
      paired_at         — ISO stamp of the successful pairing
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else state_path()
        self.data: dict = self._empty()
        self.load()

    @staticmethod
    def _empty() -> dict:
        return {
            "owner_telegram_id": None,
            "offset": 0,
            "seen_update_ids": [],
            "active_run": None,
            "open_approvals": {},
            "paired_at": None,
        }

    def load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise TypeError("state file must contain an object")
        except FileNotFoundError:
            self.data = self._empty()
            return
        except (OSError, ValueError, TypeError) as exc:
            # Corrupt state: keep the file for forensics, start fresh.
            # A gateway that cannot trust its offset bookkeeping must not
            # guess — starting fresh drops the backlog ONCE, loudly.
            corrupt = self.path.with_suffix(CORRUPT_SUFFIX)
            try:
                _replace_with_retry(self.path, corrupt)
            except OSError:
                pass
            self.data = self._empty()
            self._load_note = f"state file unreadable, started fresh ({exc})"
            return
        merged = self._empty()
        merged.update({k: v for k, v in data.items() if k in merged})
        if not isinstance(merged["seen_update_ids"], list):
            merged["seen_update_ids"] = []
        self.data = merged

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        _replace_with_retry(tmp, self.path)

    # -- typed accessors (keep the dict shape out of the callers) --------

    @property
    def owner_telegram_id(self) -> int | None:
        v = self.data.get("owner_telegram_id")
        return int(v) if v is not None else None

    def set_owner(self, telegram_id: int, paired_at: str) -> None:
        self.data["owner_telegram_id"] = int(telegram_id)
        self.data["paired_at"] = paired_at

    @property
    def offset(self) -> int:
        return int(self.data.get("offset") or 0)

    def set_offset(self, value: int) -> None:
        self.data["offset"] = int(value)

    def mark_seen(self, update_id: int) -> bool:
        """True if this update_id is new (and is now recorded)."""
        seen: list[int] = self.data.setdefault("seen_update_ids", [])
        if update_id in seen:
            return False
        seen.append(int(update_id))
        if len(seen) > _SEEN_CAP:
            del seen[: len(seen) - _SEEN_CAP]
        return True

    def set_active_run(self, run_id: str | None, chat_id: str | None = None,
                       started: str | None = None) -> None:
        self.data["active_run"] = (
            {"run_id": run_id, "chat_id": chat_id, "started": started}
            if run_id else None)

    def reset_for_tests(self) -> None:  # pragma: no cover - test helper
        self.data = self._empty()

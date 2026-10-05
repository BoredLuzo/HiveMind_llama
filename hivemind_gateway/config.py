"""Gateway configuration (gateway.toml) — validated, fail fast, NO secrets.

The file is optional at development time (defaults below), mandatory once
the gateway serves the owner. Only gateway.toml.example lives in the repo.
The bot token NEVER appears here — it comes from the environment or the
Windows Credential Manager (see token.py).
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

CONFIG_ENV_VAR = "HIVEMIND_GATEWAY_CONFIG"
DEFAULT_CONFIG_NAME = "gateway.toml"

# A token in the config file would end up in the repo, backups or zips.
# Presence of these keys (top level or nested) is a startup error.
_FORBIDDEN_KEYS = ("token", "bot_token", "secret", "api_key", "password")


@dataclass(frozen=True)
class GatewayConfig:
    # MASTER SWITCH — the gateway refuses to start unless this is
    # explicitly true (or HIVEMIND_GATEWAY_ENABLED=1 in the environment).
    # Fail-closed by design: nothing polls Telegram unless someone turned
    # the feature on deliberately.
    telegram_enabled: bool = False
    hive_base_url: str = "http://127.0.0.1:8001"
    max_text_chars: int = 4000          # longest Telegram input we accept
    rate_limit_per_min: int = 20        # per user, in-memory sliding window
    approval_expiry_s: int = 120        # gateway-owned deny expiry (WP4)
    status_min_interval_s: int = 3      # editMessageText throttle (WP2)
    update_max_age_s: int = 60          # replay window, message.date based
    long_poll_timeout_s: int = 25
    document_limit_chars: int = 200_000

    def validate(self) -> None:
        base = self.hive_base_url.rstrip("/").lower()
        loopback = ("http://127.0.0.1", "http://[::1]", "http://localhost")
        if not base.startswith(loopback):
            raise ValueError(
                "hive_base_url must be loopback — the gateway talks to "
                f"HiveMind via 127.0.0.1 only (got {self.hive_base_url!r})")
        for name in ("max_text_chars", "rate_limit_per_min",
                     "approval_expiry_s", "status_min_interval_s",
                     "update_max_age_s", "long_poll_timeout_s",
                     "document_limit_chars"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_text_chars > 4096:
            raise ValueError("max_text_chars must fit one Telegram message "
                             "(<= 4096)")
        if self.approval_expiry_s > 3600:
            raise ValueError("approval_expiry_s must stay well below the "
                             "server's auto-approve horizon (<= 3600)")


def _check_no_secrets(data: dict, where: str) -> None:
    for key, value in data.items():
        if key.lower() in _FORBIDDEN_KEYS:
            raise ValueError(f"{where}: forbidden key '{key}' — the bot "
                             "token comes from the environment, never from "
                             "gateway.toml")
        if isinstance(value, dict):
            _check_no_secrets(value, where)


def load_gateway_config(path: str | Path) -> GatewayConfig:
    """Parse + validate a gateway.toml. Raises (ValueError, OSError,
    tomllib.TOMLDecodeError) — the caller decides fail-fast vs default."""
    p = Path(path)
    data = tomllib.loads(p.read_text(encoding="utf-8"))
    _check_no_secrets(data, p.name)
    # Flat namespace on purpose: one level, keys == field names. Typos in
    # the config must be startup errors, not silently ignored settings.
    known = {f.name for f in fields(GatewayConfig)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"{p.name}: unknown keys {sorted(unknown)} "
                         f"(known: {sorted(known)})")
    cfg = GatewayConfig(**data)
    cfg.validate()
    return cfg


def resolve_config_path(start: str | Path | None = None) -> Path | None:
    """Env var wins, then gateway.toml next to `start` (the live dir)."""
    import os
    env = os.environ.get(CONFIG_ENV_VAR, "").strip()
    if env:
        p = Path(env)
        return p if p.is_file() else None
    if start is not None:
        p = Path(start) / DEFAULT_CONFIG_NAME
        return p if p.is_file() else None
    return None

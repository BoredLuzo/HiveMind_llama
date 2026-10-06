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
    # G1 force: send duo_action_approval_enabled=true with every phone run
    # so gated tools (shell/write/git) are auto-denied from the phone.
    # false = assistant-style use (phone runs may write files); the
    # engine-global duo_action_approval_enabled toggle alone governs then.
    force_approval_gate: bool = True
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


def ensure_enabled_config(directory: str | Path) -> str:
    """Setup-time convenience (universal users): make sure gateway.toml
    in `directory` carries telegram_enabled = true, so the very next
    start works after `setup-token`. Returns what was done; existing
    files keep every other key (only the master-switch line is added or
    flipped). Never touches secrets — the token stays in the vault."""
    import re
    p = Path(directory) / DEFAULT_CONFIG_NAME
    if p.exists():
        text = p.read_text(encoding="utf-8")
        pattern = re.compile(r"(?m)^(\s*)telegram_enabled\s*=\s*(true|false)\s*$")
        if pattern.search(text):
            new_text = pattern.sub(
                lambda m: m.group(1) + "telegram_enabled = true", text)
            # I5 (audit r2): sync hive_base_url with the settings port for
            # existing configs too - a toml from a previous install still
            # pointed at 8001 while the engine now runs elsewhere.
            try:
                from settings import load_settings as _ls
                _port = int((_ls() or {}).get("server_port") or 8001)
                _bu = 'hive_base_url = "http://127.0.0.1:' + str(_port) + '"'
                if not re.search(r"(?m)^hive_base_url\s*=", new_text):
                    new_text = new_text.rstrip("\n") + "\n" + _bu + "\n"
                elif not re.search(r"(?m)^hive_base_url\s*=.*:" + str(_port), new_text):
                    new_text = re.sub(r"(?m)^hive_base_url\s*=.*$", _bu, new_text)
            except (ImportError, ValueError, OSError):
                pass
            if new_text != text:
                p.write_text(new_text, encoding="utf-8", newline="")
                return f"{p.name}: telegram_enabled -> true (base_url synced)"
            return f"{p.name}: already enabled"
        p.write_text(text.rstrip("\n") + "\n\ntelegram_enabled = true\n",
                     encoding="utf-8", newline="")
        return f"{p.name}: telegram_enabled = true appended"
    # I5 (audit r2): the installer prompts for a custom server_port one
    # step earlier - a base_url still pointing at 8001 made every phone
    # run fail with engine-unreachable while the gateway looked healthy.
    try:
        from settings import load_settings as _ls
        _port = int((_ls() or {}).get("server_port") or 8001)
    except (ImportError, ValueError, TypeError, OSError):
        _port = 8001
    p.write_text(
        f"telegram_enabled = true\n"
        f"hive_base_url = \"http://127.0.0.1:{_port}\"\n",
        encoding="utf-8", newline="")
    return f"{p.name}: written (telegram_enabled = true)"


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
    """Env var wins, then gateway.toml next to `start` (the live dir),
    then gateway.toml in the state home.

    audit G10: the state home (%LOCALAPPDATA%\\HiveMindGateway) is the one
    directory that is outside every workspace, update tree and prune
    manifest — an alternate home for the config for installs that do not
    want a token-adjacent file inside the code tree. Existing installs
    (config next to the gateway) keep precedence."""
    import os
    env = os.environ.get(CONFIG_ENV_VAR, "").strip()
    if env:
        p = Path(env)
        return p if p.is_file() else None
    if start is not None:
        p = Path(start) / DEFAULT_CONFIG_NAME
        if p.is_file():
            return p
    from .state import state_home
    p = state_home() / DEFAULT_CONFIG_NAME
    return p if p.is_file() else None

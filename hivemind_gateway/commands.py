"""Command whitelist parsing (pure).

Commands are parsed ONLY from the owner's own user messages — the auth
layer guarantees that; this module only knows the whitelist and the
syntax. Forwarded messages never yield a command (brief: replay /
backlog).
"""
from __future__ import annotations

# /steer arrives with WP5; /get stays optional and OFF (WP6).
COMMAND_WHITELIST = {
    "start": "welcome + state",
    "pair": "bind the owner (one-time code from the console)",
    "new": "start a fresh HiveMind chat",
    "stop": "abort the running run",
    "status": "current run/status",
    "verbose": "toggle verbose output",
    "mode": "show/set the phone-side run mode (auto|chat|pipeline|automap|off)",
    "models": "list available models (numbered)",
    "setmodel": "select a model by number, then answer the ctx questions",
    "cancel": "abort a pending /setModel flow",
    "lock": "lock the gateway until the PC unlocks it (WP6)",
    "help": "list commands",
}


def parse_command(text: str) -> tuple[str, str] | None:
    """('/status@HiveMindBot foo' -> ('status', 'foo')) or None.
    Only whitelisted names; casing and bot-username suffix tolerated."""
    if not text or not text.startswith("/"):
        return None
    head, _, rest = text.partition(" ")
    name = head[1:].split("@", 1)[0].strip().lower()
    if not name or name not in COMMAND_WHITELIST:
        return None
    return (name, rest.strip())


def command_from_message(p) -> tuple[str, str] | None:
    """Command from a ParsedUpdate — None for forwarded messages or
    non-text updates. Take auth out of the equation: this assumes the
    update was already classified as 'owner'."""
    if p is None or p.is_forwarded:
        return None
    return parse_command(p.text)

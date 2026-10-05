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
    "workspace": "show/set the workspace for phone runs",
    "tools": "show/set the direct chat tools level for phone runs (on|off)",
    "gate": "show/set the phone approval policy (ask|deny|off)",
    "lock": "lock the gateway until the PC unlocks it (WP6)",
    "help": "list commands",
}

# /help: a real instruction list, not just the whitelist dump. Owner-facing
# quick start + the safety contract in two lines. Static on purpose: the
# behavior it describes is the tested invariant, not a config value.
HELP_TEXT = (
    "📖 HiveMind — quick guide\n"
    "\n"
    "Send any text = start a run (in the [TG] chat's workspace).\n"
    "\n"
    "— RUN MODES (pick BEFORE a task) —\n"
    "/mode auto — duo agent, CAN create/edit files ← for tasks\n"
    "/mode agentic — duo + agentic loop (thorough, slower)\n"
    "/mode chat — quick talk + web only, NO file tools\n"
    "/mode pipeline · /mode automap · /mode off\n"
    "Bare /mode lists what each mode does.\n"
    "\n"
    "— STEERING —\n"
    "/stop — abort the active run (ALWAYS works)\n"
    "/status — chat, workspace, mode, model, engine\n"
    "/new — fresh HiveMind chat\n"
    "/verbose — toggle detailed status updates\n"
    "\n"
    "— MODEL & CONTEXT —\n"
    "/models — list models (numbered)\n"
    "/setModel <no> — pick a model, answer the ctx questions\n"
    "(applies to auto/agentic runs; /cancel aborts)\n"
    "\n"
    "— WORKSPACE & TOOLS —\n"
    "/workspace <path> — working folder (must exist)\n"
    "/tools on|off — direct-chat tools for simple runs\n"
    "/gate ask|deny|off — approval policy for phone runs\n"
    "\n"
    "— SAFETY —\n"
    "• Phone runs: tools that CHANGE things (shell, files,\n"
    "git) are auto-denied (approval gate enforced) when the\n"
    "engine supports it — /status tells you which applies.\n"
    "• Mirrored PC runs send approval cards — reply\n"
    "1 (allow once) or 3 (deny). No '2'/always from the phone.\n"
    "• Only your Telegram account, private chat only.\n"
)


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

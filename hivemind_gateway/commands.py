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
    "shutdown": "shut down the PC (two-step confirm, owner-only)",
    "purge": "evict ALL models from VRAM (free the GPU)",
    "restrict": "phone runs restricted to web+reading+chat (on|off)",
    "cron": "scheduled agent runs (add|list|del|run)",
    "status": "current run/status",
    "verbose": "toggle verbose output",
    "mode": "show/set the phone-side run mode (auto|chat|pipeline|automap|off)",
    "models": "list available models (numbered)",
    "setmodel": "select a model by number, then answer the ctx questions",
    "cancel": "clear model/ctx overrides for phone runs",
    "workspace": "show/set the workspace for phone runs",
    "tools": "show/set the direct chat tools level for phone runs (on|off)",
    "gate": "show/set approvals (ask|deny|off|on|off-global: on/off = engine-wide toggle, works mid-run)",
    "ctx": "show/set the context for duo runs (number | off)",
    "preset": "load a preset globally (number from the preset list)",
    "planner": "separate planner model for duo runs (number | off)",
    "lock": "lock the gateway until the PC unlocks it (WP6)",
    "help": "list commands",
}

# /help: a real instruction list, not just the whitelist dump. Owner-facing
# quick start + the safety contract in two lines. Static on purpose: the
# behavior it describes is the tested invariant, not a config value.
HELP_TEXT = (
    "📖 HiveMind — quick guide\n"
    "\n"
    "Send any text = start a run.\n"
    "\n"
    "— RUN MODES —\n"
    "/mode auto — duo agent, CAN create/edit files\n"
    "/mode agentic — duo + agentic loop (thorough, slower)\n"
    "/mode chat — quick talk + web only, NO file tools\n"
    "/mode pipeline · /mode automap · /mode off\n"
    "Bare /mode lists what each mode does.\n"
    "\n"
    "— STEERING —\n"
    "/stop — abort the active run (ALWAYS works)\n"
    "/status — chat, workspace, mode, model, engine\n"
    "/new — fresh chat\n"
    "/verbose — toggle detailed status updates\n"
    "\n"
    "— MODEL & CONTEXT —\n"
    "/models — list models (numbered)\n"
    "/setModel <no> — pick a model + context size\n"
    "(applies to auto/agentic AND simple runs)\n"
    "\n"
    "— WORKSPACE, TOOLS & APPROVALS —\n"
    "/workspace <path> — working folder (must exist)\n"
    "/tools on|off — direct-chat tools for simple runs\n"
    "/restrict on|off — phone-run tool restriction\n"
    "/gate — show approval settings\n"
    "/gate on|off — approvals everywhere ON/OFF (live, mid-run)\n"
    "/gate ask|deny|off — phone-run policy nuance\n"
    "/cron — scheduled agent runs (add/list/del/run)\n"
    "\n"
    "— POWER —\n"
    "/purge — evict ALL models from VRAM (frees the GPU)\n"
    "/shutdown [s] — PC shutdown, two-step confirm\n"
    "(optional seconds buffer, e.g. /shutdown 600)\n"
    "\n"
    "— APPROVALS & QUESTIONS —\n"
    "🛡 Approval card: the full command is shown.\n"
    "Reply 1 (allow once) / 2 (always this chat) / 3 (deny).\n"
    "\n"
    "❓ Agent question: reply with free text —\n"
    "your answer goes straight back into the agent.\n"
    "\n"
    "— SAFETY —\n"
    "• Phone runs are restricted by default (web +\n"
    "reading + chat; no shell/writes/git). The UI toggle\n"
    "'Restrict phone runs' unlocks approval-gated actions.\n"
    "• Mirrored PC runs send approval cards here.\n"
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

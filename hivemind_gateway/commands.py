"""Command whitelist parsing (pure).

Commands are parsed ONLY from the owner's own user messages - the auth
layer guarantees that; this module only knows the whitelist and the
syntax. Forwarded messages never yield a command (brief: replay /
backlog).

2026-10-21: COMMAND_SPEC is now the single source of truth. The whitelist,
the /help text and the /help keyboard are all generated from it, so a new
command cannot ship without appearing in the help (test_gateway_command_flow
asserts that). Commands marked `requires_arg` ask back instead of running
with an empty value.
"""
from __future__ import annotations

# group -> title shown in /help
GROUPS = (
    ("run", "RUN MODES"),
    ("steer", "STEERING"),
    ("model", "MODEL & CONTEXT"),
    ("env", "WORKSPACE, TOOLS & APPROVALS"),
    ("sched", "SCHEDULE"),
    ("power", "POWER"),
)

# name -> {usage, desc, group, requires_arg}
COMMAND_SPEC: dict[str, dict] = {
    "start":    {"usage": "/start", "group": "run",
                 "desc": "welcome + state"},
    "help":     {"usage": "/help", "group": "run",
                 "desc": "this list (tap a command to use it)"},
    "mode":     {"usage": "/mode <auto|agentic|chat|pipeline|automap|off>", "group": "run",
                 "desc": "run mode for phone runs (bare = list modes)",
                 "extra": ("/mode auto - duo agent, CAN create/edit files",
                           "/mode agentic - duo + agentic loop (thorough, slower)",
                           "/mode chat - quick talk + web only, NO file tools")},
    "new":      {"usage": "/new", "group": "steer",
                 "desc": "start a fresh HiveMind chat"},
    "follow":   {"usage": "/follow <on|off>", "group": "steer",
                 "desc": "continue the chat the browser used last (default on)",
                 "extra": ("on = a phone message continues the UI chat while it is fresh",
                           "off = the phone keeps its own [TG] chat")},
    "stop":     {"usage": "/stop", "group": "steer",
                 "desc": "abort the active run (ALWAYS works)"},
    "status":   {"usage": "/status", "group": "steer",
                 "desc": "chat, workspace, mode, model, engine"},
    "decider":  {"usage": "/decider <on|off|shadow>", "group": "steer",
                 "desc": "routing decider (bare = status); off frees RAM",
                 "extra": ("on = the CPU encoder routes every note/run",
                           "off = keyword rules decide, encoder RAM freed",
                           "shadow = encoder runs and logs, does not decide")},
    "verbose":  {"usage": "/verbose", "group": "steer",
                 "desc": "toggle detailed status updates"},
    "models":   {"usage": "/models", "group": "model",
                 "desc": "list available models (numbered)"},
    "setmodel": {"usage": "/setModel <number>", "group": "model",
                 "desc": "pick a model by number, then answer the ctx question",
                 "requires_arg": True},
    "cancel":   {"usage": "/cancel", "group": "model",
                 "desc": "clear model/ctx overrides for phone runs"},
    "ctx":      {"usage": "/ctx <number|off>", "group": "model",
                 "desc": "context size for duo runs",
                 "requires_arg": True},
    "planner":  {"usage": "/planner <number|off>", "group": "model",
                 "desc": "separate planner model for duo runs",
                 "requires_arg": True},
    "preset":   {"usage": "/preset <number>", "group": "model",
                 "desc": "load a preset globally (number from the list)",
                 "requires_arg": True},
    "workspace": {"usage": "/workspace <path>", "group": "env",
                  "desc": "working folder for phone runs (must exist)",
                  "requires_arg": True},
    "tools":    {"usage": "/tools <on|off>", "group": "env",
                 "desc": "direct-chat tools for simple runs"},
    "restrict": {"usage": "/restrict <on|off>", "group": "env",
                 "desc": "phone runs restricted to web+reading+chat"},
    "gate":     {"usage": "/gate <on|off|ask|deny>", "group": "env",
                 "desc": "approvals (bare = show the current policy)",
                 "extra": ("/gate on|off - approvals ON/OFF (live, mid-run;",
                           "phone-started runs stay gated)",
                           "/gate ask|deny - phone-run policy nuance")},
    "cron":     {"usage": "/cron <add|list|del|run> ...", "group": "sched",
                 "desc": "scheduled agent runs"},
    "purge":    {"usage": "/purge", "group": "power",
                 "desc": "evict ALL models from VRAM (free the GPU)"},
    "shutdown": {"usage": "/shutdown [seconds]", "group": "power",
                 "desc": "PC shutdown, two-step confirm"},
    "lock":     {"usage": "/lock", "group": "power",
                 "desc": "lock the gateway until the PC unlocks it"},
    "pair":     {"usage": "/pair <code>", "group": "env",
                 "desc": "bind the owner (one-time code from the console)",
                 "requires_arg": True},
}

# name -> description (kept for backwards compatibility with callers/tests)
COMMAND_WHITELIST: dict[str, str] = {k: v["desc"] for k, v in COMMAND_SPEC.items()}

# Words that cancel a pending command prompt.
CANCEL_WORDS = frozenset({"stop", "cancel", "abort", "abbrechen", "abbruch"})

_EXTRA = (
    "\n"
    "- APPROVALS & QUESTIONS -\n"
    "🛡 Approval card: the full command is shown.\n"
    "Reply 1 (allow once) / 2 (always this chat) / 3 (deny).\n"
    "\n"
    "❓ Agent question: reply with free text -\n"
    "your answer goes straight back into the agent.\n"
    "\n"
    "- SAFETY -\n"
    "• Phone runs are restricted by default (web +\n"
    "reading + chat; no shell/writes/git).\n"
    "• Mirrored PC runs send approval cards here.\n"
    "• Only your Telegram account, private chat only.\n"
)


def _build_help() -> str:
    out = [
        "📖 HiveMind - quick guide",
        "",
        "Send any text = start a run.",
        "Tap a command below to run it; commands that need a",
        "value ask first (just send the value as the next message,",
        "or 'stop' to cancel).",
        "",
    ]
    for key, title in GROUPS:
        rows = [(name, spec) for name, spec in COMMAND_SPEC.items()
                if spec.get("group") == key]
        if not rows:
            continue
        out.append(f"- {title} -")
        for _, spec in rows:
            out.append(f"{spec['usage']} - {spec['desc']}")
            for extra in spec.get("extra", ()):
                out.append(f"  {extra}")
        out.append("")
    return "\n".join(out) + _EXTRA


HELP_TEXT = _build_help()


def help_keyboard() -> dict:
    """Inline keyboard: one button per command (2 per row) + a cancel row.

    callback_data stays well under Telegram's 64-byte limit.
    """
    buttons = [{"text": spec["usage"].split(" ")[0],
                "callback_data": f"cmd:{name}"}
               for name, spec in COMMAND_SPEC.items()]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return {"inline_keyboard": rows}


def param_hint(name: str) -> str:
    """Ask-back text for a command that needs a value."""
    spec = COMMAND_SPEC.get(name) or {}
    usage = spec.get("usage") or f"/{name}"
    return (
        f"⚙️ {usage}\n\n"
        "Send the value as the next message (e.g. 5), or reply "
        "'stop' to cancel."
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
    """Command from a ParsedUpdate - None for forwarded messages or
    non-text updates. Take auth out of the equation: this assumes the
    update was already classified as 'owner'."""
    if p is None or p.is_forwarded:
        return None
    return parse_command(p.text)


def wants_arg_first(name: str) -> bool:
    """True when /name without a value should ask instead of executing."""
    spec = COMMAND_SPEC.get(name) or {}
    return bool(spec.get("requires_arg"))


def is_cancel(text: str) -> bool:
    return str(text or "").strip().lower() in CANCEL_WORDS

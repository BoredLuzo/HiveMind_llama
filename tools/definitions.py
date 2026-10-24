"""Tool-Definitionen, Allowlists und Filter-Funktionen (aus server.py extrahiert)."""
from __future__ import annotations

_WEBSEARCH_AVAILABLE: bool = False
_websearch = None


def init_websearch(available: bool, module=None):
    """Initialisiert die Websearch-Integration (von server.py aufzurufen)."""
    global _WEBSEARCH_AVAILABLE, _websearch
    _WEBSEARCH_AVAILABLE = available
    _websearch = module


_INLINE_CODING_TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": (
            "Read file content. ALWAYS call read_file before edit_file on existing files. "
            "Returns RAW content (no line-number prefixes) with a header showing the total/selected line range. "
            "Files above the read budget are refused with a range hint; "
            "use get_signatures first and start_line/end_line to read only the sections you need."
        ),
        "parameters": {"type": "object", "properties": {
            "path":       {"type": "string", "description": "Absolute or relative file path"},
            "start_line": {"type": "integer", "description": "First line to read (1-indexed, optional)"},
            "end_line":   {"type": "integer", "description": "Last line to read (inclusive, optional)"}
        }, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "get_signatures",
        "description": (
            "Return a compact structural map of a file (classes, functions, methods, variables) with line numbers. "
            "Use BEFORE read_file on large files to identify relevant line ranges. "
            "Supports .py (AST-based) and .ts/.js (heuristic). "
            "Then call read_file with start_line/end_line for only the sections you need."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Absolute or relative file path"},
            "max_items": {"type": "integer", "description": "Optional max number of signature lines (default: 400)"}
        }, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "find_references",
        "description": (
            "Find all references/uses of a symbol across the workspace. "
            "Returns file paths + line numbers (definition vs. use). "
            "Preferred over run_bash grep for 'who calls X'. Max 160 results."
        ),
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "The symbol to find (e.g. 'calculateTotal' or 'Handler')"},
            "path":   {"type": "string", "description": "Scan root (default: workspace)"},
            "max_items": {"type": "integer", "description": "Optional max results (default: 160)"}
        }, "required": ["symbol"]}
    }},
    {"type": "function", "function": {
        "name": "list_dir", "description": "List the IMMEDIATE entries (files + subdirectories) of one directory level — non-recursive. Path defaults to project root if empty. Returns up to 200 entries; for deeper structure call list_dir on a subdirectory or use find_files.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path to list (default: project root, optional)"}
        }, "required": []}
    }},
    {"type": "function", "function": {
        "name": "find_files",
        "description": (
            "Find files by GLOB pattern (file name matching, NOT content search). "
            "Use for discovering project structure. Examples: '**/*.py' (all Python files), 'src/**/*.ts', 'test_*.py', '*.json'. "
            "For searching file CONTENTS use search_code."
        ),
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Glob pattern"},
            "path":    {"type": "string", "description": "Search root (default: .)"}
        }, "required": ["pattern"]}
    }},
    {"type": "function", "function": {
        "name": "search_code", "description": (
            "Search file CONTENTS with regex or plain text (file names: find_files). "
            "Preferred over run_bash for content searches. Returns file paths + line "
            "numbers; confined to the workspace."
        ),
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Regex pattern or plain search string"},
            "path":    {"type": "string", "description": "Search path (default: ., searches subdirectories)"}
        }, "required": ["pattern"]}
    }},
    {"type": "function", "function": {
        "name": "run_python",
        "description": (
            "USE WHEN: quick verification of pure-Python logic — calculations, JSON/data "
            "transformations, checking an expression's result (10s timeout, stdout captured). "
            "Runs the snippet as a STANDALONE script with the workspace as working directory; "
            "it does NOT see your conversation context. NOT for importing workspace modules, "
            "long-running processes, or shell commands — use run_bash or write a real file + run_tests."
        ),
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "Python snippet; output from stdout (use print())."}
        }, "required": ["code"]}
    }},
    {"type": "function", "function": {
        "name": "install_package",
        "description": (
            "Install a dependency (controlled way — never raw run_bash installs). "
            "Managers: npm, pip, cargo, go, dotnet, composer. Budgeted per run. "
            "Verify the import/build via run_bash afterwards."
        ),
        "parameters": {"type": "object", "properties": {
            "manager":  {"type": "string", "enum": ["npm", "pip", "cargo", "go", "dotnet", "composer"],
                         "description": "Package manager"},
            "packages": {"type": "string", "description": "Space-separated package names"},
            "dev":      {"type": "boolean", "description": "npm --save-dev"}
        }, "required": ["manager", "packages"]}
    }},
    {"type": "function", "function": {
        "name": "run_bash",
        "description": (
            "Execute a shell command. On Windows the shell is Git Bash when "
            "installed (POSIX syntax), else Windows PowerShell 5.1 - "
            "settings.run_bash_shell (auto|bash|powershell) decides, and the "
            "error output names the shell that ran. On Linux/Mac it is bash. "
            "Only for what no dedicated tool covers: content search -> search_code, "
            "file names -> find_files, references -> find_references, git -> git_status, "
            "installs -> install_package (each of these only if it is in your "
            "tool list). Long-running services: use start_background — "
            "run_bash waits and times out.\n"
            "NEVER create or edit source files via run_bash — use write_file/edit_file "
            "(on Windows '>' writes UTF-16 and there are no heredocs). "
            "Check OUTPUT before retrying; never blindly retry identical failing args."
        ),
        "parameters": {"type": "object", "properties": {
            "cmd": {"type": "string", "description": "Shell command - POSIX bash syntax when the shell is Git Bash, PowerShell syntax only when the shell is powershell.exe"}
        }, "required": ["cmd"]}
    }},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": (
            "Replace ONE unique passage in an EXISTING file. Copy old_text "
            "VERBATIM from your last read_file (exact match — no markers, no "
            "fuzzy guessing); it must appear exactly once. new_text is the "
            "replacement. For multiple changes send multiple calls. To create a "
            "new file use write_file."
        ),
        "parameters": {"type": "object", "properties": {
            "path":     {"type": "string", "description": "File path"},
            "old_text": {"type": "string", "description": "Exact text to replace — copied verbatim from read_file, unique in the file"},
            "new_text": {"type": "string", "description": "Replacement text"}
        }, "required": ["path", "old_text", "new_text"]}
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": (
            "Create a file, or overwrite it completely, with plain content (no "
            "SEARCH/REPLACE markers — that is edit_file). For a SMALL change inside "
            "a big file prefer edit_file: a full overwrite needs the complete content.\n"
            "Stay within your WRITE RULES char target. If the call is cut off by the "
            "output limit, the server keeps everything up to the last complete line "
            "and reports that line — continue with write_file_append.\n"
            "An overwrite that would shrink a file drastically is refused once: pass "
            "confirm_shrink=true only when the file is really supposed to get much "
            "smaller (a rewrite/truncation)."
        ),
        "parameters": {"type": "object", "properties": {
            "path":    {"type": "string", "description": "File path"},
            "content": {"type": "string", "maxLength": 20000, "description": "Complete file content (plain text, no markers)."},
            "confirm_shrink": {"type": "boolean", "description": "Set true to allow an overwrite that shrinks an existing file by a lot."}
        }, "required": ["path", "content"]}
    }},
    {"type": "function", "function": {
        "name": "git_status",
        "description": (
            "USE WHEN: checking git state before committing or reviewing what changed. "
            "cmd='status' -> modified/staged/untracked files; 'diff' -> unstaged changes; "
            "'log' -> recent commits; 'show' -> one commit's details. Read-only."
        ),
        "parameters": {"type": "object", "properties": {
            "cmd": {"type": "string", "enum": ["status", "diff", "log", "show"]}
        }, "required": ["cmd"]}
    }},
    {"type": "function", "function": {
        "name": "git_commit", "description": "Commit all staged and unstaged changes in the workspace to git. Use after completing a chunk or a significant implementation step. Message should be a concise one-line description of what was implemented.",
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string", "description": "Commit message (one line, imperative mood, max 72 chars)"},
            "workspace": {"type": "string", "description": "Git repo root (omit: workspace)"}
        }, "required": ["message"]}
    }},
    {"type": "function", "function": {
        "name": "write_file_append",
        "description": (
            "Append a continuation chunk VERBATIM to the end of a file — only as "
            "follow-up in the SAME write sequence (write_file part1 -> append "
            "part2 -> append part3), or to continue a write that was cut off at "
            "the output limit (start after the last complete line the server "
            "reported). Stay within your WRITE RULES char target."
        ),
        "parameters": {"type": "object", "properties": {
            "path":    {"type": "string", "description": "File path (must already exist)"},
            "content": {"type": "string", "maxLength": 20000, "description": "Content chunk, verbatim."}
        }, "required": ["path", "content"]}
    }},
    {"type": "function", "function": {
        "name": "undo_last",
        "description": (
            "Undo the latest file change of the current chunk round. "
            "Restores a file to its state BEFORE the current round's edits/writes. "
            "Use WITHOUT path to undo ALL files changed in this round, or WITH path to undo a single file. "
            "Cannot undo files that were never modified in this round. "
            "After undoing, re-read the file before further edits."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File to undo (omit: all changes this round)."}
        }, "required": []}
    }},
    {"type": "function", "function": {
        "name": "ask_user",
        "description": (
            "Pause execution to ask the user a SINGLE critical question. "
            "Use ONLY when the answer CANNOT be found by reading code, searching the project, or inferring from context. "
            "NOT for questions like 'which file should I edit?' — figure that out yourself. "
            "NOT for confirmations — just act. Valid cases: missing credentials, ambiguous architecture choice, destructive operations."
        ),
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "Specific, concise question for the user."}
        }, "required": ["question"]}
    }},
    {"type": "function", "function": {
        "name": "run_tests",
        "description": (
            "Run the project's FULL test suite (auto-detects pytest/npm/vitest/"
            "jest/cargo/go/maven/dotnet; pre-existing failures count too). "
            "MUST run before task_complete. Returns [TEST-RESULT] pass/fail "
            "lines. No suite found -> verify manually via run_bash."
        ),
        "parameters": {"type": "object", "properties": {
            "timeout": {"type": "integer", "description": "Timeout in seconds (default 90, max 300)", "default": 90},
            "lang_override": {"type": "string",
                              "enum": ["python", "javascript", "typescript", "astro", "docker", "java", "rust", "go", "csharp", "cpp"],
                              "description": "Force a language instead of auto-detect", "default": ""}
        }, "required": []}
    }},
    {"type": "function", "function": {
        "name": "start_background",
        "description": (
            "Start a long-running command in the BACKGROUND (e.g. 'npm run dev', "
            "'docker compose up', a test watcher) and return a handle. The process "
            "keeps running while you do other work — unlike run_bash which waits. "
            "Then use get_background_output(handle) to read its logs and "
            "stop_background(handle) to kill it."
        ),
        "parameters": {"type": "object", "properties": {
            "cmd": {"type": "string", "description": "Shell command to run in the background"}
        }, "required": ["cmd"]}
    }},
    {"type": "function", "function": {
        "name": "get_background_output",
        "description": (
            "Read the accumulated stdout/stderr of a background process (started via "
            "start_background). Returns its handle + running/exited status + buffered "
            "output. With no handle, lists all running background processes."
        ),
        "parameters": {"type": "object", "properties": {
            "handle": {"type": "string", "description": "The handle returned by start_background (omit to list all)"}
        }, "required": []}
    }},
    {"type": "function", "function": {
        "name": "stop_background",
        "description": "Stop a background process started via start_background, by its handle.",
        "parameters": {"type": "object", "properties": {
            "handle": {"type": "string", "description": "The handle returned by start_background"}
        }, "required": ["handle"]}
    }},
    {"type": "function", "function": {
        "name": "task_complete",
        "description": (
            "Signal the coding task is finished (all changes implemented and "
            "verified). Requires a status object: completed, blockers, "
            "build_status."
        ),
        "parameters": {"type": "object", "properties": {
            "status": {
                "type": "object",
                "description": "See fields.",
                "properties": {
                    "completed": {"type": "array", "items": {"type": "string"},
                                  "description": "What was implemented/verified"},
                    "blockers": {"type": "array", "items": {"type": "string"},
                                 "description": "What could not be done and why; empty list if none"},
                    "build_status": {"type": "string", "enum": ["passing", "failing", "untested"],
                                     "description": "Result of your final verification"}
                },
                "required": ["completed", "blockers", "build_status"]
            }
        }, "required": ["status"]}
    }},
    {"type": "function", "function": {
        "name": "browser",
        "description": (
            "Headless browser (stays open across calls). Actions: navigate, snapshot "
            "(page text + console/errors), screenshot, click, type, evaluate (JS), "
            "console, close. In-workspace file:// URLs are auto-served over loopback "
            "HTTP (ES modules/fetch work); outside-workspace file:// is rejected."
        ),
        "parameters": {"type": "object", "properties": {
            "action":   {"type": "string", "enum": ["navigate", "snapshot", "screenshot", "click", "type", "evaluate", "console", "close"],
                         "description": "Which browser action to perform"},
            "url":      {"type": "string", "description": "URL to navigate to (for action='navigate')"},
            "selector": {"type": "string", "description": "CSS selector (for action='click'/'type')"},
            "text":     {"type": "string", "description": "Text to type (for action='type')"},
            "path":     {"type": "string", "description": "Output PNG path relative to the WORKSPACE (for action='screenshot', e.g. screenshots/ui.png)"},
            "js":       {"type": "string", "description": "JavaScript expression (for action='evaluate')"},
            "full_page": {"type": "boolean", "default": False,
                          "description": "Capture full scrollable page (screenshot)"}
        }, "required": ["action"]}
    }},
    {"type": "function", "function": {
        "name": "get_datetime",
        "description": (
            "Return the current local date, time, weekday and timezone offset. "
            "Use when the exact current date/time matters: date questions "
            "('what date is it today?'), deadlines/relative dates, timestamps, "
            "or checking whether something is recent."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []}
    }},
    {
        "type": "function",
        "function": {
            # REMEMBER TOOL (2026-10-06, owner): the agent saves durable
            # facts itself - same HiveMindMemory the auto-extraction feeds,
            # so the fact is injected into every later run.
            "name": "remember",
            "description": "Save a durable fact to long-term memory "
                           "(key/value). Use for stable user/project facts "
                           "the user wants kept across chats ('the_name = "
                           "Pumuckel', 'deploy = 1.3.2'). An empty value "
                           "DELETES the key.",
            "parameters": {"type": "object", "properties": {
                "key": {"type": "string",
                        "description": "Short key, e.g. 'deploy' or "
                                       "'user_name'"},
                "value": {"type": "string",
                          "description": "The fact to store. Omit/empty to "
                                         "DELETE the key."}
            }, "required": ["key"]}
        }
    },
    {"type": "function", "function": {
        # SUBAGENT-LITE (2026-08-24, Feasibility-Report Option A)
        "name": "subagent_research",
        "description": (
            "Delegate a MULTI-FILE research task to a small subagent with its "
            "own short-lived context (read-only tools: read_file/list_dir/"
            "search_code). Use for exploratory multi-file questions where "
            "inline reading would bloat the main context. NOT for single-file "
            "lookups. Returns a compact summary; falls back to 'research "
            "inline' when resources (RAM/VRAM) are tight."
        ),
        "parameters": {"type": "object", "properties": {
            "task": {"type": "string",
                     "description": "Self-contained research question/task "
                                    "(mention paths/keywords to investigate)"}
        }, "required": ["task"]}
    }},
]


# edit_file intentionally excluded from critic — critic must not modify code.
# Defined here, AFTER _INLINE_CODING_TOOLS, to avoid NameError at module load.
_INLINE_TOOL_NAMES = {t["function"]["name"] for t in _INLINE_CODING_TOOLS}

_READ_ONLY_INLINE_TOOL_NAMES = {
    "read_file",
    "get_signatures",
    "find_references",
    "list_dir",
    "find_files",
    "search_code",
    "get_background_output",
    "get_datetime",
    "ask_user",
}

_TOOL_MODE_ALLOWLISTS: dict[str, set[str]] = {
    # CONSOLIDATION (2026-09-17): replace_lines/edit_ast stay ALLOWED here for
    # old recorded sessions, but are no longer advertised (not in
    # _INLINE_CODING_TOOLS) — the coder uses edit_file (+start_line/end_line).
    "duo_full": set(_INLINE_TOOL_NAMES) | {"web_search", "web_fetch",
                                           "replace_lines", "edit_ast"},
    # WEDGE-HANDOFF (2026-09-17): fresh-context repair delegate. Read + write
    # family, deliberately NO run_bash/git/ask_user/browser — minimal-invasive
    # fix only; all handler-level guards (noop, shrink, block-sniff,
    # READ_REQUIRED) apply unchanged.
    "fix_agent": {"read_file", "get_signatures", "list_dir", "find_files",
                  "search_code", "edit_file", "write_file",
                  "write_file_append", "undo_last", "get_datetime"},
    "duo_readonly": set(_READ_ONLY_INLINE_TOOL_NAMES) | {"web_search", "web_fetch"},
    "pre_explore": set(_READ_ONLY_INLINE_TOOL_NAMES) | {"web_search", "web_fetch"},
    "critic_verify": set(_READ_ONLY_INLINE_TOOL_NAMES) | {"run_bash"},
    "tool_agent": set(_INLINE_TOOL_NAMES) | {"web_search", "web_fetch"},
    "mcp_agent": set(_INLINE_TOOL_NAMES) | {"web_search", "web_fetch"},
    "openai_agent": set(_INLINE_TOOL_NAMES) | {"web_search", "web_fetch", "hivemind_pipeline"},
    # DIRECT-CHAT-TOOLS (2026-08-31): tiered tool sets for the simple/direct chat.
    # TIER-FIX (2026-09-02): the "read" tier is now WEBSEARCH ONLY — the model
    # can search/fetch the web but gets no file-read tools. File reading moved
    # up to the python tier ("read + python"). python tier keeps web (additive).
    # DIALOG TRIO (2026-10-06, live): without SearXNG the web discard below
    # emptied this set completely — the tool loop then bailed silently and
    # the direct chat had NO tools at all (no ask_user, no steering drain,
    # empty-run_id tool calls). ask_user/task_complete/get_datetime are
    # always dialog-level and must survive the web discard.
    "direct": {"web_search", "web_fetch",
               "ask_user", "task_complete", "get_datetime"},
    "direct_python": set(_READ_ONLY_INLINE_TOOL_NAMES) | {"web_search", "web_fetch", "run_python"},
    "direct_full": set(_INLINE_TOOL_NAMES) | {"web_search", "web_fetch"},
}


# ── L2: Tool-Subset-Gruppierungen ────────────────────────────────────

_TOOL_SUBSETS: dict[str, set[str]] = {
    "explore": {
        "find_files", "list_dir", "search_code", "read_file",
        "get_signatures", "find_references", "subagent_research",
    },
    "write": {
        "write_file", "edit_file", "write_file_append", "replace_lines", "edit_ast", "undo_last",
    },
    "run": {"run_bash", "run_python", "install_package", "start_background",
            "stop_background", "get_background_output"},
    "test": {"run_tests"},
    "git": {"git_status", "git_commit"},
    "task": {"task_complete", "ask_user"},
    "browser": {"browser"},
}


def get_tools_for_phase(phase: str) -> list:
    if phase == "all":
        return list(_INLINE_CODING_TOOLS)
    _names: set[str] = set()
    for _key in phase.replace(" ", "").split(","):
        _names.update(_TOOL_SUBSETS.get(_key.strip(), set()))
    _names.add("task_complete")
    _names.add("ask_user")
    return [t for t in _INLINE_CODING_TOOLS if t["function"]["name"] in _names]


def _tool_names_for_mode(mode: str, include_websearch: bool = False) -> set[str]:
    allowed = set(_TOOL_MODE_ALLOWLISTS.get(mode, _READ_ONLY_INLINE_TOOL_NAMES))
    if not include_websearch or not _WEBSEARCH_AVAILABLE:
        allowed.discard("web_search")
        allowed.discard("web_fetch")
    return allowed


# ── GIT-TOOLS-GATE (2026-10-17) ─────────────────────────────────────────────
# git_status/git_commit are only worth schema space when git is actually set up
# for this install. They now follow the git credentials/toggles:
#   settings git_tools_enabled: "auto" (default) | true | false
#   "auto" = at least one of git_repo_url / git_username / git_email /
#            git_token is set.
# read-only "background"/research tools are deliberately NOT gated (they work
# without any configured service).
_GIT_TOOL_NAMES = ("git_status", "git_commit")
_GIT_CREDENTIAL_KEYS = ("git_repo_url", "git_username", "git_email", "git_token")


def git_tools_enabled(settings: dict | None = None) -> bool:
    """True if the git tools should ship in a schema (see _GIT_TOOL_NAMES).

    Unreadable/unavailable settings -> True (keep the previous behaviour: the
    gate must never be the reason a tool silently vanishes on a settings
    read error)."""
    try:
        if settings is None:
            from settings import load_settings
            settings = load_settings() or {}
    except (ImportError, AttributeError, TypeError, ValueError, OSError):
        return True
    _raw = settings.get("git_tools_enabled", "auto")
    if isinstance(_raw, bool):
        return _raw                     # JSON true/false, not the string form
    _mode = str(_raw or "auto").strip().lower()
    if _mode in ("0", "false", "off", "no", "never"):
        return False
    if _mode in ("1", "true", "on", "yes", "always"):
        return True
    return any(str(settings.get(_k) or "").strip()
               for _k in _GIT_CREDENTIAL_KEYS)


def _filter_tools_for_mode(tools: list, mode: str, include_websearch: bool = False) -> list:
    allowed = _tool_names_for_mode(mode, include_websearch=include_websearch)
    out = [t for t in tools if t.get("function", {}).get("name") in allowed]
    if not git_tools_enabled():
        out = [t for t in out
               if t.get("function", {}).get("name") not in _GIT_TOOL_NAMES]
    return out


# ── TOOL-SCHEMA BUDGET FIT (2026-10-14) ────────────────────────────────────
# No model-name staffels: the schema is a CONTINUUM fitted to the run's
# schema budget (SCHEMA_FRAC of n_ctx — see core/budgets). Every tool has a
# priority (core tools are always kept) and three description levels. The
# fitter starts ALL tools at the minimal level and upgrades by priority
# while the budget holds; non-core tools that do not fit even minimally are
# dropped. At 16k ctx the full schema (~15.3k of a ~16.1k budget) survives —
# the same code auto-shortens at 8k ctx. No name knows a model.
_TOOL_PRIORITY = [
    # tier 0 — always kept, even at miserable budgets
    "read_file", "edit_file", "write_file", "run_bash", "task_complete",
    # tier 1 — the working set
    "run_tests", "search_code", "list_dir", "find_files", "get_signatures",
    "undo_last", "ask_user", "get_datetime", "write_file_append",
    # tier 2 — everything else (web, git, background, browser, research...)
]


def _tool_priority(name: str) -> int:
    try:
        return _TOOL_PRIORITY.index(name)
    except ValueError:
        return len(_TOOL_PRIORITY)


def _desc_at_level(desc: str, level: int) -> str:
    import re as _re_d
    flat = desc.replace("\n", " ").strip()
    if level >= 2:
        return flat
    if level == 1:
        return _re_d.split(r"(?<=[.!?])\s", flat)[0].strip()
    return " ".join(flat.split()[:10]).rstrip(".,;:")  # minimal: ~10 words


def fit_tools_to_budget(tools: list, budget_chars: int) -> list:
    import json as _json
    if budget_chars is None or budget_chars <= 0 or not tools:
        return list(tools)

    def _total(state: dict, pool: dict) -> int:
        s = 0
        for n, t in pool.items():
            fn = t["function"]
            s += len(_json.dumps({**t, "function": {
                **fn, "description": _desc_at_level(str(fn.get("description", "")),
                                                    state.get(n, 0))}}))
        return s

    pool = {}
    for t in tools:
        n = t.get("function", {}).get("name", "")
        if n:
            pool[n] = t
    state = {n: 0 for n in pool}

    # 1. drop non-core tools that keep the MINIMAL set over budget
    for n in sorted(pool, key=_tool_priority, reverse=True):
        if _total(state, pool) <= budget_chars:
            break
        if _tool_priority(n) == 0:
            continue  # tier 0 is always kept
        del pool[n]
        del state[n]

    # 2. upgrade by priority, two steps (minimal -> sentence -> full)
    for target_level in (1, 2):
        for n in sorted(pool, key=_tool_priority):
            if state[n] >= target_level:
                continue
            old = state[n]
            state[n] = target_level
            if _total(state, pool) > budget_chars:
                state[n] = old
    return [
        {**pool[n], "function": {**pool[n]["function"],
                                 "description": _desc_at_level(
                                     str(pool[n]["function"].get("description", "")),
                                     state[n])}}
        for n in pool
    ]


def _apply_budget_texts(tools: list, budgets) -> list:
    """Insert the run's RESOLVED budget numbers into schema-visible texts
    (audit phase 6 redesign: the schema states the truth of THIS run, not a
    module constant). Called before the budget fit — description lengths
    participate in the fit."""
    _rc = int(getattr(budgets, "read_char_cap", 0) or 0)
    if _rc <= 0:
        return tools
    out = []
    for t in tools:
        fn = t.get("function", {})
        if fn.get("name") == "read_file":
            out.append({**t, "function": {**fn, "description": (
                "Read file content. ALWAYS call read_file before edit_file on existing files. "
                "Returns RAW content (no line-number prefixes) with a header showing the total/selected line range. "
                f"Files bigger than ~{_rc} chars are REFUSED with a range hint (use get_signatures first); "
                f"reads are capped at {_rc} chars — use start_line/end_line for sections."
            )}})
        else:
            out.append(t)
    return out


def _get_inline_tools(include_websearch: bool = False, mode: str | None = None,
                      budget_chars: int | None = None) -> list:
    tools = list(_INLINE_CODING_TOOLS)
    if include_websearch and _WEBSEARCH_AVAILABLE and _websearch is not None:
        tools.extend(_websearch.get_tool_defs())
    if mode:
        tools = _filter_tools_for_mode(tools, mode, include_websearch=include_websearch)
    if budget_chars:
        try:
            from core.budgets import active as _b_active
            tools = _apply_budget_texts(tools, _b_active())
        except Exception:
            pass
        tools = fit_tools_to_budget(tools, budget_chars)
    try:
        from settings import load_settings as _ls_settings
        if not bool((_ls_settings() or {}).get("subagent_lite_enabled", True)):
            tools = [t for t in tools
                     if t.get("function", {}).get("name") != "subagent_research"]
    except Exception:
        pass
    return tools



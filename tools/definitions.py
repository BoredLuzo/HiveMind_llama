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
            "For large files use start_line/end_line to read only the sections you need; full reads are capped at 200 lines / 32000 chars."
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
            "Execute a shell command. Windows: PowerShell 5.1 (Get-ChildItem, "
            "Get-Content, Select-String, Test-Path, Remove-Item; chain with "
            "'cmd1; if ($?) { cmd2 }' — never '&&'). Linux/Mac: bash.\n"
            "Only for what no dedicated tool covers: content search -> search_code, "
            "file names -> find_files, references -> find_references, git -> git_status, "
            "installs -> install_package.\n"
            "Long-running services: use start_background — run_bash waits and times "
            "out (90s default, 600s builds).\n"
            "PowerShell stderr trap: native tools can print progress to stderr and "
            "still succeed with exit code 1 — check OUTPUT before retrying. Never "
            "blindly retry identical failing args. curl: add '-m 10' to health checks."
        ),
        "parameters": {"type": "object", "properties": {
            "cmd": {"type": "string", "description": "Shell command (PowerShell 5.1 on Windows, bash on Linux/Mac)"}
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
            "Create a NEW file with complete plain content (no SEARCH/REPLACE "
            "markers — that is edit_file). Existing files: use edit_file instead.\n"
            "If too large for one call: write the FIRST part, then finish with "
            "write_file_append(path, content='<AUTO_SPLIT_CONTINUE>') — bare token, "
            "no quotes; the remainder is stored server-side. Never resend content."
        ),
        "parameters": {"type": "object", "properties": {
            "path":    {"type": "string", "description": "File path"},
            "content": {"type": "string", "maxLength": 20000, "description": "Complete file content (plain text, no markers)."}
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
            "part2 -> append part3). Max ~20000 chars per call. After an "
            "AUTO-SPLIT: send only content='<AUTO_SPLIT_CONTINUE>' (bare token, "
            "no quotes) — the stored remainder appends automatically; never "
            "resend the content."
        ),
        "parameters": {"type": "object", "properties": {
            "path":    {"type": "string", "description": "File path (must already exist)"},
            "content": {"type": "string", "maxLength": 20000, "description": "Content chunk. AUTO-SPLIT continuation: bare token <AUTO_SPLIT_CONTINUE> (no quotes)."}
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
            "Run the project's test suite (auto-detects pytest/npm/vitest/jest/"
            "cargo/go/maven/dotnet). MUST run before task_complete. Returns "
            "[TEST-RESULT] pass/fail lines. No suite found -> verify manually "
            "via run_bash."
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
            "path":     {"type": "string", "description": "Output PNG path (for action='screenshot')"},
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
    "direct": {"web_search", "web_fetch"},
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


def _filter_tools_for_mode(tools: list, mode: str, include_websearch: bool = False) -> list:
    allowed = _tool_names_for_mode(mode, include_websearch=include_websearch)
    return [t for t in tools if t.get("function", {}).get("name") in allowed]


def _get_inline_tools(include_websearch: bool = False, mode: str | None = None) -> list:
    tools = list(_INLINE_CODING_TOOLS)
    if include_websearch and _WEBSEARCH_AVAILABLE and _websearch is not None:
        tools.extend(_websearch.get_tool_defs())
    if mode:
        tools = _filter_tools_for_mode(tools, mode, include_websearch=include_websearch)
    try:
        from settings import load_settings as _ls_settings
        if not bool((_ls_settings() or {}).get("subagent_lite_enabled", True)):
            tools = [t for t in tools
                     if t.get("function", {}).get("name") != "subagent_research"]
    except Exception:
        pass
    return tools



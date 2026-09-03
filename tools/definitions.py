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
            "Find all references/uses of a symbol (function, class, method, variable) across the workspace. "
            "LSP-like: returns file paths + line numbers tagged as definition vs. use. "
            "Use instead of run_bash grep/Select-String to trace 'who calls X' or 'where is Y defined'. "
            "path can be a file or a directory (scan root, default: workspace root). "
            "Max 160 results."
        ),
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "The symbol to find (e.g. 'calculateTotal' or 'Handler')"},
            "path":   {"type": "string", "description": "File or directory to scan (default: workspace root, optional)"},
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
            "Search file CONTENTS with regex or plain text (NOT file names — use find_files for name matching). "
            "This is the PREFERRED tool for content searches — do NOT use run_bash with Select-String/Get-Content for that. "
            "Returns matching file paths and line numbers. Example: pattern='class.*Handler' finds all Handler classes. "
            "Plain search strings work too (no regex needed). Path filters to a directory (default: project root); "
            "searches are confined to the workspace. Result volume is capped (platform-dependent)."
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
            "code": {"type": "string", "description": "The Python code to execute. Must be a valid Python snippet. Output is captured from stdout. Use print() to produce output."}
        }, "required": ["code"]}
    }},
    {"type": "function", "function": {
        "name": "install_package",
        "description": (
            "Install a dependency/package into the project (the CONTROLLED way to "
            "install — never use raw run_bash 'npm install'/'pip install'). "
            "Supported managers: npm, pip, cargo, go, dotnet, composer. "
            "Limited budget per run (duo_install_max_calls). After installing, "
            "verify the import/build works via run_bash."
        ),
        "parameters": {"type": "object", "properties": {
            "manager":  {"type": "string", "enum": ["npm", "pip", "cargo", "go", "dotnet", "composer"],
                         "description": "Package manager"},
            "packages": {"type": "string", "description": "Space-separated package names to install, e.g. 'fabric ws pg'"},
            "dev":      {"type": "boolean", "description": "Install as dev dependency (npm only, --save-dev; default false)"}
        }, "required": ["manager", "packages"]}
    }},
    {"type": "function", "function": {
        "name": "run_bash",
        "description": (
            "Execute a shell command. Windows shell is PowerShell 5.1: use Get-ChildItem / "
            "Get-Content / Select-String / Test-Path / Remove-Item; chain with "
            "'cmd1; if ($?) { cmd2 }' — never '&&'. Linux/Mac: bash.\n"
            "TOOL PREFERENCE — run_bash only for what no dedicated tool covers "
            "(build, run, test commands): content search -> search_code; file names -> find_files; "
            "'who calls X' -> find_references/get_signatures; git state -> git_status; "
            "package installs -> install_package.\n"
            "LONG-RUNNING services (dev servers, 'docker compose up'): use start_background — "
            "run_bash waits for completion and times out (90s default, 600s for builds).\n"
            "PowerShell stderr trap: native tools (git/docker/npm) can print progress to stderr and "
            "still succeed while the exit code reads 1 — check the OUTPUT for success markers before "
            "retrying. NEVER retry identical failing arguments blindly.\n"
            "curl: real curl.exe runs (alias removed); add '-m 10' to health checks so half-open "
            "ports don't hang until timeout."
        ),
        "parameters": {"type": "object", "properties": {
            "cmd": {"type": "string", "description": "Shell command (PowerShell 5.1 on Windows, bash on Linux/Mac)"}
        }, "required": ["cmd"]}
    }},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": (
            "USE WHEN: changing parts of a file that ALREADY exists.\n"
            "Send SEARCH/REPLACE blocks in `edits`. COPY the SEARCH text verbatim from read_file; "
            "add surrounding lines so each block is unique. Multiple blocks per call OK "
            "(do NOT issue one call per block).\n"
            "Matching: exact first; if that fails, a conservative fuzzy match may apply "
            "(result tells you when). Ambiguous matches are rejected, not guessed.\n"
            "WRONG: JSON args {\"old_str\": ..., \"new_str\": ...} — that is patch_file's format.\n"
            "CORRECT block: <<<<<<< SEARCH\\n<exact existing code>\\n=======\\n<replacement>\\n>>>>>>> REPLACE\n"
            "Keep each call within your OUTPUT-BUDGET hint (system prompt); split very large rewrites "
            "into several targeted blocks instead of one huge call."
        ),
        "parameters": {"type": "object", "properties": {
            "path":  {"type": "string", "description": "File path"},
            "edits": {"type": "string", "maxLength": 20000, "description": (
                "One or more SEARCH/REPLACE blocks (plain text markers, NOT JSON)."
            )}
        }, "required": ["path", "edits"]}
    }},
    {"type": "function", "function": {
        "name": "patch_file",
        "description": (
            "USE WHEN: replacing ONE small, exact snippet (old_str -> new_str). "
            "old_str must match character-for-character — copy it from read_file output; "
            "if it appears multiple times, add surrounding context lines to make it unique. "
            "For multiple changes or larger edits use edit_file with SEARCH/REPLACE blocks."
        ),
        "parameters": {"type": "object", "properties": {
            "path":    {"type": "string", "description": "File path"},
            "old_str": {"type": "string", "description": "EXACT text to find and replace — copy verbatim from read_file"},
            "new_str": {"type": "string", "description": "Replacement text"}
        }, "required": ["path", "old_str", "new_str"]}
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": (
            "USE WHEN: creating a file that does NOT exist yet.\n"
            "Pass the COMPLETE plain content — no SEARCH/REPLACE markers (that is edit_file's format).\n"
            "WRONG: using write_file to change an existing file — it overwrites the whole file; "
            "use edit_file or patch_file instead.\n"
            "For files larger than ~20000 chars: write the FIRST part here, then finish with "
            "write_file_append(path, content=\"<AUTO_SPLIT_CONTINUE>\") — the remainder is stored "
            "server-side; never resend the whole content."
        ),
        "parameters": {"type": "object", "properties": {
            "path":    {"type": "string", "description": "File path"},
            "content": {"type": "string", "maxLength": 20000, "description": "Complete file content as plain text — no SEARCH/REPLACE markers. Max ~20000 chars per call."}
        }, "required": ["path", "content"]}
    }},
    {"type": "function", "function": {
        "name": "replace_lines",
        "description": (
            "USE WHEN: swapping an exact line range whose numbers you JUST confirmed via read_file — "
            "and nothing else changed the file since. PREFER edit_file for most edits: line numbers "
            "shift after other edits to the same file. start_line/end_line are 1-indexed and inclusive. "
            "NOT for creating new files."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path"},
            "start_line": {"type": "integer", "description": "First line to replace (1-indexed, inclusive)"},
            "end_line": {"type": "integer", "description": "Last line to replace (inclusive)"},
            "replacement": {"type": "string", "description": "Replacement code block"}
        }, "required": ["path", "start_line", "end_line", "replacement"]}
    }},
    {"type": "function", "function": {
        "name": "edit_ast",
        "description": (
            "USE WHEN: replacing one whole Python function/class/variable at once — safest for large "
            "nodes because there is no indentation or text matching involved (.py only). "
            "target_type: 'function', 'class', or 'variable'. "
            "Qualified names for class methods: target_name='ClassName.method'. "
            "new_code: complete replacement code for the node."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path (.py only)"},
            "target_type": {"type": "string", "enum": ["function", "class", "variable"]},
            "target_name": {"type": "string", "description": "Name of the target node. Use ClassName.method for class methods."},
            "new_code": {"type": "string", "description": "Full replacement code for the target node"}
        }, "required": ["path", "target_type", "target_name", "new_code"]}
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
            "workspace": {"type": "string", "description": "Absolute path to the git repository root — OMIT to commit in the current workspace"}
        }, "required": ["message"]}
    }},
    {"type": "function", "function": {
        "name": "write_file_append",
        "description": (
            "Append a continuation chunk VERBATIM to the end of a file. "
            "ONLY valid as a follow-up in the SAME write sequence that created/last touched the file.\n"
            "CORRECT sequence: write_file(path, part1) -> write_file_append(path, part2) -> write_file_append(path, part3).\n"
            "WRONG: append before the file exists, or append to an unrelated file.\n"
            "Each call holds at most ~20000 chars. For AUTO-SPLIT follow-ups send only "
            "content=\"<AUTO_SPLIT_CONTINUE>\" — the remainder is stored server-side and will be "
            "appended automatically; never resend the content."
        ),
        "parameters": {"type": "object", "properties": {
            "path":    {"type": "string", "description": "File path (must already exist)"},
            "content": {"type": "string", "maxLength": 20000, "description": "Content chunk to append. For AUTO-SPLIT continuation use exactly \"<AUTO_SPLIT_CONTINUE>\"."}
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
            "path": {"type": "string", "description": "Optional file path to undo. Omit to undo all files changed in this round."}
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
            "Run the project's test suite (auto-detects language: pytest, npm test, "
            "vitest, jest, cargo test, go test, maven, dotnet). "
            "MUST be used BEFORE task_complete to verify your changes. "
            "Returns structured [TEST-RESULT] feedback with pass/fail and error lines. "
            "If the project has no test suite, it reports that — then verify manually "
            "with run_bash (e.g. build/start the app)."
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
            "Signal that the coding task is finished. Call this when all requested changes are implemented "
            "and verified. Must include a status object with completed, blockers, and build_status fields."
        ),
        "parameters": {"type": "object", "properties": {
            "status": {
                "type": "object",
                "description": (
                    "Report object with three fields: completed (what was done), "
                    "blockers (what could NOT be done and why), build_status (verification result)."
                ),
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
            "Control a headless browser (Playwright/Chromium) to verify web apps end-to-end. "
            "Actions: navigate(url) loads a page; snapshot returns page text + JS console/errors; "
            "screenshot(path) saves a PNG for vision inspection; click(selector)/type(selector,text) "
            "interact with the UI; evaluate(js) runs JavaScript in the page; console returns captured "
            "JS console/errors; close shuts the browser down. The browser stays open across calls."
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
                          "description": "(action='screenshot') capture the full scrollable page instead of the viewport"}
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
    "duo_full": set(_INLINE_TOOL_NAMES) | {"web_search", "web_fetch"},
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
        "write_file", "edit_file", "patch_file",
        "write_file_append", "replace_lines", "edit_ast", "undo_last",
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



"""Tool definitions, allowlists and filters (extracted from server.py)."""
from __future__ import annotations

_WEBSEARCH_AVAILABLE: bool = False
_websearch = None


def init_websearch(available: bool, module=None):
    """Initialize the websearch integration (called from server.py)."""
    global _WEBSEARCH_AVAILABLE, _websearch
    _WEBSEARCH_AVAILABLE = available
    _websearch = module


_INLINE_CODING_TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": (
            "Read raw file content (ALWAYS call read_file before edit_file). "
            "Returns header with total/selected line range; large files require start_line/end_line after get_signatures."
        ),
        "parameters": {"type": "object", "properties": {
            "path":       {"type": "string", "description": "File path"},
            "start_line": {"type": "integer", "description": "Start line (1-indexed)"},
            "end_line":   {"type": "integer", "description": "End line (inclusive)"}
        }, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "get_signatures",
        "description": (
            "Return compact structural outline (classes, functions, line numbers) before read_file on large .py/.ts/.js files."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path"},
            "max_items": {"type": "integer", "description": "Max signature lines (default 400)"}
        }, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "find_references",
        "description": (
            "Find all symbol references across the workspace (file paths + line numbers, max 160). Preferred over grep."
        ),
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "Symbol name"},
            "path":   {"type": "string", "description": "Scan root (default workspace)"},
            "max_items": {"type": "integer", "description": "Max results (default 160)"}
        }, "required": ["symbol"]}
    }},
    {"type": "function", "function": {
        "name": "list_dir", "description": "List immediate entries (files + subdirs, non-recursive, max 200) of a directory.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Directory path (default root)"}
        }, "required": []}
    }},
    {"type": "function", "function": {
        "name": "find_files",
        "description": (
            "Find files by glob pattern (e.g. '**/*.py', 'test_*.py'). For file contents use search_code."
        ),
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Glob pattern"},
            "path":    {"type": "string", "description": "Search root (default .)"}
        }, "required": ["pattern"]}
    }},
    {"type": "function", "function": {
        "name": "search_code", "description": (
            "Search workspace file contents with regex/text; returns matching file paths and line numbers."
        ),
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Regex or plain text"},
            "path":    {"type": "string", "description": "Search path (default .)"}
        }, "required": ["pattern"]}
    }},
    {"type": "function", "function": {
        "name": "run_python",
        "description": (
            "Run quick standalone Python snippet (45s timeout, workspace cwd). Not for servers; use run_bash or run_tests."
        ),
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "Python snippet (use print() for output)"}
        }, "required": ["code"]}
    }},
    {"type": "function", "function": {
        "name": "install_package",
        "description": (
            "Install dependency via npm/pip/cargo/go/dotnet/composer (never raw run_bash). Budgeted; verify afterwards."
        ),
        "parameters": {"type": "object", "properties": {
            "manager":  {"type": "string", "enum": ["npm", "pip", "cargo", "go", "dotnet", "composer"],
                         "description": "Package manager"},
            "packages": {"type": "string", "description": "Space-separated packages"},
            "dev":      {"type": "boolean", "description": "npm --save-dev"}
        }, "required": ["manager", "packages"]}
    }},
    {"type": "function", "function": {
        "name": "run_bash",
        "description": (
            "Run shell command (Git Bash when installed on Windows, else PowerShell 5.1; bash on Linux/Mac). "
            "Prefer dedicated tools when available: content -> search_code, files -> find_files, "
            "refs -> find_references, git -> git_status, installs -> install_package (only if it is in your tool list). "
            "NEVER write files via run_bash (Windows '>' writes UTF-16) — use write_file/edit_file. "
            "Use start_background for long servers. Check output before retrying."
        ),
        "parameters": {"type": "object", "properties": {
            "cmd": {"type": "string", "description": "Shell command (POSIX bash when Git Bash active, else PowerShell)"}
        }, "required": ["cmd"]}
    }},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": (
            "Replace one unique verbatim passage from read_file in an existing file, or pass edits=[{old_text,new_text}] (max 20 atomic writes). New files: write_file."
        ),
        "parameters": {"type": "object", "properties": {
            "path":     {"type": "string", "description": "File path"},
            "old_text": {"type": "string", "description": "Exact unique text from read_file"},
            "new_text": {"type": "string", "description": "Replacement text"},
            "edits":    {"type": "array", "description": "Up to 20 ordered {old_text,new_text} replacements",
                         "items": {"type": "object", "properties": {
                             "old_text": {"type": "string", "description": "Exact unique text"},
                             "new_text": {"type": "string", "description": "Replacement text"}
                         }, "required": ["old_text", "new_text"]}},
            "confirm_shrink": {"type": "boolean", "description": "Allow replacing most of file"},
            "allow_overwrite": {"type": "boolean", "description": "Allow dropping existing top-level symbols"}
        }, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "apply_patch",
        "description": (
            "Apply atomic multi-file unified diff (exact context required, rolled back on any mismatch). Single-file edits: prefer edit_file."
        ),
        "parameters": {"type": "object", "properties": {
            "patch": {"type": "string", "description": "Unified diff text"}
        }, "required": ["patch"]}
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": (
            "Create a file or overwrite it completely with plain text. For small edits prefer edit_file. "
            "Long files: continue cut-off chunks via write_file_append. Large shrink requires confirm_shrink=true."
        ),
        "parameters": {"type": "object", "properties": {
            "path":    {"type": "string", "description": "File path"},
            "content": {"type": "string", "maxLength": 20000, "description": "Complete plain file content"},
            "confirm_shrink": {"type": "boolean", "description": "Allow large file shrink"},
            "allow_overwrite": {"type": "boolean", "description": "Allow dropping existing top-level symbols"}
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
        "name": "git_commit", "description": "Commit the files you wrote in this run to git (scoped add + scoped commit; the whole index is never swept). Use after completing a chunk or a significant implementation step. Message should be a concise one-line description of what was implemented.",
        "parameters": {"type": "object", "properties": {
            "message": {"type": "string", "description": "Commit message (one line, imperative mood, max 72 chars)"},
            "paths": {"type": "array", "items": {"type": "string"}, "description": "Files to commit (workspace-relative, the files you changed in this run)"},
            "workspace": {"type": "string", "description": "Git repo root (omit: workspace)"}
        }, "required": ["message", "paths"]}
    }},
    {"type": "function", "function": {
        "name": "write_file_append",
        "description": (
            "Append verbatim continuation chunk to end of file (follow-up to cut-off write_file, starting after last complete line)."
        ),
        "parameters": {"type": "object", "properties": {
            "path":    {"type": "string", "description": "Existing file path"},
            "content": {"type": "string", "maxLength": 20000, "description": "Verbatim chunk"}
        }, "required": ["path", "content"]}
    }},
    {"type": "function", "function": {
        "name": "undo_last",
        "description": (
            "Restore files modified in the current round to their pre-round state (omit path for all files; re-read afterwards)."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File to undo (omit for all this round)"}
        }, "required": []}
    }},
    {"type": "function", "function": {
        "name": "ask_user",
        "description": (
            "Pause to ask user ONE blocking question that cannot be answered from code (credentials, ambiguous design, destructive op)."
        ),
        "parameters": {"type": "object", "properties": {
            "question": {"type": "string", "description": "Concise question"}
        }, "required": ["question"]}
    }},
    {"type": "function", "function": {
        "name": "run_tests",
        "description": (
            "Run full test suite (pytest/npm/vitest/jest/cargo/go/maven/dotnet). "
            "Must run before task_complete; pre-existing failures count too. Returns [TEST-RESULT] lines."
        ),
        "parameters": {"type": "object", "properties": {
            "timeout": {"type": "integer", "description": "Timeout s (default 90, max 300)", "default": 90},
            "lang_override": {"type": "string",
                              "enum": ["python", "javascript", "typescript", "astro", "docker", "java", "rust", "go", "csharp", "cpp"],
                              "description": "Force language", "default": ""},
            "paths": {"type": "array", "items": {"type": "string"},
                      "description": "Optional workspace-relative test targets"}
        }, "required": []}
    }},
    {"type": "function", "function": {
        "name": "start_background",
        "description": (
            "Start long-running process in background and return handle; inspect with get_background_output and stop with stop_background."
        ),
        "parameters": {"type": "object", "properties": {
            "cmd": {"type": "string", "description": "Background shell command"}
        }, "required": ["cmd"]}
    }},
    {"type": "function", "function": {
        "name": "get_background_output",
        "description": (
            "Read stdout/stderr + status of background handle (omit handle to list all running)."
        ),
        "parameters": {"type": "object", "properties": {
            "handle": {"type": "string", "description": "Handle from start_background (optional)"}
        }, "required": []}
    }},
    {"type": "function", "function": {
        "name": "stop_background",
        "description": "Stop a background process started via start_background, by its handle.",
        "parameters": {"type": "object", "properties": {
            "handle": {"type": "string", "description": "Handle from start_background"}
        }, "required": ["handle"]}
    }},
    {"type": "function", "function": {
        "name": "task_complete",
        "description": (
            "Signal the ENTIRE task done after edits & verification (completed, blockers, build_status). "
            "Ends the run unless more_work_remaining=true (use true to checkpoint a subtask and continue)."
        ),
        "parameters": {"type": "object", "properties": {
            "status": {
                "type": "object",
                "description": "Completion report",
                "properties": {
                    "completed": {"type": "array", "items": {"type": "string"},
                                  "description": "Implemented/verified items"},
                    "blockers": {"type": "array", "items": {"type": "string"},
                                 "description": "Remaining blockers (empty if none)"},
                    "build_status": {"type": "string", "enum": ["passing", "failing", "untested", "blocked"],
                                     "description": "Final verification status"}
                },
                "required": ["completed", "blockers", "build_status"]
            },
            "more_work_remaining": {
                "type": "boolean",
                "default": False,
                "description": "True to log a subtask checkpoint and keep the run open; false when all work is done.",
            },
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
    # ── LSP TOOLS (2026-10-31): language-server features via infra/lsp_client.
    # One broad, generic surface - NO per-language flags; the only switch is
    # settings.lsp_enabled. Positions are 1-based (converted to LSP 0-based
    # in tools/handlers/lsp.py). A missing server answers LSP_UNAVAILABLE
    # with an install hint; lsp_status never starts a process.
    {"type": "function", "function": {
        "name": "lsp_status",
        "description": (
            "Report the language servers available for the workspace "
            "(language, command, found). Run this first when an lsp_* tool "
            "answers LSP_UNAVAILABLE. Never starts a server process."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []}
    }},
    {"type": "function", "function": {
        "name": "lsp_diagnostics",
        "description": (
            "Get compiler/linter diagnostics for one file from its language "
            "server, formatted [SEVERITY] path:line:col message (source). "
            "Use after edits to catch type errors before run_tests. Starts "
            "the server lazily on first use."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path (source file)"}
        }, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "lsp_definition",
        "description": (
            "Jump to the definition of the symbol at a 1-based line/character "
            "position. Returns file:line:col locations from the language "
            "server. More precise than search_code for 'where is X defined'."
        ),
        "parameters": {"type": "object", "properties": {
            "path":      {"type": "string", "description": "File path"},
            "line":      {"type": "integer", "description": "Line number (1-based)"},
            "character": {"type": "integer", "description": "Column number (1-based)"}
        }, "required": ["path", "line", "character"]}
    }},
    {"type": "function", "function": {
        "name": "lsp_references",
        "description": (
            "Find all references to the symbol at a 1-based line/character "
            "position across the workspace (file:line:col each). Use for "
            "'who calls/uses X' when text-based find_references is too noisy."
        ),
        "parameters": {"type": "object", "properties": {
            "path":      {"type": "string", "description": "File path"},
            "line":      {"type": "integer", "description": "Line number (1-based)"},
            "character": {"type": "integer", "description": "Column number (1-based)"}
        }, "required": ["path", "line", "character"]}
    }},
    {"type": "function", "function": {
        "name": "lsp_hover",
        "description": (
            "Hover information (type signature, docs) for the symbol at a "
            "1-based line/character position. Answers 'what type is this / "
            "what does this API do' without opening more files."
        ),
        "parameters": {"type": "object", "properties": {
            "path":      {"type": "string", "description": "File path"},
            "line":      {"type": "integer", "description": "Line number (1-based)"},
            "character": {"type": "integer", "description": "Column number (1-based)"}
        }, "required": ["path", "line", "character"]}
    }},
    {"type": "function", "function": {
        "name": "lsp_symbols",
        "description": (
            "Document outline: classes, functions, methods, variables with "
            "kinds and line ranges, from the language server. Like "
            "get_signatures but server-accurate and multi-language."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path"}
        }, "required": ["path"]}
    }},
    {"type": "function", "function": {
        "name": "lsp_workspace_symbols",
        "description": (
            "Search symbols (classes, functions, ...) across the whole "
            "workspace by query. Answers 'where is the class/interface named "
            "X' project-wide; per-file structure is lsp_symbols."
        ),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Symbol name or prefix (e.g. 'Handler')"}
        }, "required": ["query"]}
    }},
    {"type": "function", "function": {
        "name": "lsp_code_actions",
        "description": (
            "List quickfixes/refactorings the language server offers for a "
            "range (titles + kinds only). NOTHING is applied - pick one and "
            "implement it with edit_file. Useful after lsp_diagnostics."
        ),
        "parameters": {"type": "object", "properties": {
            "path":       {"type": "string", "description": "File path"},
            "start_line": {"type": "integer", "description": "Range start line (1-based)"},
            "start_char": {"type": "integer", "description": "Range start column (1-based)"},
            "end_line":   {"type": "integer", "description": "Range end line (1-based)"},
            "end_char":   {"type": "integer", "description": "Range end column (1-based)"}
        }, "required": ["path", "start_line", "start_char", "end_line", "end_char"]}
    }},
    {"type": "function", "function": {
        # MUTATING (approval-gated in runner._APPROVAL_TOOLS): but the write
        # never happens in the handler - it returns the summarized rename
        # plan and the agent applies it with edit_file.
        "name": "lsp_rename",
        "description": (
            "Prepare a project-wide rename of the symbol at a 1-based "
            "position. Returns the planned edits per file WITHOUT writing "
            "anything - apply the plan with edit_file. Approval-gated."
        ),
        "parameters": {"type": "object", "properties": {
            "path":      {"type": "string", "description": "File path"},
            "line":      {"type": "integer", "description": "Line number (1-based)"},
            "character": {"type": "integer", "description": "Column number (1-based)"},
            "new_name":  {"type": "string", "description": "New symbol name"}
        }, "required": ["path", "line", "character", "new_name"]}
    }},
    {"type": "function", "function": {
        # MUTATING (approval-gated in runner._APPROVAL_TOOLS): the handler
        # applies the server's TextEdits and REWRITES the file.
        "name": "lsp_format",
        "description": (
            "Format one file with its language server (textDocument/"
            "formatting) and write the result back atomically. Use after "
            "edits so the file matches the project style; then re-run "
            "lsp_diagnostics. Approval-gated."
        ),
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string", "description": "File path (source file)"}
        }, "required": ["path"]}
    }},
]

# Delivered via Telegram sendDocument. Added to the schema only for runs the
# Telegram gateway started (`include_send_file`), so desktop/API runs never
# see a channel they cannot use. The handler further requires that the run
# wrote the file itself and that it stays inside the workspace.
_SEND_FILE_TOOL = {"type": "function", "function": {
    "name": "send_file",
    "description": (
        "Queue a file you created in THIS run for delivery to the Telegram "
        "chat. Use it when the task asks for a deliverable (HTML page, "
        "document, image); delivery happens after final verification. Only "
        "files written by this run inside the workspace are accepted; "
        "secrets and files over 50 MB are refused."
    ),
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string",
                 "description": "File path (relative to the workspace)"}
    }, "required": ["path"]}
}}

# VIEW-IMAGE: Conditional schema, added ONLY when vision/mmproj is active for
# the running model (`include_view_image`). Never advertised to text-only runs.
_VIEW_IMAGE_TOOL = {"type": "function", "function": {
    "name": "view_image",
    "description": (
        "Inspect a local image file (PNG, JPG, JPEG, WEBP, GIF, BMP) inside the "
        "workspace visually. Use this after taking a browser screenshot or to "
        "examine UI assets, charts, or diagrams. The image is attached "
        "immediately after this round's tool results."
    ),
    "parameters": {"type": "object", "properties": {
        "path": {"type": "string", "description": "Image file path relative to the workspace"},
    }, "required": ["path"]}
}}

_PIPELINE_TOOL = {"type": "function", "function": {
    "name": "hivemind_pipeline",
    "description": (
        "Delegate a complex query or task to the full HiveMind multi-agent "
        "pipeline (analyst + synthesizer) or auto-routed workflow."
    ),
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "Task or question for the HiveMind pipeline"},
        "mode":  {"type": "string", "description": "Optional routing mode (default: auto)"},
    }, "required": ["query"]},
}}


# edit_file intentionally excluded from critic — critic must not modify code.
# Defined here, AFTER _INLINE_CODING_TOOLS, to avoid NameError at module load.
_INLINE_TOOL_NAMES = {t["function"]["name"] for t in _INLINE_CODING_TOOLS}

_READ_ONLY_INLINE_TOOL_NAMES = {
    "read_file",
    "view_image",
    "get_signatures",
    "find_references",
    "list_dir",
    "find_files",
    "search_code",
    "get_background_output",
    "get_datetime",
    "ask_user",
}

# ── LSP MODE-GATE GROUPS (2026-10-31) ───────────────────────────────────────
# One broad, generic language-server surface, NO per-language flags. The
# inspect tools and lsp_code_actions (returns action titles only, applies
# nothing) are read-tier; lsp_rename is mutating (approval-gated in
# runner._APPROVAL_TOOLS) and reaches exactly the full tiers - which get it
# automatically because the lsp tools are part of _INLINE_CODING_TOOLS /
# _INLINE_TOOL_NAMES. Read tiers that already list get_signatures/
# find_references get the read group + code actions explicitly below.
_LSP_READ_TOOLS = frozenset({
    "lsp_status", "lsp_diagnostics", "lsp_definition", "lsp_references",
    "lsp_hover", "lsp_symbols", "lsp_workspace_symbols",
})
_LSP_CODE_ACTIONS = frozenset({"lsp_code_actions"})
_LSP_READ_TIER_EXTRAS = _LSP_READ_TOOLS | _LSP_CODE_ACTIONS

_TOOL_MODE_ALLOWLISTS: dict[str, set[str]] = {
    # CONSOLIDATION (2026-09-17): replace_lines/edit_ast stay ALLOWED here for
    # old recorded sessions, but are no longer advertised (not in
    # _INLINE_CODING_TOOLS) — the coder uses edit_file (+start_line/end_line).
    "duo_full": set(_INLINE_TOOL_NAMES) | {"web_search", "web_fetch",
                                           "replace_lines", "edit_ast",
                                           "send_file", "view_image"},
    # CORE CODER (2026-11-08): token-saving default for the coder/planner.
    # Read + edit + run + tests only; Web/Git/Subagent become opt-in optionals.
    "duo_core": {
        "read_file", "view_image", "get_signatures", "find_references", "list_dir",
        "find_files", "search_code", "edit_file", "write_file",
        "write_file_append", "apply_patch", "undo_last", "run_bash",
        "run_python", "run_tests", "task_complete", "ask_user",
        # LSP diagnostics stays in core: post-write auto-diagnostics only
        # fire when a server is warm, and this is the tool that warms it.
        # LSP auto-diagnostics runs automatically on write/edit; explicit lsp_*
        # tools, get_datetime, and send_file stay in duo_core; duo_lean has 8.
        "get_datetime", "lsp_diagnostics", "send_file",
    },
    "duo_lean": {
        "read_file", "edit_file", "write_file",
        "run_bash", "run_tests",
        "search_code", "find_files",
        "task_complete",
    },
    # WEDGE-HANDOFF (2026-09-17): fresh-context repair delegate. Read + write
    # family, deliberately NO run_bash/git/ask_user/browser — minimal-invasive
    # fix only; all handler-level guards (noop, shrink, block-sniff,
    # READ_REQUIRED) apply unchanged.
    "fix_agent": {"read_file", "view_image", "get_signatures", "list_dir", "find_files",
                  "search_code", "edit_file", "write_file",
                  "write_file_append", "undo_last", "get_datetime"},
    # LSP (2026-10-31): read tiers with get_signatures/find_references get
    # the LSP inspect tools + code actions (titles only). The full tiers
    # (duo_full, tool_agent, mcp_agent, openai_agent, direct_full) get all
    # lsp_* tools incl. lsp_rename via _INLINE_TOOL_NAMES.
    "duo_readonly": set(_READ_ONLY_INLINE_TOOL_NAMES) | {"web_search", "web_fetch"}
                    | _LSP_READ_TIER_EXTRAS,
    "duo_mcp": set(_READ_ONLY_INLINE_TOOL_NAMES)
               | {"web_search", "web_fetch", "task_complete", "send_file"},
    "pre_explore": set(_READ_ONLY_INLINE_TOOL_NAMES) | {"web_search", "web_fetch"}
                   | _LSP_READ_TIER_EXTRAS,
    "critic_verify": set(_READ_ONLY_INLINE_TOOL_NAMES) | {"run_bash"}
                     | _LSP_READ_TIER_EXTRAS,
    "tool_agent": set(_INLINE_TOOL_NAMES) | {"web_search", "web_fetch", "view_image"},
    "mcp_agent": set(_INLINE_TOOL_NAMES) | {"web_search", "web_fetch", "view_image"},
    "openai_agent": set(_INLINE_TOOL_NAMES) | {"web_search", "web_fetch", "hivemind_pipeline", "view_image"},
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
    "direct_python": set(_READ_ONLY_INLINE_TOOL_NAMES) | {"web_search", "web_fetch", "run_python"}
                     | _LSP_READ_TIER_EXTRAS,
    # RESEARCH TIER (2026-11-08): Direct is the web/research assistant. Web +
    # read/search only; no write/run/lsp/git/mcp. Token-saving middle ground
    # between "direct" (web only) and "direct_python".
    "direct_research": set(_READ_ONLY_INLINE_TOOL_NAMES)
                       | {"web_search", "web_fetch", "task_complete"},
    "direct_mcp": set(_READ_ONLY_INLINE_TOOL_NAMES)
                  | {"web_search", "web_fetch", "task_complete", "send_file"},
    "direct_full": set(_INLINE_TOOL_NAMES) | {"web_search", "web_fetch",
                                             "send_file"},
}


# ── L2: Tool-Subset-Gruppierungen ────────────────────────────────────

_TOOL_SUBSETS: dict[str, set[str]] = {
    "explore": {
        "find_files", "list_dir", "search_code", "read_file",
        "get_signatures", "find_references", "subagent_research",
    },
    "write": {
        "write_file", "edit_file", "write_file_append", "replace_lines", "edit_ast", "undo_last",
        "apply_patch",
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


_WEB_TOOL_NAMES = ("web_search", "web_fetch")
_WEBSEARCH_OPT_IN_MODES = frozenset({"duo_lean", "duo_core"})

_SUBAGENT_TOOL_NAME = "subagent_research"
_SUBAGENT_OPT_IN_MODES = frozenset({
    "duo_lean", "duo_core", "duo_full",
    "direct_research", "direct_python", "direct_full",
    "tool_agent", "mcp_agent", "openai_agent",
})


def subagent_tools_enabled(settings: dict | None = None) -> bool:
    """True if subagent_research should ship as an active Optional tool."""
    try:
        if settings is None:
            from settings import load_settings
            settings = load_settings() or {}
    except (ImportError, AttributeError, TypeError, ValueError, OSError):
        return False
    raw = (settings or {}).get("subagent_lite_enabled", False)
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() not in ("", "0", "false", "off", "no")


_VIEW_IMAGE_OPT_IN_MODES = frozenset({
    "duo_full", "duo_core", "duo_lean", "fix_agent",
    "duo_readonly", "duo_mcp", "pre_explore", "critic_verify",
    "tool_agent", "mcp_agent", "openai_agent",
    "direct_python", "direct_research", "direct_mcp", "direct_full",
})


def _tool_names_for_mode(
    mode: str,
    include_websearch: bool = False,
    include_git: bool | None = None,
    include_subagent: bool | None = None,
    include_view_image: bool | None = None,
) -> set[str]:
    allowed = set(_TOOL_MODE_ALLOWLISTS.get(mode, _READ_ONLY_INLINE_TOOL_NAMES))
    if include_websearch and _WEBSEARCH_AVAILABLE:
        if mode in _WEBSEARCH_OPT_IN_MODES:
            allowed.update(_WEB_TOOL_NAMES)
    else:
        allowed.difference_update(_WEB_TOOL_NAMES)
    if include_git is True:
        if mode in _GIT_OPT_IN_MODES:
            allowed.update(_GIT_TOOL_NAMES)
        elif mode in _GIT_READ_OPT_IN_MODES:
            allowed.add("git_status")
    elif include_git is False:
        allowed.difference_update(_GIT_TOOL_NAMES)
    if include_subagent is True:
        if mode in _SUBAGENT_OPT_IN_MODES:
            allowed.add(_SUBAGENT_TOOL_NAME)
    elif include_subagent is False:
        allowed.discard(_SUBAGENT_TOOL_NAME)
    if include_view_image is False:
        allowed.discard("view_image")
    elif mode in _VIEW_IMAGE_OPT_IN_MODES:
        allowed.add("view_image")
    return allowed


# ── GIT-TOOLS-GATE (2026-10-17, updated 2026-11-12) ──────────────────────────
# git_status/git_commit are exposed only when git credentials are configured
# AND the git tools toggle is not turned off:
#   settings git_tools_enabled: "auto" (default) | true | false
#   No credentials => ALWAYS False (even if toggle is "true").
#   With credentials => "false" keeps them off; "auto"/"true" turns them on.
# read-only "background"/research tools are deliberately NOT gated (they work
# without any configured service).
_GIT_TOOL_NAMES = ("git_status", "git_commit")
_GIT_CREDENTIAL_KEYS = ("git_repo_url", "git_username", "git_email", "git_token")
_GIT_OPT_IN_MODES = frozenset({
    "duo_lean", "duo_core", "duo_full", "direct_full",
    "tool_agent", "mcp_agent", "openai_agent",
})
_GIT_READ_OPT_IN_MODES = frozenset({"direct_research", "direct_python"})


def has_git_credentials(settings: dict | None = None) -> bool:
    """True when at least one git credential/identity field is non-empty."""
    if settings is None:
        from settings import load_settings
        settings = load_settings() or {}
    return any(str(settings.get(_k) or "").strip()
               for _k in _GIT_CREDENTIAL_KEYS)


def git_tools_enabled(settings: dict | None = None) -> bool:
    """True if the git tools should ship in a schema (see _GIT_TOOL_NAMES).

    Requires at least one configured git credential even when
    git_tools_enabled is "true"/True. Unreadable/unavailable settings -> True
    (keeps the historical safeguard so a transient read failure never drops tools)."""
    try:
        if settings is None:
            from settings import load_settings
            settings = load_settings() or {}
    except (ImportError, AttributeError, TypeError, ValueError, OSError):
        return True
    _has_creds = any(str(settings.get(_k) or "").strip()
                     for _k in _GIT_CREDENTIAL_KEYS)
    _raw = settings.get("git_tools_enabled", "auto")
    if isinstance(_raw, bool):
        return _raw and _has_creds      # JSON true/false, requires credentials
    _mode = str(_raw or "auto").strip().lower()
    if _mode in ("0", "false", "off", "no", "never"):
        return False
    return _has_creds


def lsp_tools_enabled(settings: dict | None = None) -> bool:
    """True if the lsp_* schemas should ship.

    Same rule as the handler gate: settings["lsp_enabled"], default on.
    Unreadable settings -> True (a settings read error must not silently
    remove the tools)."""
    try:
        if settings is None:
            from settings import load_settings
            settings = load_settings() or {}
    except (ImportError, AttributeError, TypeError, ValueError, OSError):
        return True
    raw = settings.get("lsp_enabled", True)
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() not in ("0", "false", "off", "no")


def _filter_tools_for_mode(
    tools: list,
    mode: str,
    include_websearch: bool = False,
    include_git: bool | None = None,
    settings: dict | None = None,
    include_subagent: bool | None = None,
    include_view_image: bool | None = None,
) -> list:
    _eff_git = include_git
    if _eff_git is None and settings is not None:
        _eff_git = git_tools_enabled(settings)
    _eff_sub = include_subagent
    if _eff_sub is None and settings is not None:
        _eff_sub = subagent_tools_enabled(settings)
    allowed = _tool_names_for_mode(
        mode, include_websearch=include_websearch, include_git=_eff_git,
        include_subagent=_eff_sub, include_view_image=include_view_image)
    allow_mcp = (not mode or mcp_allowed_in_mode(mode))
    out = [
        t for t in tools
        if t.get("function", {}).get("name") in allowed
        or (allow_mcp and str(t.get("function", {}).get("name") or "").startswith("mcp_"))
    ]
    if _eff_git is None and not git_tools_enabled(settings):
        out = [t for t in out
               if t.get("function", {}).get("name") not in _GIT_TOOL_NAMES]
    if _eff_sub is None and not subagent_tools_enabled(settings):
        out = [t for t in out
               if t.get("function", {}).get("name") != _SUBAGENT_TOOL_NAME]
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
    "find_references", "run_python",
    "undo_last", "ask_user", "write_file_append", "apply_patch",
    # tier 1b — full/direct extras
    "get_datetime",
    "send_file",
    "view_image",
    # LSP: the two tools a small-ctx run needs most. Without priority they
    # were dropped first by the schema fitter (audit 2026-10-31, P2-5).
    "lsp_status", "lsp_diagnostics",
    # tier 2 — everything else (web, git, background, browser, research...)
]
_TIER0_TOOLS = frozenset({
    "read_file", "edit_file", "write_file", "run_bash", "task_complete",
})


def _tool_priority(name: str, mode: str = "") -> int:
    if mode in ("direct_mcp", "duo_mcp", "mcp_agent") and str(name).startswith("mcp_"):
        return 6  # keep MCP tools alongside search/list in MCP-centric tiers
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


def fit_tools_to_budget(tools: list, budget_chars: int, *, mode: str = "") -> list:
    import json as _json
    if budget_chars is None or budget_chars <= 0 or not tools:
        return list(tools)

    pool: dict[str, dict] = {}
    order: list[str] = []
    variants: dict[str, tuple[dict, dict, dict]] = {}
    sizes: dict[str, tuple[int, int, int]] = {}
    for t in tools:
        fn = t.get("function", {})
        n = fn.get("name", "")
        if not n:
            continue
        desc = str(fn.get("description", ""))
        v = tuple(
            {**t, "function": {**fn, "description": _desc_at_level(desc, lv)}}
            for lv in (0, 1, 2)
        )
        if n not in pool:
            order.append(n)
        pool[n] = t
        variants[n] = v  # type: ignore[assignment]
        sizes[n] = (len(_json.dumps(v[0])), len(_json.dumps(v[1])), len(_json.dumps(v[2])))

    state = {n: 0 for n in order}
    cur_total = sum(sizes[n][0] for n in pool)

    # 1. drop non-core tools that keep the MINIMAL set over budget
    for n in sorted(pool, key=lambda name: _tool_priority(name, mode), reverse=True):
        if cur_total <= budget_chars:
            break
        if n in _TIER0_TOOLS:
            continue  # tier 0 is always kept
        cur_total -= sizes[n][0]
        del pool[n]
        del state[n]

    # 2. upgrade by priority, two steps (minimal -> sentence -> full)
    for target_level in (1, 2):
        for n in sorted(pool, key=lambda name: _tool_priority(name, mode)):
            old = state[n]
            if old >= target_level:
                continue
            delta = sizes[n][target_level] - sizes[n][old]
            if cur_total + delta <= budget_chars:
                state[n] = target_level
                cur_total += delta
    return [variants[n][state[n]] for n in order if n in pool]


def _apply_budget_texts(tools: list, budgets) -> list:
    """Insert the run's RESOLVED budget numbers into schema-visible texts
    (audit phase 6 redesign: the schema states the truth of THIS run, not a
    module constant). Called before the budget fit — description lengths
    participate in the fit. (2026-11-11, D3: compacted — apply_patch joined
    the schema pool and the fit is zero-sum at 16k; every fact kept, the
    read cap must stay visible or the resolved-budget pin breaks.)"""
    _rc = int(getattr(budgets, "read_char_cap", 0) or 0)
    if _rc <= 0:
        return tools
    out = []
    for t in tools:
        fn = t.get("function", {})
        if fn.get("name") == "read_file":
            out.append({**t, "function": {**fn, "description": (
                "Read file content. ALWAYS read_file before edit_file on "
                "existing files. Raw content, total-lines header. Over "
                f"~{_rc} chars REFUSED (get_signatures first); capped at "
                f"{_rc} chars — use start_line/end_line for sections."
            )}})
        else:
            out.append(t)
    return out


# MCP-CLIENT (beta, 2026-10-26): the tool names are dynamic, so a static
# allowlist can never contain them. These modes are allowed to use them: they
# already hold write and exec rights, so an MCP tool adds no new capability
# class. Read-only modes (duo_readonly, pre_explore, critic_verify) and the
# plain direct chat (mode "direct") are deliberately excluded - direct_full
# is the full-access direct tier and stays included. (I15, 2026-10-31: the
# old comment said "the direct chat is excluded", which read as if
# direct_full were excluded too - it is not.)
MCP_TOOL_MODES = frozenset({"duo_full", "duo_mcp", "tool_agent", "mcp_agent",
                            "openai_agent", "direct_full", "direct_mcp"})


def mcp_allowed_in_mode(mode: str | None) -> bool:
    """True if this mode may see and call MCP tools.

    I4 (2026-10-31 audit, documented decision): mode=None deliberately
    allows MCP tools - there is no mode filter to violate. That is the
    escape hatch for unit tests and direct API calls; the HTTP surface
    (/internal/tool/exec) always sends a real mode and defaults to
    "mcp_agent", so the IDE path never runs mode-less.
    """
    if not mode:
        return True          # no mode filter at all (tests, ad-hoc calls)
    return mode in MCP_TOOL_MODES


def _mcp_tool_defs() -> list:
    """Cached specs of the configured MCP servers. Never starts a process."""
    try:
        from infra import mcp_client as _mcp
        if _mcp.enabled():
            return _mcp.tool_defs()
    except (ImportError, AttributeError, TypeError):
        pass
    return []


def _get_inline_tools(include_websearch: bool = False, mode: str | None = None,
                      budget_chars: int | None = None,
                      include_send_file: bool = False,
                      include_view_image: bool = False,
                      include_git: bool | None = None,
                      settings: dict | None = None,
                      include_subagent: bool | None = None) -> list:
    tools = list(_INLINE_CODING_TOOLS)
    if include_send_file:
        tools.append(_SEND_FILE_TOOL)
    if include_view_image:
        tools.append(_VIEW_IMAGE_TOOL)
    if mode == "openai_agent":
        tools.append(_PIPELINE_TOOL)
    if include_websearch and _WEBSEARCH_AVAILABLE and _websearch is not None:
        tools.extend(_websearch.get_tool_defs())
    if mode:
        tools = _filter_tools_for_mode(
            tools, mode, include_websearch=include_websearch,
            include_git=include_git, settings=settings,
            include_subagent=include_subagent,
            include_view_image=include_view_image)
    # MCP CLIENT (beta): exactly one place. The names are dynamic, so a static
    # mode allowlist cannot hold them and they are added AFTER the filter.
    # (_mcp_tool_defs is cache-only; servers start on /mcp/reload or at boot.)
    if not mode or mcp_allowed_in_mode(mode):
        tools.extend(_mcp_tool_defs())
    if not lsp_tools_enabled(settings):
        tools = [t for t in tools
                 if not str(t.get("function", {}).get("name", "")).startswith("lsp_")]
    if budget_chars:
        try:
            from core.budgets import active as _b_active
            tools = _apply_budget_texts(tools, _b_active())
        except Exception:
            pass
        tools = fit_tools_to_budget(tools, budget_chars, mode=mode or "")
    if include_subagent is False or (
        include_subagent is None and not mode and not subagent_tools_enabled(settings)
    ):
        tools = [t for t in tools
                 if t.get("function", {}).get("name") != _SUBAGENT_TOOL_NAME]
    return tools



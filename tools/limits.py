"""Single source of truth for tool I/O limits (2026-10-14, audit phase 1).

The schema descriptions in definitions.py are generated FROM these constants
and the handlers import the same constants — tests/test_schema_truth.py pins
all sides together so schema text can never drift from behavior again. The
audit found the read_file schema claiming "capped at 200 lines" while the
handler aborts at 1201 lines, plus a stale 400-line note in write results:
small models obey the text and ritual-read in 200-line slices the handler no
longer needs.
"""

# ── read_file (tools/handlers/file_ops.py) ──────────────────────────────────
# A full read of a file with MORE lines than this is refused with
# FILE_TOO_LARGE_NEED_RANGE (the model must use start_line/end_line).
READ_FULL_LINE_ABORT = 1200
# Full AND range reads truncate to this many chars with a [TRUNCATED] tail.
READ_CHAR_CAP = 32000

# ── universal executor cap (core/tool_executor.py _cap_tool_result) ────────
EXEC_RESULT_CAP = 8000

# ── run_bash / run_python (tools/handlers/exec_tools.py) ───────────────────
BASH_CAP = 6000
PYTHON_CAP = 4000

# ── web_fetch (tools/websearch.py) ──────────────────────────────────────────
WEBFETCH_CAP = 4000

# Project audit - structure, dead ends and risks (read-only)

2026-10-18. Nothing was executed for this audit: no model runs, no test suite.
Everything below comes from reading the tree and measuring it.

---

## The numbers

| Metric | Value |
|---|---|
| Python files (app, without tests) | 186 / **3.31 MB** |
| Largest module | `core/duo_runner.py` - **439 KB** |
| Largest single function | `run_code_duo` - **6,709 lines** |
| Functions >= 120 lines | **55** |
| `except Exception` | 915 |
| `except ...: pass` | **397** |
| TODO / FIXME / HACK | 22 |
| `shell=True` in app code | 1 (`tools/background.py`, the background launcher) |
| `eval` / `exec` / `os.system` | **0** |
| Endpoints in `routers/` vs `server.py` | 105 vs **18** |
| Test files | 156 |
| Log rotation | configured (10 MB x 3 for `hivemind.log`) |

---

## What stands out (ordered by what it costs you)

### 1. `core/duo_runner.py` is one 6,709-line function

`run_code_duo` spans planner phase, VRAM phase, coder loop, tool loop, compression,
critic, retries and finish - in a single scope, with 43 `except: pass` blocks
inside it. Consequences:

- you cannot unit-test a phase; every change is an end-to-end gamble,
- the file is 439 KB, four times the next module, and grew with every hotfix,
- the `core/duo/` package already shows the way out: `_pre_explore`, `_vram` and
  `_prefetch` were extracted - `run_code_duo` was not.

Direction: phases into `core/duo/phases/{plan,coder,tools,finish}.py` with one
small context object; keep `run_code_duo` as the orchestrator of ~200 lines. No
behaviour change required, pure move + parameter passing.

### 2. 397 silent `except: pass` (43 of them in `duo_runner`, 20 in `tools/runner`)

This is the same failure family that made the classmate's install invisible: an
error occurs, something swallows it, the user sees "completed". In a 6,000-line
function these are also the reason bugs like a stale `_split_discard_note` survive.

Direction: one helper `suppress("what", exc)` that logs at DEBUG with a stack id,
plus a lint ratchet (`ruff` custom rule or a grep gate in `tests/`) that stops the
count from growing. Converting all 397 at once is not sensible; the ratchet is.

### 3. `uv.lock` does not exist

`pyproject.toml` and `requirements.txt` carry ranges (`pydantic>=2.11`,
`fastapi>=0.115`), so **every install resolves whatever is newest that day**.
That is exactly why one machine got `pydantic 2.14.1 / pydantic-core 2.50.1` and
another could end up on a different pair - and why the "ArgsKwargs" class of
failure is possible at all.

Direction: commit `uv.lock`, install with `uv sync --frozen` in `install.bat`
and `update.bat`. This turns "works on my machine" into a reproducible set and is
a few hours of work, not a rewrite.

### 4. The debug router is mounted unconditionally

`app.include_router(debug_router)` - no flag, no token. That exposes
`/debug/tasks` (asyncio task stacks with file paths), `/debug/models` (registry and
filesystem internals), `/thinking/debug` and `/budget/debug` on an unauthenticated
loopback API. Any local process (or a page that manages a no-cors POST/GET) can
read internals. Nothing catastrophic, but it is information you did not mean to
publish.

Direction: `debug_api_enabled` (default **false**), include the router only then.

### 5. `logs/write_truncations.jsonl` records full generated file contents

7 entries, 110 KB, each with the complete raw `write_file` call (28 KB of
`index.html` content per entry). It is a debug aid that stores user code - and it
never rotates.

Direction: metadata only (`ts, tool, model, finish_reason, path, chars`), keep the
payload behind an explicit `debug_write_dump` switch. Or move it out of `logs/`
into a session-scoped file the UI can clear.

### 6. Dead settings that look alive in the UI

- `duo_distilled_executor` - **read by nothing** in Python.
- 8 more are referenced only in `app.js`: `automap_code_duo_enabled`,
  `automap_duo_pre_explore`, `automap_duo_parallel_preexplore`,
  `automap_pipeline_websearch_enabled`, `duo_pair`, `startup_preload_analyst`,
  `startup_preload_coder`, `startup_preload_judge_in_agentic`.

Verified by searching the whole tree for the literal key: `duo_distilled_executor`,
`automap_code_duo_enabled`, `automap_duo_pre_explore`, `duo_pair`,
`startup_preload_analyst` and `startup_preload_coder` appear **nowhere** in Python
outside `settings.py`. A user can toggle them in the UI and nothing changes - a
bug in the product's honesty, not just in the code.

Direction: either wire them or remove them from `DEFAULT_SETTINGS` **and** the UI,
plus a test: every default key must appear in at least one non-settings file.

### 7. Two API layouts side by side

`routers/` holds 105 endpoints across 14 modules; `server.py` still holds 18
handlers and 28 async helpers in 107 KB (`/stream`, `/tts`, `/stt`, `/health`,
gateway control, `/v1/chat/completions`). A newcomer cannot tell where an endpoint
lives.

Direction: finish the split (`routers/{stream,voice,gateway,openai_v1}.py`), keep
`server.py` as the app factory.

### 8. Names that lie

`core/agentic_tool_loop.py::post_with_retry` is **687 lines** - a whole agentic
round, not a retry helper. `backend/manager_load.py::_start_process` is **1,285
lines**. A function whose name promises one thing and does ten is the single
biggest time sink during debugging.

### 9. Routes the UI never calls (36 of 95) - mostly legitimate

The first pass matched paths without their router prefixes, so `/ps`, `/checkout`
and `/detail` are in fact `/vram/ps`, `/git/checkout`, `/git/detail`. Checked
individually:

- **legit API surface** the UI is not the caller of: `/abort/...`, `/steer`,
  `/approval/decide`, `/resume`, `/presets/{name}`, `/learned/...`,
  `/model_configs/...`, `/v1/...` - the Telegram gateway, the MCP server and
  scripts use them.
- **ordering is fine**: the `/chats/{chat_id}` catch-all is registered at line 596,
  after `/chats/search` (line 537) - no shadowing.
- **the only real question marks** are the four debug endpoints (see 4) - they are
  intentional, just unguarded.

### 10. What is genuinely good (so the picture is fair)

- **Logging**: rotating handler (10 MB x 3), faulthandler + signal + exit markers
  and a heartbeat file - a crash is diagnosable, which is rare at this size.
- **Secret hygiene**: `render.filter_secrets` is applied before anything leaves
  (chat, Telegram, TTS), and the planner redacts token-shaped strings.
- **Tool layer**: mode allowlists, approval gate, workspace containment, write
  transaction + `undo_last`, dead-name guard, `search_code` without a shell.
- **Release discipline**: `export-ignore` keeps dev material out of the zip, the
  gate checks 13 things, the installer pins its interpreter, CRLF in `.bat`.
- **Tests**: 156 standalone suites, deterministic, no model needed.

---

## If I could only do three things

1. **`uv.lock` + `--frozen`** (hours): removes the whole "different pydantic on
   every machine" class, including the failure that cost a day this week.
2. **Gate the debug router** (minutes): one setting, one `if`.
3. **`write_truncations.jsonl` to metadata** (minutes): stop storing user code in
   a log file nobody rotates.

Then, in order of long-term payoff: the `except: pass` ratchet, the dead-settings
test, and - the big one - splitting `run_code_duo` into phase modules. That last
one is the difference between "careful changes" and "any change is a risk".

None of this blocks 1.4.0. The runtime is healthy; the **change cost** is the
problem, and it grows with every hotfix that lands in that one function.

# Audit — tool use (general)

Scope: the whole tool subsystem, not the voice work. Inspected (largest first):

| File | Size | Role |
|---|---|---|
| `tools/runner.py` | 72 KB | the dispatch + approval + destructive gate + budgets |
| `core/tool_executor.py` | 62 KB | the tool round loop, parse errors, truncation injection |
| `core/tool_exec_helpers.py` | 60 KB | per-tool helpers, near-duplicate detection, hints, prefetch |
| `tools/handlers/exec_tools.py` | 42 KB | run_python / run_bash / streaming proc |
| `tools/handlers/file_ops.py` | 42 KB | read/write/edit/append |
| `core/tool_loop.py` | 37 KB | loop guard, unknown-tool abort, harness stats |
| `tools/websearch.py` | 32 KB | search backends |
| `tools/definitions.py` | 32 KB | the 25 tool schemas |
| `tools/sandbox.py` | 6.5 KB | Windows job objects (mem/proc limits, kill tree) |
| `utils/tool.py`, `utils/file.py` | 19/11 KB | arg parsing, write hints, path containment |

Verdict: the safety bones are good — better than the average local agent. The weak
points are not "the model can escape"; they are consistency, duplication and the
growth pattern of the tool list.

---

## 1. What is genuinely well built

1. **Approval gate with pre-decisions** (`tools/runner.py:563`). Returns
   `None` / `("NOTE", text)` / `("DENY", tool_error)`; consumes a decision that was
   staged during generation, drops a stale decision that belongs to a *different*
   tool (`:588`), remembers "approve this file/command" per workspace, and denies
   with a model-facing error that tells the model to pick another approach. The
   budget/approval order is right: approval is checked **before** the call burns
   budget (`:1472`).
2. **Workspace containment** (`utils/file.py::_inline_check_workspace`): protected
   HiveMind internals, a **junction walk up the tree** (Windows reparse points),
   and `resolve().relative_to(workspace)` for containment. The write path
   re-checks right before `os.replace` (`file_ops.py:26`) so a junction swapped in
   between cannot redirect the write. That is a real, thought-through TOCTOU fix.
3. **Sandbox** (`tools/sandbox.py`): job objects with memory + process-count
   limits, `kill_tree`, applied to every `run_python`/`run_bash`/streaming process.
4. **Budgets with actionable refusals**: separate pools for `web_search`,
   `web_fetch`, `install_package`, each returning a tool error that says what to do
   instead (`runner.py:1455-1494`), not a silent no-op.
5. **Loop/taxonomy instrumentation**: `core/harness_stats.py` counts
   `unknown_tool`, `invalid_args`, `repeat_call`, `parse_error`, and
   `core/tool_loop.py` aborts a model that keeps calling a tool that does not
   exist (`:444`). That is the right place to catch hallucinated tools.
6. **Truncation instead of blowing the context**: `_handle_too_large`,
   `LINE_CAP` deques for streamed subprocess output, `[TOOL_ERROR_META]` JSON next
   to the human message.

---

## 2. Problems found

### 2.1 `search_code` executed a shell string built from model input (fixed)

`tools/handlers/code_intel.py` built `rg … "pattern" "path" 2>nul` and
`grep … | head -50` strings and ran them with `create_subprocess_shell`. The
sanitizer removed only ``$ ` " '``. On Windows `cmd` expands `%NAME%` **even inside
double quotes**, so a pattern/path containing `%PATH%` was expanded, and any future
edit to that sanitizer could reintroduce a real separator (`&`, `|`, `>`).

Why it mattered more than `run_bash`: `search_code` is read-only and runs **without
an approval prompt** — a shell string there is a shell the model reaches for free.

Fixed: every backend is now an **argv list** (`create_subprocess_exec`), the
50-line cap is applied in Python, and `2>nul` / `| head` are gone (they only
existed because of the shell). Verified against the real workspace: normal regex,
literal, `%PATH%` (now: no matches instead of an expanded path), `a & b` (now: a
regex, not a separator), 10 s timeout unchanged.

### 2.2 `start_background` skipped the destructive gate (fixed)

`_destructive_gate` covered `run_python` and `run_bash` only
(`tools/runner.py`), while `start_background` executes the same kind of shell
command (`tools/background.py:96`, `shell=True` by design) and is only protected by
the approval gate. With `duo_action_approval_enabled=false` (the documented
"desktop mode"), an `rm -rf`-style command went through the background path with
no pattern check at all.

Fixed: `start_background` is now gated like `run_bash` (same pattern list, same
refusal message).

### 2.3 The tool list lives in four places (open)

A new tool must be added in: `tools/definitions.py` (schema), `_APPROVAL_TOOLS`
(`runner.py`), the handler map/if-chain (`_run_inline_tool`), and the UI/render
menu. Nothing links them; forgetting one fails at runtime, not at import.

Evidence of the growth: `_run_inline_tool` is 304 lines with 17 `name ==` /
`name in (...)` special cases. The special-casing is legitimate (budgets, approval,
arg aliasing) but it accumulates per tool.

Recommendation (not done — it is a refactor): one `TOOL_SPECS` registry entry per
tool carrying `schema`, `handler`, `approval`, `destructive`, `budgets`, and build
`_APPROVAL_TOOLS` + the handler map from it. Then a missing entry is an import-time
assert instead of a runtime surprise.

### 2.4 Unparseable arguments silently become `{}` (open)

`utils/tool.py::parse_tool_args` returns `{}` when JSON parsing fails (twice: raw,
then a backslash repair). The tool then runs with no arguments. For a tool with
required args it produces a confusing missing-arg error at best; for a tool with
only optional args it runs with **defaults** — e.g. a `list_dir` on the workspace
root instead of the intended subdirectory. The harness already counts
`parse_error`, but the call is still attempted.

Recommendation: return a sentinel (`{"__parse_error__": <first 200 chars>}`) and
have `_execute_one_tool` answer with a `PARSE_ERROR` tool error instead of calling
the handler. That keeps the model's retry loop honest (it sees *why*).

### 2.5 Budget counters outside a run context reset per call (open, low)

`_consume_web_search_budget` etc. store the counter in a `ContextVar` and create a
fresh list when none exists. Any call made **outside** the run context (tests,
direct API use, a future scheduler) therefore gets a fresh budget every time.
Harmless today because real runs always have the context; worth a comment or an
explicit "no counter, no limit" branch so it is a decision, not an accident.

### 2.6 Duplicated arg-name tolerance (open, low)

`args.get("cmd") or args.get("command")`, `args.get("code") or args.get("source")`,
`_JSON_EDIT_KEY_OLD/NEW` tuples — every handler invents its aliases. It works, but
every new alias must be remembered in each place that reads the arg (approval
normalisation included). A single `_ARG_ALIASES` map applied once at parse time
would remove the class.

### 2.7 `_APPROVAL_TOOLS` is a manual list (open, low)

It currently holds `run_bash, run_python, install_package, start_background,
write_file, edit_file, write_file_append, git_commit` — complete as far as the 25
shipped tools go. But `undo_last`, `task_complete` and `browser` are state-changing
in their own way and are deliberately not gated; that decision is not written down
anywhere in the file.

---

## 3. Structural check

- **Placement is consistent**: `tools/` = tool surface (definitions, runner,
  handlers), `core/` = the loop that drives it, `utils/` = pure helpers. New code
  (voice) did not touch this layout, and the websearch work stayed inside
  `tools/websearch.py`.
- **The loop is testable**: the new suites pin tool behaviour at the source/unit
  level (`test_websearch_quality`, `test_websearch_budget_stop`,
  `test_tool_error_taxonomy`, `test_workspace_and_cpu_guards`, …). 142 suites.
- **No new dependency** is needed for any tool; `httpx`, `subprocess`, job objects
  only.

## 4. Regression evidence for the two fixes

- `search_code` rewritten and exercised against the real workspace (regex, literal,
  `%PATH%`, `a & b`): results unchanged for real searches, no shell involved.
- `start_background` now returns the same `GATE_DESTRUCTIVE` refusal as `run_bash`.
- Suite **142/142**, `ruff` clean on both touched files.

## 5. Recommended order of work

1. `parse_tool_args` sentinel (2.4) — smallest change with the largest effect on
   model behaviour after a malformed call.
2. Tool registry (2.3) — enables 2.6 and removes a whole class of "forgot to
   register it" bugs.
3. Write down the approval policy per tool (2.7) next to the registry.

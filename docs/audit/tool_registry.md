# Audit 2 — tool registry (2.3), parse errors (2.4), budget counters (2.5), aliases (2.6), approval list (2.7)

Follow-up to `audit_tool_use.md`. This round is the response to the four open
findings, with the numbers re-measured instead of estimated.

---

## 2.3 — the tool list: measured properly

My first report said "four places". That was wrong in one direction and right in
another. Measured now (`_INLINE_TOOL_HANDLER_MAP`, `_tool_names_for_mode`,
`_APPROVAL_TOOLS`, `_PHONE_SAFE_TOOLS` read at import time):

| Structure | Size | Role |
|---|---|---|
| `tools/definitions.py` schemas | 25 core (+ web) | what the model sees |
| `_TOOL_MODE_ALLOWLISTS` | 6 modes | which of them each mode offers |
| `_INLINE_TOOL_HANDLER_MAP` | **30** | implementation lookup |
| `_APPROVAL_TOOLS` | 8 → **11** | human approval gate |
| `_PHONE_SAFE_TOOLS` | 13 | restricted (phone) allowlist |
| `_destructive_gate` names | 2 → **3** | pattern check for shell tools |
| `_path_based_tools`, `_WRITE_TOOLS_NO_FUZZY`, `_READ_TOOLS`, `_WRITE_TOOLS` | 4 sets | per-tool behaviour |
| `server.py` `openai_agent` dispatch | — | `hivemind_pipeline` lives there |
| `tools/definitions.py` prompt text | — | tool names in the guidance |

So it is **ten** places, not four. What can be done better, in order of value:

1. **A test that links them (done).** `tests/test_tool_registry_consistency.py`
   (21 checks) asserts: every offered name has a handler, every handler is offered
   by some mode (or explicitly legacy), every offered write tool is approval-gated,
   the phone list holds no write/shell tool, every mode's names resolve, and the
   approval gate reads the same argument spellings the tool executes. This is the
   cheap 80 %: a missing entry becomes a red test instead of a live surprise.
2. **One `TOOL_SPECS` entry per tool** (not done - a refactor). Each entry would
   carry `schema`, `handler`, `approval`, `read_only`, `budgets`, and the four
   sets above would be *derived*. Then `_APPROVAL_TOOLS` cannot drift, and
   `definitions.py` stops being a second list.
3. **Generate the prompt's tool section from the schemas** (not done). Today the
   guidance text in `definitions.py` names tools by hand; a renamed tool leaves a
   stale sentence.

Also found while measuring: `_TOOL_MODE_ALLOWLISTS["openai_agent"]` contains
`hivemind_pipeline`, which has **no handler in the inline map** - it is dispatched
by `server.py:1733`. Legitimate, but it is exactly the kind of exception that
belongs in one commented place; the new test now names it (`EXTERNAL`).

## 2.4 — parse errors: implemented, and the real gap was elsewhere

Verification first: `parse_tool_args` returns `{}` for broken JSON, and **the duo
path already caught it** (`tool_executor.py`, `_args_parse_failed` →
`INVALID_JSON` tool error). The **agentic loop** (`core/tool_loop.py`) did not: it
took `{}` and called the tool.

Fixed there: a non-empty argument string that parses to nothing now produces a
`[TOOL_ERROR:INVALID_JSON]` tool message plus the existing retry feedback
(`parse_error`, counted in `harness_stats`). Empty, `{}`, `null` and `none` stay
"no arguments" so tools with only optional args keep working.

## 2.5 — what I meant about the counters

The web-search / fetch / install budgets live in `ContextVar`s that the run sets
up (`_consume_web_search_budget` reads `_web_search_count.get(None)`). Called
**inside** a run, the same list is reused, so the counter climbs and the budget
trips. Called **outside** a run (unit test, direct API call, a future scheduler)
the ContextVar is `None`, a fresh `[0]` is created, and increments die with the
call - the budget can never trip.

That is why `tests/test_websearch_budget_stop.py` can loop the tool. It is not a
bug, but it was undocumented, which made it look like one. The function now says
so in a comment. No behaviour change.

## 2.6 — aliases: verified, and now pinned

The concern was a gate that approves one field while the tool executes another.
Measured:

| Tool | Gate reads | Handler reads | Agree |
|---|---|---|---|
| `run_bash`, `start_background` | `cmd` **or** `command` | `cmd` or `command` | yes |
| `run_python` | `code` **or** `source` | `code` or `source` | yes |
| write tools | `path` only (`_norm_write_path`) | `path` only | yes |

So there is no gate/execution mismatch **today**. It is still a maintenance trap,
so the four agreements are now assertions in the registry test. A future
`file_path` alias added to a handler but not to `_norm_write_path` fails the suite
instead of silently escaping the path-scoped approval memory.

## 2.7 — the approval list: one real hole

`edit_ast` and `replace_lines` are offered in `duo_full` and **write files**, but
they were not in `_APPROVAL_TOOLS`. With `duo_action_approval_enabled=true` the
model could rewrite a file through either of them without a card. `patch_file`
kept a live handler that no mode offers (reachable, because the duo path did not
validate the name against the offered list).

Fixed:
1. `edit_ast`, `replace_lines`, `patch_file` added to `_APPROVAL_TOOLS`.
2. A **dead-name guard** in `_run_inline_tool`: a name that no mode offers is
   rejected with `TOOL_NOT_FOUND` before any handler runs (this is what makes
   `patch_file` unreachable everywhere, and it future-proofs against a resurrected
   legacy handler).
3. The policy is written down **next to the list** (what is gated and why
   `undo_last`, `remember`, `task_complete`, `browser` are deliberately not).

Not changed, on purpose: the per-mode strictness. `duo_readonly` offers 9 tools,
but the dispatcher does not refuse `write_file` if a model calls it anyway. Adding
that would be the logically next step, but it touches every read-only phase
(automap pre-explore, critic verify) and needs a live run to prove no legitimate
flow depended on it. Recorded as the recommended next step, not smuggled in.

## Evidence

- `tests/test_tool_registry_consistency.py` — **21 checks**, new, registered.
- `test_action_approval` 59, `test_execute_tool_round` 47, `test_tool_arg_schemas`
  13, `test_tool_error_taxonomy` 26, `test_websearch_budget_stop` 24 - all green.
- Full suite **143/143**.
- `ruff` clean on `tools/runner.py`, `core/tool_loop.py`.

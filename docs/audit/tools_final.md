# Audit 4 — every tool (final pass)

Scope: all 30 inline tools, their gating, their descriptions, and the two write
tools in detail. Matrix generated from the live code
(`_INLINE_TOOL_HANDLER_MAP`, `_APPROVAL_TOOLS`, `_MODE_STRICT_NAMES`,
`_PHONE_SAFE_TOOLS`, `_TOOL_MODE_ALLOWLISTS`), not from memory.

---

## 1. Gating matrix (as of `f850faf` + this commit)

| Tool | Class | in `duo_full` | approval | mode-strict | phone-safe |
|---|---|---|---|---|---|
| `read_file`, `list_dir`, `find_files`, `search_code`, `get_signatures`, `find_references` | READ | yes | - | - | yes |
| `get_background_output`, `get_datetime` | READ/NET | yes | - | - | yes |
| `git_status` | READ | only with credentials | - | - | no |
| `write_file`, `write_file_append`, `edit_file`, `edit_ast`, `replace_lines` | WRITE | yes (except `patch_file`) | **yes** | yes | no |
| `undo_last` | WRITE | yes | **no (deliberate)** | yes | no |
| `run_bash`, `run_python`, `install_package`, `start_background` | EXEC | yes | **yes** | yes | no |
| `stop_background` | EXEC | yes | no (kills what it started) | yes | no |
| `run_tests` | EXEC | yes | **no (deliberate, see 3.2)** | yes | no |
| `git_commit` | STATE | only with credentials | **yes** | yes | no |
| `remember` | STATE | yes | no (own memory DB) | - | yes |
| `task_complete`, `ask_user` | STATE | yes | no | - | yes |
| `browser`, `subagent_research` | NET | yes | no | - | **no** |
| `web_search`, `web_fetch` | NET | conditional | - | - | yes |
| `patch_file` | WRITE | **no** (legacy, guard rejects) | yes | yes | no |

Consistency checks, all green:
- offered-but-no-handler: **none** (external: `hivemind_pipeline`, served by `server.py`);
- handler-but-not-offered: `patch_file` only, rejected by the dead-name guard;
- write tools offered without approval: **none** (after `edit_ast`/`replace_lines`
  were added);
- phone-safe tools that mutate: **none**.

## 2. Git behind credentials: yes, three layers

1. `_filter_tools_for_mode` drops `git_status`/`git_commit` from the schema when
   `git_tools_enabled()` is false (no repo URL, no user, no email, no token).
   Verified live: with the current settings the schema contains **zero** git tools.
2. The dispatcher's mode guard refuses machine-changing tools outside the mode -
   `git_commit` is mode-strict, so a mode without it cannot commit.
3. The handlers themselves check `_shared._GIT_TOOLS_AVAILABLE` and answer
   `GIT_UNAVAILABLE` - so even a hand-crafted call cannot reach `git`.
   `git_status` also whitelists its sub-command (`status|diff|log|show`), so no
   `-c`/`--exec-path` injection.

## 3. Findings

### 3.1 `write_file`'s description contradicted its handler (fixed)

Description: *"Create a NEW file … Existing files: use edit_file instead."*
Reality: the handler **overwrites** existing files (with a shrink guard), and the
documented continuation path (`write_file part1` → `write_file_append part2`)
depends on that. A model following the description would refuse to rewrite a file.

Also: the shrink guard needs `confirm_shrink=true`, which was **not in the
schema** - the escape hatch was unreachable for any model that does not invent
arguments.

Fixed: description now says *create or overwrite completely, prefer `edit_file` for
a small change*, documents the truncation/append path, and the schema carries
`confirm_shrink` (boolean, optional).

### 3.2 `run_tests` is un-gated on purpose - and that is a known residual risk

`run_bash` and `run_python` ask; `run_tests` does not, although it **executes
project code** (pytest collects `conftest.py`, npm runs scripts). The reason it
must stay un-gated: the auto-test ladder runs it after every write, so a card
would appear on every edit.

The protection that remains: writing the test is gated (`write_file`), the
command is not user-supplied, and the workspace is contained. Residual risk: a
pre-existing `conftest.py` in a cloned repo runs without a card. Documented, not
changed - gating it would trade a real workflow for a theoretical case.

### 3.3 `undo_last` and `stop_background` un-gated (documented)

`undo_last` restores a state that already existed (reverting an approved change);
`stop_background` kills a process the model started itself (starting it needed the
card). Both stay mode-strict, both are listed in the policy block next to
`_APPROVAL_TOOLS`, and the consistency test pins the exception list.

### 3.4 `write_file` answered with an `edit_file` error code (fixed)

An empty body produced `EDIT_FILE_EMPTY_EDITS` from `write_file`. Now
`WRITE_FILE_EMPTY`.

### 3.5 `write_file_append` description: reverted by the budget contract

I had added "the file must exist" to its description - then reverted it: it is a
**non-core** tool, and a longer text there breaks the schema-budget contract.
The information already lives in the error (`FILE_NOT_FOUND` says exactly that).

### 3.6 Tool descriptions: no paradoxes left

Checked every description for the classic contradictions:

| Tool | Verdict |
|---|---|
| `ask_user` | clean: "ONLY when the answer cannot be found … NOT for confirmations" - explicit and consistent with the gate |
| `task_complete` | clean: requires the `status` object, matches the schema |
| `edit_file` | clean: "exactly once", "copy verbatim", points to `write_file` for new files |
| `write_file` | **was contradictory, fixed** (3.1) |
| `write_file_append` | clean (append-only, follow-up in the same sequence) |
| `run_tests` | clean: "MUST run before task_complete", "no suite -> verify via run_bash" |
| `undo_last` | clean: no path = all files of the round, with path = one file |
| `git_status` | clean: enumerates the four sub-commands |

### 3.7 `write_file` / `write_file_append` - the guards are solid

Read end to end:
- workspace containment **before** the exists probe (no path oracle);
- `get_transaction().capture_before()` → `undo_last` works;
- block-format sniff: SEARCH/REPLACE or `<parameter=` markers are refused
  (this used to collapse a 1259-line file);
- no-op write refused (`WRITE_FILE_NOOP`);
- **suspicious shrink** refused unless `confirm_shrink` (now reachable);
- CRLF preservation;
- `write_file_append` refuses a missing file, inserts the missing newline at the
  boundary, and is written for **exactly once** (nothing after the write may
  produce an error - a live duplicate-append bug is called out in the code);
- append with empty content refused (`WRITE_FILE_APPEND_EMPTY`).

## 4. Test changes made by this audit

- `test_tool_descriptions`: pinned the **old** contradictory sentence - replaced by
  the create-or-overwrite contract + the `confirm_shrink` mention.
- `test_budgets`: the assertion *"max(non-core description) <= max(core) + 1"* is a
  brittle proxy (a core tool whose whole text is one short sentence is "shorter"
  than an upgraded non-core tool while still being fully described). Replaced with
  the meaningful properties: every kept tool has a description, and shrinking never
  adds tools or text.

## 5. Verdict

Every mutating tool is either approval-gated or documented as deliberately not, the
git tools are triple-gated, the phone allowlist contains nothing that writes, and
the write path has the strongest guards in the codebase. The one intentional
exception (`run_tests`) is named here instead of hidden.

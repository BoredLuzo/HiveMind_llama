# HiveMind 1.4.1

A follow-up release to the project audit: installs are now reproducible, the UI
has no switches that do nothing, and the debug/telemetry surface is smaller.

## Reproducible installs

`uv.lock` ships with this zip (64 pinned packages) and both installers sync from
it (`uv sync --frozen`). Before, every install resolved the newest package of the
day - which is how two machines ended up with different pydantic versions and one
of them with a broken compiled module.

## No dead switches

Nine settings existed in the UI but were read by nothing. They are gone, and the
controls that *looked* like them now drive the real thing:

- the Automap card writes `duo_pre_explore`, `duo_parallel_preexplore` and
  `pipeline_websearch_enabled` (the three `automap_*` twins were dead),
- startup preload keeps one working switch (`startup_preload_enabled`) - the
  per-role "preload analyst/coder/judge" rows are removed,
- `duo_distilled_executor` and `duo_pair`-as-a-setting are gone (`duo_pair` stays
  as documented UI state for the pair selector).

## Less exposure

- The **debug endpoints are off by default** (`debug_api_enabled`): they exposed
  asyncio task stacks with file paths and registry internals on an unauthenticated
  local API.
- `logs/write_truncations.jsonl` no longer stores the raw tool call (i.e. generated
  file content). It keeps path, lengths, `finish_reason` and whether the JSON was
  complete - enough to spot a truncated write without warehousing user code.

## Docs

`docs/settings.md` is generated again: `python deploy/gen_settings_docs.py` (the
script the doc always referenced but that was never committed). `--check` fails
when the doc drifts, and the suite pins it.

## Upgrade

Run `update.bat` (it backs the code up first). The update now also re-syncs the
environment from the lockfile. `settings.json`, `models.json`, `.venv`, `llama`,
`sessions` and `logs` are untouched.

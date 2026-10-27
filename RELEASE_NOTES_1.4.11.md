# Release 1.4.11

Scope: v1.4.10..v1.4.11. Themes: the MCP client gets HTTP and a card, settings writes stop losing changes, the bash guard closes its remaining holes, and a MoE sizes itself.

## MCP client (beta) grows up

- A server entry can use `url` instead of `command` now: Streamable HTTP next to
  stdio. Answers may come as JSON or as an SSE stream, a `Mcp-Session-Id` from
  the server is carried on every call, and nothing is started, so there is no
  process to kill. Both transports share the tool prefix, the name cap, the mode
  gate and the approval gate.
- New card in the config page: enable, the server list as JSON, Save, Reload and
  a live status line. Until now the beta could only be configured by editing
  `settings.json`.
- The tool list follows `nextCursor`, so a paginating server no longer loses
  every tool after the first page.
- `mcp_reload` is a real tool: the model can restart the servers itself when a
  call reports a dead one. It uses the configuration the client was started
  with.
- Documentation says what the server process actually sees: proxy tools work in
  the engine's workspace, local ones in `HIVEMIND_WORKSPACE`.

## Settings changes no longer get lost

- Two writers that each changed a key used to lose one of them: `save_settings`
  is atomic per write, but the read-modify-write around it was not (measured:
  `a=111` and `b=222` ended as `a=0`, `b=222`). All changes go through
  `mutate_settings()` now, which holds the lock across read, change and write.
- The temp file carries the process id and one retry, so a vanished temp file
  cannot fail a save.

## bash without leftovers, second round

- The operator list was incomplete: a single `&` still created files named `&`,
  `echo` and `done`, and a newline silently dropped the second command. The
  guard covers `&`, `|`, `;`, `>`, `<`, backtick, `$(`, newline, CR and `^` now,
  and the shims report the tool name like their siblings.

## Model sizing without table edits

- A MoE model that is not in the VRAM table is estimated from its own profile:
  `moe_cpu_experts` plus `vram_gb_override`, with an optional
  `kv_bytes_per_token` for the KV cost. A local model with a profile needs no
  repo edit; before, a missing table line fell back to the dense estimate and
  the pre-flight refused the load.
- Two bugs on the way: an entry had been merged into another model's dict
  (valid Python, so nothing complained and that model was unreachable), and the
  MoE path raised `KeyError` for any entry without KV fields.

## Installer

- `--no-drafters` is an explicit opt-out that names its price; `--essential`
  does not skip drafters any more (they are in use since speculative decoding is
  back on).
- Model downloads resume with a Range request and check the final size instead
  of starting an 18 GB download from zero.

## Reliability

- 21 `asyncio.create_task` calls without a strong reference go through
  `utils/bg_tasks.spawn`: the task cannot be garbage-collected mid-flight and a
  failure is logged instead of lost.
- `_atomic_write` keeps the target file's POSIX mode (the 0o600 temp file
  stripped the executable bit from every rewritten script).
- The git helpers, the two PowerShell calls in the settings endpoint and the
  model downloads have timeouts; a held `.git/index.lock` can no longer hang a
  run step.
- `sync_to_installs` takes its file list from `git ls-files`, so a scratch file
  in the working tree no longer lands in the install.
- The regression runner fails when a registered suite is missing instead of
  skipping it silently; a UI test no longer asserts on the live install's
  presets.

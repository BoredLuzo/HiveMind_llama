# Release 1.4.12

Scope: v1.4.9..v1.4.12. The 1.4.10 and 1.4.11 releases were withdrawn
(they shipped a maintainer path inside `deploy/prepush_check.ps1`), so this
release is the upgrade target from 1.4.9 and contains everything those two
had, plus the 1.4.12 work below. Themes: the Hermes coder in the installer is
the new APEX-Mini, the MCP client learns auth and retries, long runs became
diagnosable, presets became safe to re-save, and several silent bugs are gone.

## If you come from 1.4.9: what 1.4.10 and 1.4.11 already brought

- **Chunking** (opt-in, `duo_chunking`) plans only what the request names, a
  chunk is done after its test verdict, the last chunk needs the critic's
  approval like every other, and both chunking and pre-explore ship off.
- **MCP client** (beta, off by default): use external MCP servers as tools
  over stdio or Streamable HTTP, with a config card in the UI, paginated tool
  lists and an `mcp_reload` tool the model can call itself.
- **Coder speed**: speculative decoding (draft-MTP) is back on - measured
  +55% decode on the 35B coder, +54% on qwen3.5:4b-mtp, for ~2% prefill.
  Greedy output is not bit-identical with it; `HIVEMIND_MTP_SPEC=0` restores
  that.
- **Settings writes no longer lose keys** (read-modify-write is locked now),
  and the installer's judge/decider model became an optional step.
- `POST /vram/load` reports the context it used and where it came from.


## New coder model in the installer

- The Hermes entry is now **Hermes3.6 Genesis APEX-Mini** from the author's
  Genesis-Hermes repository: 20.2 GB weights, the same projector, and the chat
  template the author ships with it is downloaded next to the weights and used
  for the model. Same architecture as before (MoE 35B with 3B active, MTP head,
  vision, 262k trained context), so MTP, CPU expert offloading and the VRAM
  estimate carry over unchanged.
- The old tag keeps its profile, so presets and installs that already have the
  larger file keep working. New installs get the Mini.

## Long runs stay diagnosable

- Every coder round logs its own numbers now: generation time, decode rate,
  prompt tokens actually processed and the free VRAM afterwards. A run that
  slows down shows whether the model was busy or something else was.
- The read timeout takes the context into account. On an 8 GB card prefill runs
  at roughly 90-130 tokens per second, so a 38k prompt costs several minutes;
  that used to end as "timeout" while the server was still working.
- `llama_ctx_checkpoints` (default 4) and `llama_cache_ram_mib` (default 1024)
  are settings now instead of fixed flags. Both bound what llama.cpp keeps per
  slot; `0` disables them.

## Fixed

- The coder slot's in-use guard never ran: an async lookup was called without
  awaiting it, the error was swallowed, and the guard stayed off since it was
  added. A slot could therefore be evicted in the middle of a round.
- A load that only just fits now reports how much VRAM is left afterwards and
  warns that decoding will slow down as the context grows.

## MCP client: auth, version negotiation, retries

- HTTP server entries can authenticate: `headers` are sent verbatim, and
  `token_env` names an environment variable whose value goes out as
  `Authorization: Bearer ...`. A missing or empty variable fails the start
  with the variable name in the message; tokens never appear in logs.
- HTTP 401/403 answer as `[TOOL_ERROR: MCP_AUTH_FAILED]` and keep the
  connection alive - a wrong token is a config mistake, not a dead server.
- Protocol versions are negotiated and re-offered after a reload; a server
  that rejects the offer gets exactly one retry with the version it
  advertised.
- HTTP requests retry connect errors, read timeouts and 502/503/504 with
  0.5/1.5/3.0 s backoff. `tools/call` is only retried on connect errors (the
  tool may already have executed), and a timed out call sends
  `notifications/cancelled`.

## Model profiles and the load log

- A model profile's `kv_cache_type` applies to dense models now (profile >
  family table > global), mirroring the MoE path.
- `[VRAM-LOAD]` logs where the context came from (`ctx_source`).

## UI

- `until_finished` defaults to on: a coding run finishes the job instead of
  stopping at the round limit.
- Every context perf field carries a tooltip, and the model filter accepts
  several search terms ("qwen mtp" finds qwen3.5:4b-mtp now).

## Fixed

- The coder slot's in-use guard never ran: an async lookup was called without
  awaiting it, the error was swallowed, and the guard stayed off since it was
  added. A slot could therefore be evicted in the middle of a round.
- A load that only just fits now reports how much VRAM is left afterwards and
  warns that decoding will slow down as the context grows.
- The installer's judge download never ran (a 0x0C byte in the path). Fixed
  and pinned by an encoding test.
- The judge model and the routing decider are optional installer steps now,
  off by default, both saying how to install them later.
- The release zip no longer ships a personal path inside
  `deploy/prepush_check.ps1` (the two withdrawn releases carried it).
- `update.bat` ignored `server_port` in settings.json (the file was read
  without JSON parsing), so the "instance running" guard always checked
  port 8001 - wrongly blocking an update behind a running engine, or not
  guarding a custom port at all.
- Re-saving a preset destroyed its saved custom prompts (they were
  overwritten with the built-in base prompts). A re-save now keeps them;
  only a new preset starts from the base.
- Applying a preset now live-syncs `vram_preflight_gate` into the backend
  (it used to take effect only after a restart), bumps the settings
  revision against stale-tab overwrites, and stops freezing bookkeeping
  keys (`active_preset`, save/apply stamps) into every snapshot.
- The preset auto-load guard compared ISO stamps as strings and flipped
  once per year across a daylight-saving boundary, reverting newer user
  edits on boot. Stamp offsets are parsed now.
- The README no longer names retired search defaults (`bing,duckduckgo`,
  `all`) and documents the Telegram gateway, the routing decider, the
  watchdog and presets.

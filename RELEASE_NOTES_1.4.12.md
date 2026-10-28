# Release 1.4.12

Scope: v1.4.11..v1.4.12. Themes: the Hermes coder in the installer is the new APEX-Mini, the MCP client learns auth and retries, long runs became diagnosable, and several silent bugs are gone.

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
  `deploy/prepush_check.ps1`.

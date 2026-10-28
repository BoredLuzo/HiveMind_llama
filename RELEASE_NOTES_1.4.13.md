# Release 1.4.13

Scope: the fixes and measurements after the 1.4.12 upload (which was
replaced three times on GitHub and is superseded by this release).

## Speed: the 35B coder loads without mmap

- The apex/compact coder profile pins `load_mode: none`: llama-bench
  tg128 measured 2.92 t/s with mmap+mlock vs 13.77 t/s with none, live
  15-16 t/s. The expert pages page in from NVMe under RAM pressure -
  that was the "35B drops to 10 t/s after an hour" mechanism.
- A RAM-GUARD in the loader rejects `load_mode: none` when free RAM is
  below GGUF size + 8 GB (live: three stacked hermes commits froze the
  machine at 56 GB commit, SSD at 100 %) and falls back to plain mmap.
- Spark-X2.6 on Vulkan runs ~6 t/s (bench-verified twice, one
  unexplained 21 t/s reading) - prefer Qwen3.5-4B-MTP for coding.
- MiniCPM-V 4.7 is vision-capable again (projector wired, bench and
  live verified).

## Fixes

- Presets: a load applies the llama startup flags immediately, warns
  about models missing locally, re-saving keeps custom prompts, the
  autoload guard survives daylight-saving stamp compares.
- MCP client audit fixes: servers that never started no longer show as
  running, handshake error statuses fail loudly, `GET /mcp/servers`
  redacts env/header values, `close_all` forgets dead servers.
- One context resolver (chat, load, duo) instead of three copies.
- A trailing `-ud` in a model name reaches the KV family table again.
- `update.bat` ignored `server_port` in settings.json (read without
  JSON parsing), so the "instance running" guard always checked port
  8001.
- Gateway: `/mode auto` on the phone hands every message to the engine
  decider, and an answering phone chat keeps the run parameters it
  started with.

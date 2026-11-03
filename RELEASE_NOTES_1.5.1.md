# Release 1.5.1

Security and reliability round on top of 1.5.0, plus better memory retrieval,
honest privacy notes and new opt-in MoE load controls. No settings are reset
and no model has to be re-downloaded.

Upgrade with `update.bat` (or the startup check offers it), or extract the zip
next to the old install. Settings, models, sessions and the venv are kept.

## Security and reliability

- MCP file/search tools enforce the secret denylist on every path, including
  the forwarded engine path. `/settings` no longer persists GET-only keys and
  preset exports are scrubbed of tokens.
- The approval gate fails closed without a run id; taint from web/MCP content
  survives prefetched tools; dropped SSE frames are marked as an incomplete
  answer instead of being saved silently.
- Tainted runs also ask before `run_tests` and mutating MCP tools (even with
  the approval toggle off), and `/abort` during an approval pause stops the
  run instead of only reporting success.
- The repo-map delta now survives the compression it is appended for, and the
  tail/decode loop guards keep counting across tool rounds.
- Gateway: `/stop` during the start window no longer leaves the engine run
  alive, abort after a resume works again, and a 429-truncated answer is
  labelled.
- `write_file` preserves CRLF, model downloads verify the Hugging Face LFS
  SHA256, and git undo/consolidate keep untracked files and deletions.
- Linux: ROCm is a real backend, startup never kills a foreign process on the
  llama ports, and the death-forensics block works.

## MoE expert cache (opt-in) and VRAM fit (default on)

- `moe_cache_mib` (`--moe-cache-mib`) keeps hot experts in VRAM. It is off by
  default, capability-checked, counted in the VRAM pre-flight, and a failed
  load retries once without the flag. On a Vulkan RX 6600 the cache made Groot
  slower in our measurement (27.5 → 6.0 t/s), so leave it off there.
- `llama_fit` (`--fit` / `--fit-target`) sizes unset arguments to the free
  VRAM. Fit alone was neutral in the same test (27.3 t/s); it is a toggle in
  the model tab.
- MoE loads now use half the logical CPUs for `--threads`/`--threads-batch`.
- The CPU fallback and CPU backend are honored: an explicit layer override
  wins over the model registry, `--fit` is not applied on a CPU backend, and
  CPU-only hosts no longer evict peers for a GPU VRAM budget.

## LSP

13 languages verified end to end, including installer packs for Go, C/C++ and
Lua. Servers run with a scrubbed environment; a repo's `.vscode/settings.json`
is not forwarded unless you opt in (`lsp_forward_workspace_settings`). LSP
executes project toolchains, so only use it in trusted workspaces.

## Memory retrieval uses SQLite FTS5

Repo insights are now found by **bm25 text ranking** (porter stemming, prefix
match) instead of the old 96-bin hash vectors. That means a query for "auth"
finds an insight about "authentication failures", and a query with no overlap
returns nothing instead of a random near-zero hit. Same-path hints still rank
first. No new dependency — FTS5 ships with Python's `sqlite3`; if a build
lacks it, the old cosine path stays as the fallback.

## Privacy

- The README now lists **what leaves the machine** and when: Telegram, web
  search, model downloads, the update check and external MCP servers.
- `docs/gateway_setup.md` states plainly that a bot's traffic is not
  end-to-end encrypted and passes through Telegram.
- Soul skill distillation is **off by default** now (it spent one extra LLM
  call after every run); switch it on in the SOUL tab.
- Auto stays as it is; only AutoMap and the soul engine are labelled
  experimental.

## Safety

- `force_kill_all` kills only this installation's own servers now. The old
  version swept by image name and would have killed LM Studio or a second
  HiveMind; the name-wide sweep needs `/vram/kill_all?all=true`.
- Linux live VRAM is read from sysfs (`mem_info_vram_*`) for AMD/Intel cards,
  with `nvidia-smi` as the fallback.

## Installer

- The voice setup can now log in to Hugging Face itself: when the gated TTS
  model cannot be fetched, it offers a hidden token prompt and retries,
  instead of asking you to run the command elsewhere.

## Tests

257 regression suites, ruff clean, 13 pre-push checks.

# Release 1.5.1

Small follow-up to 1.5.0: better memory retrieval, honest privacy notes and
three safety fixes. No settings are reset and no model has to be re-downloaded.

Upgrade with `update.bat` (or the startup check offers it), or extract the zip
next to the old install. Settings, models, sessions and the venv are kept.

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

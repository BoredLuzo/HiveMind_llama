# Release 1.6.2

Patch release adding optional pure-NumPy local TTS (`ConwayResearch/Underdog-Woof`),
fixing `"remember that"` memory writes when thinking tokens precede extraction,
and cleaning historical `RELEASE_NOTES_*.md` files out of the repository root.

No settings are reset and no existing model has to be re-downloaded. Upgrade
with `update.bat` (or the startup check offers it), or extract the release zip
next to your old install — `settings.json`, `models.json`, `sessions\`, `logs\`,
`voice_models\`, and `.venv` are kept.

## Underdog Woof Local CPU TTS (Opt-In)

- Added an optional **Underdog Woof** (`woof`) backend under **Settings -> Voice ->
  Text to speech** alongside the default Pocket TTS backend: pure NumPy on CPU
  (~40.6 MB int8 weights, voices `juno` and `mira`), with no PyTorch install and
  no Hugging Face login required (`deploy/fetch_woof.py`, `core/tts_woof.py`,
  `core/tts_engine.py`, `deploy/setup_voice.bat --woof`).
- Weights, runtime code, and verbatim `LICENSE` / `NOTICE` files are downloaded
  on demand into `voice_models/underdog_woof/` (never bundled in release zips)
  and protected during updates (`deploy/updater.py`, `deploy/update_prune.ps1`).
  Underdog Woof is licensed under the Underdog Non-Commercial License 1.0
  (non-commercial use only; attribution shown automatically when active).

## Websearch Quality & SearXNG Reliability

- Fixed random/off-topic search results caused by legacy `"bing,duckduckgo"`
  pins on older installs (`settings.py` now clears legacy `"bing,duckduckgo"` +
  `"all"` settings on load, and `configure(engines="")` properly resets empty
  engine lists and flushes cached hits in `tools/websearch.py`).
- Updated `searxng-config/settings.yml.example` to enable `duckduckgo web` and
  `yahoo` alongside `google cse`, keeping CAPTCHA-blocked `duckduckgo`,
  inactive `mojeek`, and fallback-spam `bing` disabled.
- Added strict `site:` / `-site:` domain enforcement, query-term relevance
  filtering & ranking (rejecting zero-overlap fallback pages), `msockid` /
  `utm_*` tracking cleanup, automatic healthy-engine rescue when default
  instance engines fail, and accurate `[N]` result counts in the web UI pill.

## Memory Extraction & Repo Cleanup

- Fixed explicit memory shortcuts (`"remember that..."`) returning empty writes
  when model thinking tokens interfered with extraction (`context/chat_util.py`,
  `core/chat_shortcuts.py`, `hive_functions/pipeline.py`).
- Organized historical `RELEASE_NOTES_*.md` files under `docs/releases/` while
  excluding them from release zips via `.gitattributes` to keep the repository
  root clean.

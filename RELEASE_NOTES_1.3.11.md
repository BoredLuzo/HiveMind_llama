# HiveMind 1.3.11

Web search that actually answers, a planner-briefing cap, and the first piece of
the 1.4 voice work (off by default).

## Web search

The same search used to return the same handful of pages for every query. Four
causes, all fixed:

- the shipped SearXNG config *replaced* every upstream default instead of merging
  with it, so requests went out without a User-Agent (Wikimedia answers 403 and
  the wikipedia engine returned nothing, silently);
- google was bot-blocked from residential lines and poisoned general queries;
  `bing` answered English queries with another region's pages ("Berlin
  population" → Chinese forum posts);
- the tool itself pinned two engines and `language="all"`, overriding whatever the
  instance had enabled;
- `docker compose up --build` rebuilt the image, but an old anonymous volume kept
  shadowing `/etc/searxng` — the container served a config from days earlier.
  Every `compose up` in `searxng.bat` now passes `--renew-anon-volumes`.

A query that names Wikipedia is answered by the MediaWiki API directly (with
shorter-prefix retries and title ranking), which is what makes factual questions
work again. Search results now lead with the authoritative page:

| query | first result |
|---|---|
| Nintendo 64 technical specifications | en.wikipedia.org/wiki/Nintendo_64 |
| what is the capital of Portugal | en.wikipedia.org/wiki/Lisbon |
| Berlin population | en.wikipedia.org/wiki/Berlin |
| python asyncio tutorial | realpython.com/async-io-python |

A exhausted web-search budget no longer kills the run: it disables `web_search`
for the rest of that run and the coder continues with its other tools. Near
identical follow-up searches are recognised (token overlap, not exact strings) and
stop after the third attempt.

## Coder briefing limit

The planner's thinking is injected into the coder as a briefing only up to
`duo_coder_briefing_think_tokens` (default 1500 estimated tokens). Above that the
coder gets the pure plan instead — a planner that thinks 8-16k tokens before
writing a plan otherwise buries it. `0` = plan only, `-1` = always inject. The
settings row is "Coder briefing limit" under Thinking, and every run logs
`[CODER-BRIEFING] mode=… think_tokens=… limit=…`.

## Voice (off by default, English only)

- A **Voice** card in the settings: an honest status line (engine / model /
  ready), a Download button with progress for the whisper.cpp + Parakeet
  binaries and models, a 3 s microphone test that shows the transcript in place,
  and model / language / preload / thread controls.
- A **red mic button left of send** in the chat: click to record, click to stop,
  the transcript is appended to your message (never sent automatically), Escape
  cancels.
- Nothing runs until you enable it: no downloads, no processes, no microphone.
  The audio packages are optional (`requirements-voice.txt`), and
  `deploy/fetch_whisper.py` fetches the binaries and models.

## Upgrading

Run `update.bat`, or install the zip over the existing folder — settings, presets
and models are untouched. If you use the bundled SearXNG, run `searxng.bat
install` once so the new engine config and the volume handling apply.

## Known issues

- Voice is English only and has no streaming; the STT path spawns one process per
  request (nothing stays resident unless `stt_preload` is on).
- Linux is suite-verified only; this release was smoke-tested on Windows.
- The SearXNG `wikipedia` engine still returns nothing on this SearXNG build; the
  direct MediaWiki path covers Wikipedia queries instead.

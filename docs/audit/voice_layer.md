# Audit 3 — the newest voice layer (read aloud, new chat, model list, licences)

Scope: everything from the last three commits (`0d4f657`, `db68805`, `1d8ef80`,
`ce15670`) — the read-aloud pump, the new-chat silence, the model-list hint, the
licence plumbing and the latency work. Found five semantic bugs, all fixed.

## 1. The read-aloud pump could wedge permanently (two ways)

`_readAloudNext()` plays a prefetched promise and prefetches the next chunk. Two
paths left it "busy" forever, so **no further speech ever started**:

**1a. Switching TTS off and on again.** `_syncTtsCard`'s off-branch cleared the
queue and the audio element but **not `_raPending`** (the in-flight fetch). The
pump's start condition is `!_readAloudBusy && !_raPending` - with a stale pending
entry, every later call was ignored. Fix: the off-branch calls `_readAloudStop()`,
which clears `_raPending` too.

**1b. Any manual playback.** `voiceSpeakText` (speaker button) and
`voicePlaySample` replace `_ttsAudio`. Killing a playing element does **not** fire
`onended`, and `onended` is what advances the queue - so clicking a speaker while a
read-aloud was running stalled the pump for the rest of the session. Fix: both
entry points call `_readAloudStop()` first (they take over the audio element
deliberately).

## 2. The "short first chunk" promise did not hold for unpunctuated text

The splitter only breaks at `.!?;,`. A pasted URL or a long clause without
punctuation therefore became **one** first chunk of several hundred characters -
the exact slow start the split was meant to remove. Fix: if the first chunk is
still longer than 90 characters, cut it at the last space before 90 and push the
remainder back into the queue.

## 3. The speaker button never appeared during a run

`_syncSpeakButtons()` was only called from `loadSettings` and from the TTS card
sync. During a live run the answer grew, but nothing re-evaluated the button, so it
stayed hidden until the page was reloaded - i.e. the feature looked broken exactly
when it was needed. Fix: a throttled `_speakSyncSoon()` (4/s) runs from the token
flush loop, plus one call when the run ends.

## 4. `settings.py` still defaulted to the non-commercial voice

`tts_engine.DEFAULT_VOICE` is `anna` (CC BY 4.0), but the **settings default** was
still `cosette` and `resolve_voice()` reads the settings key first. A fresh install
would therefore have started on the CC BY-NC recording anyway - the licence fix was
cosmetic. Fix: `"tts_voice": "anna"` in `settings.py`, plus `docs/settings.md`,
`docs/voice_setup.md` and the `tts_engine` docstring (the 1.4.0 release notes now
live only in the GitHub release).
(The owner's own settings pin `cosette`, so nothing changed for them.)

## 5. Latency work: one real regression, caught by the suite

The keep-alive client initially dropped the per-request timeout (the client carried
a fixed 600 s read timeout), so `tts_timeout_s` no longer cut a hanging synthesis.
`test_tts_engine` failed on exactly that. Fix: the timeout is passed per request
while the client stays shared.

## What was already correct (checked, not changed)

- `_raTakeNext()` re-checks `_readAloudWanted(kind)` per item, so a switch flipped
  off while items wait drops exactly those items.
- `_readAloudStop()` clears queue, pending fetch, busy flag and the audio element.
- The chunk cap (600 chars) is applied **before** splitting, so the split can never
  exceed the engine's own cap.
- Prefetch never runs ahead more than one chunk - a 600-character answer generates
  at most ~5 requests, and only one is in flight at a time.
- `voiceEnableDefaults()` writes the switches it flips (visible, reversible), and
  it never overrides a user who already set one.
- Licensing: the engine table is the single source (`/tts/voices` serves licence +
  source), the UI prints it, the README links the table.

## Open, deliberate

1. **The sidecar stays warm for 15 minutes after a page load when TTS is on**
   (pre-warm). That is ~1.3 GB RAM idle. It is the price for "speech starts in a
   second"; lowering `tts_idle_stop_s` is the knob.
2. **Cold start ~12 s** on the first request after the sidecar stopped. Cannot be
   removed without keeping PyTorch resident permanently.
3. **No streaming from the sidecar**: the whole WAV of a chunk is synthesised
   before WSOLA + playback, so ~0.9 s is the floor for the first 60 characters.
   Real streaming would cut that further and is a separate sidecar change.

## Release readiness (1.4.0, without the decision model)

Ready, with the caveats named above and one process point:

| Check | State |
|---|---|
| Version + changelog + notes | `1.4.0` consistent (gate check b) |
| Suite / gate | 143/143, 13/13 |
| Live | running `1.4.0`, `/tts`, `/tts/voices`, `/stt` verified against the real sidecar |
| Zip | built at `df30c24` - **stale**, must be rebuilt from the new HEAD |
| Tag | `v1.4.0` points at `df30c24` - must move to the release commit |
| Licences | documented per voice, default switched to a commercial-safe voice |
| Linux | installer/unit reviewed + `bash -n`, **not** executed on a real host |
| Telegram voice | needs a gateway restart (new bridge code) |

Recommendation: rebuild zip + tag on the final commit, run the gate once more, then
publish. Nothing in the audit blocks the release; items 1-3 above are documented
trade-offs, not defects.

# Voice licensing — what we may use, and what it obliges

Checked 2026-10-17 against the **installed packages** (not from memory) and the
Hugging Face / GitHub metadata. This page exists because one shipped voice is
non-commercial and two models carry attribution obligations.

## Summary

| Piece | License | Obligation |
|---|---|---|
| HiveMind's own voice code (`core/tts_*.py`, `core/stt_*.py`) | the repo's own license | none |
| `pocket-tts` **code** (installed by `uvx`) | **MIT** (`kyutai-labs/pocket-tts`) | keep the copyright notice with substantial copies (we do not vendor it) |
| **Pocket TTS model weights** (`kyutai/pocket-tts`) | **CC BY 4.0** | **attribution**, plus the gated "prohibited use" terms |
| whisper models (`ggerganov/whisper.cpp` → `ggml-*.bin`) | **MIT** (the HF repos for the ggml weights - incl. openai/whisper-* and Systran/faster-whisper-* - are tagged MIT) | notice |
| whisper.cpp + parakeet binaries (`ggml-org/whisper.cpp`) | MIT | notice |
| Parakeet weights (`ggml-org/parakeet-GGUF`) | repo says MIT, upstream `nvidia/parakeet-tdt-0.6b-v3` says **CC BY 4.0** | treat as **CC BY 4.0** (the stricter one) |
| TinyDecide (decision-model candidate) | Apache-2.0 | notice |
| GLiNER2.5-Decide (decision-model candidate) | Apache-2.0 | notice |

## The voices (the part that matters)

Pocket TTS maps a voice name to a recording from `kyutai/tts-voices`. That repo
has **no single license** - each folder has its own. Read out of the installed
`pocket_tts/utils/utils.py`:

| Voice | Source file | License |
|---|---|---|

`kyutai/tts-voices` declares **no single licence** for the collection -
each recording is governed by the dataset it came from, listed per file below.

| `anna` (shipped default) | `vctk/p228_023_enhanced.wav` | CC BY 4.0 |
| `alba` | `alba-mackenna/casual.wav` | CC BY 4.0 |
| `eve` | `vctk/p361_023_enhanced.wav` | CC BY 4.0 |
| `jane` | `vctk/p339_023_enhanced.wav` | CC BY 4.0 |
| `vera` | `vctk/p229_023_enhanced.wav` | CC BY 4.0 |
| `bill_boerst` | `voice-zero/bill_boerst.wav` | **CC0** |
| **`cosette`** | `expresso/ex04-ex02_confused_001_channel1_499s.wav` | **CC BY-NC 4.0 - non-commercial only** |

Consequences:

1. **CC BY 4.0 (VCTK, Alba MacKenna)**: commercial use is allowed, **attribution is
   required**. The Voice card prints the license per voice and a credit line
   (`core.tts_engine.ATTRIBUTION`); the README links here.
2. **`cosette` is non-commercial.** It stays selectable (private use is fine - the
   owner's own settings pin it), but it is **not** the shipped default any more.
   A fresh install starts on `anna` (CC BY 4.0). The dropdown labels it
   `non-commercial`.
3. **Do not add a voice from `expresso/` or `ears/` as a default** - both are
   CC BY-NC.

## The gated model terms

`kyutai/pocket-tts` is a gated repo: the user accepts the conditions on Hugging
Face before the download works. Those terms forbid, among other things, **voice
impersonation or cloning without explicit consent**, deception (presenting
generated audio as a real recording) and unlawful or privacy-invasive content.

What HiveMind does with it: reads *its own answers* aloud in a **fixed, shipped
voice** - no cloning, no voice upload, no impersonation, no watermark removal. The
user's own recordings (the microphone test, Telegram voice notes) are transcribed
and **discarded immediately** - they are not stored, not used as a voice prompt,
and the temp WAV is deleted in a `finally` block.

## Attribution text (used in the UI and the README)

> Voice: Kyutai Pocket TTS (model CC BY 4.0, code MIT) - voice recordings: VCTK /
> Alba MacKenna (CC BY 4.0), Voice-Zero (CC0), Expresso (CC BY-NC 4.0,
> non-commercial)

Speech to text: OpenAI Whisper models (Apache-2.0) and NVIDIA Parakeet
(CC BY 4.0) via whisper.cpp (MIT).

## If you want a zero-obligation setup

- Voice: `bill_boerst` (CC0) - no attribution, no restriction.
- Speech to text: `tiny`/`base`/`small` (Whisper, Apache-2.0) instead of Parakeet.

# Privacy: what can leave the machine

HiveMind makes no hidden calls. Every row below is either opt-in per action or
has an off switch. Everything else stays on disk: settings, chats, memory and
logs live in the install folder. (Short summary in the
[README](../README.md).)

| Destination | Trigger | What is sent | Switch it off |
|---|---|---|---|
| Hugging Face (`huggingface.co`) | you download a model or voice (installer menu, model manager); one-time `hf auth login` for gated models | model/voice name, HF token for gated downloads | download nothing and point the model folder at files you already have |
| PyPI (`pypi.org`) | first use of text to speech: `uv` installs `pocket-tts` + PyTorch | package names, Python/platform | leave TTS off |
| `github.com` release assets | install/update fetches llama.cpp and whisper.cpp binaries | file names | use the release ZIP and place the binaries yourself |
| `api.github.com` | Git publish (only when you use the Git tool) or the startup update check (no personal data) | repo name, commit and token when publishing; current version for the update check | do not publish / remove the token; `HIVEMIND_NO_UPDATE_CHECK=1` |
| your SearXNG → DuckDuckGo/Mojeek | a web search the agent (or you) runs | the search terms | keep SearXNG down; use a tool profile without web search |
| Docker Hub (`searxng/searxng`) | `searxng.bat` pulls the image once | image name | pull it yourself / do not start the container |
| arbitrary URLs | `web_fetch` / `browser` tool calls | the URL and the request | tool profile without web/browser |
| Playwright CDN | browser optional setup/update (`playwright install chromium`) | platform + Chromium version | skip the browser optional |
| Telegram | gateway on: every message, answer, photo, voice note and file | the conversation and its attachments | gateway off (and revoke the bot token) |
| external MCP servers | a tool call on a server you configured | tool name + arguments | remove the server |

## Model downloads and tokens

HiveMind never creates a Hugging Face token. Public models download
anonymously (including the LFS checksum lookup). For gated models such as
Pocket TTS, create a Read token at <https://huggingface.co/settings/tokens>
and either run `hf auth login` (the installer reads the token file it writes)
or set `HF_TOKEN`. Without one, a gated download stops with a login hint
instead of a traceback. Step by step (Windows, Linux/systemd,
background-user pitfalls, 401/403 troubleshooting):
[huggingface_token.md](huggingface_token.md).

## Fully offline

`HIVEMIND_NO_UPDATE_CHECK=1`, gateway off, no MCP servers, SearXNG not
running, a tool profile without web tools, and no model/voice downloads.
Installer/updater network use (PyPI, GitHub binaries, Playwright) happens only
when you run those steps with the matching optionals.

Everything else stays on disk: settings, chats, memory and logs live in the
install folder.

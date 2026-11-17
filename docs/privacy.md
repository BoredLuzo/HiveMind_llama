# Privacy: what can leave the machine

HiveMind makes no hidden calls. Every row below is either opt-in per action or
has an off switch. Everything else stays on disk: settings, chats, memory and
logs live in the install folder. (Short summary in the
[README](../README.md).)

| Destination | Trigger | What is sent | Switch it off |
|---|---|---|---|
| Hugging Face (`huggingface.co`) | you download a model, voice, or optional GLiNER2.5 routing decider (`hf auth login` for gated models) | model/voice/decider name, HF token for gated downloads | download nothing and point `models/` at existing local GGUFs; keep decider off |
| PyPI & PyTorch (`pypi.org`, `download.pytorch.org/whl/cpu`) | installer/updater Python deps, Pocket TTS (`uvx`), or optional GLiNER2.5 decider (`gliner2` + CPU torch) | package names, Python/platform | use existing `.venv`, leave Pocket TTS and GLiNER decider off |
| `github.com` release assets | installer/updater fetches HiveMind release ZIPs (`update.bat`), `llama.cpp`, and `whisper.cpp` binaries | asset/release filenames | place binaries manually / upgrade via local ZIP (`update.bat --zip`) |
| `api.github.com` | startup update check, Git validation/repo-create/publish (`routers/git.py`), or `git` tool | version tag check, repo/commit + token when using Git setup/publish | `HIVEMIND_NO_UPDATE_CHECK=1`; leave Git credentials unset |
| your SearXNG → DuckDuckGo Web / Yahoo (plus direct `*.wikipedia.org/w/api.php` on Wikipedia queries / empty non-`site:` fallback) | web search enabled (`web_search`) | the search query terms | keep SearXNG stopped / `+Websearch` off (disables both SearXNG & Wikipedia fallback) |
| Docker Hub (`searxng/searxng`) | `searxng.bat` builds/pulls the image once | image tag | pull manually / do not start the container |
| arbitrary URLs | `web_fetch` / `browser` tool calls | requested URL + headers | disable `web`/`browser` tools |
| Playwright CDN | installer core sync (`install.bat`, `deploy/install_linux.sh`, `check_optionals.bat`) installs headless Chromium | OS platform + Chromium build | do not invoke `browser` tool at runtime |
| Package registries (`npmjs.org`, `static.rust-lang.org`, `winget`/MS Store) | optional LSP packs (`install_lsp_packs.bat`) or Core Utils (`install.bat` `[4/9]`) | selected package ID | skip optional LSP/Core Utils installer prompts |
| Telegram (`api.telegram.org`) | gateway enabled (`telegram_enabled`) | paired chat messages, voice notes, images, files | keep gateway off / revoke bot token |
| external MCP servers | a tool call on a configured server (`mcp_client_enabled`) | tool name + arguments | leave MCP client off / remove server entries |

## Model downloads and tokens

HiveMind never creates a Hugging Face token. Public models — including
GGUFs, Whisper/Parakeet STT, and the optional **Underdog Woof** TTS
(`ConwayResearch/Underdog-Woof`) — download anonymously without any login.
For gated models such as Pocket TTS (`kyutai/pocket-tts`), create a Read token
at <https://huggingface.co/settings/tokens> and either run `hf auth login`
(the installer reads the token file it writes) or set `HF_TOKEN`. Without one,
a gated download stops with a login hint instead of a traceback. Step by step
(Windows, Linux/systemd, background-user pitfalls, 401/403 troubleshooting):
[huggingface_token.md](huggingface_token.md).

## Fully offline

`HIVEMIND_NO_UPDATE_CHECK=1`, gateway off, no MCP servers, SearXNG not
running, a tool profile without web tools, and no model/voice downloads.
Installer/updater network use (PyPI, GitHub binaries, Playwright) happens only
when you run those steps with the matching optionals.

Everything else stays on disk: settings, chats, memory and logs live in the
install folder.

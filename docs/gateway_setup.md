# HiveMind Telegram Gateway — Setup

Control HiveMind from your phone: you send a message, your PC does the
work, the result comes back as a Telegram message. Setup takes about
fifteen minutes.

What you need before starting:

- HiveMind installed and running (check `http://127.0.0.1:8001/health`
  in a browser — it should show a line of JSON).
- A Telegram account with two-step verification turned on
  (Settings → Devices → Cloud Password). Do this first; the account
  becomes the key to your PC.
- While you're in that menu, remove old sessions you don't recognize.
- The PC stays on as long as you want to use the bot.

## What leaves the machine

The engine itself never contacts a cloud LLM — everything runs on your
hardware. The gateway is different: it is a bridge to Telegram, and a bot's
traffic is **not end-to-end encrypted**. Anything you send to or receive from
the bot passes through Telegram's servers and stays in that chat history.

| What | Where it goes |
|---|---|
| your texts, voice notes, photos and PDFs | Telegram, then to your engine over loopback |
| answers, plans, tool-call summaries and approval cards | engine -> Telegram -> your phone |
| the bot token | stored in **Windows Credential Manager (`keyring`)** or `$env:HIVEMIND_TG_TOKEN` — **never** in `gateway.toml` (rejected by `_FORBIDDEN_KEYS`) |

Nothing is sent to the developer, and no analytics are involved. Keep secrets
out of phone prompts, use a private chat (the gateway answers nothing else),
and remember that `/purge` and the approval gate exist for exactly this
surface.

## 1. Create the bot

1. Open a chat with **@BotFather**, send `/newbot`.
2. Pick a display name (anything).
3. Pick a username — it has to end in `bot`, e.g. `fritz_hivemind_bot`.
4. BotFather replies with a **token**, a long string.

That token is the key to the bot. Whoever has it controls it. Don't
paste it into chats, screenshots, files or mails.

## 2. Put the token in the shell — nowhere else

Open PowerShell and run (you'll be asked for the token, it stays
invisible while typing):

```powershell
$s = Read-Host "Bot token" -AsSecureString
$env:HIVEMIND_TG_TOKEN = [System.Net.NetworkCredential]::new('', $s).Password
```

The token now lives in the memory of this one shell window. Not in the
history, not in a file, not in any log.

**One-step setup (recommended):** run `start_gateway.bat setup` (or answer `Y` at installer step `[9/9]`). It validates your token, saves it invisibly in Windows Credential Manager (`keyring`, already installed in `.venv`), writes `gateway.toml`, sets `telegram_enabled = true`, and syncs `hive_base_url`. Or store via PowerShell into Credential Manager directly (input masked):

```powershell
.venv\Scripts\python.exe -c "import keyring, getpass; keyring.set_password('hivemind_gateway', 'bot_token', getpass.getpass('Bot token: '))"
```

After that, `start_gateway.bat` (or `start_gateway_headless.bat`, or **Settings → Telegram Gateway → Process → Start/Stop** in the UI) picks up the token automatically without env vars. Rotate via `@BotFather` `/revoke` + re-running `start_gateway.bat setup`; delete via Windows Credential Manager (`hivemind_gateway`).

## 3. Start the gateway

`gateway.toml` (checked in repo root, `%LOCALAPPDATA%\HiveMindGateway\gateway.toml`, or `$env:HIVEMIND_GATEWAY_CONFIG`) must enable `telegram_enabled = true` (or set `$env:HIVEMIND_GATEWAY_ENABLED = "1"`).

```powershell
start_gateway.bat            # interactive window (Ctrl+C stops)
start_gateway_headless.bat   # background process (stop via `start_gateway.bat stop` or UI)
```

Only one poller per token can run (a second instance aborts loudly). On first start, the console prints a **pairing code** (valid 5 minutes).

## 4. Pair your phone

1. In Telegram, search for **your own bot** (`@fritz_hivemind_bot`),
   open the chat, press **Start**. (Not the BotFather chat — it doesn't
   know /pair.)
2. Send `/pair <CODE>` — read the code off the console character by
   character.
3. When it answers "Paired.", you're done. The code is used up and
   pairing closes itself.

If the code was wrong or too old, the phone stays silent on purpose —
the console shows the attempt and a counter. Five wrong tries lock
pairing until you restart the gateway (a restart prints a fresh code).
If the console says CONFLICT, something else is polling with your
token; stop that other instance and start again.

## 5. Use it

- Sending a **plain message** starts a run on your PC.
- You get status updates while it works, then the result. Anything
  longer than three messages arrives as a `.txt` file.
- **Chat & control:** `/new` (fresh chat), `/follow <on|off>` (default `on`: follows active PC browser chat for up to 1h; `off` isolates `[TG]`), `/stop`, `/status`, `/verbose`, `/workspace <path>`, `/send <path>`, `/cron` (automations), `/purge`, `/shutdown`.
- **Mode & models:** `/mode <auto|agentic|chat|pipeline|off>`, `/models`, `/setModel <nr>` (applies immediately), `/preset <nr>`, `/ctx <nr|off>`, `/cancel` (clears phone model/ctx overrides), `/planner`, `/decider`.
- **Security & approvals:** `/restrict <on|off>`, `/gate <ask|deny|off>`, `/tools`, `/lock`, `/help`.
- **Single run queue:** If a browser or phone run is already active, the bot tells you instead of queueing.

## Stopping and emergencies

- **Stop the gateway:** Ctrl+C in its shell or `start_gateway.bat stop` in another window (also stops a detached/hidden instance). The installer offers it as the last optional step (after models, decider, language servers, SearXNG and voice).
- **Kill switch:** create a file named `gateway.disabled` in
  `%LOCALAPPDATA%\HiveMindGateway` and the gateway refuses to start or
  carry on, no matter what the phone sends.
- **Rotate the token:** `/revoke` in BotFather, then set the new token
  as in step 2.
- **Unpair:** delete `%LOCALAPPDATA%\HiveMindGateway\gateway_state.json`
  on the PC and restart — you get a fresh pairing code.

---

## Security notes

**Who can talk to it.** Only the paired account, only in a private
chat. Everyone else is ignored without an answer. No groups, no
forwarded messages, no second account.

**How pairing works.** The one-time code exists only on your console.
Five minutes, single use, five wrong tries lock it until a restart.
Nobody pairs from a distance — the PC screen is the only place the code
exists.

**Where the token lives.** In the RAM of the shell you started it from,
optionally the Windows Credential Manager. It is scrubbed from every
log line (including the URLs, which contain it), and the release
packaging refuses zips that carry it. **Honest limit of the Credential
Manager:** it keeps the token out of files, logs and backups — it is
NOT a protection against software running on your PC. Anything under
your Windows user (including this project's own `run_bash`, whose
service name is documented here) can read it without a prompt. That is
the same trust class as every local secret; what keeps the agent
honest is that every shell command it wants to run appears in full in
the approval card or the transcript. If the token ever leaks, rotate
it: `/revoke` in BotFather kills the old one instantly.

**Open ports.** None. The gateway polls Telegram outbound; there is no
webhook and nothing listens. To HiveMind it talks over 127.0.0.1 only.
A second poller on the same token makes it quit loudly instead of
fighting.

**What the agent can reach from the phone.** File access stays workspace-confined
(escapes/traversal are blocked by the engine). By default (`telegram_phone_restricted: true`,
toggle via `/restrict`), phone runs stay restricted to read-only safe tools (`read_file`,
`search_code`, `web_search`, etc.). When you lift restriction (`/restrict off`),
tappable inline approval cards (`✅ Once` / `📁 Always (chat)` / `⛔ Deny`) govern
writes and execution per `/gate <ask|deny|off>` / **Phone approvals** in UI (default
`deny`; mirrored PC runs allow `Once` or `Deny`, never `Always`).

**Manipulated content.** Untrusted repos/web pages stay bounded by workspace
confinement, `/restrict`, and the approval gate.

**Replays and double runs.** Updates older than 60 seconds are dropped,
duplicates recognized, and the position in Telegram's queue is saved
*before* a run starts — a crash can lose a run but never run it twice.

**Off switch.** The kill-switch file on the PC always wins over the
phone. Runs are never restarted on their own; after a crash the bot
asks instead of continuing by itself.

**Known limits, on purpose:**

- Telegram chats are not end-to-end encrypted. Answers, code and .txt
  files sit on Telegram's servers. There is a secret filter before
  sending, but it's a net, not a guarantee.
- Whoever takes over your Telegram account steers the agent. That's why
  two-step verification is a requirement, not a suggestion.
- The pairing lock counts globally: someone who deliberately sends five
  wrong codes can close the window. Annoying, not dangerous — a restart
  fixes it.
- Approval memory is deliberately narrow: "Once" approves that single call,
  and "Always (chat)" remembers the exact call for the current chat only —
  a new command or a new chat asks again. There is no blanket
  "always allow everything" switch.

## Voice notes & media (opt-in)

- **Voice input (`telegram_voice_in` in UI / `settings.json`, or `voice_in = true` in `gateway.toml`):** downloads Telegram voice notes (20 MB / 300 s cap), transcribes via `/stt`, and runs the transcript with the same gates/approvals.
- **Voice answers (`telegram_voice_out` in UI / `settings.json`, or `voice_out = true` in `gateway.toml`):** returns answers as Ogg/Opus voice notes using HiveMind TTS (`anna` default via Pocket TTS or `juno` via Underdog Woof; both disabled until enabled in *Settings → Voice*). Audio codecs (`soundfile`, `numpy`) come with `uv sync --all-extras` (fallback `requirements-voice.txt`).
- **Images & documents:** incoming photos attach to runs (`telegram_image_in` default `true`, or `image_in` in `gateway.toml`); PDFs/text files respect 20 MB / `120,000` chars / `40` pages extraction limits.

Additional **`settings.json`** keys (configured in *Settings → Telegram Gateway* / *Voice* — **do not** place these in `gateway.toml`):

| `settings.json` key | Default | Meaning |
|---|---|---|
| `telegram_result_mode` | `both` | `both` (text + voice note), `text`, or `voice` (falls back to text if TTS fails) |
| `telegram_progress_msgs` | `true` | OFF hides status/tool progress lines (keeps approvals and final answer) |
| `tts_preload_run` | `true` | Pre-warms TTS when a run starts to avoid cold-start delay |

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

**Recommended: store it ONCE instead (Windows Credential Manager).**
The shell variable above dies with the window — for everyday use you
would repeat this before every start. The gateway can instead read the
token from the Windows Credential Manager, which survives restarts and
updates. One-time setup (token stays invisible, nothing is written to
disk or history):

```powershell
pip install keyring
python -c "import keyring, getpass; keyring.set_password('hivemind_gateway', 'bot_token', getpass.getpass('Bot token: '))"
```

After that, no env var is needed: `start_gateway.bat` (or
`python -m hivemind_gateway.main`) picks the token up by itself. The
gateway never prints the token and never accepts one from a file.
Rotating it: BotFather `/revoke`, then repeat the one-liner. Deleting
it: remove the `hivemind_gateway` entry in the Windows Credential
Manager (Systemsteuerung → Anmeldeinformationsverwaltung).

## 3. Start the gateway (same shell)

The gateway is **off by default** — a master switch must be on before it
touches Telegram. Either create a one-line `gateway.toml` next to the
gateway:

```toml
telegram_enabled = true
```

or set the switch for this shell only:

```powershell
$env:HIVEMIND_GATEWAY_ENABLED = "1"
```

Then start:

```powershell
# adjust the path to YOUR HiveMind folder first:
cd C:\Tools\HiveMind
python -m hivemind_gateway.main
```

If it answers with "another gateway instance seems to run", an older
gateway is still alive: close that window (Ctrl+C) or end its PID
(`taskkill /PID <number> /F`), then start again. Only one instance per
bot token is allowed — the second one refuses on purpose.

The console prints a **pairing code** (uppercase letters and digits
only, valid for five minutes). Keep this window open — the window *is*
the gateway. Ctrl+C stops it.

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
- Commands: `/new` (fresh chat) · `/stop` (abort) · `/status` ·
  `/verbose` (show more detail) · `/mode` (run mode for phone runs
  only — auto/chat/pipeline/automap, `/mode off` follows the engine
  settings again) · `/models` (numbered model list) · `/setModel <nr>`
  (pick a model; the bot then asks one line: preset, ctx_thinking,
  ctx_model — preset load is global, ctx applies to duo/agentic phone
  runs) · `/cancel` (abort that flow) · `/help`.
- One run at a time. If something is already running — including from
  the browser — the bot tells you so instead of queueing.
- Bot conversations land as their own `[TG]` chats inside HiveMind and
  don't mix with your browser chats.

## Stopping and emergencies

- **Stop the gateway:** Ctrl+C in its shell.
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

**What the agent can reach from the phone.** File access stays inside
the chat's workspace — paths outside, including `../` tricks, are
rejected by the engine (verified against the real handler). Tools that
change anything (shell commands, file writes, git commits) run behind
the engine's approval gate, and the gateway forces that gate ON for its
own runs and answers every approval request with an automatic deny —
such tools cannot execute from the phone, no matter how the engine's
own approval toggle is set (`/status` states this). Tappable approvals
("1" once / "3" deny from the phone) exist for mirrored PC runs.

**Manipulated content.** The agent reads repos and web pages, and those
can contain text that tries to steer it. Pairing doesn't protect
against that — the workspace boundary does, and the hardened remote
profile coming next goes further: no shell, no push, no deletions from
the phone.

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
- There is no "always allow" button, and there won't be. The gateway
  never loosens a safety rule.

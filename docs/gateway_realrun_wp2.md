# WP2 REALRUNS — Durchführungsplan (6 Läufe)

Stand: `feature/telegram-gateway` nach WP2-Commit. Live läuft
(127.0.0.1:8001). State-Dir jungfräulich. Reihenfolge und Erwartungen pro
Brief (WP2-Pflichtläufe 1–4) plus die beiden Zusatzläufe 5–6 aus der
Owner-Runde. Der Agent wertet danach HiveMind-Log und Gateway-Konsole aus
und zitiert die Zeilen im Report.

## VORBEREITUNG (einmal, PC)

1. Token-Shell öffnen (PowerShell) und Token setzen — NICHT in den Chat,
   nicht in die History:

   ```powershell
   $s = Read-Host "Bot token" -AsSecureString
   $env:HIVEMIND_TG_TOKEN = [System.Net.NetworkCredential]::new('', $s).Password
   ```

2. Gateway aus DIESER Shell starten:

   ```powershell
   cd C:\Users\NtheP\Desktop\HiveMind\repo_gateway
   python -m hivemind_gateway.main
   ```

3. Auf der Konsole erscheint der **Pairing-Code** (gültig 5 Min).
4. Handy: Bot anstellen, `/pair <code>` senden. Erwartet: „Paired."
5. Scratch-Workspace für die Läufe vorbereiten (damit die Probe-Läufe
   nicht im echten Projekt landen — der `[TG]`-Chat erbt sonst den
   last-workspace):

   ```powershell
   mkdir C:\Users\NtheP\Desktop\HiveMind\live\ws_tg_probe
   ```

   Dann dem `[TG]`-Chat diesen Workspace setzen (der Gateway überschreibt
   das Feld nicht — sein PUT trägt nur messages):

   ```
   PUT http://127.0.0.1:8001/chats/<TG_CHAT_ID>
   {"workspace": "C:/Users/NtheP/Desktop/HiveMind/live/ws_tg_probe", "base_rev": <aktuelle rev>}
   ```

   Die `<TG_CHAT_ID>` steht nach Lauf 1 in der Gateway-Konsole bzw. in
   GET /chats (Titel beginnt mit „[TG]").

## LAUF 1 — Textlauf vom Handy

| Schritt | Aktion |
|---|---|
| 1.1 | Handy: `Neue Datei tg_probe1.txt mit dem Inhalt "lauf eins" anlegen` senden |
| 1.2 | Warten auf Status-Nachricht (editiert sich) und Endergebnis |
| 1.3 | PC: `GET /chats` → `[TG]`-Chat im Browser öffnen |

**Erwartet:** Endergebnis am Handy; im `[TG]`-Chat der UI stehen beide
Turns (user + assistant) und sind inhaltlich identisch mit dem, was das
Handy zeigte; der Chat lädt in der UI sauber. `ws_tg_probe/tg_probe1.txt`
existiert.

## LAUF 2 — /stop mittendrin

| Schritt | Aktion |
|---|---|
| 2.1 | Handy: `Schreibe ein sehr langes Gedicht (mindestens 500 Wörter)` senden |
| 2.2 | Während der Status tickt: `/stop` senden |
| 2.3 | PC: `nvidia-smi` |

**Erwartet:** sichtbare Abbruch-Bestätigung; wenige Sekunden danach kein
Modell-Prozess mehr in nvidia-smi (kein VRAM-Halten); danach startet
Lauf 2b (`Sag nur: ok`) normal.

## LAUF 3 — Gateway-Neustart mit laufendem Run

| Schritt | Aktion |
|---|---|
| 3.1 | Handy: langes-Gedicht-Prompt erneut senden |
| 3.2 | Während der Lauf tickt: Gateway-Konsole mit **Strg+C** beenden |
| 3.3 | Gateway neu starten (gleiche Token-Shell, gleicher Befehl) |
| 3.4 | Erwartung prüfen, dann **3.5**: Task sofort hart killen (`taskkill /F /IM python.exe` — Achtung: killt alle Python; besser PID der Gateway-Konsole) und erneut starten |
| 3.6 | Handy: `/stop` senden (Aufräumen des Orphans) |

**Erwartet:** KEIN neuer Pairing-Code („owner already bound" auf der
Konsole — State liegt in %LOCALAPPDATA%\HiveMindGateway); am Handy kommt
die Orphan-Warnung mit der run_id und dem /stop-Hinweis; kein automatischer
Neustart des Laufs; nach 3.6 bestätigt /stop den Abbruch. Bei 3.2 (Strg+C)
kann der Brick desselben Laufs auch direkt fragen — beide Kill-Arten
enden imselben Recovery-Pfad.

## LAUF 4 — VRAM-Block

| Schritt | Aktion |
|---|---|
| 4.1 | PC: zweites Modell laden (z. B. über die UI/llama-Verwaltung), bis nvidia-smi voll ist |
| 4.2 | Handy: normalen Lauf senden |
| 4.3 | PC: zweites Modell entladen |
| 4.4 | Handy: denselben Lauf erneut senden |

**Erwartet:** 4.2 liefert die **lesbare** Meldung („Modell konnte nicht
geladen werden (VRAM blockiert?)…") statt Funkstille; 4.4 läuft normal
durch.

## LAUF 5 — Approval gegen den echten Core (wichtigster Zusatz)

Vorbereitung am PC: in den HiveMind-Einstellungen
`duo_action_approval_enabled` ANstellen (Gate aktiv). Wichtig: Default
`duo_action_approval_timeout_s = 0` lassen — dann gibt es bewusst KEINEN
Server-Timer und der Gateway-Deny ist die einzige Instanz.

| Schritt | Aktion |
|---|---|
| 5.1 | Handy: `Lege die Datei tg_probe5.txt im Workspace an (Inhalt: "approval test")` senden |
| 5.2 | Auf die Freigabe-Anfrage des Agents warten |

**Erwartet:** Handy zeigt „🔒 Freigabe-Anfrage automatisch abgelehnt";
die Datei `ws_tg_probe/tg_probe5.txt` **existiert nicht** danach; der
Lauf endet mit einem sauberen Ergebnis (Agent wählte einen anderen Weg
oder brach ab). Log-Seite: `/approval/decide/{run_id}` mit `"3"` wird im
HiveMind-Log sichtbar. Danach Gate wieder AUSstellen.

## LAUF 6 — Gleichzeitigkeit (Heuristik-Realtest)

| Schritt | Aktion |
|---|---|
| 6.1 | PC: Lauf im Browser/UI starten (langsames Prompt) |
| 6.2 | Handy: währenddessen normalen Text senden |
| 6.3 | Erwartung notieren; dann umgekehrt: Handy-Lauf starten, parallel UI-Lauf |

**Erwartet:** Die zweite Seite bekommt die Busy-Ablehnung („Es läuft
bereits ein Lauf…") und es kommt zu KEINER zweiten gleichzeitigen
Generierung (8 GB VRAM). Wo die Heuristik greift/fehlt, wird im Report
festgehalten — der serverseitige 409-Guard (WP3) ist die eigentliche
Lösung dafür.

## NACH DEN LÄUFEN

- Agent wertet aus: Gateway-Konsolen-Log, `live/logs/hivemind.log`
  ([HISTORY-SEED], [RUN-TRACE], approval-Zeilen), State-Datei, nvidia-smi
  -Beobachtungen — Zitate im WP2-Report (Format wie gehabt: git show
  --stat, grep-Belege, NOT tested, Open questions).
- Scratch-Workspace kann danach gelöscht werden.
- Danach: Owner-Go für WP3 (Core: Remote-Profil, 409-Guard, api_version).

## RELEASE-NOTES-BACKLOG (WP6 sammelt)

- Pairing-Lock ist GLOBAL nach 5 Fehlversuchen bis zum Neustart (nur
  `/pair` zählt); Neustart = frischer Code. Kein Leak, aber ein Fremder
  kann das Fenster zumachen.
- Bot-Chats sind NICHT Ende-zu-Ende-verschlüsselt: Antworten, Code und
  .txt-Dokumente laufen über Telegram-Server (Secret-Filter ist aktiv,
  aber kein Ersatz).
- Sprachumschaltung der Bot-Texte (derzeit Deutsch) → Backlog 1.3.1.

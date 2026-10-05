# HiveMind Telegram-Gateway — Einrichtung in 15 Minuten

So steuerst du HiveMind von deinem Handy: Nachricht senden, dein PC
arbeitet, Ergebnis kommt zurück. Der Guide ist für die eigene
Einrichtung geschrieben — Sicherheitsfragen stehen unten im Audit.

---

## Voraussetzungen

- HiveMind ist installiert und läuft (Test: `http://127.0.0.1:8001/health`
  im Browser zeigt JSON).
- Du hast ein Telegram-Konto **mit Zwei-Schritt-Verifizierung**
  (Einstellungen → Geräte → Cloud-Passwort setzen). Ja vorher, das Konto
  ist hinterher der Schlüssel zu deinem PC.
- Räume alte Telegram-Sitzungen aus (gleicher Menüpunkt).
- Der PC bleibt an, solange du den Bot nutzen willst.

## Schritt 1 — Bot bei Telegram erstellen

1. In Telegram nach **@BotFather** suchen, Chat öffnen, `/newbot` senden.
2. Anzeigename wählen (beliebig).
3. Username wählen — **muss auf `bot` enden**, z. B. `fritz_hivemind_bot`.
4. BotFather antwortet mit einem **Token** (lange Zeichenkette).

> Der Token ist der Schlüssel zum Bot: Wer ihn hat, kontrolliert ihn.
> Niemals in Chats, Screenshots, Dateien oder E-Mails.

## Schritt 2 — Token NUR in die Shell

PowerShell öffnen und eingeben (der Token wird abgefragt, unsichtbar
getippt):

```powershell
$s = Read-Host "Bot token" -AsSecureString
$env:HIVEMIND_TG_TOKEN = [System.Net.NetworkCredential]::new('', $s).Password
```

Der Token lebt damit nur im Speicher dieser einen Shell — nicht in der
History, nicht in Dateien, nicht in Logs.

## Schritt 3 — Gateway starten (gleiche Shell!)

```powershell
cd <dein-hivemind-ordner>
python -m hivemind_gateway.main
```

Die Konsole zeigt einen **Pairing-Code** (nur Großbuchstaben A–Z und
Ziffern 2–7, 5 Minuten gültig). Dieses Fenster offen lassen — es IST der
Gateway. Beenden mit Strg+C.

## Schritt 4 — Koppeln

1. In Telegram deinen **eigenen Bot** suchen (`@fritz_hivemind_bot`) →
   Chat öffnen → **Start** drücken.
   *(Nicht den BotFather-Chat — der kennt kein /pair.)*
2. Senden: `/pair <CODE>` — den Code **Zeichen für Zeichen** von der
   Konsole ablesen.
3. Kommt „**Paired.**" — fertig. Der Code ist verbraucht, das Pairing
   ist damit dauerhaft zu.

**Fehlerfälle:** Bei falschem/abgelaufenem Code kommt bewusst **keine
Antwort am Handy** — die Konsole zeigt den Versuch mit Zähler. Nach
5 Fehlversuchen sperrt das Pairing bis zum Neustart (Neustart = frischer
Code). Bei „CONFLICT" auf der Konsole läuft etwas anderes mit deinem
Token — andere Instanz stoppen, Gateway neu starten.

## Schritt 5 — Loslegen

- Einfach **Text senden** = ein Lauf auf deinem PC.
- Status kommt als Live-Update, danach das Ergebnis; sehr lange
  Ergebnisse als `.txt`-Datei.
- Befehle: `/new` (frischer Chat) · `/stop` (Abbruch) · `/status` ·
  `/verbose` (mehr Details) · `/help`.
- Ein Lauf gleichzeitig — ist einer aktiv (auch aus dem Browser),
  bekommst du eine Busy-Meldung statt Warteschlange.
- Die Bot-Unterhaltungen liegen als eigene `[TG]`-Chats in HiveMind und
  stören deine Browser-Chats nicht.

## Aufräumen & Notfall

- **Beenden:** Strg+C in der Gateway-Shell.
- **Ganz abschalten (Kill-Switch):** Datei `gateway.disabled` im Ordner
  `%LOCALAPPDATA%\HiveMindGateway` anlegen — der Gateway startet dann
  nicht mehr, egal was das Handy will.
- **Token rotieren:** In BotFather `/revoke` → alten Token ungültig
  machen → neuen Token wie in Schritt 2 setzen.
- **Kopplung lösen:** Am PC `%LOCALAPPDATA%\HiveMindGateway\gateway_state.json`
  löschen → Gateway starten → neuer Pairing-Code.

---

## Security-Audit (Stand 1.3-preview, ehrlich)

**Wer darf reden?** Nur der eine gekoppelte Account, nur im privaten
Chat. Alle anderen Absender werden ohne Antwort verworfen (nur Log).
Gruppen, Weiterleitungen und Fremd-Accounts sind draußen.

**Wie sicher ist die Kopplung?** Der Einmalcode existiert NUR auf deiner
PC-Konsole, gilt 5 Minuten, genau einmal, wird in Konstantzeit
verglichen, 5 Fehlversuche = Sperre bis Neustart. Der PC ist der
Trust-Anker: Niemand kann sich aus der Ferne koppeln — wer den Code
nicht am Bildschirm lesen kann, kommt nicht rein.

**Wo liegt der Token?** Nur im RAM der Start-Shell (optional:
Windows-Credential-Manager). Er taucht in keinen Logs auf (alle
Ausgaben werden geschwärzt — inklusive der Token-URLs, im Realtest
bestätigt), nicht im Repo, nicht im Release-Zip (Leak-Check verweigert
solche Pakete).

**Welche Ports?** Keine. Der Gateway ruft Telegram aktiv ab
(Long-Polling), es gibt keinen Webhook und keinen offenen Port. Zur
HiveMind-Engine spricht er nur über 127.0.0.1. Erkennt er einen
zweiten Poller auf seinem Token, beendet er sich lautstark statt zu
kämpfen.

**Was darf der Agent vom Handy aus?** Dateizugriff ist auf den
Workspace des Chats beschränkt — Zugriffe außerhalb (auch mit `../`-
Tricks) werden vom Core abgelehnt (gegen den echten Code geprüft).
Werkzeuge, die eine Freigabe verlangen (Befehle ausführen, Dateien
schreiben, Git-Commits), werden aktuell **automatisch abgelehnt** —
Freigabe-Buttons am Handy kommen mit WP4. Bis dahin gilt: den Bot nur
gegen einen Arbeitsbereich ohne echte Daten richten.

**Was ist mit manipulierten Inhalten (Prompt-Injection)?** Der Agent
liest Repo- und Web-Inhalte; dort kann Text stehen, der ihn steuern
will. Die Kopplung schützt nicht dagegen — die Grenzen des
Arbeitsbereichs und (mit WP3) das verschärfte Fernsteuerungsprofil tun
das: kein Shell-Befehl, kein Git-Push, keine Löschungen vom Handy.

**Replay/Doppel-Ausführung?** Nachrichten älter als 60 Sekunden werden
verworfen, Duplikate erkannt, die Position in der Telegram-Warteschlange
wird gespeichert, BEVOR ein Lauf startet — ein Absturz kann einen Lauf
höchstens verlieren, nie doppelt ausführen.

**Notbremse?** Die Kill-Switch-Datei am PC gewinnt immer gegen das
Handy. Kein Lauf wird automatisch neu gestartet; nach einem Absturz
fragt der Bot nach, statt von sich aus weiterzumachen.

**Bekannte Grenzen (bewusst offen):**

- Telegram-Chats sind **nicht Ende-zu-Ende-verschlüsselt** — Antworten,
  Code und `.txt`-Dateien liegen auf Telegram-Servern. Der Secret-Filter
  vor dem Senden ist ein Riegel, kein Freibrief.
- Wer dein **Telegram-Konto** übernimmt, steuert deinen Agent. Deshalb
  2FA-Pflicht und Sitzungs-Hygiene.
- Die Pairing-Sperre zählt global: Ein Fremder, der absichtlich 5× ein
  falsches `/pair` sendet, macht das Fenster zu (nervig, nicht
  gefährlich — Neustart behebt es).
- „Immer erlauben"-Buttons gibt es absichtlich nicht — der Gateway
  lockert nie eine Sicherheitsregel.

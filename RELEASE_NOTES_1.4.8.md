# Release 1.4.8

Umfang: v1.4.7..HEAD (16 Commits). Der Fokus: die Sprachkette ueber
Telegram (Audio rein -> Transkript -> Decider -> Run) und
Betriebssicherheit.

## Voice-Kette ueber Telegram (ende-zu-ende live verifiziert)
- Sprachnote -> Transkript -> Decider -> Run: live durchlaufen
  ("What to do tomorrow in stuttgart?" wurde sauber transcribiert, Run
  gestartet, Web-Suche lief, Antwort als 30-s-Voice-Note geliefert).
- from_voice-Flag gefixt (3203145): der 1.4.6-Voice-Decider war toter
  Code (FrozenInstanceError auf dem frozen ParsedUpdate wurde
  verschluckt) - Sprachnoten liefen immer als settings[mode]. Jetzt:
  mode auto + duo_agentic_mode false, der Decider routet.
- STT: to_wav16k lief mit tmp_dir=None in Path(None) - jede Voicenote
  500te vor dem Engine-Audio-Decode (3d0b025). Default: %TEMP%.

## Decider (Suite-getestet, RAM-Freigabe neu)
- /decider Telegram-Kommando (bare = Status, on|off|shadow). OFF
  entlaedt das Encoder-Modell (~1,8 GB RAM) SOFORT via den neuen
  Settings-Hook - nicht erst beim naechsten Engine-Restart (6784226).
- DecisionCache persistiert ueber Engine-Restarts (0367cee) - Routing
  ab Sekunde 1 konsistent, 7-Tage-TTL, best-effort IO.

## Sicherheit (Suite-getestet)
- External-Bind-Guard: HIVEMIND_HOST auf 0.0.0.0/LAN erzwingt den
  HIVEMIND_INTERNAL_TOKEN fuer nicht-lokale Clients (a3eb4a2). Der
  Default-Bind bleibt 127.0.0.1.

## Updater / Installer
- Prune-Logik nach deploy/update_prune.ps1 extrahiert (der Inline-
  PowerShell zerbrach im cmd-Quoting - Prune UND Manifest-Rewrite
  waren still tot; live betroffen am 22.10.) (47358ec).
- Installer: Models-Schritt skipbar, Decider-Umgebung (gliner2 + torch
  CPU + transformers + Modell) als Option - vorher war nur das Gewicht
  drin und der Decider fiel stumm auf Rules zurueck (bdd61d9).

## Fixes (live verifiziert / Suite)
- Frontend: parallele write_file-Calls teilen sich keinen Pending-Tab
  mehr (der "haengt in generation"-Fall), tote Calls entfernt
  (c7912c4, 05044f4).
- Gateway: /status zeigt aktive UI-Runs (c4bf796); stille
  Process-Toedeungen werden vom Watchdog geloggt (live/watchdog_engine.bat).

## Ehrlichkeit
- "Live verifiziert" oben = echtes Audio, echter Run auf dieser Box.
  Alles unter "Suite-getestet" ist offline geprueft (174/174), kein
  GPU-/LLM-Lasttest. Keine Performance-Aussagen.

## Bekannte Grenzen
- parakeet-q4k ist englisch-fokussiert; whisper-small (de) ist als
  STT-Modell waehlbar, tiny/base sind installierbar.
- Der Gateway-Main-Prozess kann still sterben (Ursache offen); der
  Supervisor startet ihn neu, Nachrichten im Todesfenster sind
  verworfen.

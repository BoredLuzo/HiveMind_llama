# TODO — Wartbarkeit, Release-Härtung, Wettbewerbsfähigkeit

**Stand:** 2026-11-07 · Branch `dev` @ `7ce41e6` + Batch 10
**Legende:** P0 = blockiert Release · P1 = als Nächstes · P2 = geplant · P3 = strategisch
**Typ:** BUG / DESIGN / DEBT / TEST / RELEASE / COMPETE

## A. Design-Lücken aus dem Live-Test (alle offen)

- [ ] **A1 · P1 · DESIGN (zurückgestellt: Direct-Feature später) — Pause auf Direct-Runs ist wirkungslos.**
  `/pause/{run_id}` setzt `request_pause_after_chunk`, geprüft wird das Flag nur im
  Duo-Chunk-Loop (`core/duo_runner.py:2957`). Ein Direct-Stream pausiert nicht.
  *Akzeptanz:* Direct-Runner prüft an einer sicheren Token-/Rundengrenze und hält an;
  `/pause-state/{chat_id}` wird aktiv; Resume setzt fort. Alternativ UI ehrlich
  beschriften („Pause nur für Duo/Chunk-Runs“) und Endpoint 409 liefern.
  *Evidenz:* Live-Matrix 2026-11-07, `/pause` → `pause_requested`, danach
  `/pause-state` `{"active":false}`, `/resume` 409.

- [ ] **A2 · P1 · DESIGN (zurückgestellt: Direct-Feature später) — Steer-Injection nur an Tool-/Chunk-Grenzen.**
  Queue (`/api/run/{run_id}/steer`) funktioniert; ein reiner Direct-Run ohne Tools
  injiziert den Text nicht mitten in der Antwort, sondern erst nach Run-Ende
  (Frontend-Queue).
  *Akzeptanz:* definierter Direct-Boundary (z. B. alle N Tokens/runde) ODER UI sagt
  klar „wird nach dem Lauf übernommen“. Test für Queue→Injection→Modellantwort.
  *Evidenz:* `core/tool_loop.py:553`, `core/duo_runner.py:3604`; Direct-Runner hat
  keinen Drain.

- [ ] **A3 · P1 · DESIGN/QUALITY — Auto + Agentic hijackt einfache Tool-Prompts.**
  Mit `duo_agentic_mode=true` geht „List files“ in den Code-Duo-Pfad und endet nach
  Listen-/Loop-Wiederholungen in `loop_detected` statt im Tool-Agent.
  *Akzeptanz:* Tool-Erkennung vor dem Agentic-Hijack; einfache Listen-/Read-Prompts
  landen im Direct-Tools-/Tool-Agent-Pfad, Code-Duo bleibt für echte Coding-Tasks.
  *Evidenz:* Live-Matrix, 745 Events, `[LOOP-DETECT-STOP] explore_only=3`.

- [ ] **A4 · P1 · DEBT/QUALITY — Legacy-Tool-Route erreicht nur Max-Runden.**
  Nach dem `ResponseNotRead`-Fix (`c5f686e`) läuft die Route, aber der 2b-Agent
  wiederholt Tool-Calls und endet mit `max_tool_rounds` statt einer Antwort.
  *Akzeptanz:* Round-Limit-/Prompt-Parität mit dem Direct-Tools-Pfad; ein einfacher
  Read-Task endet mit `completed` und einer Textantwort. Duplikat-Logik gegen
  `tool_loop.py` bereinigen (C7-Hinweis aus dem Bericht).
  *Evidenz:* Live-Retest 2026-11-07, `done stop_reason=max_tool_rounds`.

- [ ] **A5 · P2 · DESIGN — Doppelter Auto-Answer bei `ask_user_auto_proceed`.**
  Karten-Timer (JS, 30 s) und Engine-Timeout (30 s) können gleichzeitig antworten.
  Aktuell harmlos (spätes `set_user_answer` ist No-op/409), aber zwei Quellen.
  *Akzeptanz:* eine Quelle (Engine) oder Karten-Timer sendet kein Auto-Answer mehr,
  wenn das Setting aktiv ist; Race-Test.
  *Evidenz:* `static/app.js` Karten-Timer, `tools/runner.py` Governor-Timeout.

- [ ] **A6 · P2 · TEST — UI-Verdrahtung von `ask_user_auto_proceed` nur per API geprüft.**
  Checkbox unter dem Action-Approval-Gate ist im Browser nicht durchgeklickt worden.
  *Akzeptanz:* Browser-Test (Playwright): Toggle → `settings.json` → Reload → Toggle
  bleibt; Karten-Timer und Engine-Timeout sichtbar konsistent.

- [ ] **A7 · P3 · DESIGN — `coverage_setup` schlägt unter Coverage-Messung fehl.**
  Der Check „coverage not in sys.modules“ ist im gemessenen Prozess by design falsch.
  *Akzeptanz:* Suite erkennt `COVERAGE_PROCESS_START` und überspringt genau diesen
  Check (Suiten-Ergebnis 321/321 auch gemessen).

## B. Wartbarkeit (offen)

- [ ] **B1 · P1 · DEBT — C4/C5-Extraktion: gemessen und verworfen.**
  C4 (269 Z.) berührt **60+ Loop-State-Variablen**: neben den 32 Runner-Inputs
  auch run-persistente Compression-Zähler (`_MAX_COMPRESSIONS`,
  `_compress_fail_streak`, `_ctx_compressions`, `_ctx_critical_warned`,
  `_ctx_evictions`, `_force_compress_next`, `_last_force_compress_round`,
  `_swa_triggered_compress`, …) sowie `_dtool_msgs`, `_est_tokens`,
  `_guard_tokens`. Eine saubere Extraktion erzeugt genau das verbotene
  60-Feld-`RoundState`-Objekt; der erste Versuch wurde vollständig
  zurückgerollt (kein Code verändert). C5 (230 Z., 3 Loop-Crossings) hat
  dieselbe Klasse.
  *Entscheidung:* **nicht als Ganzes extrahieren.** Stattdessen nur kleine,
  kohäsive Sub-Blöcke mit ≤5 Outputs ziehen (Muster W4a:
  `duo_loop_guards`) oder den Loop bewusst als Monolith belassen.
  *Akzeptanz (falls später):* Characterization-Test vor jeder Bewegung;
  kein State-Objekt >15 Felder; `duo_runner` darf nicht durch Interface-Code
  wachsen.

- [x] **B2 · P1 · DEBT — `backend/manager_load.py` aus Pyright-TARGETS ausgelassen.**
  55 Cross-Mixin-Attributfehler; ein TYPE_CHECKING-Kontrakt wie in
  `manager_prefetch.py` fehlt.
  *Erledigt 2026-11-07:* TYPE_CHECKING-Kontrakt wie `manager_prefetch.py`,
  55 → 0 Fehler, in TARGETS, Baseline 1/1 (nur `run_code_duo`-Komplexität).

- [ ] **B3 · P2 · DEBT — Source-String-Pins → Verhaltens-Tests.**
  195/333 Suiten nutzen den Shared Harness; viele Pins greppen weiter Quelltext.
  *Akzeptanz:* pro angefasster Suite mindestens ein Verhaltens-Test; Pins nur für
  Architektur-Invarianten, die sich nicht verhalten testen lassen.
  *Fortschritt 2026-11-07:* Pin `j` in `test_split_audit_fixes` ist jetzt ein
  echter `_phase_moe`-Probe (Cache-MiB als int, Slot-Bool-Marker, Flags im cmd,
  Cache-aus-Pfad), kein Quelltext-Grep mehr. Die neuen Helper-Suiten
  (`pipeline_runner`, `chat_run`) sind vollständig Verhalten.

- [ ] **B4 · P2 · TEST — Restliche Harness-Migration.** (195 von 333 Suiten migriert; Sonderformate offen)
  Suiten mit Sonderformat (`=== Results:`, `passed=N failed=M`, Skip-Summary,
  `<5 Checks`) sind noch nicht migriert.
  *Akzeptanz:* Harness um diese Formate erweitert oder bewusst als Ausnahmen
  dokumentiert; keine Verhaltensänderung.

- [x] **B5 · P2 · DEBT — Tote UI-/Settings-Altlasten.**
  `chat_autosave_enabled` (nur clientseitig), `duo_pair` (nur UI); entscheiden:
  verdrahten oder entfernen (Defaults, app.js, Docs).
  *Erledigt 2026-11-07:* beide sind **keine** toten Keys, sondern bewusst
  clientseitig: `chat_autosave_enabled` steuert, ob die UI den Chat vor
  `/stream` bzw. nach Edits persistiert (`static/app.js:6852/7012/10864`),
  `duo_pair` merkt sich die Paar-Auswahl, während
  `agents.duo_coder.model` die Engine-Seite ist. `chat_autosave_enabled` ist
  jetzt mit Default `True` in `settings_defaults.py` und damit in
  `docs/settings.md` dokumentiert; kein Engine-Reader nötig.

- [ ] **B6 · P2 · DEBT — Pyright-Rest: 1 Fehler (`run_code_duo` complexity).**
  Verschwindet mit B1; danach Baseline 0 anstreben.
  *Akzeptanz:* `deploy/pyright_baseline.py` → 0/0.

- [ ] **B7 · P3 · DEBT — Ruff-Regelsatz erweitern.** (F401: 537 → 179; noch nicht aktiviert)
  Aktuell nur E9/F821/F601/F811/F841/W605; `target-version` ist 3.14.
  *Fortschritt 2026-11-07:* 358 ungenutzte Importe entfernt (54 Dateien),
  6 begründete `# noqa: F401`-Keeps, ruff/test grün. F401 bleibt aus, bis
  Fassaden (`llama_server_manager`, `manager_load`, `__init__`) per
  `__all__`/per-file-ignores explizit sind und die Test-Residuen bereinigt sind.

- [ ] **B8 · P2 · TEST — Coverage 46,12 % gezielt heben.**
  Schwächste Kernmodule: `core/pipeline_runner.py` 2,5 %, `duo_compress_cycle` 4,1 %,
  `chat_run` 10,3 %, `llama_start_phases` 11,1 %, `_pre_explore` 11,6 %,
  `manager_load` 18,9 %.
  *Akzeptanz:* pro Modul mindestens ein Happy-Path- und ein Fehlerpfad-Test;
  Gesamt-Coverage >60 % ohne reine String-Pins.
  *Fortschritt 2026-11-07:* `project_state` (40), `background` (31), `log_noise` (33),
  `keep_awake` (27), `testmap` (26), `manager_health` (46), `compression` (58),
  `chat_route_decision` (25) mit neuen Verhaltens-Suiten abgedeckt. Batch 11:
  `pipeline_runner`-Helfer (17 Checks), `chat_run`-Helfer (14),
  `llama_start_phases._phase_claim` (+13) und der MoE-Phase-Probe (+4).
  Coverage-Neuvermessung steht noch aus.

- [x] **B9 · P3 · TEST — Testlaufzeit und Flake-Quarantäne.**
  Voller Lauf ~8–12 min (6 Worker); `--changed` existiert, aber kein Impact-Mapping
  und keine Flake-Historie.
  *Erledigt 2026-11-07:* der Runner schreibt pro Lauf Pass/Fail/Dauer nach
  `tests/.regression_history.json` (git-ignoriert, letzte 30 Läufe),
  `--flake-report [N]` zeigt Flips, Pass-Rate und Median-Dauer,
  `tests/quarantine.json` ist die geprüfte Quarantäne-Liste (`[QUAR]`,
  `--include-quarantined`, `--no-history`). Kein Retry: ein Flake bleibt
  sichtbar. Zwei Over-Selection-Bugs in `--changed` gefunden und gefixt:
  Doku-Dateien wurden vor der Auswahl nicht entfernt, und `tests/<fixture>`
  erzeugte das Muster `tests` (253/331 Suiten). Gemischte 7-Datei-Änderung:
  331 → 14 Suiten in 2,3 s; `core/tool_loop.py` allein → 17 Suiten in 38 s
  (33 s davon `mcp_client`), also unter der 60-s-Zielmarke.
  19 Mechanik-Checks in `tests/test_regression_runner_b9.py`.

- [x] **B10 · P3 · DEBT — Abhängigkeiten härten.**
  `requirements.txt` nutzt `>=`; `uv.lock` ist die exakte Quelle.
  *Erledigt 2026-11-07:* beide CI-Workflows prüfen `uv lock --check`
  (`astral-sh/setup-uv@v5`); der Updater nutzt bereits `uv sync --frozen`.
  `requirements.txt` bleibt Installer-Fallback.

## C. Release-/Update-Härtung (offen)

- [x] **C1 · P1 · RELEASE — Release-ZIP aus `HEAD` bauen und frisch installieren.**
  Bisher nur Quell-/Clean-Checkout getestet; kein `package_release.bat`-Lauf und
  keine Installation aus dem erzeugten ZIP.
  *Erledigt 2026-11-07:* ZIP aus isoliertem Worktree (`git archive HEAD`),
  368 Entries, ein Root, keine Leaks (settings/presets/models/tests/sessions),
  BUILD_INFO=c4efbac. Frische `uv sync --frozen`-Installation: `/health` ok,
  35 Modelle, Direct-Chat completed, `read_file`-Tool, UI 200. Einziger Befund:
  korrupter 0-Byte-Eintrag im lokalen uv-Cache für pydantic-core; `uv cache
  clean pydantic-core` + Reinstall behebt es (nicht im ZIP).

- [x] **C2 · P1 · RELEASE — Update-Migration 1.5.5 → nächste Version testen.**
  `server.py`-Kompatibilitätsanker ist gepinnt, aber der **alte** 1.5.5-`update.bat`
  wurde noch nicht gegen ein ZIP aus dem aktuellen Tree ausgeführt.
  *Erledigt 2026-11-07:* Original-1.5.5-Installation + altes `update.bat --zip`
  gegen das neue ZIP: Backup `update_backup_1.5.5`, Apply, Verify grün, neue
  Module (`duo_coder_preloop`, `duo_task_complete_gate`, `chat_tools_route`,
  `duo_loop_guards`) vorhanden, `update.bat` auf neuen Shim geswapst,
  BUILD_INFO/Manifest korrekt. Der `server.py`-Anker hat die alte Probe getragen.

- [ ] **C3 · P1 · RELEASE — Erster `windows-full`-Cron-Lauf (Montag) beobachten.**
  Python 3.14, `--ci-subset --jobs 2`, Exklusiv-Lane, Timeout 60.
  *Akzeptanz:* grüner Lauf oder dokumentierte Fixture-Nachbesserung; keine
  WinError-10053-Flakes.

- [ ] **C4 · P2 · RELEASE — Versions-Anker beim Bump automatisieren.**
  Der Text-Anker in `server.py` muss mit `core/version.py` synchron bleiben (Pin h2).
  *Akzeptanz:* Release-/Bump-Skript aktualisiert beide oder ein Test schlägt bei
  Drift früh fehl (aktuell nur Test, kein Bump-Hook).

- [ ] **C5 · P2 · TEST — Externe Integrationsmatrix.**
  Telegram-Gateway (Token/Owner), echte STT/TTS-Audioverarbeitung, echte
  Vision-Inferenz mit Bild, SearXNG, konfigurierte MCP-Server, `send_file`.
  *Akzeptanz:* je Integration ein dokumentierter Smoke-Test mit Credentials/
  Fixtures; Status heute: **nicht getestet**, nicht „grün“.

## D. Wettbewerbsfähigkeit (offen)

- [ ] **D1 · P2 · COMPETE — Benchmark-Runner: relativer Zuwachs statt absoluter Quote.**
  (Empfehlung aus dem Review mit Sonnet, 2026-11-07)
  Keine Outcome-Messung für Coding-Qualität (nur Pins/Units). Wichtig ist
  **nicht** eine hohe absolute Quote, sondern der relative Zuwachs gegenüber
  der Baseline: In RExBench scheitern Aider, Claude Code und OpenHands mit
  neun Modellen an den meisten Aufgaben; auch mit Hinweisen bleibt der beste
  Wert unter 40 %. WebGen-Bench: beste Kombination 27,8 %. Absolute Zahlen
  taugen daher nicht als Ziel.
  *Setup:*
  - **Gleiches Modell, gleicher Server.** Ein real genutztes Modell
    (`qwen3.5:4b-mtp`, `qwen3.6:35b-a3b-ud`); alle Bedingungen laufen über
    dasselbe llama-server-Setup, damit Quantisierung und Sampling nicht den
    Unterschied machen.
  - **Vier Bedingungen:** (a) Modell pur (ein Prompt, keine Tools),
    (b) HiveMind Direct + Full-Tools, (c) HiveMind Code-Duo agentic,
    (d) Aider mit demselben Modell als OpenAI-kompatiblem Backend.
  - **Aufgaben mit automatischer Prüfung:** 20–40 kleine Tasks mit
    versteckten Tests (grün/rot), naheliegender gemeinsamer Maßstab sind
    Aiders öffentliche Benchmark-Aufgaben (genaue Menge ungeprüft), plus
    5–10 Tasks aus eigenen Repos, wo „richtig“ bekannt ist.
  - **Wiederholungen:** 3–5 Läufe pro Aufgabe und Bedingung, Pass-Rate
    statt Einzelergebnis (lokale Modelle streuen stark). Zusätzlich Laufzeit
    und Tokenverbrauch messen — ein Harness, das doppelt so lange braucht,
    muss sich das durch bessere Ergebnisse verdienen.
  - **HiveMind-Vorteil:** OpenAI-kompatibler Endpoint im
    Agent-Tool-Loop-Modus; der Runner spricht HiveMind wie die anderen
    Bedingungen an.
  *Auswertung:* Delta gegen Baseline (Modell pur) je Task-Kategorie; ein
  schlechtes Ergebnis ist nützlich, weil es zeigt, welche Teile (Planner,
  Chunking) kosten statt nützen.
  *Akzeptanz:* `bench/`-Runner + Task-Format + Result-JSON, reproduzierbarer
  Lauf, Wochenvergleich; Report mit Pass-Rate, Delta, Laufzeit und Tokens je
  Bedingung. **Kein absolutes Quoten-Ziel.**

- [ ] **D2 · P2 · COMPETE — Kontext-Engine.**
  Tree-Scout/Static-Map + FTS vorhanden; inkrementeller Index, Retrieval und
  Budget pro Task fehlen.
  *Akzeptanz:* weniger Tokens pro Task bei gleicher/greater Eval-Success-Rate.

- [ ] **D3 · P2 · COMPETE — Edit-/Diff-Engine.**
  `edit_file`/Fuzzy und `write_file` funktionieren; patch-natives Editieren und
  Multi-File-Refactor mit Abhängigkeitsreihenfolge fehlen.
  *Akzeptanz:* Multi-File-Task in der Eval-Suite ohne Full-File-Rewrite-Churn.

- [ ] **D4 · P3 · COMPETE — Verifikations-Tiefe.**
  `run_tests` + Testmap + Auto-Verify vorhanden; selektive Coverage-/Impact-
  Auswahl und Best-of-N/Draft-Verify fehlen.
  *Akzeptanz:* weniger unnötige Testläufe, höhere Verified-Rate in der Eval-Suite.

- [ ] **D5 · P3 · COMPETE — Sandbox für `run_bash`.**
  Workspace-/Destructive-Guards vorhanden, aber keine OS-Isolation.
  *Akzeptanz:* optionaler Container/Job-Object-Sandbox-Modus mit dokumentierten
  Grenzen; Default bleibt lokal.

- [ ] **D6 · P3 · COMPETE — Outcome-basiertes Routing.**
  Judge/AutoMap routen statisch; Ergebnisse (Erfolg/Kosten pro Task-Typ) fließen
  nicht zurück.
  *Akzeptanz:* Routing-Hinweise aus abgeschlossenen Runs, messbar in der Eval-Suite.

- [ ] **D7 · P3 · COMPETE — Diff-Review/Undo-Tree in der UI.**
  Git-Checkpoints existieren; komfortables Review/Revert pro Run fehlt.
  *Akzeptanz:* Run-Diff sichtbar, ein Klick auf Checkpoint-Restore.

- [ ] **D8 · P3 · COMPETE — Benchmark gegen Claude Code / Codex / Aider.**
  Ohne Vergleich keine „competitive“-Aussage.
  *Akzeptanz:* dieselbe Task-Suite wie D1, dokumentierte Success-/Kosten-/
  Latenzwerte; D1 (relativer Zuwachs) ist die Pflicht, D8 die Kür.

## E. Bereits erledigt (Referenz)

- W1: Commit, CHANGELOG, Ruff 3.14, Update-Kompatibilitätsanker (`server.py`).
- W2: `--changed`-Runner, Exception-Drift, `ask_user_auto_proceed` verdrahtet.
- W3: Pyright 191→1, Shared Harness, Freshness-Bug, `retryable`-Bug.
- W4: `duo_loop_guards.py`, Guard-C-Verhaltens-Test.
- W5: Coverage-Setup, Harness-Batch 2, `uv.lock`.
- W6: Harness-Batch 3 (63 Suiten), Pyright +17 Module, Coverage-Wrapper-Fix,
  `numpy`/`tzdata`-Lücken.
- Live-Matrix 2026-11-07: API/CRUD/Tools/Modes/Control; Tool-Route-Bug
  `ResponseNotRead` gefixt (`c5f686e`), 322/322 lokal grün.
- Batch 8/9 (2026-11-07): 358 ungenutzte Importe entfernt, Harness auf
  193/331 Suiten, vier Coverage-Suiten (`testmap`, `manager_health`,
  `compression`, `chat_route_decision`), `uv lock --check` in beiden
  CI-Workflows.
- Batch 10 (2026-11-07): B5 dokumentiert (UI-only-Keys), B9 Flake-Historie/
  Quarantäne/`--flake-report`, zwei `--changed`-Over-Selection-Bugs gefixt,
  D1-Benchmark-Plan (relativer Zuwachs) aufgenommen.
- Batch 11 (2026-11-07): B8-Verhaltens-Suiten für `pipeline_runner`- und
  `chat_run`-Helfer, `_phase_claim` (+13) und MoE-Phase (+4; ersetzt den
  Quelltext-Pin `j`), 333/333 Suiten grün.

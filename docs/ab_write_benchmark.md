# A/B Write-System Benchmark (alt vs. neu)

Zweck: messen, ob das neue Write-System (Phase 1, Commits `41dbfb6`+`e14f64f`
+ Folgefixes) schwache Modelle (Sharp-MiniCPM5-2B) messbar besser performt
als der alte Tier/Marker-Stand. Ein einzelner sauberer 6950-Char-Write bei
temp 1.0 ist Rauschen, kein Beleg — und die Truncation-Rate im Alltag
entscheidet, ob Phase 2 (adaptive Hints) überhaupt nötig ist.

## Aufbau

| Seite | Stand | Port | Ort |
|---|---|---|---|
| ALT  | `393d1ae` (v1.2.3-Tag, vor den 9 Feature-Commits) | 8003 | `..\HiveMind_dev_ab_baseline` (Worktree) |
| NEU  | aktueller Dev-Clone-Stand | 8002 | `..\HiveMind_dev` (Clone) |

Beide Seiten: gleiche `models_dir` (<models_dir>), gleiches Coder-Modell
(`minicpm5:2b-sharp`), gleiche Sampling-Settings. **Frischer Workspace pro
Run** — der 00:24-Run hat ein unvollständiges Part-1-Fragment hinterlassen;
alte Workspaces verfälschen die Zählung.

### Setup ALT-Seite (Stand 2026-09-30: ERFLEDIGT)

- Worktree `..\HiveMind_dev_ab_baseline` auf `393d1ae` angelegt
- `settings.json` (Port 8003) + `models.json` hineinkopiert
- Junction `llama` → Clone-`llama` (kein 2-GB-Copy; alternativ env
  `HIVEMIND_LLAMA_BIN`)
- Clone-`settings.json` steht für die Benchmark-Phase auf **8002**
  (Live-Kopie bleibt unberührt auf 8001; danach Clone zurück auf 8001)
- Boot-Smoke-Test: `/health` → `{"status":"ok","version":"1.2.3"}` nach 3 s

Start je Seite (getrennt, nie gleichzeitig — gemeinsamer VRAM):

```bat
:: ALT:  cd <ab-worktree>
::         <clone>\.venv\Scripts\python.exe run.py
:: NEU:  cd <clone>
::         .venv\Scripts\python.exe run.py
```

Für den deterministischen Harness-Vergleich ohne Modell wird der Server
NICHT gebraucht: `.venv\Scripts\python.exe tests\replay_write_paths.py`
läuft dieselben Fixtures durch ALT und NEU (manuelles Tool, nicht Teil der
Unit-Suite). Stand 2026-09-30: NEU schreibt die Sharp-Klasse komplett (ALT:
Split bei ~5000 + Marker-Zwang); ALT re-splittet sogar salvagierte
Truncations; ALT schreibt Repetitions-Loops vollständig in die Datei, NEU
trimmt sie (Schwelle: ≥ 6 identische Zeilen).

## Feste Tasks (jeweils 5 Runs pro Seite, gleicher Prompt-Wortlaut)

1. **Neuer File-Write ~250 Zeilen:** „Schreibe src/pong.py — ein vollständiges
   Pong-Spiel mit pygame: Titelbildschirm, Punktestand, Ball-Beschleunigung,
   Game-Over-Screen. Eine Datei, keine Teilung."
2. **Daten-dichter Write >8k Chars:** „Schreibe data/movies.json mit 40
   Film-Einträgen (title, year, genre, director, cast, rating, synopsis mit
   2-3 Sätzen)."
3. **Mehr-File-Feature:** „Erstelle ein CLI-Tool src/notes.py + src/storage.py
   + README.md: Notizen anlegen/listen/suchen, JSON-Datei als Speicher."
4. **Edit-Kontrolle (sollte neutral sein):** „Füge in src/storage.py eine
   export_md()-Funktion hinzu" (nach Task 3 im selben Workspace — prüft, ob
   das neue System edit_file unverändert lässt).

## Metriken (pro Seite über alle Runs aggregieren)

```bash
grep -c "LOOP-DETECT-STOP"      logs/hivemind.log   # harness-verursachte Abbrüche
grep -c "WRITE-TRUNCATION"      logs/hivemind.log   # Truncation-Events (nur NEU)
grep -c "WRITE-SALVAGE"         logs/hivemind.log   # Salvage-Recoveries (nur NEU)
grep -c "AUTO_SPLIT_PENDING"    logs/hivemind.log   # Marker-Fehler (nur ALT)
grep    "AUTO-STOP"             logs/hivemind.log   # tool_errors=... pro Run
grep -c "REPEATED ERROR"        logs/hivemind.log   # Retry-Serien
```

Gemessen wird **Harness-Verhalten, nicht Task-Erfolg** (der rauscht bei
temp 1.0): Abbrüche, Retries, Duplikate — plus **Zeit bis zur fertigen
Datei** (erste bis letzte Write-Event-Zeile im Log; bei lokaler Inferenz
die Größe, die man beim Arbeiten spürt).

NEU schreibt zusätzlich jeden malformeden Write-Call roh in
`logs/write_truncations.jsonl` (letzte 20 Vorfälle; `finish_reason` trennt
echte max_tokens-Cuts von sonstigem JSON-Brech) — der Korpus für das
nächste Replay, dann mit echtem Modell-Output statt synthetischen Fixtures.

Manuell pro Run: „Datei vollständig?" (Zeilenzahl vs. Task), „doppelter
Inhalt?" (`sort | uniq -d` auf Zeilen, bzw. verdächtige Wiederholungs-Blöcke),
„läuft es?" (Task 1/3: python -m py_compile + Start).

## Entscheidung

- Truncation-Rate auf NEU niedrig (< ~5 % der Runs mit WRITE-TRUNCATION)
  und ALT klar mehr Abbrüche/Duplikate → **Phase 2 zählen lassen, adaptive
  Hints erst bauen, wenn die Daten es verlangen.**
- NEU trunciert häufig UND salvagt sauber → adaptiver Hint nach dem
  Messprinzip: Truncation = Messung, nächster Hint ≈ 80 % der angekommenen
  Chars, nur nach unten, nur im Speicher, nur via Tool-Result.

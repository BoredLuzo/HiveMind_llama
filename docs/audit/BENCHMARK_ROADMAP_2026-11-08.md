# Benchmark-Roadmap — HiveMind vs. Raw-Modell vs. andere Harnesses

**Stand:** 2026-11-08 · Branch `dev` @ `1864488` + F1-Fix
**Ziel:** Nicht eine absolute Quote, sondern der **relative Zuwachs** gegenüber
dem rohen Modell — und danach der ehrliche Vergleich mit Aider/Claude
Code/Codex auf derselben Task-Suite.

## 1. Was schon steht

- `bench/runner.py` mit vier Bedingungen:
  `raw` (ein Prompt, Diff-Anwendung), `hivemind_direct` (Full-Tools),
  `hivemind_duo` (agentic), `aider`.
- 21 Starter-Tasks (9 Bugfix, 5 Feature, 3 Refactor, 4 Multi-File), jede
  rot-vor-Fix/grün-nach-Referenzfix verifiziert.
- Versteckte Tests (`tests/` im Task-Ordner) werden erst beim Check
  einkopiert und überschreiben gleichnamige Agent-Dateien — nicht
  manipulierbar.
- Ergebnis-JSON mit Pass-Rate, Delta vs. `raw`, Median-Dauer und Tokens;
  Schreiben nach jeder Zelle (ein Crash verliert keine Stunden).
- `bench/README.md` beschreibt Task-Format und Grenzen.

## 2. Phasen

### Phase 0 — Preconditions (einmalig, ~30 min)
- [ ] Frische/isolierte HiveMind-Instanz mit eigener `settings.json`, kein
      Produktions-Workspace, keine Secrets im Environment.
- [ ] Ein Modell für alle Bedingungen festlegen (`qwen3.5:4b-mtp` als
      schneller Start; `qwen3.6:35b-a3b-ud` als Qualitätslauf).
- [ ] llama-server-Port und HiveMind-Port notieren (`--llama`, `--hivemind`).
- [x] Aider installieren und `aider --version` prüfen (0.86.2 in
      `C:\Users\NtheP\Desktop\HiveMind_bench_tools\aider`, per
      `--aider-bin`/`HIVEMIND_AIDER_BIN` angeben; nicht im PATH).
- [ ] Task-Korpus auf 5–10 echte Repo-Tasks erweitern (siehe §4).

### Phase 1 — Harness-Smoke (~15–30 min) — **bestanden 2026-11-08**
Zwei Tasks, zwei Bedingungen, ein Lauf:
```bat
python bench/runner.py --tasks bench/tasks --conditions raw,hivemind_direct ^
  --runs 1 --model qwen3.5:4b-mtp ^
  --llama http://127.0.0.1:8080 --hivemind http://127.0.0.1:8001
```
**Gate:** `raw` darf patchen (Diff wird angewendet), `hivemind_direct` postet
`mode=simple` + `direct_tools_tier=full`, das Ergebnis-JSON enthält beide
Bedingungen. Wenn nicht: Harness fixen, nicht Modelle.
**Ergebnis:** `docs/audit/BENCHMARK_SMOKE_2026-11-08.md` — Direct 2/2,
Duo 1/1, Raw 0/2 (ungültige Diffs). Zwei Messfehler (Modus, Tokens) und die
Modell-Fairness wurden dabei gefixt.

### Phase 2 — Raw-Baseline kalibrieren (1 Abend)
- **Format-Entscheidung (2026-11-08):** `raw_files` (Full-File) ist die
  primäre No-Tool-Baseline (3 Läufe); `raw` (Diff) läuft einmal als strikte
  Gegenprobe. Im Smoke: `raw` 0/2, `raw_files` 2/2 bei denselben Tasks —
  das 0 % war ein Syntax-, kein Verständnisproblem. Getrennt ausweisen.
- 21 Tasks × `raw_files` × 3 Läufe = 63 Zellen (+ 21 Diff-Zellen).
- Zweck: Rauschen messen, Task-Ausreißer finden, eine belastbare
  Baseline-Pass-Rate je Task.
- **Gate:** Kein Task darf im Check flaken (3/3 gleiches Ergebnis oder
  dokumentierte Streuung); Tasks mit Check-Flakiness werden repariert oder
  entfernt.

### Phase 3 — HiveMind-Bedingungen (1 Nacht)
- `hivemind_direct` und `hivemind_duo`, je 3 Läufe = 126 Zellen.
- Messen: Pass-Rate, Delta vs. `raw`, Median-Dauer, Tokens.
- **Gate:** `hivemind_direct` > `raw` auf dem Gesamtkorpus, sonst zuerst
  A3/A4-/Prompt-Ursachen suchen.
- Duo nur weiterverfolgen, wenn es Direct bei Coding-Tasks schlägt und die
  Mehrkosten (Zeit/Tokens) rechtfertigt.

### Phase 4 — Aider-Baseline (1 Nacht)
- `aider` mit demselben Modell und llama-server, 3 Läufe = 63 Zellen.
- Aider-Flags: `--yes --no-git --no-auto-commits --message`.
- **Gate:** Vergleichbarkeit dokumentieren (Aider sieht dieselben sichtbaren
  Dateien, aber seine Prompt-/Edit-Strategie ist anders).

### Phase 5 — Fremde Harnesses (D8, danach)
- Claude Code / Codex: nur mit ihren Standardmodellen sinnvoll; das ist
  **kein** Same-Model-Vergleich. Getrennt ausweisen:
  - „Same model“: raw / HiveMind direct / HiveMind duo / Aider.
  - „Reference“: Claude Code, Codex mit ihren Default-Modellen.
- Keine Firmen-API-Keys in die lokale Instanz geben; Läufe in einem
  separaten, leeren Workspace.
- **Gate:** Nur Aussagen treffen, die die Fairness-Caveats nennen.

### Phase 6 — Auswertung und Wochenvergleich
- Report je Bedingung: Pass-Rate, Delta, Median-Dauer, Tokens, pro Kategorie.
- Statistik: 3–5 Läufe, Wilson-Intervall für Pass-Raten, gepaarter
  McNemar-Test pro Task (gleiche Tasks, gleiche Bedingung).
- Mindest-Effektgröße vorher festlegen (z. B. +10 Prozentpunkte), sonst ist
  ein Delta innerhalb des Rauschens kein Sieg.
- Wöchentlich wiederholen; Trend statt Einzelwert.

## 3. Zeit- und Ressourcenbudget

| Phase | Zellen | 4B (grob) | 35B (grob) |
|---|---:|---|---|
| Smoke | 4 | 15–30 min | 1–2 h |
| Raw 3× | 63 | 2–4 h | 8–16 h |
| Direct+Duo 3× | 126 | 4–8 h | 16–32 h |
| Aider 3× | 63 | 2–4 h | 8–16 h |
| **Gesamt** | **256** | **~1 Arbeitstag** | **mehrere Tage** |

Empfehlung: **4B zuerst**, 35B nur für den Wochen-Report. Läufe nachts, ein
Modell gleichzeitig (ein VRAM-Slot).

## 4. Task-Korpus erweitern (vor Phase 2)

5–10 Tasks aus echten Repos, Kriterien:
- Kleiner, klar umrissener Bugfix/Feature (1–3 Dateien).
- Deterministischer Check ohne Netzwerk/Zeit/Zufall.
- „Richtig“ ist bekannt (Referenz-Commit oder Maintainer-Antwort).
- Keine Secrets, keine großen Fixtures, Laufzeit < 60 s.
- Lizenz erlaubt die lokale Nutzung; Task nicht aus dem Modell-Trainingsset
  ableiten (Kontamination vermeiden).

Format wie `bench/tasks/<id>/`: `task.json`, sichtbares `repo/`, verstecktes
`tests/`. Referenz-Fix separat dokumentieren, nicht in `repo/` legen.

## 5. Risiken und Gegenmaßnahmen

| Risiko | Gegenmaßnahme |
|---|---|
| Lokale Modelle streuen stark | 3–5 Läufe, Pass-Rate, Wilson-Intervall |
| Agent editiert versteckte Tests | Tests werden erst beim Check einkopiert und überschrieben |
| Workspace kontaminiert | frischer Copy pro Zelle, getrennte `runN/cond`-Ordner |
| Server-/VRAM-Zustand verfälscht | ein Modell gleichzeitig, Health-Check vor jedem Lauf |
| Aider-Flags/Prompts nicht vergleichbar | Same-Model-Caveat im Report, feste Flags |
| Absolute Zahlen wirken schlecht | Delta vs. Raw als Headline, RExBench/WebGen als Kontext |
| Ein Crash verliert den Lauf | JSON wird nach jeder Zelle atomar geschrieben |

## 6. Go/No-Go

- **Go für Phase 2**, wenn Phase 1 grün ist und mindestens 20 Tasks einen
  stabilen Check haben.
- **Go für Phase 3**, wenn die Raw-Baseline dokumentiert und das Rauschen
  bekannt ist.
- **Kein „besser als X“-Satz**, solange nicht dieselbe Task-Suite, dasselbe
  Modell (Same-Model-Teil) und mehrere Läufe vorliegen.

## 7. Deliverables

- `bench/results/run-<timestamp>.json` je Lauf (nicht eingecheckt).
- `docs/audit/BENCHMARK_REPORT_<datum>.md` mit Tabelle, Delta, Caveats.
- Ein Wochen-Trend (Pass-Rate/Delta je Bedingung), keine Einzelwerte.

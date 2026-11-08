# Benchmark-Smoke 2026-11-08 (Phase 1)

**Ziel:** Nur den Harness end-to-end validieren, keine Aussage über Qualität.
**Umgebung:** dev-Tree `repo_gateway` (Commit `7456842` + lokale Usage-Zählung),
Engine auf `127.0.0.1:8001`, llama-server b12001 Vulkan auf `127.0.0.1:8080`,
Modell `qwen3.5:4b-mtp` (Q4_K_M). Ein Lauf pro Zelle.

## Ergebnisse

| Bedingung | Tasks | Pass | Dauer | Tokens | Bemerkung |
|---|---|---:|---:|---:|---|
| `raw` | 2 | **0/2** | 5,5 s / 12,9 s | 334 / 781 | Modell lieferte korrupten bzw. nicht anwendbaren Diff; Harness/Patch/Check liefen korrekt |
| `raw_files` | 2 | **2/2** | 3,5 s / 6,3 s | 680 | Full-File-Format (`### FILE:`) statt Diff — beide Fixes korrekt angewendet |
| `hivemind_direct` | 2 | **2/2** | 25,5 s / 20,3 s | 15.559 / 21.273 | `mode=simple`, Full-Tools, versteckte Tests grün |
| `hivemind_duo` | 1 | **1/1** | 52,0 s | 68.478 | agentic Code-Duo, versteckter Test grün |
| `aider` | 1 | **1/1** | 10,4 s | 833 | Aider 0.86.2 aus separatem venv (`HiveMind_bench_tools/aider`), `--aider-bin` gesetzt |

Roh-JSON: `%TEMP%\hm_bench_smoke_raw.json`, `..._hm2.json`, `..._duo.json`
(nicht eingecheckt).

## Was der Smoke gefunden und gefixt hat

1. **Falscher Modus:** `hivemind_direct` postete `mode="chat"`, das die Engine
   nicht kennt → Judge-Pfad. Jetzt `mode="simple"`.
2. **Modell-Fairness:** Die HiveMind-Bedingungen nutzten ihre Settings-Modelle
   (1,7B direct / 35B duo), nicht `--model`. Jetzt erzwingt der Request
   `direct_model` bzw. `duo_coder/critic/planner_model` = `--model`.
3. **Token-Metrik:** Tool-Läufe meldeten fast 0 Tokens, weil `ToolLoop` die
   `usage`-Blöcke verwarf. Jetzt summiert `ToolLoopState` Prompt/Completion/
   Cached über alle Runden, `direct_runner` emittiert `usage_meta`, der
   Benchmark summiert diese Events (done-Event nur als Fallback).
   Gegenprobe: derselbe Direct-Smoke meldet danach 36.832 statt 104 Tokens.
4. **Hidden Tests** sind manipulationssicher (Verzeichnis-Shadowing wird
   ersetzt), Timeouts werden pro Task respektiert, Ergebnisse werden nach
   jeder Zelle atomar geschrieben.
5. **Raw-Format:** `raw` (Diff) ist als strikte Baseline 0/2, `raw_files`
   (Full-File) löst dieselben Tasks 2/2. Die 0 % waren also ein
   Formatproblem, kein fehlendes Task-Verständnis. Beide bleiben getrennt.

## Offene Punkte vor Phase 2

- **Raw-Format entschieden:** `raw_files` wird die **primäre** No-Tool-Baseline
  (3 Läufe), `raw` (Diff) läuft einmal als strikte Gegenprobe. Beide werden im
  Report getrennt ausgewiesen; `raw_files` ist bewusst kein Werkzeug-Agent,
  sondern ein Ein-Prompt-Modell mit Full-File-Ausgabe.
- **Aider installiert** (0.86.2, eigenes venv unter
  `C:\Users\NtheP\Desktop\HiveMind_bench_tools\aider`, nicht im PATH):
  `--aider-bin`/`HIVEMIND_AIDER_BIN` setzen. Der Smoke lief 1/1 (10,4 s,
  833 Tokens); Aider braucht die Workspace-Dateien als Chat-Argumente
  (macht der Runner jetzt automatisch).
- **5–10 echte Repo-Tasks** ergänzen (Roadmap §4), sonst messen die 21
  synthetischen Tasks vor allem Prompt-Glück.
- **Duo-Kosten:** 68k Tokens für eine Ein-Zeilen-Aufgabe; Duo muss seinen
  Mehrwert bei echten Coding-Tasks zeigen, nicht bei trivialen.

## Verdikt

Phase 1 ist **bestanden**: Alle fünf Bedingungen haben den kompletten Pfad
(HTTP/SSE → Agent → Workspace → versteckter Test → JSON) durchlaufen; Direct,
Duo, Aider und Raw-Files haben die Tasks gelöst, Raw-Diff ist ehrlich
gescheitert. Die zwei gefundenen Messfehler (Modus, Tokens) hätten die
D1-Zahlen sonst still verfälscht.

**Nächster Schritt:** Entscheidung zum Raw-Format, Aider installieren,
dann Phase 2 (Raw-Baseline 3×) gemäß `BENCHMARK_ROADMAP_2026-11-08.md`.

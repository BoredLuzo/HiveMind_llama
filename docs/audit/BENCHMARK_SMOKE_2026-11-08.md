# Benchmark-Smoke 2026-11-08 (Phase 1)

**Ziel:** Nur den Harness end-to-end validieren, keine Aussage über Qualität.
**Umgebung:** dev-Tree `repo_gateway` (Commit `7456842` + lokale Usage-Zählung),
Engine auf `127.0.0.1:8001`, llama-server b12001 Vulkan auf `127.0.0.1:8080`,
Modell `qwen3.5:4b-mtp` (Q4_K_M). Ein Lauf pro Zelle.

## Ergebnisse

| Bedingung | Tasks | Pass | Dauer | Tokens | Bemerkung |
|---|---|---:|---:|---:|---|
| `raw` | 2 | **0/2** | 5,5 s / 12,9 s | 334 / 781 | Modell lieferte korrupten bzw. nicht anwendbaren Diff; Harness/Patch/Check liefen korrekt |
| `hivemind_direct` | 2 | **2/2** | 25,5 s / 20,3 s | 15.559 / 21.273 | `mode=simple`, Full-Tools, versteckte Tests grün |
| `hivemind_duo` | 1 | **1/1** | 52,0 s | 68.478 | agentic Code-Duo, versteckter Test grün |
| `aider` | — | — | — | — | nicht installiert; Runner meldet das ehrlich als Fehlzelle |

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

## Offene Punkte vor Phase 2

- **`raw` = 0/2** ist als Baseline legitim, macht den relativen Zuwachs aber
  degeneriert. Vor der Kalibrierung entscheiden:
  (a) Diff-Format beibehalten und 0 % akzeptieren, oder
  (b) dem Raw-Modell ein einfacheres Full-File-Format (`### FILE: path`)
  geben, damit die Baseline nicht nur an der Diff-Syntax scheitert.
  Empfehlung: (b) testen, aber getrennt ausweisen.
- **Aider installieren** (`pip install aider-chat` oder offizieller Installer)
  und Phase 4 vorbereiten.
- **5–10 echte Repo-Tasks** ergänzen (Roadmap §4), sonst messen die 21
  synthetischen Tasks vor allem Prompt-Glück.
- **Duo-Kosten:** 68k Tokens für eine Ein-Zeilen-Aufgabe; Duo muss seinen
  Mehrwert bei echten Coding-Tasks zeigen, nicht bei trivialen.

## Verdikt

Phase 1 ist **bestanden**: Alle drei lauffähigen Bedingungen haben den
kompletten Pfad (HTTP/SSE → Agent → Workspace → versteckter Test → JSON)
durchlaufen; Direct und Duo haben die Tasks gelöst, Raw ist ehrlich
gescheitert. Die zwei gefundenen Messfehler (Modus, Tokens) hätten die
D1-Zahlen sonst still verfälscht.

**Nächster Schritt:** Entscheidung zum Raw-Format, Aider installieren,
dann Phase 2 (Raw-Baseline 3×) gemäß `BENCHMARK_ROADMAP_2026-11-08.md`.

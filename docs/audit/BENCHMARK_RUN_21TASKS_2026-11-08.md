# Benchmark-Lauf 2026-11-08 — 21 Tasks × 3 Läufe

**Frage:** Ist HiveMind Direct auf dem Starter-Korpus besser als Aider — und
rechtfertigt der Tool-Loop seine Token-Kosten?

## Design

- **21 Tasks**, 3 Läufe pro Task/Bedingung = **63 Zellen je Bedingung**,
  252 Zellen gesamt.
- Ein Modell für alle Bedingungen: `qwen3.5:4b-mtp` (Q4_K_M, Vulkan,
  llama-server b12001).
- `raw` (Diff), `raw_files` (Full-File) und `aider` liefen gegen denselben
  llama-server; `hivemind_direct` im separaten HiveMind-Block (eigener Slot).
- HiveMind-Settings pro Request gepinnt: `until_finished=true`,
  `direct_tools_tier=full`; Aider 0.86.2 mit `--yes-always --no-git
  --no-auto-commits`, Workspace-Dateien im Chat.
- Statistik: zellweise Pass-Rate mit 95-%-Wilson-Intervall; gepaarter
  McNemar-Test auf Task-Ebene (Majority aus 3 Läufen).

## Ergebnis

| Bedingung | Zellen | Pass | 95-%-CI | Median | Tokens/Zelle |
|---|---:|---:|---|---:|---:|
| `raw` (Diff) | 63 | **0 %** | [0, 6] | 7,2 s | 670 |
| `raw_files` | 63 | **71,4 %** | [59, 81] | 5,4 s | 419 |
| **`aider`** | 63 | **90,5 %** | [81, 96] | **12,0 s** | **946** |
| **`hivemind_direct`** | 63 | **92,1 %** | [83, 97] | 19,0 s | **21.228** |

### Gepaarte Vergleiche (21 Tasks, Majority)

| Paar | nur A | nur B | McNemar p |
|---|---:|---:|---:|
| aider vs. hivemind_direct | 1 | 2 | **1,00** |
| aider vs. raw_files | 4 | 0 | 0,125 |
| hivemind_direct vs. raw_files | 5 | 0 | 0,0625 |
| raw vs. raw_files | 0 | 15 | 0,0001 |

**Kernaussage:** Zwischen Aider und HiveMind Direct ist auf diesem Korpus
**kein Qualitätsunterschied messbar** (1 vs. 2 abweichende Tasks, p = 1,0).
Beide liegen ~20 Prozentpunkte über der No-Tool-Baseline; der McNemar-Test
ist bei 21 Tasks für diesen Abstand noch nicht signifikant (p = 0,06/0,13),
aber die Richtung ist eindeutig und der Diff-Baseline-Effekt ist es auch.

### Nach Kategorie

| Bedingung | Bugfix (9) | Feature (5) | Multi-File (4) | Refactor (3) |
|---|---:|---:|---:|---:|
| raw_files | 0,89 | 0,60 | 0,50 | 0,67 |
| aider | 1,00 | 0,60 | 1,00 | 1,00 |
| hivemind_direct | 1,00 | **1,00** | 0,75 | 1,00 |

- **Direct** gewinnt die Feature-Tasks (`feature_parse_bool`,
  `feature_word_freq`: je 2/3, Aider 0/3).
- **Aider** gewinnt die Multi-File-Tasks (`multi_config`: 3/3 vs. 1/3).
- Bugfix und Refactor: beide 100 %.
- Die Zellen pro Kategorie sind klein (3–9 Tasks); das sind Hinweise, keine
  belastbaren Kategorie-Rankings.

### Kosten

Direct braucht bei gleicher Pass-Rate **~22× so viele Tokens** wie Aider
(21.228 vs. 946 pro Zelle) und ~1,6× die Zeit (19,0 vs. 12,0 s). Auf diesen
kleinen Tasks ist der Tool-Loop reiner Overhead; er müsste sich bei
größeren/mehrdeutigen Tasks durch bessere Exploration und Verifikation
rechnen.

## Grenzen

- 21 synthetische Tasks, ein 4B-Modell; keine Aussage über 35B oder echte
  Repos.
- 3 Läufe pro Zelle reichen für große Effekte, nicht für 5–10-Punkte-Unterschiede.
- Aider sieht dieselben Dateien, arbeitet aber mit einer anderen
  Edit-Strategie; „Same Model“ ist gegeben, „Same Agent“ nicht.
- Duo fehlt bewusst: bei 4B editiert der Coder nicht (0/6 im Vorlauf);
  ein 35B-Lauf ist nötig, bevor Duo bewertet wird.

## Schlussfolgerung

1. **Auf dem Starter-Korpus ist HiveMind Direct nicht besser als Aider** —
   gleiche Pass-Rate, deutlich teurer. Das ist der ehrliche Ist-Stand.
2. **Beide schlagen die No-Tool-Baseline klar** (raw_files 71 % → ~90 %),
   was den Wert von Agent-Tooling grundsätzlich zeigt.
3. **Die Kategorien sind komplementär:** Direct hilft bei Feature-Tasks,
   Aider bei Multi-File. Das ist der interessanteste Ansatzpunkt für
   Verbesserungen (Edit-/Diff-Engine, Multi-File-Planung).
4. **Nächster Lauf:** echte Repo-Tasks + 35B-Duo. Erst dort kann Direct
   seinen Token-Overhead durch Exploration/Verifikation zurückverdienen.

Roh-JSONs: `%TEMP%\hm_bench_full_raw_aider.json`,
`%TEMP%\hm_bench_full_direct.json` (nicht eingecheckt).

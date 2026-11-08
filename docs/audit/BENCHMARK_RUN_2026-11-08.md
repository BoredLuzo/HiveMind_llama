# Ruhiger Benchmark-Lauf 2026-11-08 (6 Tasks, 4B)

**Kein Phase-2-Report.** Kleiner, kontrollierter Vergleich, um Aider
einzubeziehen und die Messkette zu prüfen. Keine statistische Signifikanz.

## Setup

- 6 Tasks: `fix_add`, `fix_off_by_one`, `fix_mutable_default`,
  `feature_clamp`, `refactor_extract`, `multi_move`.
- Ein Modell für alle Bedingungen: `qwen3.5:4b-mtp` (Q4_K_M), Vulkan.
- HiveMind-Bedingungen pinnen die Agentic-Defaults: `until_finished=true`,
  `duo_planner=true`, `duo_chunking=false`; Direct `direct_tools_tier=full`.
- `raw_files` und `aider`: 2 Läufe (12 Zellen). `direct`/`duo`: 1 Lauf
  (6 Zellen), weil die Bedingungen im getrennten Block liefen.
- Aider 0.86.2 aus `HiveMind_bench_tools/aider`, `--aider-bin` gesetzt.

## Ergebnis

| Bedingung | Zellen | Pass | Median | Tokens/Zelle | Hinweis |
|---|---:|---:|---:|---:|---|
| `raw_files` | 12 | **67 %** | 6,0 s | 420 | No-Tool-Baseline |
| `aider` | 12 | **100 %** | 12,4 s | 970 | günstigste und schnellste 100-%-Bedingung |
| `hivemind_direct` | 6 | **100 %** | 20,2 s | 22.554 | ~23× Aider-Tokens, ~1,6× Zeit |
| `hivemind_duo` | 6 | **0 %** | 8,8 s | 745 | Ursache: `duo_tool_rounds=0` in den Install-Settings; nach Pinnen 2/2 (siehe Korrektur) |

### Pro Task

| Task | raw_files | aider | hm_direct | hm_duo |
|---|---:|---:|---:|---:|
| feature_clamp | 2/2 | 2/2 | 1/1 | 0/1 |
| fix_add | 2/2 | 2/2 | 1/1 | 0/1 |
| fix_mutable_default | 0/2 | 2/2 | 1/1 | 0/1 |
| fix_off_by_one | 2/2 | 2/2 | 1/1 | 0/1 |
| multi_move | 0/2 | 2/2 | 1/1 | 0/1 |
| refactor_extract | 2/2 | 2/2 | 1/1 | 0/1 |

## Korrektur (2026-11-08, nach dem 21-Task-Lauf)

Die 0 % bei `hivemind_duo` waren **kein 4B-Modellproblem**. Die Install-Settings
hatten `duo_tool_rounds=0`; damit läuft der Coder ohne Tools (Text only). Nach
dem Pinnen des ausgelieferten Defaults (`duo_tool_rounds=50`) im Benchmark-Body:

- `fix_add` und `fix_off_by_one`: **2/2 PASS**, ~58 s, ~62k Tokens/Zelle.
- Der Coder liest/schreibt jetzt, der Planner funktioniert.

Die Aussage „Duo ist mit 4B nicht arbeitsfähig" ist damit **widerlegt**; sie
war ein Messfehler. Ein vollständiger Coder-allein-Lauf gegen Aider folgt.

## Interpretation

1. **Aider ist auf diesen kleinen Tasks die stärkste Bedingung**: 100 %,
   schnell, ~1k Tokens pro Zelle. Es sieht die Workspace-Dateien direkt und
   braucht keine Tool-Runden.
2. **HiveMind Direct ist gleichwertig in der Pass-Rate, aber teuer**:
   100 % nach dem Pinnen der Defaults, aber ~22,5k Tokens pro Zelle
   (Prompt-Wiederholung über Tool-Runden). Für kleine Tasks ist der
   Token-Overhead schwer zu rechtfertigen; bei größeren Tasks kann er sich
   durch Kontext-/Tool-Nutzung relativieren.
3. **Duo war im ersten Lauf kein Modellproblem:** `duo_tool_rounds=0` schaltete
   die Tools ab. Nach dem Pinnen läuft der 4B-Coder (2/2 im Smoke).
4. **Raw-Files-Baseline**: 67 %, scheitert an `fix_mutable_default` und
   `multi_move` (Dateien geschrieben, Check rot). Die Diff-Baseline (`raw`)
   wäre auf denselben Tasks voraussichtlich 0 % — deshalb ist `raw_files`
   die primäre No-Tool-Baseline.

## Grenzen

- 6 synthetische Tasks, 1–2 Läufe: keine Konfidenzintervalle, kein
  Signifikanzanspruch.
- Direct/Duo liefen in einem separaten Block mit 1 Lauf; Aider/raw mit 2.
- Nur ein 4B-Modell; Duo-Ergebnis ist modellgrößenabhängig.
- Aider bekam die Workspace-Dateien als Chat-Argumente (Runner-Feature);
  das ist die faire Same-Model-Bedingung, aber Aiders eigene Edit-Strategie
  bleibt anders.

## Nächster sinnvoller Schritt

1. **35B-Lauf für Duo** (gleiche 6 Tasks, 1 Lauf): erst damit ist eine
   Duo-Aussage möglich. `qwen3.6:35b-a3b-ud` oder das vorhandene
   Hermes-35B-A3B.
2. Aider/Direct mit 3 Läufen auf denselben Tasks für Rauschschätzung.
3. Danach echte Repo-Tasks ergänzen und Phase 2/3 fahren.

Roh-JSONs lagen unter `%TEMP%\hm_bench_subset_*.json` (nicht eingecheckt).

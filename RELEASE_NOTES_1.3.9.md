# Release 1.3.9

Scope: b3d0c4e..HEAD (27 commits; dev is a superset of origin/main).
Honesty note: "suite" = the offline regression suite (105/105 PASS, no
LLM, no GPU). "Seen live" = observed in a real engine session; ONLY the
symptoms below were live. NONE of today's fixes were re-verified live
afterwards. Runtime behavior (models, speed, VRAM, prefill) is
UNMEASURED — no performance claims.

## Symptoms seen live, fixes pinned in the suite
- tree_scout ran in the wrong workspace (gravSandbox) while RUN-TRACE
  reported the request workspace → WS-PROVENANCE: request > real resume
  > chat_run resolution; ENV never primary (8302c23). PUT /chats/{id}
  rejects dead workspace paths (d09b56e, audit N9).
- write_file 15.6 KB, read_file showed 3.9 KB → model concluded a
  broken write (c9467ab): range reads now carry the full line AND char
  count plus a continuation line; the executor re-cap no longer cuts
  that note away; the file on disk was always complete.
- PowerShell syntax looped in bash (exit 2) → hint and schema name the
  shell that actually runs; NONZERO errors carry it too (749ac6e).
- Confirmation loop on harmless python runs → the destructive gate now
  gates only genuinely destructive patterns (e6e5d58).
- edit_match_fail on many identical lines → autojunk=False; anchors and
  ratios no longer collapse (de861de).

## Suite-tested, NOT live-verified
- b261de3 content (restored via f157631 after revert 39c834f):
  MiniCPM-V 4.7 + LFM2.5-24B registration (text-only, DSpark drafts),
  host-RAM caps (--cache-ram 1024 / --ctx-checkpoints 4,
  llama_cache_ram_mib / llama_ctx_checkpoints, env override HIVEMIND_*;
  the 8192/32 binary defaults are reachable via env), MoE pin (profile
  beats name heuristic), /vram/estimate via _MOE_TABLE,
  task_complete test truth. Suite: test_restored_caps_truth (8 checks,
  no slot start). Live runs of these models are NOT part of this
  release.
- Tool-pipeline audit phases 0-7 (7ac1f9f, 28f9b5e): schema continuum,
  mask-first compression, prefix-cache hygiene — pinned in the suite
  (test_schema_truth et al.), effect on real runs UNMEASURED. Same for
  the prefix/cache stability commit 63eb515 (append-only corrections):
  suite evidence only, no live evidence.
- Budgets redesign without model-class staffels (d307463), prefill
  budgets with 60-s rule + EMA floor 512 (cfb06e4, 8ce756d, 1583982),
  chunk-resume durability (bc12b5a), debug_dump_request (1725429),
  engine death forensics, bench forensics (cddae6a), resume shutdown
  semantics doc (834ded8).
- Security: request workspace wins, commit gate, run-scoped git staging
  (3d0a4ad).
- Installer/updater: BUILD_INFO with commit hash, update.bat --zip with
  layout guard, rollback docs (52971da, c08611a). Batch logic tested
  against fake zips only, never executed against live/.
- Test infra: UTF-8 suites without PYTHONUTF8 (ba28342), new suites
  (106 total).

## Known limitations (honest)
- Without Git Bash installed, run_bash runs via powershell.exe and the
  hint follows that fallback (c2a3e43) — but the installer neither
  installs Git nor warns when Git Bash is missing (reported, open).
- The installer import check + guided repair (tools/env_check.py,
  346a39e) is only proven by the offline suite against artificially
  broken packages in %TEMP% — it has NOT been exercised on a foreign
  machine's fresh install.

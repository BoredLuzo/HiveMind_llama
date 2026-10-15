# Release 1.3.9

Scope: commits b3d0c4e..HEAD (22). Honesty note: "suite" below means the
standalone regression suite (104/104 PASS, offline, no LLM/GPU). "live"
means observed in a real engine session — nothing here was re-verified
live after the fix; runtime behavior (models, speed, VRAM) is UNMEASURED
and no performance claim is made.

## Workspace provenance (suite + live root-cause)
- WS-PROVENANCE (8302c23): a stale resume block or the process-global
  HIVEMIND_WORKSPACE could override the request workspace inside
  pre-explore (live: RUN-TRACE calc_app, tree scout on gravSandbox).
  Priority now: request > real resume > chat_run resolution; ENV never
  primary. PUT /chats/{id} rejects dead workspace paths (d09b56e, audit
  N9).

## Tool pipeline (suite; symptoms seen live)
- read_file truncation honesty: range reads carry full line/char count
  and a continuation line; the executor re-cap no longer cuts that note
  or tells the model to "use read_file" on a read_file result
  (c9467ab; live: 15.6 KB write read back as 3.9 KB).
- run_bash: OS hint and schema name the shell that actually runs
  (Git Bash vs PowerShell); NONZERO errors carry the shell name
  (749ac6e; live: PowerShell syntax looped in bash).
- fuzzy edit: autojunk=False on all SequenceMatcher sites — anchors and
  ratios on identical-line blocks were corrupted (de861de).
- destructive gate: routine python (subprocess, requests, eval, ...)
  no longer asks for confirmation; rm -rf / del /s / git reset --hard
  stay gated (e6e5d58; live: confirmation loop on harmless runs).

## Reliability / budgets (suite)
- Prefill-aware budgets (60-s rule, EMA feed with 512-token floor,
  per-tool cap fields), budget redesign without model-class staffels,
  chunk-resume durability, cache stability fixes, debug_dump_request,
  engine death forensics, resume shutdown-semantics documentation
  (cfb06e4, 8ce756d, cddae6a, 1725429, 63eb515, 834ded8).

## Installer / updater (suite-tested logic only, never run against live)
- package_release embeds the exact commit hash; update.bat gains
  `--zip <path>` (local zip, layout guard core/+utils/, backup and
  excludes unchanged, rollback documented) and a BUILD_INFO fallback
  (52971da).

## Known open
- Inject-Budget-Gate wiring, repomap consolidation, gateway audit
  N7/N9-adjacent gateway items (N9 engine side closed here).

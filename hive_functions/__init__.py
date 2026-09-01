"""
hive_functions/
═══════════════
The functional brain of HiveMind — high-level agent logic
that sits above the infrastructure layer.

Contents:
  planner.py            — Task planning, inloop planner, plan steps
  prompts.py            — All LLM system / user prompt strings
  pipeline.py           — Run pipeline orchestration
  pre_explore.py        — Pre-explore entry point + file reading
  tree_scout.py         — Workspace tree scanning + TOML contracts
  chunking.py           — Chunked task decomposition + resume
  memory.py             — Agent memory read/write
  soul_engine.py        — Personality / soul system
  skill_distiller.py    — Skill extraction from agent runs
  test_runner.py        — Agentic test execution
  loop_machine.py       — Execution state controller
  git_tools.py          — Git integration: autocommit, diff, status
  ctx_utils.py          — Context pipeline utilities
  language_config.py    — Per-language run/test/lint configs
  num_ctx_config.py     — Central num_ctx limits + safety
  safe_profile_policy.py — Runtime profile safety rules
  hivemind_feature/     — AST tools + feature detection
"""

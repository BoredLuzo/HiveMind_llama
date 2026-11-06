"""Soul-evolution triggers (2026-11-05 cycle break 4/5).

Both trigger functions lived in server.py; chat_run and peer_ratings
imported the running server to reach them. They are self-contained wrappers
over hive_functions.soul_engine — the only server-owned input is
`base_path` (soul files live next to server.py), which is injected once via
`init_soul_evolution` at startup, exactly like init_insights.
"""
from __future__ import annotations

import logging
from pathlib import Path

from core.state import pipeline, registry_all, settings
from model_configs import append_learning_log, read_learning_log
from hive_functions.soul_engine import (
    maybe_evolve_soul,
    MIN_RUNS_FOR_EVOLUTION, EVOLUTION_INTERVAL_RUNS,
    FORCED_EVOLUTION_COOLDOWN,
)

_logger = logging.getLogger("hivemind.learning.soul_evolution")

_base_path: Path | None = None
_last_forced_evolution_run: int = 0


def init_soul_evolution(base_path: Path) -> None:
    """Inject the server-owned base path once at startup."""
    global _base_path
    _base_path = base_path


async def _maybe_trigger_soul_evolution_forced(run_count: int):
    """Immediately on bad peer scores, ignoring the normal interval.
    Cooldown: at least FORCED_EVOLUTION_COOLDOWN runs between forced evolutions."""
    global _last_forced_evolution_run
    # Soul-Heal (14.08.): soul_evolve_agent.enabled als Toggle respektieren ?
    _sea_cfg0 = settings.get("soul_evolve_agent", {})
    if isinstance(_sea_cfg0, dict) and not bool(_sea_cfg0.get("enabled", False)):
        return
    if run_count - _last_forced_evolution_run < FORCED_EVOLUTION_COOLDOWN:
        return
    _last_forced_evolution_run = run_count

    _sea = settings.get("soul_evolve_agent", "direct")
    # Direct use as model= would pass a dict ─ Ollama crash.
    reflection_model = _sea.get("model", "direct") if isinstance(_sea, dict) else str(_sea)
    new_soul = await maybe_evolve_soul(
        base_path           = _base_path,
        ollama_client       = pipeline.ollama,
        model               = reflection_model,
        learning_log_reader = read_learning_log,
        registry_all_fn     = registry_all,
        total_runs          = max(run_count, MIN_RUNS_FOR_EVOLUTION),
    )
    if new_soul:
        append_learning_log(_base_path, reflection_model, {
            "event":                      "soul_evolution_forced",
            "trigger":                    "low_peer_scores",
            "run_count":                  run_count,
            "evolution_count":            new_soul.get("evolution_count", 0),
            "selbstverstaendnis_preview": new_soul.get("selbstverstaendnis", "")[:100],
        })
    # NOTE: run_soul_cycle() is no longer called here ─ it now runs after
    # every completed Duo loop. Calling it here too caused double-decay:
    # insights lost 0.16 instead of 0.08 per cycle.


async def _maybe_trigger_soul_evolution(run_count: int):
    # Soul-Heal (14.08.): soul_evolve_agent.enabled als Toggle respektieren ?
    _sea_cfg = settings.get("soul_evolve_agent", {})
    if isinstance(_sea_cfg, dict) and not bool(_sea_cfg.get("enabled", False)):
        return
    if run_count < MIN_RUNS_FOR_EVOLUTION:
        return
    if (run_count - MIN_RUNS_FOR_EVOLUTION) % EVOLUTION_INTERVAL_RUNS != 0:
        return
    _sea2 = settings.get("soul_evolve_agent", "direct")
    reflection_model = _sea2.get("model", "direct") if isinstance(_sea2, dict) else str(_sea2)
    new_soul = await maybe_evolve_soul(
        base_path           = _base_path,
        ollama_client       = pipeline.ollama,
        model               = reflection_model,
        learning_log_reader = read_learning_log,
        registry_all_fn     = registry_all,
        total_runs          = run_count,
    )
    if new_soul:
        append_learning_log(_base_path, reflection_model, {
            "event":                      "soul_evolution",
            "run_count":                  run_count,
            "evolution_count":            new_soul.get("evolution_count", 0),
            "selbstverstaendnis_preview": new_soul.get("selbstverstaendnis", "")[:100],
        })
    # NOTE: run_soul_cycle() removed here ─ runs after every Duo loop now.
    # Previously caused double-decay when soul evolution + duo completion coincided.




import json
import re
import time
import logging
from pathlib import Path
from typing import Optional, Callable

from utils.file import write_json_atomic

from settings import load_settings as _load_settings
settings = _load_settings()  # Runtime settings dict

_logger = logging.getLogger("hivemind.soul_engine")

MIN_RUNS_FOR_EVOLUTION   = 10
EVOLUTION_INTERVAL_RUNS  = 5
FORCED_EVOLUTION_COOLDOWN = 10

_SOUL_FILENAME = "soul.json"

_SEED_SOUL = {
    "selbstverstaendnis": "",
    "version":            1,
    "evolution_count":    0,
    "run_count":          0,
    "last_evolved":       "",
    "last_reason":        "",
    "evolution_log":      [],
}


# ── Persistence ────────────────────────────────────────────────────────────────

def _soul_path(base_path: Path) -> Path:
    return base_path.parent / _SOUL_FILENAME


def load_soul(base_path: Path) -> dict:
    p = _soul_path(base_path)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for k, v in _SEED_SOUL.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return dict(_SEED_SOUL)


def save_soul(base_path: Path, soul: dict):
    write_json_atomic(_soul_path(base_path), soul)


def reset_soul(base_path: Path):
    save_soul(base_path, dict(_SEED_SOUL))


def get_soul_summary(base_path: Path) -> dict:
    soul = load_soul(base_path)
    text = soul.get("selbstverstaendnis", "")
    return {
        "version":         soul.get("version", 1),
        "evolution_count": soul.get("evolution_count", 0),
        "run_count":       soul.get("run_count", 0),
        "last_evolved":    soul.get("last_evolved", ""),
        "last_reason":     soul.get("last_reason", ""),
        "preview":         text[:200] if text else "",
        "has_learned":     bool(text),
        "evolution_log":   soul.get("evolution_log", [])[-5:],
    }


# ── Prompt-Layer ──────────────────────────────────────────────────────────────

def build_soul_prompt_layer(soul: dict, style_only: bool = False) -> str:


    text = soul.get("selbstverstaendnis", "").strip()
    if not text:
        return ""
    if style_only:
        return f"[Hivemind character hint]\n{text[:300]}"
    version    = soul.get("version", 1)
    evolutions = soul.get("evolution_count", 0)
    return (
        f"[Learned self-understanding — version {version}, {evolutions} evolutions]\n"
        f"{text}\n[End of self-understanding]"
    )


# ── Evolution ─────────────────────────────────────────────────────────────────

_EVOLUTION_PROMPT = """You are the reflection agent of Hivemind.
Analyze the recent peer-rating logs and formulate an improved self-understanding.

Current self-understanding:
{current}

Recent rating events:
{log_summary}

Active agents: {agents_summary}

Formulate a precise self-understanding for Hivemind (max. 400 words).
Answer ONLY with the new text, no preamble, no JSON."""


async def maybe_evolve_soul(
    base_path: Path,
    ollama_client,
    model: str,
    learning_log_reader: Callable,
    registry_all_fn: Callable,
    total_runs: int,
) -> Optional[dict]:


    soul     = load_soul(base_path)
    last_run = soul.get("run_count", 0)

    if total_runs < MIN_RUNS_FOR_EVOLUTION:
        return None
    if last_run >= total_runs:
        return None

    agents   = registry_all_fn()
    all_logs = []
    for model_name in set(agents.values()):
        try:
            entries = learning_log_reader(base_path, model_name, limit=30)
            all_logs.extend([e for e in entries if e.get("event") == "peer_rating"])
        except Exception:
            pass

    all_logs.sort(key=lambda e: e.get("ts", ""), reverse=True)
    recent = all_logs[:20]

    if not recent:
        soul["run_count"] = total_runs
        save_soul(base_path, soul)
        return None

    avg_score  = sum(e.get("score", 0.5) for e in recent) / len(recent)
    log_lines  = []
    for e in recent[:10]:
        score = e.get("score", "?")
        rater = e.get("rater", "?")
        rated = e.get("rated_agent", "?")
        w     = "; ".join(e.get("weaknesses", [])[:2])
        log_lines.append(f"  {rater}→{rated}: score={score}" + (f" | {w}" if w else ""))

    log_summary     = f"Ø {avg_score:.2f} / {len(recent)} Ratings\n" + "\n".join(log_lines)
    agents_summary  = ", ".join(f"{k}={v}" for k, v in agents.items()
                                if k not in ("duo_coder", "duo_critic", "vision"))
    current_text    = soul.get("selbstverstaendnis", "(empty)")

    prompt = _EVOLUTION_PROMPT.format(
        current=current_text[:600],
        log_summary=log_summary,
        agents_summary=agents_summary,
    )

    try:
        new_text = await ollama_client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=600,
        )
        new_text = new_text.strip()
        if not new_text or len(new_text) < 50:
            return None

        log_entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "version":   soul.get("version", 1),
            "avg_score": round(avg_score, 3),
            "reason":    f"Run {total_runs}: avg_score={avg_score:.2f}",
            "text":      current_text[:300],
        }
        evo_log = soul.get("evolution_log", [])
        evo_log.append(log_entry)

        soul["selbstverstaendnis"] = new_text
        soul["version"]            = soul.get("version", 1) + 1
        soul["evolution_count"]    = soul.get("evolution_count", 0) + 1
        soul["run_count"]          = total_runs
        soul["last_evolved"]       = time.strftime("%Y-%m-%dT%H:%M:%S")
        soul["last_reason"]        = f"avg_score={avg_score:.2f}, runs={total_runs}"
        soul["evolution_log"]      = evo_log[-20:]

        save_soul(base_path, soul)
        return soul

    except Exception:
        return None


# ── Skill Distillation + Skill Writing ────────────────────────────────────────

SKILLS_DIR_NAME = "learning_logs/skills"


def _skills_dir(base_path: Path) -> Path:
    """Return path to skills directory relative to server.py."""
    return base_path.parent / SKILLS_DIR_NAME


def _write_skills(insights: list[dict], base_path: Path = None):
    """Write top-20 insights as .md skill files.

    Args:
        insights: List of insight dicts from Memory._insights.
        base_path: Path to server.py (for resolving skills dir).
    """
    if base_path is None:
        return
    skills_dir = _skills_dir(base_path)
    skills_dir.mkdir(parents=True, exist_ok=True)

    top = sorted(insights, key=lambda x: float(x.get('relevance_score', 0)), reverse=True)[:20]

    existing = set(skills_dir.glob('skill_*.md'))
    written = set()

    for i, ins in enumerate(top):
        slug = re.sub(r'[^a-z0-9]+', '_', ins.get('insight', '')[:40].lower()).strip('_')
        if not slug:
            slug = f"skill_{i:02d}"
        fname = skills_dir / f"skill_{i:02d}_{slug}.md"
        content = (
            f"# Skill: {ins.get('insight', '')[:80]}\n\n"
            f"**Relevanz:** {ins.get('relevance_score', 0):.2f}  "
            f"**Gesehen:** {ins.get('merge_count', 1)}x  "
            f"**Quelle:** {ins.get('source', '?')}\n\n"
            f"{ins.get('insight', '')}\n\n"
            f"**Pfade:** {', '.join(p for p in (ins.get('trigger_paths') or [ins.get('trigger_path', '')]) if p)}\n"
        )
        try:
            fname.write_text(content, encoding='utf-8')
            written.add(fname)
        except Exception as e:
            _logger.warning("Skill write failed for %s: %s", fname.name, e)

    # Remove old skill files that are no longer in top-20
    for old in existing - written:
        try:
            old.unlink()
        except Exception:
            pass


def run_soul_cycle(memory, settings: dict, focus_paths: list[str] = [], base_path: Path = None):
    """Run Skill Distillation and optional Skill Writing after Soul Evolution.

    This is an add-on to the existing Soul Evolution logic.
    Called by server.py after each completed Duo loop.

    Args:
        memory: Memory instance (has ._insights, ._persist_insights(), ._insight_lock).
        settings: Settings dict (contains soul_skill_distillation, soul_skill_writing toggles).
        focus_paths: List of currently active file paths (from agentic loop / git diff).
        base_path: Path to server.py (for resolving skills dir).
    """
    # Distillation: Decay, Merge, Evict
    _current_insights: list[dict] = []
    if settings.get('soul_skill_distillation', True):
        try:
            from .skill_distiller import SkillDistiller
            distiller = SkillDistiller()
            # Thread-safe: lock _insights for read-modify-write
            lock = getattr(memory, '_insight_lock', None)
            if lock:
                lock.acquire()
            try:
                memory._insights = distiller.run(memory._insights, focus_paths=focus_paths)
                memory._persist_insights()  # Distiller always flushes — this is the main persist point
                _current_insights = list(memory._insights)  # snapshot under lock
            finally:
                if lock:
                    lock.release()
            _logger.info("[SkillDistiller] Ran distillation: %d insights remaining, focus_paths=%s",
                        len(memory._insights), focus_paths[:3])
        except Exception as e:
            _logger.warning("[SkillDistiller] Failed: %s", e)

    # Skill Writing: Top-20 insights as .md files (opt-in)
    # Uses the snapshot taken under lock — avoids reading memory._insights without
    # the lock while remember_repo_insight could be modifying it from another thread.
    if settings.get('soul_skill_writing', False) and _current_insights:
        try:
            _write_skills(_current_insights, base_path=base_path)
            _logger.info("[SkillWriting] Wrote skill files to %s", _skills_dir(base_path))
        except Exception as e:
            _logger.warning("[SkillWriting] Failed: %s", e)

    # PERF-3: If distillation was disabled or skipped but insights are dirty,
    # flush them to disk so no data is lost between cycles.
    if not _current_insights and hasattr(memory, 'flush_insights_if_dirty'):
        try:
            memory.flush_insights_if_dirty()
        except Exception:
            pass

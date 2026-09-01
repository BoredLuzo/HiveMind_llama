"""
model_configs — Base + Learned Config Management for HiveMind Agents
=====================================================================

Provides per-model, per-agent configuration management:
  - Base configs: Hardcoded sensible defaults per agent type
  - Learned configs: Runtime-discovered optimal parameters (persisted as JSONL)
  - Effective config: Merged base + learned (learned overrides base)

File layout (auto-created):
  model_configs/
    __init__.py              ← this file
    base/                    ← (optional, not used — base is in-memory)
    learned/<model>/         ← one dir per model
      <agent>.json           ← learned config for that agent
    learning_logs/           ← JSONL append-only logs
      <model>.jsonl          ← per-model event log
"""

import json
import time
import copy
import threading
from pathlib import Path
from typing import Optional

# Thread-safety lock for append_learning_log — multiple async tasks can
# call it concurrently (peer ratings, soul evolution, insight extractor).
_log_write_lock = threading.Lock()

# ── Base Defaults per Agent Type ──────────────────────────────────────────────
# These are the starting configurations for each agent role.
# Models will override these with learned configs over time.

_BASE_DEFAULTS: dict[str, dict] = {
    "analyst": {
        "temperature": 0.3,
        "max_tokens": 1100,
        "system_prompt_override": "",
        "notes": "Deep analysis, thorough exploration",
    },
    "refiner": {
        "temperature": 0.3,
        "max_tokens": 400,
        "system_prompt_override": "",
        "notes": "Concise refinement",
    },
    "critic": {
        "temperature": 0.2,
        "max_tokens": 600,
        "system_prompt_override": "",
        "notes": "Critical evaluation",
    },
    "synthesizer": {
        "temperature": 0.2,
        "max_tokens": 900,
        "system_prompt_override": "",
        "notes": "Synthesis and integration",
    },
    "direct": {
        "temperature": 0.4,
        "max_tokens": 600,
        "system_prompt_override": "",
        "notes": "Direct response",
    },
    "judge": {
        "temperature": 0.1,
        "max_tokens": 120,
        "system_prompt_override": "",
        "notes": "Quick judgment calls",
    },
    "duo_coder": {
        "temperature": 0.2,
        "max_tokens": 12000,
        "system_prompt_override": "",
        "notes": "Agentic coding with tool use",
    },
    "duo_critic": {
        "temperature": 0.15,
        "max_tokens": 600,
        "system_prompt_override": "",
        "notes": "Critic in duo mode",
    },
    "vision": {
        "temperature": 0.3,
        "max_tokens": 400,
        "system_prompt_override": "",
        "notes": "Vision/image analysis",
    },
}

# ── Internal State ────────────────────────────────────────────────────────────
_initialized: bool = False


def _learned_dir(base_path: Path, model_name: str) -> Path:
    """Return path to learned configs dir for a specific model."""
    return Path(base_path).parent / "model_configs" / "learned" / model_name.replace(":", "_")


def _log_dir(base_path: Path) -> Path:
    """Return path to learning logs directory."""
    return Path(base_path).parent / "model_configs" / "learning_logs"


def _log_path(base_path: Path, model_name: str) -> Path:
    """Return path to the JSONL log file for a model."""
    return _log_dir(base_path) / f"{model_name.replace(':', '_')}.jsonl"


def _ensure_dirs(base_path: Path, model_name: str = "") -> None:
    """Ensure directory structure exists."""
    ld = _log_dir(base_path)
    if not ld.exists():
        ld.mkdir(parents=True, exist_ok=True)
    if model_name:
        md = _learned_dir(base_path, model_name)
        if not md.exists():
            md.mkdir(parents=True, exist_ok=True)


# ── Public API ────────────────────────────────────────────────────────────────

def init_base_configs(base_path: Path) -> None:
    """Initialize base config directory structure. Called once at startup."""
    global _initialized
    _ensure_dirs(base_path)
    _initialized = True


def get_base_config(base_path: Path, agent: str) -> dict:
    """Return the base config for an agent type. Returns empty dict if unknown."""
    cfg = _BASE_DEFAULTS.get(agent)
    if cfg is None:
        return {}
    return copy.deepcopy(cfg)


def list_base_configs(base_path: Path) -> dict:
    """Return all base configs (agent → config dict)."""
    return copy.deepcopy(_BASE_DEFAULTS)


def get_learned_config(base_path: Path, model_name: str, agent: str) -> Optional[dict]:
    """Load a learned config for a model+agent combination. Returns None if not found."""
    _ensure_dirs(base_path, model_name)
    p = _learned_dir(base_path, model_name) / f"{agent}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def save_learned_config(base_path: Path, model_name: str, agent: str, config: dict) -> None:
    """Persist a learned config for a model+agent combination."""
    _ensure_dirs(base_path, model_name)
    p = _learned_dir(base_path, model_name) / f"{agent}.json"
    # Atomic write via tmp + replace
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def get_effective_config(base_path: Path, model_name: str, agent: str, use_learned: bool = True) -> dict:
    """Return merged base + learned config. Learned values override base."""
    base = get_base_config(base_path, agent)
    if not use_learned:
        return base
    learned = get_learned_config(base_path, model_name, agent)
    if learned:
        base.update({k: v for k, v in learned.items() if v is not None})
    return base


def list_learned_models(base_path: Path) -> list[str]:
    """List all models that have at least one learned config."""
    learned_root = Path(base_path).parent / "model_configs" / "learned"
    if not learned_root.exists():
        return []
    models = []
    for d in learned_root.iterdir():
        if d.is_dir() and any(d.glob("*.json")):
            # Reverse the colon encoding
            models.append(d.name.replace("_", ":", 1) if ":" not in d.name else d.name)
    return sorted(models)


def list_learned_configs(base_path: Path, model_name: str) -> list[str]:
    """List agent names that have learned configs for a model."""
    d = _learned_dir(base_path, model_name)
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def delete_learned_config(base_path: Path, model_name: str, agent: str) -> bool:
    """Delete a learned config. Returns True if it existed and was deleted."""
    p = _learned_dir(base_path, model_name) / f"{agent}.json"
    if p.exists():
        p.unlink()
        return True
    return False


def reset_learned_configs(base_path: Path, model_name: str) -> int:
    """Delete ALL learned configs for a model. Returns count of deleted configs."""
    d = _learned_dir(base_path, model_name)
    if not d.exists():
        return 0
    count = 0
    for p in d.glob("*.json"):
        p.unlink()
        count += 1
    # Remove empty directory
    try:
        d.rmdir()
    except OSError:
        pass
    return count


def append_learning_log(base_path: Path, model_name: str, entry: dict) -> None:
    """Append a JSONL entry to the learning log for a model.

    Thread-safe: protected by _log_write_lock to prevent interleaved writes
    when called from concurrent async tasks (peer ratings, soul evolution, etc.).
    """
    _ensure_dirs(base_path)
    p = _log_path(base_path, model_name)
    if "ts" not in entry:
        entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    with _log_write_lock:
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def read_learning_log(base_path: Path, model_name: str, limit: int = 50) -> list[dict]:
    """Read the last N entries from a model's learning log (newest first)."""
    p = _log_path(base_path, model_name)
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").strip().splitlines()
        entries = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(entries) >= limit:
                break
        return entries
    except Exception:
        return []


def clear_learning_log(base_path: Path, model_name: str) -> bool:
    """Delete the learning log for a model. Returns True if it existed."""
    p = _log_path(base_path, model_name)
    if p.exists():
        p.unlink()
        return True
    return False

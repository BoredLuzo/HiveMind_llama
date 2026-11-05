# HiveMind — Local Multi-Agent AI Coding Assistant


from __future__ import annotations
import copy
import json
import os
import logging
import threading
import time
from datetime import datetime as _dt
from pathlib import Path

_logger = logging.getLogger("hivemind.settings")

SETTINGS_FILE      = Path(__file__).parent / "settings.json"
PRESETS_FILE       = Path(__file__).parent / "presets.json"
CUSTOM_PROMPTS_DIR = Path(__file__).parent / "custom_prompts"

# DEFAULTS SPLIT (2026-11-05): DEFAULT_SETTINGS/DEFAULT_AGENT_CFG moved to
# settings_defaults.py (pure data, no I/O) — re-exported here so every
# existing `from settings import DEFAULT_SETTINGS` keeps working.
from settings_defaults import DEFAULT_SETTINGS, DEFAULT_AGENT_CFG  # noqa: F401,E402


# ── Defaults ─────────────────────────────────────────────────────────────────
# Recommended models (setup_models.bat / fetch_models.py) as defaults — they
# are the models the installer downloads.
# CODER-TEMP (2026-08-31): higher than for Refiner/Critic — creative coding.



DEFAULT_PRESETS = {}

# ── Load / Save ───────────────────────────────────────────────────────────────

_load_cache_key: tuple | None = None
_load_cache_data: dict | None = None

# FORENSICS-WATERMARK (2026-10-25): (mtime_ns, size) of settings.json at the
# last read/write by THIS process. save_settings warns when the on-disk file
# changed in between — the live 2026-10-24/25 revert case (llama_mlock /
# duo_test_feedback_chunk) was unprovable because saves were silent.
_last_sync_stat: tuple | None = None


def load_settings() -> dict:
    """Cached based on mtime.

    Avoids per-call disk read + JSON parse + ~60-line migration
    (which e.g. used to run per run_bash/run_python spawn via tools/sandbox.enabled()
    and is executed there). Each reader gets a flat copy so that
    mutations of the returned dict do not corrupt the cache. save_settings()
    changes the file
    """
    global _load_cache_key, _load_cache_data, _last_sync_stat
    _key = None
    try:
        _st = SETTINGS_FILE.stat()
        _key = (_st.st_mtime_ns, _st.st_size)
    except Exception:
        _key = None
    _last_sync_stat = _key
    if _key is not None and _key == _load_cache_key and _load_cache_data is not None:
        return copy.deepcopy(_load_cache_data)
    data = _load_settings_from_disk()
    if _key is not None:
        _load_cache_key = _key
        _load_cache_data = copy.deepcopy(data)
    return data


def _load_settings_from_disk() -> dict:
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if "image_forces_pipeline" in data and "image_desc_full_pipeline" not in data:
                data["image_desc_full_pipeline"] = data.pop("image_forces_pipeline")
            data.pop("duo_important_task_default", None)
            for _dead_key in ("duo_agentic_post_review_enabled",
                              "duo_agentic_post_review_max_tokens",
                              "duo_agentic_post_review_context_injection"):
                data.pop(_dead_key, None)
            # MIGRATION (presets re-enabled 2026-09-02): `active_preset` persists
            # so the UI can highlight the loaded preset; the removed runtime
            # override keys (duo presets/runtime-profile) stay stripped.
            for _dead_preset_key in ("duo_use_presets",
                                     "duo_use_preset_models",
                                     "duo_runtime_profile_lock_override"):
                data.pop(_dead_preset_key, None)
            # MIGRATION (0.99.2): duo_planner_content_tokens -> duo_planner_max_tokens.
            _old_cap = data.get("duo_planner_content_tokens")
            if "duo_planner_max_tokens" not in data:
                if _old_cap is not None:
                    try:
                        _old_cap_i = int(_old_cap)
                    except (TypeError, ValueError):
                        _old_cap_i = 0
                    data["duo_planner_max_tokens"] = _old_cap_i if _old_cap_i > 0 else DEFAULT_SETTINGS["duo_planner_max_tokens"]
                else:
                    data["duo_planner_max_tokens"] = DEFAULT_SETTINGS["duo_planner_max_tokens"]
            data.pop("duo_planner_content_tokens", None)
            _xa = data.get("exploration_agent")
            if isinstance(_xa, dict) and _xa.get("enabled") and not (_xa.get("model") or "").strip():
                _xa["model"] = DEFAULT_SETTINGS["exploration_agent"]["model"]
            # if data.get("disable_thinking_in_planner") is True:
            #     data["disable_thinking_in_planner"] = False
            _tbc = data.get("_thinking_before_chunking")
            if isinstance(_tbc, bool) and data.get("duo_chunking"):
                data["duo_agentic_thinking"] = _tbc
            if data.get("duo_parallel_preexplore") == "auto":
                data["duo_parallel_preexplore"] = True
            for _ws_key in ("searxng_engines", "searxng_language"):
                _ws_val = data.get(_ws_key)
                if _ws_val is not None and not str(_ws_val).strip():
                    data[_ws_key] = None
            data.pop("pre_explore_parallel", None)
            # STALE-KEYS (2026-10-31, audit): keys from older UI/preset
            # exports with no production reader. OUT of DEFAULT_SETTINGS and
            # dropped on load so they cannot round-trip forever.
            for _stale_key in (
                    "automap_code_duo_enabled", "automap_duo_pre_explore",
                    "automap_duo_parallel_preexplore",
                    "automap_pipeline_websearch_enabled",
                    "duo_distilled_executor", "startup_preload_analyst",
                    "startup_preload_coder", "startup_preload_judge_in_agentic",
                    "searxng_enabled", "tool_profile", "budget_schema_budget",
                    "budget_tool_result_cap", "judge_bias",
                    "duo_candidate_models",
                    "git_branch"):
                data.pop(_stale_key, None)
    # MIGRATION: duo_test_feedback → new split toggles (chunk/final)
            _legacy_tf = data.get("duo_test_feedback") if "duo_test_feedback" in data else None
            if "duo_test_feedback_chunk" not in data and _legacy_tf is not None:
                data["duo_test_feedback_chunk"] = bool(_legacy_tf)
            if "duo_test_feedback_final" not in data and _legacy_tf is not None:
                data["duo_test_feedback_final"] = bool(_legacy_tf)
            # duo_planner_model persists across restarts. (The forced None here
            # wiped a user/preset-selected planner model on every reload —
            # live: preset "1" Loaded hermes as planner model, after a restart
            # the planner silently fell back to the coder model.)
            if "duo_planner_model" not in data:
                data["duo_planner_model"] = None
            # MIGRATION: moe_cpu_experts int (globaler Override) -> dict (per-Model)
            if not isinstance(data.get("moe_cpu_experts"), dict):
                data["moe_cpu_experts"] = {}
            for k, v in DEFAULT_SETTINGS.items():
                data.setdefault(k, v)
    # Fix: merge individual agent keys from DEFAULT_AGENT_CFG.
            _sa = data.setdefault("agents", {})
            for _ak, _av in DEFAULT_AGENT_CFG.items():
                _sa.setdefault(_ak, _av)
            for _ak, _av in DEFAULT_AGENT_CFG.items():
                if _ak in _sa and isinstance(_sa[_ak], dict):
                    _sa[_ak].setdefault("model", _av.get("model", ""))
                    _sa[_ak].setdefault("temperature", _av.get("temperature", 0.3))
                    _sa[_ak].setdefault("max_tokens", _av.get("max_tokens", 400))
                    _sa[_ak].setdefault("thinking", _av.get("thinking", False))
                    _sa[_ak].setdefault("thinking_budget", _av.get("thinking_budget", 0))
            return data
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)

_settings_write_lock = threading.RLock()


def _now_iso() -> str:
    return _dt.now().astimezone().isoformat(timespec="seconds")


def save_settings(settings: dict, *, saved_at: str | None = None):
    """Persist the settings dict atomically.

    saved_at: explicit stamp for callers that must keep two keys in lockstep
    (the preset load writes preset_applied_at == settings_saved_at, so the
    autoload guard sees "applied at the latest save", not "one write older").

    FOREIGN-WRITE GUARD (2026-10-25): if the on-disk file changed since this
    process last read/saved it, that is a writer outside this process (hand
    edit, another install) - say so in the log before overwriting it with the
    in-memory state. Detection only; the save itself always proceeds.

    Callers that change a key must use mutate_settings() instead: this function
    writes the dict it is given and cannot see a change made after its read.
    """
    global _last_sync_stat
    _pre_stat = None
    try:
        _st = SETTINGS_FILE.stat()
        _pre_stat = (_st.st_mtime_ns, _st.st_size)
    except OSError:
        _pre_stat = None
    if (_pre_stat is not None and _last_sync_stat is not None
            and _pre_stat != _last_sync_stat):
        _logger.warning(
            "[settings] foreign write detected: on-disk %s != last sync %s - "
            "overwriting with the in-memory state. If a hand edit just "
            "disappeared, this line is why.", _pre_stat, _last_sync_stat)
    with _settings_write_lock:
        _write_settings_unlocked(settings, saved_at=saved_at)


def _write_settings_unlocked(settings: dict, *, saved_at: str | None = None) -> None:
    """One atomic write. Callers hold _settings_write_lock."""
    global _last_sync_stat
    settings["settings_saved_at"] = saved_at or _now_iso()
    _data = json.dumps(settings, ensure_ascii=False, indent=2)
    # PID in the name: two processes can carry the same thread id, and two
    # writers sharing one temp file can interleave into invalid JSON.
    # Replacement can lose a race against a foreign cleaner or a second writer
    # in the same process; one retry with a fresh name is enough and keeps the
    # write atomic.
    for _attempt in range(2):
        _tmp = SETTINGS_FILE.with_name(
            f".settings_{os.getpid()}_{threading.get_ident()}_{_attempt}.tmp")
        try:
            with open(_tmp, "w", encoding="utf-8") as _fh:
                _fh.write(_data)
                _fh.flush()
                try:
                    os.fsync(_fh.fileno())   # data before the atomic replace
                except OSError as _fs_err:
                    _logger.warning("[settings] fsync failed (%s) - continuing",
                                    _fs_err)
            _tmp.replace(SETTINGS_FILE)      # atomar, OS-Garantie
            break
        except PermissionError:
            if _attempt:
                raise
        finally:
            # a failed write must not leave .settings_*.tmp behind
            if _tmp.exists():
                try:
                    _tmp.unlink()
                except OSError:
                    pass
    try:
        _st = SETTINGS_FILE.stat()
        _last_sync_stat = (_st.st_mtime_ns, _st.st_size)
    except OSError:
        pass


def mutate_settings(fn):
    """Read, change and write settings as ONE locked step.

    save_settings() is atomic per write, but the read-modify-write around it is
    not: two callers that each load(), set a key and save() lose one key
    (measured: a=111 and b=222 ended as a=0, b=222). Everything that changes
    settings goes through here.

    fn gets the current dict, mutates it and may return a replacement.
    Returns the dict that was written.
    """
    with _settings_write_lock:
        current = load_settings()
        result = fn(current)
        data = result if isinstance(result, dict) else current
        _write_settings_unlocked(data)
    return data



# ── Presets (user config snapshots) ──────────────────────────────────────────
def load_presets() -> dict:
    if PRESETS_FILE.exists():
        try:
            # utf-8-sig: tolerate a UTF-8 BOM. External Windows editors add one;
            # strict utf-8 json.loads then failed, load_presets() silently
            # returned {} and every preset load 404'd.
            data = json.loads(PRESETS_FILE.read_text(encoding="utf-8-sig"))
            # PRESET-HYGIENE (2026-10-31, audit): old exports carry the apply
            # bookkeeping; it is skipped on load but stays as stale dust in
            # every re-export.
            if isinstance(data, dict):
                for _p in data.values():
                    if isinstance(_p, dict):
                        for _k in ("active_preset", "settings_saved_at",
                                   "preset_applied_at", "git_token",
                                   "models_dir", "_registry"):
                            _p.pop(_k, None)
            return data
        except Exception:
            pass
    return dict(DEFAULT_PRESETS)


_presets_write_lock = threading.Lock()


def save_presets(presets: dict):
    # ATOMIC-WRITE (2026-10-31, audit): the hardened settings path carries a
    # PID in the temp name and retries once on Windows PermissionError; the
    # preset path had neither, so two processes could interleave.
    _data = json.dumps(presets, ensure_ascii=False, indent=2)
    with _presets_write_lock:
        _tmp = PRESETS_FILE.with_name(
            f".presets_{os.getpid()}_{threading.get_ident()}.tmp")
        try:
            _tmp.write_text(_data, encoding="utf-8")
            try:
                _tmp.replace(PRESETS_FILE)
            except PermissionError:
                time.sleep(0.1)
                _tmp.replace(PRESETS_FILE)
        finally:
            try:
                _tmp.unlink(missing_ok=True)
            except OSError:
                pass


# PRESET-PROMPT-ROLES (2026-11-01): the ONE list of prompt keys a preset can
# really override at runtime, with the base prompt each one falls back to.
# The prompt editor used to build its dropdown from the UI's agent metadata,
# so it offered `intent` (rule-based intent detection - no prompt at all) and
# `duo_critic` (the Duo runner reads duo_critic_code / duo_critic_general),
# while hiding the two real critic keys and duo_synthesizer. Editing a role
# that nothing reads writes a file that is never loaded - a silent no-op.
# `base` is the key in hive_functions.prompts.PROMPTS the role falls back to.
PRESET_PROMPT_ROLES = (
    {"key": "analyst", "label": "Analyst", "base": "analyst",
     "hint": "Pipeline: breaks the problem into components"},
    {"key": "refiner", "label": "Refiner", "base": "refiner",
     "hint": "Pipeline: fixes the constraints the critic found"},
    {"key": "critic", "label": "Critic", "base": "critic",
     "hint": "Pipeline: ERR / MISS / FIX / CONTRA verdict"},
    {"key": "synthesizer", "label": "Synthesizer", "base": "synthesizer",
     "hint": "Pipeline: integrates the roles into the final answer"},
    {"key": "duo_synthesizer", "label": "Duo Synthesizer", "base": "duo_synthesizer",
     "hint": "Agentic runs: the final report"},
    {"key": "direct", "label": "Direct", "base": "direct",
     "hint": "Direct chat answers (simple mode)"},
    {"key": "vision_agent", "label": "Vision Agent", "base": "vision_agent",
     "hint": "Image preprocessing before a run"},
    {"key": "duo_coder", "label": "Duo Coder", "base": "duo_coder",
     "hint": "Agentic coder (tool calls + thinking)"},
    {"key": "duo_critic_code", "label": "Duo Critic (code)", "base": "duo_critic_code",
     "hint": "Critic in code mode (Duo + agentic review)"},
    {"key": "duo_critic_general", "label": "Duo Critic (general)", "base": "duo_critic_general",
     "hint": "Critic in non-code Duo mode"},
)

# LEGACY-KEYS (2026-11-01): the old editor saved the Duo critic under the bare
# key `duo_critic`, which no runner reads. Reading it as a fallback for both
# real critic keys makes those existing edits effective instead of dead; new
# saves always write the specific key.
_PROMPT_READ_ALIASES = {
    "duo_critic_code": ("duo_critic",),
    "duo_critic_general": ("duo_critic",),
}


def get_custom_prompt(preset_name: str, agent_key: str) -> str | None:
    path = CUSTOM_PROMPTS_DIR / f"{preset_name}_{agent_key}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    for _legacy in _PROMPT_READ_ALIASES.get(agent_key, ()):
        _lp = CUSTOM_PROMPTS_DIR / f"{preset_name}_{_legacy}.txt"
        if _lp.exists():
            return _lp.read_text(encoding="utf-8").strip()
    return None


def save_custom_prompt(preset_name: str, agent_key: str, content: str):
    CUSTOM_PROMPTS_DIR.mkdir(exist_ok=True)
    path = CUSTOM_PROMPTS_DIR / f"{preset_name}_{agent_key}.txt"
    path.write_text(content, encoding="utf-8")


def delete_custom_prompts_for_preset(preset_name: str):
    if not CUSTOM_PROMPTS_DIR.exists():
        return
    for f in CUSTOM_PROMPTS_DIR.glob(f"{preset_name}_*.txt"):
        f.unlink()


def copy_prompts_to_preset(src_preset: str | None, dst_preset: str,
                            agent_keys: list[str], base_prompts: dict):
    for key in agent_keys:
        src_content = get_custom_prompt(src_preset, key) if src_preset else None
        content = src_content or base_prompts.get(key, "")
        if content:
            save_custom_prompt(dst_preset, key, content)

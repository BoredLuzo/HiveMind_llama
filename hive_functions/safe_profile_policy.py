"""Safe-profile policy: matrix -> settings.

Supports two matrix formats (2026-08-31):
  - NEW (preferred, installer-oriented): flat
        { "<policy_name>": { vram_budget_gb, duo_runtime_profile,
                             duo_coder_ctx_*, default_keep_alive,
                             model_overrides: { role: {model, max_tokens, ...} },
                             guardrails: {...}, max_concurrent_models } }
  - OLD (compatibility):
        { "default_policy": "<name>", "policies": { "<name>": {...profiles...} } }

The policy mutates the passed settings dict in-place (as before) and
returns a status snapshot + updated_keys.
"""
import json
from pathlib import Path


_DISABLED_POLICIES = {"", "off", "disabled", "custom", "manual"}

# roles that live in settings["agents"] (pipeline.agents are built from them).
_AGENT_ROLES = {
    "analyst", "refiner", "critic", "synthesizer",
    "direct", "judge", "vision",
    "duo_coder", "duo_critic",
}

# Duo-Rollen zusaetzlich auf ihre flachen Settings-Keys mappen (duo_runner liest sie).
_DUO_ROLE_SETTINGS = {
    "duo_coder": "duo_coder_model",
    "duo_critic": "duo_critic_model",
}

# direct scalar keys adopted 1:1 from the policy.
_SCALAR_SETTINGS = {
    "vram_budget_gb", "duo_runtime_profile",
    "duo_coder_ctx_agentic", "duo_coder_ctx_until_finished", "duo_coder_ctx_normal",
    "default_keep_alive", "smart_preload_keep_alive", "max_concurrent_models",
}

# guardrail block -> flat settings keys (no consumer yet, for the future).
_GUARDRAIL_SETTINGS = {
    "allow_cpu_offload": "allow_cpu_offload",
    "warn_on_cpu_offload": "warn_on_cpu_offload",
    "max_model_size_gb": "max_model_size_gb",
    "prefer_smaller_models": "prefer_smaller_models",
}

# User preferences the safe-profile policy must NEVER force-overwrite: they
# are runtime choices (e.g. the user's saved VRAM budget), not hard safety
# limits. A value in the matrix for these keys is only informational/documentation
# and is not applied. Mirrors the existing "CARDS-WIN / user choice wins"
# philosophy used for agent models.
_USER_PREF_KEYS = {"vram_budget_gb"}


def _as_float(value, default=None):
    try:
        return float(value)
    except Exception:
        return default


def _as_int(value, default=None):
    try:
        return int(value)
    except Exception:
        return default


def _as_str(value, default=None):
    try:
        return str(value).strip()
    except Exception:
        return default


def _as_bool(value, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n"):
        return False
    return default


def _resolve_matrix_path(workspace_root: Path, matrix_file: str) -> Path:
    p = Path(str(matrix_file or "").strip())
    if p.is_absolute():
        return p
    return (workspace_root / p).resolve()


def _load_matrix(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _dict_or_empty(value) -> dict:
    return value if isinstance(value, dict) else {}


def _put_if_not_none(dst: dict, key: str, value):
    if value is not None:
        dst[key] = value


def _matrix_policy(matrix: dict, policy_name: str) -> tuple[str, dict | None]:
    """Resolve (policy_name, policy) for NEW and OLD matrix format."""
    if isinstance(matrix.get("policies"), dict):
        # ALT: {"default_policy": name, "policies": {name: {...}}}
        policies = matrix["policies"]
        if not policy_name:
            policy_name = str(matrix.get("default_policy") or "").strip()
    else:
        # NEU: flach {name: {...}}
        policies = matrix
        if not policy_name and isinstance(matrix.get("default_policy"), str):
            policy_name = str(matrix["default_policy"]).strip()
        if not policy_name:
            for _k, _v in matrix.items():
                if _k in ("default_policy", "description"):
                    continue
                if isinstance(_v, dict):
                    policy_name = str(_k)
                    break
    policy = policies.get(policy_name)
    if not isinstance(policy, dict):
        return policy_name, None
    return policy_name, policy


def _policy_patch(policy: dict) -> dict:
    """Policy -> flaches Settings-Patch (+ agents-Unterpatch)."""
    patch: dict = {}
    if not isinstance(policy, dict):
        return patch

    for key in _SCALAR_SETTINGS:
        if key in policy and policy[key] is not None:
            patch[key] = policy[key]

    # OLD format: profiles (fast/balanced/critical) -> flat duo settings.
    _profiles = _dict_or_empty(policy.get("profiles"))
    _fast = _dict_or_empty(_profiles.get("fast"))
    _balanced = _dict_or_empty(_profiles.get("balanced"))
    _critical = _dict_or_empty(_profiles.get("critical"))
    if _fast.get("target_model"):
        patch["duo_profile_speed_model"] = str(_fast["target_model"])
    if _balanced.get("target_model"):
        patch["duo_profile_quality_model"] = str(_balanced["target_model"])
    for _src, _src_key, _dst_key in (
        (_balanced, "runtime_timeout_s", "duo_run_timeout_seconds"),
        (_critical, "runtime_timeout_s", "duo_run_timeout_critical_seconds"),
        (_critical, "tool_rounds_cap", "duo_max_tool_rounds_runtime_cap"),
    ):
        _put_if_not_none(patch, _dst_key, _as_int(_src.get(_src_key), None))

    # model_overrides -> settings["agents"][role] (+ flache Duo-Keys)
    agent_patch: dict = {}
    for role, cfg in _dict_or_empty(policy.get("model_overrides")).items():
        if not isinstance(cfg, dict) or role not in _AGENT_ROLES:
            continue
        entry: dict = {}
        _put_if_not_none(entry, "model", _as_str(cfg.get("model"), None))
        _put_if_not_none(entry, "max_tokens", _as_int(cfg.get("max_tokens"), None))
        _put_if_not_none(entry, "temperature", _as_float(cfg.get("temperature"), None))
        _put_if_not_none(entry, "thinking", _as_bool(cfg.get("thinking"), None))
        _put_if_not_none(entry, "thinking_budget", _as_int(cfg.get("thinking_budget"), None))
        if entry:
            agent_patch[role] = entry
        if role in _DUO_ROLE_SETTINGS and cfg.get("model"):
            _put_if_not_none(patch, _DUO_ROLE_SETTINGS[role], _as_str(cfg.get("model"), None))
    if agent_patch:
        patch["agents"] = agent_patch

    # guardrails -> flat keys (intended for later use)
    for src_key, dst_key in _GUARDRAIL_SETTINGS.items():
        _guard = _dict_or_empty(policy.get("guardrails"))
        if src_key in _guard and _guard[src_key] is not None:
            patch[dst_key] = _guard[src_key]

    return patch


def apply_safe_profile_policy(settings: dict, workspace_root: Path) -> dict:
    """Apply safe profile policy to settings and return a state snapshot."""
    if not isinstance(settings, dict):
        return {"applied": False, "reason": "invalid_settings"}

    policy_name = str(settings.get("safe_profile_policy", "") or "").strip()
    if policy_name and policy_name.lower() in _DISABLED_POLICIES:
        return {
            "applied": False,
            "reason": "policy_disabled",
            "policy": policy_name,
            "matrix_file": str(settings.get("safe_profile_matrix_file", "") or ""),
            "updated_keys": [],
        }

    matrix_file = str(settings.get("safe_profile_matrix_file", "model_configs/safe_profile_matrix.json") or "").strip()
    matrix_path = _resolve_matrix_path(workspace_root, matrix_file)
    if not matrix_path.exists():
        return {
            "applied": False,
            "reason": "matrix_missing",
            "policy": policy_name,
            "matrix_file": matrix_file,
            "matrix_path": str(matrix_path),
            "updated_keys": [],
        }

    matrix = _load_matrix(matrix_path)
    if not isinstance(matrix, dict):
        return {
            "applied": False,
            "reason": "matrix_invalid",
            "policy": policy_name,
            "matrix_file": matrix_file,
            "matrix_path": str(matrix_path),
            "updated_keys": [],
        }

    policy_name, policy = _matrix_policy(matrix, policy_name)
    if not isinstance(policy, dict):
        return {
            "applied": False,
            "reason": "policy_not_found",
            "policy": policy_name,
            "matrix_file": matrix_file,
            "matrix_path": str(matrix_path),
            "updated_keys": [],
        }

    patch = _policy_patch(policy)

    # CARDS-WIN (2026-09-01): a safe-profile model_override is only a DEFAULT.
    # If the user picked a different model on the Agent-tab card (anything != the
    # shipped default), keep the user's choice and drop the policy's model
    # override for that role (other keys like max_tokens still apply).
    try:
        from settings import DEFAULT_AGENT_CFG as _DEFAULT_AGENTS
    except Exception:
        _DEFAULT_AGENTS = {}
    _agent_patch = patch.get("agents")
    if isinstance(_agent_patch, dict):
        for _role, _entry in list(_agent_patch.items()):
            if not isinstance(_entry, dict) or "model" not in _entry:
                continue
            _cur = (settings.get("agents", {}).get(_role, {}) or {}).get("model", "")
            _dfl = (_DEFAULT_AGENTS.get(_role, {}) or {}).get("model", "")
            if _cur and _cur != _dfl:
                _agent_patch[_role] = {k: v for k, v in _entry.items() if k != "model"}
                if not _agent_patch[_role]:
                    del _agent_patch[_role]
                if _role in _DUO_ROLE_SETTINGS:
                    patch.pop(_DUO_ROLE_SETTINGS[_role], None)

    updated_keys: list[str] = []

    for key, value in patch.items():
        if key == "agents":
            continue
        if key in _USER_PREF_KEYS:
            # USER-WINS (2026-09-02): never clobber a saved user preference
            # (e.g. vram_budget_gb). The policy default stays informational.
            continue
        if settings.get(key) != value:
            settings[key] = value
            updated_keys.append(key)

    agents = settings.setdefault("agents", {})
    for role, entry in _dict_or_empty(patch.get("agents")).items():
        role_cfg = agents.setdefault(role, {})
        if not isinstance(role_cfg, dict):
            role_cfg = {}
            agents[role] = role_cfg
        for k, v in entry.items():
            if role_cfg.get(k) != v:
                role_cfg[k] = v
                updated_keys.append(f"agents.{role}.{k}")

    return {
        "applied": True,
        "reason": "ok",
        "policy": policy_name,
        "matrix_file": matrix_file,
        "matrix_path": str(matrix_path),
        "updated_keys": updated_keys,
    }

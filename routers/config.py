"""Config API-Router — Settings, Memory."""
import asyncio
import copy
import logging
import math
import re
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from backend.runtime_flags import RUNTIME_FLAG_KEYS as _RUNTIME_FLAG_KEYS
from settings_defaults import SETTINGS_SCHEMA_EXCLUDED_KEYS, SETTINGS_UI_META
from settings import (
    save_settings,
    load_presets, save_presets,
    get_custom_prompt, save_custom_prompt,
    delete_custom_prompts_for_preset, copy_prompts_to_preset,
    PRESET_PROMPT_ROLES,
    DEFAULT_AGENT_CFG, DEFAULT_SETTINGS,
    _now_iso,
    _validate_preset_name,
    _validate_agent_key,
)
from hive_functions.prompts import PROMPTS
from vram.loader import _bk_load
import core.state as _state
from core.state import (
    settings, registry_all,
    registry_set, registry_sync_from_pipeline,
    apply_settings_to_pipeline,
    _refresh_safe_profile_policy,
    _ws_configure, _get_num_ctx,
)

logger = logging.getLogger("hivemind.server")

router = APIRouter(prefix="", tags=["Config"])


def _sync_runtime_flags(context_label: str) -> None:
    """Broadcast runtime flags from `settings` into backend/core module globals."""
    try:
        from backend.runtime_flags import apply_runtime_flags as _apply_flags
        _apply_flags(settings)
    except ImportError as exc:
        logger.warning("[%s] runtime flag sync skipped: %s", context_label, exc)


def _populate_default_agents(sa: dict) -> None:
    for _ak, _av in DEFAULT_AGENT_CFG.items():
        sa.setdefault(_ak, copy.deepcopy(_av))
        if isinstance(sa[_ak], dict):
            sa[_ak].setdefault("model", _av.get("model", ""))
            sa[_ak].setdefault("temperature", _av.get("temperature", 0.3))
            sa[_ak].setdefault("max_tokens", _av.get("max_tokens", 400))
            sa[_ak].setdefault("thinking", _av.get("thinking", False))
            sa[_ak].setdefault("thinking_budget", _av.get("thinking_budget", 0))

# UI-REV (2026-09-09): monotonically incremented on every accepted POST
# /settings and exposed via GET /settings so the UI can tag its patches.
# A patch from a stale tab (older ui_rev) touching a protected key loses that
# key — see post_settings. Escape hatch: "settings_force": true.
_settings_rev = 0
_UI_REV_PROTECTED_KEYS = {
    "duo_compress_threshold", "duo_compress_auto_floor",
    "duo_cache_friendly_ctx", "duo_partial_compression",
    "duo_compress_local_only",
    # STALE-TAB RACE (2026-10-06, live x2): an open tab's old snapshot
    # reverted the gate/timeout/restrict after every restart - these now
    # carry the same rev guard.
    "duo_action_approval_enabled", "duo_action_approval_timeout_s",
    "telegram_phone_restricted", "telegram_mirror_enabled",
    "telegram_gateway_enabled", "telegram_approval_mode",
    "git_token",
    # GIT PANEL (2026-10-07, user feedback: git fields must not reset on save): every panel click posts the whole form snapshot - an
    # older tab silently wiped email/auto-push/auto-commit again and
    # again while the owner was mid-test.
    "git_repo_url", "git_username", "git_email", "git_auto_push",
    "git_default_branch", "git_commit_prefix", "duo_git_autocommit",
    "duo_git_checkpoints",
}

# Secrets / maschinen-spezifische Werte werden nie in ein Preset gespeichert
# und nie aus einem geladen. models_dir ist ein absoluter Install-Pfad (vom
# Installer gesetzt) - ein Preset-Snapshot/-Auto-Load darf ihn nicht auf ""
# zuruecksetzen (live beobachtet: startup auto-load hat models_dir gekillt).
# UI-STATE KEYS (2026-10-03): image_processing_mode/vision_agent_mode haben
# keinen Backend-Leser - die eine Wahrheit fuer Preprocessing lebt in
# vision_model.json (/vision/config), der Pipeline-Runner ist sequenziell.
# Ein Preset darf diesen Zustand weder einfrieren noch zurueckspielen.
# POLICY (2026-10-18, owner): a preset is a FULL settings snapshot - voice, STT,
# Telegram voice and the git identity belong to it, so loading "phone + voice"
# actually restores the phone + voice setup. Only two things are never stored:
#   * secrets (git_token) - a preset file is shared/exported, a token in it is a
#     leak;
#   * machine paths (models_dir) - an absolute install path that a snapshot on
#     another machine must not overwrite.
# `_registry` is internal bookkeeping. image_processing_mode / vision_agent_mode
# are UI state (not preset state); vision_agent_mode IS read live by
# core/pipeline_runner.py, so it must persist in settings.json even though a
# preset never carries it.
_PRESET_NEVER_KEYS = {"git_token", "_registry", "models_dir",
                      "image_processing_mode", "vision_agent_mode",
                      # SECURITY-POSTURE (audit 2026-11-03): a shared preset
                      # must not be able to disable approvals / phone
                      # restrictions or add an MCP server (spawn = RCE) on
                      # the next apply. These keys stay local to the install.
                      "duo_action_approval_enabled",
                      "duo_action_approval_timeout_s",
                      "telegram_phone_restricted",
                      "telegram_approval_mode",
                      "telegram_gateway_enabled",
                      "mcp_client_enabled", "mcp_servers",
                      "lsp_forward_workspace_settings",
                      "workspace",
                      # BOOKKEEPING (2026-10-28, preset audit): these three are
                      # written by the apply path itself (active_preset, both
                      # stamps). In the snapshot they froze the saving moment
                      # into every exported file and came back as stale state
                      # on load.
                      "active_preset", "settings_saved_at", "preset_applied_at"}

# Valid preset keys that are intentionally not in DEFAULT_SETTINGS: they are
# read at runtime and must survive a preset apply (P1, audit 2026-11-03).
_PRESET_EXTRA_KEYS = {"mcp_client_max_calls_per_run", "telegram_gateway_enabled"}

# PRESET-CTX-OVERLAY (2026-09-04): nur diese Keys darf der Client beim
# Preset-Save explizit uebergeben (verhindert das Race zwischen debounced
# postSettings und dem Snapshot). Alles andere bleibt Server-seitig gesnapshottet.
_PRESET_OVERLAY_KEYS = {
    "duo_coder_ctx_agentic",
    "duo_coder_ctx_normal",
    "duo_planner_ctx_target",
    "duo_critic_ctx",
}


def _sync_pipeline_agents() -> int:
    """Push settings["agents"] into the LIVE pipeline after a preset load.

    The load used to touch settings only: the pipeline kept the old max_tokens /
    model, so
      * a run right after the load used the OLD budget (answers truncated), and
      * the next preset save overlaid those stale pipeline values back into the
        snapshot, silently storing the old budget ("speichert nicht komplett").
    Returns the number of fields applied (0 without a pipeline, e.g. in tests).
    """
    pipeline = getattr(_state, "pipeline", None)
    if not pipeline or not getattr(pipeline, "agents", None):
        return 0
    applied = 0
    for role, cfg in (settings.get("agents") or {}).items():
        ag = pipeline.agents.get(role)
        if ag is None or not isinstance(cfg, dict):
            continue
        for field, cast in (("model", str), ("temperature", float),
                            ("max_tokens", int), ("thinking_budget", int)):
            if field not in cfg:
                continue
            if cfg[field] is None:
                if field == "temperature":
                    ag.temperature = None
                    applied += 1
                continue
            try:
                setattr(ag, field, cast(cfg[field]))
                applied += 1
            except (TypeError, ValueError):
                continue
        if "thinking" in cfg:
            ag.thinking = None if cfg["thinking"] is None else bool(cfg["thinking"])
            applied += 1
    return applied


def _preset_snapshot(extra: dict | None = None) -> dict:
    snap = {}
    for key, value in settings.items():
        if key in _PRESET_NEVER_KEYS:
            continue
        snap[key] = copy.deepcopy(value)
    if isinstance(extra, dict):
        for key in _PRESET_OVERLAY_KEYS:
            if key in extra and key not in _PRESET_NEVER_KEYS:
                snap[key] = copy.deepcopy(extra[key])
    # Agent-Karten: keep every stored per-agent field (model, temperature,
    # max_tokens, thinking, thinking_budget, plus any extra keys a preset may
    # carry) and overlay the LIVE pipeline values so a just-changed card
    # setting is what gets persisted.
    _ag_snap = copy.deepcopy(settings.get("agents") or {})
    if _state.pipeline:
        for k, a in _state.pipeline.agents.items():
            _entry = _ag_snap.setdefault(k, {})
            _entry["model"] = a.model
            _entry["temperature"] = a.temperature
            _entry["max_tokens"] = a.max_tokens
            _entry["thinking"] = bool(getattr(a, "thinking", False))
            _entry["thinking_budget"] = int(getattr(a, "thinking_budget", 0) or 0)
    # AGENT OVERLAY (2026-10-18): the client sends the agent cards it has on
    # screen; per role, only the known fields are taken. This closes the race
    # between a card edit and the snapshot (the ctx fields had the same fix).
    if isinstance(extra, dict) and isinstance(extra.get("agents"), dict):
        for _role, _cfg in extra["agents"].items():
            if not isinstance(_cfg, dict):
                continue
            _dst = _ag_snap.get(_role)
            if not isinstance(_dst, dict):
                _dst = {}
            for _f in ("model", "temperature", "max_tokens", "thinking",
                       "thinking_budget", "ctx"):
                if _f in _cfg:
                    _dst[_f] = copy.deepcopy(_cfg[_f])
            _ag_snap[_role] = _dst
    snap["agents"] = _ag_snap
    snap["prompts"] = {}
    return snap


async def _apply_preset_internal(name: str, *, persist: bool = True,
                                 warnings: list | None = None) -> bool:
    """Explicit preset Load — replaces the whole user configuration (models,
    context, options, mode, prompts). Never runs automatically at startup.
    PRESET-WORKSPACE-GUARD (2026-09-30): a preset workspace path that does
    not exist is NOT applied (the current workspace stays active) and the
    caller receives a warning to surface in the UI — a silent switch to a
    dead path used to cripple every following run (workspace chain)."""
    presets = load_presets()
    if name not in presets:
        return False
    p = presets[name]
    if "agents" in p:
        _agents_cfg = p["agents"]
        if isinstance(_agents_cfg, dict) and "agents" in _agents_cfg and isinstance(_agents_cfg["agents"], dict):
            _agents_cfg = _agents_cfg["agents"]
        if isinstance(_agents_cfg, dict) and _agents_cfg:
            # MERGE, do not replace: fields the preset does not carry keep their
            # current value, and roles it never heard of survive. Replacing the
            # dict dropped max_tokens/thinking for older presets, which silently
            # shrank the output budget and truncated long answers.
            _cur = settings.get("agents")
            if not isinstance(_cur, dict):
                _cur = {}
            _merged = copy.deepcopy(_cur)
            for _role, _cfg in _agents_cfg.items():
                if not isinstance(_cfg, dict):
                    continue
                _dst = _merged.get(_role)
                if not isinstance(_dst, dict):
                    _dst = {}
                _dst.update(copy.deepcopy(_cfg))
                _merged[_role] = _dst
            settings["agents"] = _merged
    _applied_keys: set[str] = set()
    # PRESET-ALIAS-MIGRATION (2026-10-31, audit): presets saved before
    # duo_ubatch existed only carry llama_ubatch_size; the resolver gives
    # duo_ubatch priority, so such a preset could never set the effective
    # ubatch. Mirror the alias onto the canonical key on apply.
    if "duo_ubatch" not in p and "llama_ubatch_size" in p:
        settings["duo_ubatch"] = copy.deepcopy(p["llama_ubatch_size"])
        _applied_keys.add("duo_ubatch")
    for key, value in p.items():
        if key in {"agents", "prompts", "active_preset"}:
            continue
        if key in _PRESET_NEVER_KEYS:
            continue
        if key not in DEFAULT_SETTINGS and key not in _PRESET_EXTRA_KEYS:
            # STALE-KEY (2026-10-31): old presets carry keys with no reader;
            # applying them put them back into settings.json on every load.
            # P1 (audit 2026-11-03): say so instead of dropping silently.
            logger.warning("[PRESET] key %r has no default - skipped", key)
            continue
        if key == "workspace" and isinstance(value, str) and value.strip():
            if not Path(value.strip()).exists():
                _warn = (f"workspace '{value.strip()}' from preset '{name}' does not "
                         f"exist — kept the current workspace")
                logger.warning("[PRESET] %s", _warn)
                if warnings is not None:
                    warnings.append(_warn)
                continue
        settings[key] = copy.deepcopy(value)
        _applied_keys.add(key)
    apply_settings_to_pipeline(settings)
    # PRESET-LOAD-SYNC (2026-10-28, preset audit): these five settings are
    # llama-server STARTUP flags held in module globals (server.py:370-405).
    # A preset load used to change only settings.json - the values took
    # effect after an engine restart. Sync them the way the startup path
    # does; imports isolated so a missing module logs instead of failing
    # the apply (no-new-silent-excepts guard).
    if _applied_keys & _RUNTIME_FLAG_KEYS:
        _sync_runtime_flags("PRESET")
        # PRESET-MODEL-CHECK (2026-10-28, preset audit): missing models used
    # to fail silently at the first run, and registry_sync wrote them
    # into the target's registry.json without a word. Check every model
    # the preset just set and surface the missing ones as warnings (the
    # load endpoint forwards them to the UI).
    try:
        from backend.llama_models import resolve_model_path as _rmp
        _missing: list[str] = []
        for _role, _cfg in (settings.get("agents") or {}).items():
            _m = (_cfg or {}).get("model") if isinstance(_cfg, dict) else None
            if _m and not _rmp(str(_m)):
                _missing.append(f"{_role}: {_m}")
        for _k in ("direct_model", "duo_planner_model",
                   "duo_coder_model", "duo_critic_model"):
            _m = settings.get(_k)
            if _m and not _rmp(str(_m)):
                _missing.append(f"{_k}: {_m}")
        if _missing:
            _w = ("models not found locally (these will fail until they "
                  "exist in the models folder): " + "; ".join(_missing))
            if warnings is not None:
                warnings.append(_w)
            logger.warning("[PRESET] %s", _w)
    except ImportError:
        logger.warning("[PRESET] model check skipped (llama_models unavailable)")
    # PRESET-STALE-TAB (2026-10-28): bump the settings revision like
    # post_settings does, or a second tab holding an older snapshot could
    # overwrite freshly loaded preset values through POST /settings.
    global _settings_rev
    _settings_rev += 1
    settings["active_preset"] = name
    if _state._WEBSEARCH_AVAILABLE:
        _ws_configure(
            host=settings.get("searxng_host", "http://localhost:8888"),
            enabled=settings.get("pipeline_websearch_enabled", False)
            or settings.get("duo_websearch_enabled", False),
            engines=settings.get("searxng_engines"),
            language=settings.get("searxng_language"),
        )
    if persist:
        # PRESET-AUTOLOAD-GUARD (2026-10-23): the autoload skips re-applying
        # when settings_saved_at is newer than this stamp — user edits must
        # survive reboots. Both stamps land in ONE write.
        _stamp = _now_iso()
        settings["preset_applied_at"] = _stamp
        settings["settings_saved_at"] = _stamp
        await asyncio.to_thread(save_settings, copy.deepcopy(settings), saved_at=_stamp)
    registry_sync_from_pipeline()
    # RUNTIME-SYNC: a preset can change loader settings as well. The helper
    # lives in backend/runtime_sync (NOT in server) so importing it cannot
    # drag the whole server module - model index validation included - into
    # the preset path.
    try:
        from backend.runtime_sync import sync_loader_settings as _resync_preset
        _resync_preset(settings)
    except (ImportError, AttributeError) as _rs_err:
        logger.debug("[preset] backend runtime sync skipped: %s", _rs_err)
    return True


def preset_autoload_stale(applied_at, saved_at) -> bool:
    """True when settings were saved AFTER the active preset was applied.

    The startup auto-load must then NOT re-apply the snapshot — the user's
    newer config wins. Equal stamps (a load persists both in one write) and
    missing stamps (old settings files) keep the historical apply-always
    behavior."""
    if not applied_at or not saved_at:
        return False
    # DST-PROOF (2026-10-28, preset audit): the plain string compare flipped
    # the order once per year across a daylight-saving boundary (a 1h window
    # where '+02:00' stamps sorted after '+01:00' ones, reverting user edits
    # from the autoload). Parse the offsets; unparseable stamps fall back to
    # the historical string compare, which cannot raise.
    try:
        from datetime import datetime
        return (datetime.fromisoformat(str(saved_at))
                > datetime.fromisoformat(str(applied_at)))
    except (TypeError, ValueError):
        return str(saved_at) > str(applied_at)


_MASKED_SECRET_VALUES = frozenset({"***", "****"})
_MCP_ARG_SECRET_FLAGS = ("--api-key", "--token", "--password", "--secret")
_MCP_ARG_SECRET_PREFIXES = ("--api-key=", "--token=", "--password=", "--secret=")


def _restore_mcp_args(incoming_args: list, prev_args: list) -> list:
    """Restore masked secret arguments ('***'/'****') from prev_args."""
    out = []
    prev_strs = [str(a) for a in prev_args] if isinstance(prev_args, list) else []
    for idx, raw_arg in enumerate(incoming_args):
        s = str(raw_arg)
        low = s.lower()
        prev_tok_low = str(incoming_args[idx - 1]).lower() if idx > 0 else ""
        if s in _MASKED_SECRET_VALUES and prev_tok_low in _MCP_ARG_SECRET_FLAGS:
            restored = None
            if (idx > 0 and idx < len(prev_strs)
                    and prev_strs[idx - 1].lower() == prev_tok_low
                    and prev_strs[idx] not in _MASKED_SECRET_VALUES):
                restored = prev_strs[idx]
            else:
                for j in range(len(prev_strs) - 1):
                    if (prev_strs[j].lower() == prev_tok_low
                            and prev_strs[j + 1] not in _MASKED_SECRET_VALUES):
                        restored = prev_strs[j + 1]
                        break
            out.append(restored if restored is not None else raw_arg)
            continue
        matched_prefix = next((p for p in _MCP_ARG_SECRET_PREFIXES if low.startswith(p)), None)
        if matched_prefix:
            rhs = s.split("=", 1)[1]
            if rhs in _MASKED_SECRET_VALUES:
                restored = None
                if (idx < len(prev_strs)
                        and prev_strs[idx].lower().startswith(matched_prefix)
                        and prev_strs[idx].split("=", 1)[1] not in _MASKED_SECRET_VALUES):
                    restored = prev_strs[idx]
                else:
                    for cand in prev_strs:
                        if (cand.lower().startswith(matched_prefix)
                                and cand.split("=", 1)[1] not in _MASKED_SECRET_VALUES):
                            restored = cand
                            break
                out.append(restored if restored is not None else raw_arg)
                continue
        out.append(raw_arg)
    return out


def _restore_redacted_mcp_servers(incoming, existing) -> list:
    """Restore masked ('***'/'****') MCP headers/env/args from existing config."""
    if not isinstance(incoming, list):
        return incoming
    by_name: dict[str, dict] = {}
    if isinstance(existing, list):
        for sv in existing:
            if isinstance(sv, dict) and sv.get("name"):
                by_name[str(sv["name"])] = sv
    out = []
    for sv in incoming:
        if not isinstance(sv, dict):
            out.append(sv)
            continue
        c = copy.deepcopy(sv)
        prev = by_name.get(str(c.get("name") or ""))
        if isinstance(prev, dict):
            for k in ("headers", "env"):
                if isinstance(c.get(k), dict) and isinstance(prev.get(k), dict):
                    for ik, iv in list(c[k].items()):
                        if iv in _MASKED_SECRET_VALUES and ik in prev[k]:
                            c[k][ik] = copy.deepcopy(prev[k][ik])
            if isinstance(c.get("args"), list) and isinstance(prev.get("args"), list):
                c["args"] = _restore_mcp_args(c["args"], prev["args"])
        out.append(c)
    return out


def _redact_mcp_servers(servers) -> list:
    """Mask MCP auth headers/env values for GET /settings (audit 2026-11-03).

    GET /mcp/servers already redacted these; /settings echoed them verbatim
    into the browser (and preset exports carried the whole list). Presets no
    longer carry mcp_servers; this is the read-path mask.
    """
    out = []
    if not isinstance(servers, list):
        return out
    for _sv in servers:
        if not isinstance(_sv, dict):
            out.append(_sv)
            continue
        _c = copy.deepcopy(_sv)
        for _k in ("headers", "env"):
            if isinstance(_c.get(_k), dict):
                _c[_k] = {_ik: "***" for _ik in _c[_k]}
        _args = _c.get("args")
        if isinstance(_args, list):
            _mask_next = False
            _new_args = []
            for _a in _args:
                _s = str(_a)
                if _mask_next:
                    _new_args.append("***")
                    _mask_next = False
                    continue
                if _s.lower() in ("--api-key", "--token", "--password", "--secret"):
                    _mask_next = True
                elif any(_s.lower().startswith(p) for p in (
                        "--api-key=", "--token=", "--password=", "--secret=")):
                    _s = _s.split("=", 1)[0] + "=***"
                _new_args.append(_s)
            _c["args"] = _new_args
        out.append(_c)
    return out


@router.get("/settings")
async def get_settings():
    s = dict(settings)
    if "mcp_servers" in s:
        s["mcp_servers"] = _redact_mcp_servers(s.get("mcp_servers"))
    # SECURITY (UX-FIX 2026-10-07, user feedback: git fields must not reset on save): the masked '****' read as a broken tiny credential. The
    # field now arrives EMPTY with an explicit saved flag; the panel
    # shows the state as a placeholder instead of a fake value.
    _gt_saved = bool(s.get("git_token"))
    s["git_token"] = "" if _gt_saved else s.get("git_token", "")
    s["git_token_saved"] = _gt_saved
    _sa = s.setdefault("agents", {})
    _populate_default_agents(_sa)
    s["_registry"] = registry_all()
    s["_safe_profile_state"] = dict(_state._safe_profile_state)
    s["settings_rev"] = int(_settings_rev)
    try:
        from backend.llama_config import (
            _MOE_EXPERT_COUNTS as _moe_tbl,
            detect_moe_count as _moe_detect,
            is_moe_model as _moe_check,
        )
        # MOE-AUTODETECT (2026-08-27): moe_expert_defaults = kalibrierte Tabelle
        _defaults = dict(_moe_tbl)
        _autodetect: dict[str, bool] = {}
        _cands: set[str] = set()
        for _v in registry_all().values():
            if _v:
                _cands.add(str(_v))
        for _ag in (s.get("agents") or {}).values():
            if isinstance(_ag, dict) and _ag.get("model"):
                _cands.add(str(_ag["model"]))
        for _c in _cands:
            _key = str(_c).removesuffix("-ud")
            if not _moe_check(_key):
                continue
            _n = _moe_detect(_key)
            if _n > 0 and _key not in _defaults:
                _defaults[_key] = _n
                _autodetect[_key] = True
        s["moe_expert_defaults"] = _defaults
        s["moe_autodetect"] = _autodetect
        # MOE-PIN (2026-10-14): per-model explicit answers from the profiles
        # ("moe": true/false) — the UI shows whether a model is pinned dense /
        # pinned MoE or just name-autodetected.
        try:
            from model_configs.models_registry import get_moe_pin as _moe_pin_of
            _pins: dict[str, bool] = {}
            for _c in _cands:
                _pk = str(_c).removesuffix("-ud")
                _pv = _moe_pin_of(_pk)
                if _pv is not None:
                    _pins[_pk] = _pv
            s["moe_pins"] = _pins
        except Exception:
            pass
    except Exception:
        pass
    # CTX-DEFAULTS (2026-08-27): effective num_ctx default per agent role from
    # num_ctx_config.get_num_ctx — so agent cards with Context "Auto" show the
    # real model default and can cap max_tokens on it.
    try:
        from hive_functions.num_ctx_config import get_num_ctx as _gnc
        _ctx_defs: dict[str, int] = {}
        _role_of = {
            "analyst": "analyst", "refiner": None, "critic": None,
            "synthesizer": None, "direct": None, "judge": None,
            "duo_coder": "duo_coder", "duo_critic": None,
        }
        for _ak, _role in _role_of.items():
            _m = (s.get("agents") or {}).get(_ak, {}).get("model", "")
            if not _m:
                _m = registry_all().get(_ak, "")
            if not _m:
                continue
            _ctx = _gnc(str(_m), _role) if _role else _gnc(str(_m))
            if _ctx:
                _ctx_defs[_ak] = int(_ctx)
        _vm = (s.get("vision_agent_model") or "").strip()
        if _vm:
            _vctx = _gnc(_vm, "vision")
            if _vctx:
                _ctx_defs["vision"] = int(_vctx)
        s["ctx_defaults"] = _ctx_defs
    except Exception:
        pass
    return s


_NULLABLE_TYPE_OVERRIDES: dict[str, str] = {
    "duo_planner_model": "str",
    "duo_image_mode": "str",
    "duo_planner_ctx_cap": "int",
    "duo_planner_ctx_target": "int",
    "duo_coder_ctx_agentic": "int",
    "duo_coder_ctx_normal": "int",
    "duo_critic_ctx": "int",
}

_SCHEMA_GROUP_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("llama_", "moe_", "gpu_"), "engine"),
    (("spec_decode_", "mtp_", "dflash_"), "spec_decode"),
    (("vram_", "total_vram_", "allow_cpu_offload", "warn_on_cpu_offload",
      "max_concurrent_models", "max_model_size_gb", "prefer_smaller_models",
      "pin_direct_after_response", "judge_keepalive_enabled",
      "default_keep_alive", "smart_preload_keep_alive"), "vram"),
    (("duo_compress_", "duo_partial_compression", "duo_mask_before_compress",
      "duo_max_compressions", "session_compress_", "session_history_"), "compression"),
    (("default_unprofiled_sampling",), "sampling"),
    (("duo_tool_sandbox", "read_guard_enabled", "duo_write_guard_enabled",
      "run_bash_shell", "constraint_mode"), "sandbox"),
    (("duo_deep_retrieval", "duo_index_", "duo_tree_scout", "duo_static_map",
      "duo_pin_static_map", "duo_context_budget_chars", "duo_repo_memory",
      "duo_symbol_ref", "duo_pre_explore", "duo_parallel_preexplore",
      "duo_partition_max_files", "duo_pass_explore_files",
      "duo_coder_explore_chars", "exploration_agent"), "retrieval"),
    (("duo_test_", "duo_auto_test_", "duo_selective_tests", "duo_error_rollup"), "testing"),
    (("duo_planner_", "disable_thinking_in_planner", "duo_thinking_per_chunk",
      "duo_evict_planner_on_coder"), "planner"),
    (("vision_", "pipeline_vision_", "image_", "duo_image_", "duo_coder_images_off"), "vision"),
    (("stt_", "tts_"), "voice"),
    (("telegram_",), "telegram"),
    (("git_", "duo_git_"), "git"),
    (("lsp_",), "lsp"),
    (("mcp_",), "mcp"),
    (("searxng_", "pipeline_websearch_", "duo_websearch_", "websearch_",
      "duo_install_max_calls"), "websearch"),
    (("subagent_lite_",), "subagent"),
    (("routing_", "intent_agent"), "routing"),
    (("soul_", "learning_"), "soul"),
    (("smart_preload_", "startup_preload_", "judge_prefetch_", "preload_", "prefetch_"), "preload"),
    (("ask_user_",), "ask_user"),
    (("debug_", "mem_watch_enabled"), "debug"),
    (("duo_", "direct_tools_", "until_finished"), "agentic"),
)

_RESTART_SCHEMA_KEYS = frozenset({
    "gpu_backend", "llama_max_slots", "llama_update_channel", "debug_api_enabled",
})
_NEXT_LOAD_SCHEMA_KEYS = frozenset({
    "vision_mmproj_always", "duo_coder_always_vision",
})


def _infer_setting_type(key: str, default_val: object) -> tuple[str, bool]:
    if default_val is None:
        return _NULLABLE_TYPE_OVERRIDES.get(key, "null"), True
    if isinstance(default_val, bool):
        return "bool", False
    if isinstance(default_val, int):
        return "int", False
    if isinstance(default_val, float):
        return "float", False
    if isinstance(default_val, str):
        return "str", False
    if isinstance(default_val, list):
        return "list", False
    if isinstance(default_val, dict):
        return "object", False
    return type(default_val).__name__, False


def _infer_setting_group(key: str) -> str:
    for prefixes, group in _SCHEMA_GROUP_RULES:
        if any(key == p or key.startswith(p) for p in prefixes):
            return group
    return "general"


def _infer_apply_mode(key: str, group: str) -> str:
    if key in _RESTART_SCHEMA_KEYS:
        return "restart"
    if group == "engine" or key.startswith(("llama_", "moe_")) or key in _NEXT_LOAD_SCHEMA_KEYS:
        return "next_load"
    return "live"


def _load_schema_descriptions() -> dict[str, str]:
    try:
        from deploy.gen_settings_docs import _clean_note, _comments
        return {k: _clean_note(v) for k, v in _comments().items() if v}
    except (ImportError, OSError):
        # Release archives omit deploy/gen_settings_docs.py; parse inline comments directly.
        defaults_path = Path(__file__).resolve().parent.parent / "settings_defaults.py"
        if not defaults_path.is_file():
            return {}
        out: dict[str, str] = {}
        in_defaults = False
        for line in defaults_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("DEFAULT_SETTINGS = {"):
                in_defaults = True
                continue
            if in_defaults and line.startswith("}"):
                break
            if not in_defaults:
                continue
            m = re.match(r'\s*"([a-z0-9_]+)":\s*[^#]*,\s*(?:#\s*(.*))?$', line)
            if m and m.group(2):
                out[m.group(1)] = m.group(2).strip()
        return out


def build_settings_schema() -> dict:
    """Build structured schema metadata for every user-configurable DEFAULT_SETTINGS key."""
    descriptions = _load_schema_descriptions()
    entries: list[dict] = []
    seen_groups: list[str] = []
    seen_group_set: set[str] = set()

    for key in sorted(DEFAULT_SETTINGS):
        if key in SETTINGS_SCHEMA_EXCLUDED_KEYS:
            continue
        default_val = DEFAULT_SETTINGS[key]
        cur_val = settings.get(key, default_val)
        if key == "mcp_servers":
            cur_val = _redact_mcp_servers(cur_val)
        kind, nullable = _infer_setting_type(key, default_val)
        meta = SETTINGS_UI_META.get(key, {})
        group = str(meta.get("group") or _infer_setting_group(key))
        apply_mode = str(meta.get("apply_mode") or _infer_apply_mode(key, group))
        if group not in seen_group_set:
            seen_group_set.add(group)
            seen_groups.append(group)
        entries.append({
            "key": key,
            "type": kind,
            "nullable": nullable,
            "default": copy.deepcopy(default_val),
            "value": copy.deepcopy(cur_val),
            "description": descriptions.get(key, ""),
            "group": group,
            "apply_mode": apply_mode,
            "choices": list(meta["choices"]) if "choices" in meta else None,
            "min": meta.get("min"),
            "max": meta.get("max"),
            "step": meta.get("step"),
        })

    return {
        "ok": True,
        "count": len(entries),
        "groups": sorted(seen_groups),
        "entries": entries,
        "settings_rev": int(_settings_rev),
    }


@router.get("/settings/schema")
async def get_settings_schema():
    """Return structured schema entries for all user-configurable settings."""
    return build_settings_schema()


def _recommended_card_temperature(model: str, role_cfg: dict) -> float | None:
    """Model-recommended card temperature from model_configs/models/*.json.

    Picks the mode block matching the model's thinking capability and the
    card's current thinking flag; falls back to whichever mode exists.
    Returns None when the model has no registry recommendation."""
    try:
        from model_configs.models_registry import (
            get_capabilities as _reg_caps,
            get_sampling as _reg_sampling,
        )
        _sampling = _reg_sampling(model)
        _model_thinks = bool((_reg_caps(model) or {}).get("thinking", False))
    except (ImportError, AttributeError, OSError, TypeError):
        return None
    if not isinstance(_sampling, dict) or not _sampling:
        return None
    _mode = "thinking" if _model_thinks and role_cfg.get("thinking") else "non_thinking"
    _block = _sampling.get(_mode)
    if not isinstance(_block, dict):
        _block = next((v for v in _sampling.values() if isinstance(v, dict)), None)
    if not isinstance(_block, dict):
        return None
    _raw_temp = _block.get("temperature")
    if _raw_temp is None:
        # float(None) raised TypeError -> return None; explicit guard, same result.
        return None
    try:
        _temp = float(_raw_temp)
    except (TypeError, ValueError):
        return None
    return _temp


def _sync_recommended_sampling(settings: dict, roles: list[str]) -> None:
    """On an explicit MODEL change, adopt the model's recommended card
    temperature — but only where the user has not set their own (marker).
    The model itself, ctx and thinking are never touched."""
    _user_sampling = settings.get("agents_user_sampling_choice")
    _user_sampling = _user_sampling if isinstance(_user_sampling, dict) else {}
    _agents = settings.setdefault("agents", {})
    for _role in roles:
        _cfg = _agents.get(_role)
        if not isinstance(_cfg, dict):
            continue
        if "temperature" in (_user_sampling.get(_role) or set()):
            continue  # user customized the card temperature — it wins
        _rec = _recommended_card_temperature(str(_cfg.get("model") or ""), _cfg)
        if _rec is not None and float(_cfg.get("temperature") or 0) != _rec:
            _cfg["temperature"] = _rec


_GET_ONLY_SETTINGS = frozenset({
    "_registry", "_safe_profile_state", "settings_rev", "git_token_saved",
    # derived at GET time (P2, audit 2026-11-03): echoing them back used to
    # persist derived state and pull it into presets.
    "moe_expert_defaults", "moe_autodetect", "moe_pins", "ctx_defaults",
})


_RUNTIME_UNDERSCORE_SETTINGS = frozenset({
    "_model_sampling_overrides",
    "_model_profiles",
})


def _strip_get_only_settings(data: dict) -> dict:
    """B6 (audit 2026-11-03): never persist keys that GET /settings injects.

    GET adds runtime state (_registry, _safe_profile_state, settings_rev,
    git_token_saved); a client echoing the GET body back wrote those into
    settings.json. Private (underscore) keys are dropped generically.
    """
    return {k: v for k, v in (data or {}).items()
            if k not in _GET_ONLY_SETTINGS
            # A real setting may be underscore-prefixed
            # (_thinking_before_chunking is read/written by the UI).
            and (
                not str(k).startswith("_")
                or k in DEFAULT_SETTINGS
                or k in _RUNTIME_UNDERSCORE_SETTINGS
            )}


@router.post("/settings")
async def post_settings(req: Request):
    global _settings_rev
    data = await req.json()
    # UI-REV STALE-TAB GUARD (2026-09-09): a patch from a stale tab (snapshot
    # predates newer changes) must not silently revert the compression keys.
    _stripped_keys: list = []
    if isinstance(data, dict):
        _ui_rev = data.pop("ui_rev", None)
        _settings_force = bool(data.pop("settings_force", False))
    else:
        _ui_rev, _settings_force = None, False
    if not _settings_force:
        try:
            # int(None) raised TypeError -> -1; explicit guard, same result.
            _ui_rev_i = int(_ui_rev) if _ui_rev is not None else -1
        except (TypeError, ValueError):
            _ui_rev_i = -1
        if 0 <= _ui_rev_i < _settings_rev:
            _touched = _UI_REV_PROTECTED_KEYS.intersection(data.keys())
            if _touched:
                logger.warning(
                    "[SETTINGS-GUARD] stale ui_rev %s < %s — protected keys stripped: %s "
                    "(allow with \"settings_force\": true)",
                    _ui_rev_i, _settings_rev, sorted(_touched),
                )
                for _k in _touched:
                    data.pop(_k)
                _stripped_keys = sorted(_touched)
    if "ctx_overrides" in data and not isinstance(data.get("ctx_overrides"), dict):
        return JSONResponse({"error": "ctx_overrides must be an object"}, status_code=400)
    # the whole workspace chain was crippled (follow-up ran on repo root).
    if "workspace" in data:
        _ws_raw = str(data.get("workspace") or "").strip()
        if _ws_raw and not Path(_ws_raw).exists():
            logger.warning(
                "[SETTINGS-GUARD] workspace '%s' does not exist - rejected "
                "(previous value stays active)", _ws_raw,
            )
            return JSONResponse({
                "error": f"Workspace path does not exist: {_ws_raw}",
            }, status_code=400)
        data["workspace"] = _ws_raw  # trim durchreichen (leer = Env/Default)
    _prev_direct_model = str(
        ((settings.get("agents") or {}).get("direct") or {}).get("model") or ""
    ).strip()
    _model_changed_roles: list[str] = []  # set inside the agents block below
    if "agents" in data:
        _incoming_agents = data.pop("agents")
        # EXPLICIT-MODEL-CHOICE (2026-09-10): a model sent by the UI is a user
        # decision — record it so apply_safe_profile_policy never stomps it,
        # even when the choice equals the shipped default (CARDS-WIN blind spot).
        _model_choice = settings.setdefault("agents_user_model_choice", {})
        if not isinstance(_model_choice, dict):
            _model_choice = {}
            settings["agents_user_model_choice"] = _model_choice
        _sampling_choice = settings.setdefault("agents_user_sampling_choice", {})
        if not isinstance(_sampling_choice, dict):
            _sampling_choice = {}
            settings["agents_user_sampling_choice"] = _sampling_choice
        if isinstance(_incoming_agents, dict):
            for _ak, _av in _incoming_agents.items():
                if isinstance(_av, dict) and str(_av.get("model") or "").strip():
                    _model_choice[_ak] = str(_av["model"]).strip()
                    _model_changed_roles.append(_ak)
                if isinstance(_av, dict) and "temperature" in _av:
                    _sc_list = _sampling_choice.setdefault(_ak, [])
                    if _av.get("temperature") is None:
                        if "temperature" in _sc_list:
                            _sc_list.remove("temperature")
                    elif "temperature" not in _sc_list:
                        _sc_list.append("temperature")
        _sa = settings.setdefault("agents", {})
        if isinstance(_incoming_agents, dict):
            for _ak, _av in _incoming_agents.items():
                if _ak == "duo_coder" and isinstance(_av, dict):
                    try:
                        _in_mt = int(_av.get("max_tokens") or 0)
                    except Exception:
                        _in_mt = 0
                    try:
                        _cur_mt = int((settings.get("agents", {}).get("duo_coder") or {}).get("max_tokens") or 0)
                    except Exception:
                        _cur_mt = 0
                    # 20.08.-bug (stale tab POSTs a cached snapshot, 8000→2800)
                    #   external clients.)
                    if 0 < _in_mt < _cur_mt and not bool(data.get("agents_force")):
                        logger.warning(
                            "[SETTINGS-GUARD] duo_coder.max_tokens decrease %d→%d ignored "
                            "(stale protection; explicitly allow with \"agents_force\": true)",
                            _cur_mt, _in_mt,
                        )
                        _av = dict(_av)
                        _av.pop("max_tokens", None)
                if isinstance(_av, dict) and isinstance(_sa.get(_ak), dict):
                    _sa[_ak].update(_av)
                else:
                    _sa[_ak] = _av
        _populate_default_agents(_sa)
    _DEEP_MERGE_KEYS = {"soul_evolve_agent", "intent_agent", "exploration_agent"}
    for _dmk in _DEEP_MERGE_KEYS:
        if _dmk in data:
            _incoming = data.pop(_dmk)
            _existing = settings.setdefault(_dmk, {})
            if isinstance(_incoming, dict) and isinstance(_existing, dict):
                for _dk, _dv in _incoming.items():
                    if _dmk != "soul_evolve_agent" and _dk == "model" and _dv == "" and _existing.get("model"):
                        _inc_enabled = _incoming.get("enabled")
                        if _inc_enabled is not False:
                            continue
                    _existing[_dk] = _dv
            else:
                settings[_dmk] = _incoming
    # ACTIVE-PRESET GUARD (2026-09-30): active_preset is owned exclusively by
    # preset load/delete and the startup auto-load — verified per grep that no
    # legitimate writer POSTs it (frontend only reads it, app.js:901). A
    # settings blob carrying the key must not clear or rename the active
    # preset behind the user's back.
    data.pop("active_preset", None)
    # H-audit: GET masks the token as '****' - a stale-tab snapshot POSTs
    # it back and settings.update() would silently DESTROY the credential
    # while has_credentials() keeps reporting True.
    if isinstance(data, dict) and data.get("git_token") in ("****", "***", ""):
        # mask echo or empty field: never destroy the stored credential
        data.pop("git_token", None)
    if isinstance(data, dict) and isinstance(data.get("mcp_servers"), list):
        data["mcp_servers"] = _restore_redacted_mcp_servers(
            data["mcp_servers"], settings.get("mcp_servers")
        )
    data = _strip_get_only_settings(data)
    if "direct" in _model_changed_roles and "direct_model" not in data:
        settings.pop("direct_model", None)
    _ctx_ov_roles = (settings.get("ctx_overrides") or {}).get("roles")
    if isinstance(_ctx_ov_roles, dict):
        if "duo_coder_ctx_agentic" in data or "duo_coder_ctx_normal" in data:
            _ctx_ov_roles.pop("duo_coder", None)
        if "duo_critic_ctx" in data:
            _ctx_ov_roles.pop("duo_critic", None)
    settings.update(data)
    # UI-SAVE-FLAG-SYNC: sync all runtime flags (ubatch, cache, checkpoints,
    # vram_preflight_gate, KV cache, spec decode, sampling, compression) live.
    if _RUNTIME_FLAG_KEYS & set(data):
        _sync_runtime_flags("settings")
    _xa = settings.get("exploration_agent")
    if isinstance(_xa, dict) and _xa.get("enabled") and not (_xa.get("model") or "").strip():
        _xa["model"] = DEFAULT_SETTINGS["exploration_agent"]["model"]
    if _model_changed_roles:
        _sync_recommended_sampling(settings, _model_changed_roles)
    _refresh_safe_profile_policy()
    apply_settings_to_pipeline(settings)
    if "direct" in _model_changed_roles:
        _new_direct_model = str(
            ((settings.get("agents") or {}).get("direct") or {}).get("model") or ""
        ).strip()
        await _unpin_old_direct_model_if_changed(_prev_direct_model, _new_direct_model)
    elif data.get("pin_direct_after_response") is False:
        try:
            from core.direct_runner import unpin_previous_direct_model as _unpin_prev_direct
            await _unpin_prev_direct("", evict=False)
        except (ImportError, AttributeError, RuntimeError, OSError, ValueError) as _e:
            logger.debug("[settings] direct pin release on toggle off skipped: %s", _e)
    if _state._WEBSEARCH_AVAILABLE and any(k in data for k in (
            "duo_websearch_enabled", "pipeline_websearch_enabled",
            "searxng_host", "searxng_engines", "searxng_language")):
        _ws_configure(
            host=settings.get("searxng_host", "http://localhost:8888"),
            enabled=settings.get("pipeline_websearch_enabled", False)
            or settings.get("duo_websearch_enabled", False),
            engines=settings.get("searxng_engines"),
            language=settings.get("searxng_language"),
        )
    try:
        await asyncio.to_thread(save_settings, copy.deepcopy(settings))
    except Exception as e:
        logger.warning("[settings] save_settings failed: %s", e, exc_info=True)
        return JSONResponse({"error": f"Save failed: {str(e)[:120]}"}, status_code=500)
    # RUNTIME-SYNC (2026-11-02): settings that the LOADER reads were only
    # pushed to the backend modules at startup - a changed moe_cpu_experts or
    # llama_mlock therefore did nothing until the next restart (live: the MoE
    # override stayed at the profile value for every run).
    try:
        from backend.runtime_sync import sync_loader_settings as _resync
        _resync(settings)
    except (ImportError, AttributeError) as _rs_err:
        logger.debug("[settings] backend runtime sync skipped: %s", _rs_err)
    _settings_rev += 1
    # DECIDER-UNLOAD (2026-10-22, user request): switching routing_decider
    # to off must free the encoder model (~1.8 GB RAM) immediately, not at
    # the next engine restart. Best-effort - a failure here never fails
    # the settings save.
    try:
        from routing.decider import react_to_routing_settings as _dec_react
        await asyncio.to_thread(_dec_react, settings)
    except (ImportError, TypeError, ValueError, OSError) as e:
        logger.debug("[settings] decider react skipped: %s", e)
    return {"ok": True, "settings_rev": int(_settings_rev),
            "stripped": _stripped_keys}


async def _unpin_old_direct_model_if_changed(old_model: str, new_model: str) -> None:
    """Release and unload a pinned previous Direct model when the user switches Direct models."""
    old_clean = str(old_model or "").strip()
    new_clean = str(new_model or "").strip()
    if not old_clean or old_clean == new_clean:
        return
    try:
        _judge_mdl = str((registry_all() or {}).get("judge") or "").strip()
    except (TypeError, AttributeError):
        _judge_mdl = ""
    if _judge_mdl and old_clean == _judge_mdl:
        return
    try:
        from core import direct_runner as _dr
        if getattr(_dr, "_LAST_DIRECT_PINNED_MODEL", "") == old_clean:
            await _dr.unpin_previous_direct_model(new_clean, evict=True)
            return
        from backend.llama_client import manager as _mgr
        evicted = await _dr._release_direct_pinned_slot(
            _mgr, old_clean, evict=True, require_pinned=False,
        )
        logger.info(
            "[settings] %s previous direct model %r after switch to %r",
            "unloaded" if evicted else "unpinned",
            old_clean, new_clean,
        )
    except (ImportError, AttributeError, RuntimeError, OSError, ValueError) as _e:
        logger.debug("[settings] unpin previous direct model %r skipped: %s", old_clean, _e)


@router.post("/settings/agent")
async def set_agent(req: Request):
    d = await req.json()
    key = d.get("agent")
    if not _state.pipeline or key not in _state.pipeline.agents:
        return JSONResponse({"error": f"Unknown: {key}"}, status_code=400)
    _model = d.get("model") or ""
    _prev_direct_model = (
        str(((settings.get("agents") or {}).get("direct") or {}).get("model") or "").strip()
        if key == "direct"
        else ""
    )
    try:
        if _model:
            registry_set(key, _model)
            settings.setdefault("agents", {}).setdefault(key, {})["model"] = _model
            if key == "direct":
                settings.pop("direct_model", None)
            _mc = settings.setdefault("agents_user_model_choice", {})
            if isinstance(_mc, dict):
                _mc[key] = _model
            _excluded = settings.setdefault("automap_excluded", [])
            if key not in _excluded:
                _excluded.append(key)
        elif key == "duo_critic" and "model" in d:
            registry_set(key, "")
            settings.setdefault("agents", {}).setdefault(key, {})["model"] = ""
            _mc = settings.get("agents_user_model_choice")
            if isinstance(_mc, dict):
                _mc.pop(key, None)
        if "temperature" in d:
            _sc = settings.setdefault("agents_user_sampling_choice", {})
            _sc_list = _sc.setdefault(key, []) if isinstance(_sc, dict) else []
            if d["temperature"] is None:
                if "temperature" in _sc_list:
                    _sc_list.remove("temperature")
                _state.pipeline.agents[key].temperature = None
                settings.setdefault("agents", {}).setdefault(key, {})["temperature"] = None
            else:
                if isinstance(_sc, dict) and "temperature" not in _sc_list:
                    _sc_list.append("temperature")
                v = float(d["temperature"])
                _state.pipeline.agents[key].temperature = v
                settings.setdefault("agents", {}).setdefault(key, {})["temperature"] = v
        elif _model:
            _sync_recommended_sampling(settings, [key])
            _synced_t = (settings.get("agents", {}).get(key) or {}).get("temperature")
            if _synced_t is not None:
                _state.pipeline.agents[key].temperature = float(_synced_t)
        if "max_tokens" in d:
            if d["max_tokens"] is None:
                raise ValueError("max_tokens missing")
            v = int(d["max_tokens"])
            _state.pipeline.agents[key].max_tokens = v
            settings.setdefault("agents", {}).setdefault(key, {})["max_tokens"] = v
        if "thinking" in d:
            _t = None if d["thinking"] is None else bool(d["thinking"])
            _state.pipeline.agents[key].thinking = _t
            settings.setdefault("agents", {}).setdefault(key, {})["thinking"] = _t
        if "thinking_budget" in d:
            if d["thinking_budget"] is None:
                raise ValueError("thinking_budget missing")
            _tb = int(d["thinking_budget"])
            _state.pipeline.agents[key].thinking_budget = _tb
            settings.setdefault("agents", {}).setdefault(key, {})["thinking_budget"] = _tb
        if "ctx" in d:
            if d["ctx"] is None:
                raise ValueError("ctx missing")
            _ctx = int(d["ctx"])
            _co = settings.setdefault("ctx_overrides", {})
            _co.setdefault("roles", {})[key] = _ctx if _ctx > 0 else None
            if _ctx <= 0:
                _co["roles"].pop(key, None)
            elif key == "duo_coder":
                settings["duo_coder_ctx_agentic"] = _ctx
                settings["duo_coder_ctx_normal"] = _ctx
            elif key == "duo_critic":
                settings["duo_critic_ctx"] = _ctx
    except Exception as e:
        logger.warning("[settings/agent] configuration failed (%s): %s", key, e, exc_info=True)
        return JSONResponse({"error": f"Configuration failed: {str(e)[:120]}"}, status_code=500)
    # NO model load on save (user decision 2026-08-19): only change the
    # settings, do not load. The model is loaded lazily on first real use
    # (pipeline/duo runners call ensure_loaded).
    if key == "direct" and _model:
        await _unpin_old_direct_model_if_changed(_prev_direct_model, _model)
    try:
        await asyncio.to_thread(save_settings, copy.deepcopy(settings))
    except Exception as e:
        logger.warning("[settings/agent] save_settings failed: %s", e, exc_info=True)
        return JSONResponse({"error": f"Save failed: {str(e)[:120]}"}, status_code=500)
    return {"ok": True}


@router.post("/settings/all_model")
async def set_all_model(req: Request):
    d = await req.json()
    m = d.get("model", "")
    if not m:
        return JSONResponse({"error": "model missing"}, status_code=400)
    if _state.pipeline:
        for key in list(_state.pipeline.agents.keys()):
            registry_set(key, m)
    _mc = settings.setdefault("agents_user_model_choice", {})
    settings.pop("direct_model", None)
    for key in settings["agents"]:
        settings["agents"][key]["model"] = m
        if isinstance(_mc, dict):
            _mc[key] = m
    _sync_recommended_sampling(settings, list(settings.get("agents", {}).keys()))
    _sync_pipeline_agents()
    try:
        await asyncio.to_thread(save_settings, copy.deepcopy(settings))
    except Exception as e:
        logger.warning("[settings/all_model] save_settings failed: %s", e, exc_info=True)
        return JSONResponse({"error": f"Save failed: {str(e)[:120]}"}, status_code=500)
    return {"ok": True}


@router.post("/moe-pin")
async def set_moe_pin(req: Request):
    """MOE-PIN (2026-10-14): the model-config answer to "is this a MoE?" —
    written into the model's own profile JSON (key "moe"). true/false pins it
    over every name heuristic (30B+ autodetect included), null removes the
    pin (back to auto). Takes effect on the next model load."""
    d = await req.json()
    m = str(d.get("model", "")).strip()
    if not m:
        return JSONResponse({"ok": False, "error": "model missing"}, status_code=400)
    moe = d.get("moe", None)
    if moe is not None and not isinstance(moe, bool):
        return JSONResponse({"ok": False, "error": "moe must be true, false or null"}, status_code=400)
    try:
        from model_configs.models_registry import set_profile_key
        _path = await asyncio.to_thread(set_profile_key, m, "moe", moe)
    except Exception as e:
        logger.warning("[config/moe-pin] failed for %s: %s", m, e, exc_info=True)
        return JSONResponse({"ok": False, "error": str(e)[:120]}, status_code=500)
    if _path is None:
        return JSONResponse(
            {"ok": False,
             "error": f"no model profile found for '{m}' — open its model config once so the profile JSON exists"},
            status_code=404)
    return {"ok": True, "model": m, "moe": moe}


_MODEL_PROFILE_KEY_SCHEMA: dict[str, str] = {
    **dict.fromkeys(("moe", "jinja", "distilled", "mtp", "vision_preprocessing"), "bool"),
    **dict.fromkeys((
        "num_ctx", "num_ctx_analyst", "num_ctx_duo_coder", "num_ctx_vision",
        "moe_cpu_experts", "gpu_layers", "ubatch", "thinking_budget",
        "kv_bytes_per_token", "dspark_draft_n_max", "mtp_draft_n_max",
    ), "int"),
    **dict.fromkeys(("vram_gb_override", "mtp_draft_p_min"), "float"),
    **dict.fromkeys((
        "chat_template", "mmproj_filename", "dspark_draft_filename",
        "kv_cache_type", "load_mode",
    ), "str"),
    "reasoning": "reasoning",
    "capabilities": "dict",
    "sampling": "dict",
    "extra_args": "str_list",
}

_SAMPLING_NUMERIC_BOUNDS: dict[str, tuple[float, float, bool]] = {
    "temperature": (0.0, 2.0, False),
    "top_p": (0.0, 1.0, False),
    "top_k": (0.0, 1000.0, True),
    "min_p": (0.0, 1.0, False),
    "presence_penalty": (-2.0, 2.0, False),
    "repetition_penalty": (0.5, 2.0, False),
    "repeat_penalty": (0.5, 2.0, False),
    "seed": (-1.0, 2147483647.0, True),
    "thinking_budget": (0.0, 131072.0, True),
}
_SAMPLING_BOOL_KEYS = frozenset({"preserve_thinking", "cache_prompt"})


def _validate_sampling_block(block: dict, prefix: str = "sampling") -> str | None:
    for k, v in block.items():
        if v is None:
            continue
        if k in _SAMPLING_BOOL_KEYS:
            if not isinstance(v, bool):
                return f"{prefix}.{k} requires boolean or null"
        elif k in _SAMPLING_NUMERIC_BOUNDS:
            lo, hi, is_int = _SAMPLING_NUMERIC_BOUNDS[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                return f"{prefix}.{k} requires finite number or null"
            if is_int and not isinstance(v, int):
                return f"{prefix}.{k} requires integer or null"
            if float(v) < lo or float(v) > hi:
                return f"{prefix}.{k} must be between {lo:g} and {hi:g}"
        elif k == "reasoning_effort":
            if not isinstance(v, str) or v.strip().lower() not in (
                "auto", "none", "minimal", "low", "medium", "high", "xhigh", "max"
            ):
                return f"{prefix}.{k} must be one of auto/none/minimal/low/medium/high/xhigh/max or null"
        elif isinstance(v, dict):
            err = _validate_sampling_block(v, f"{prefix}.{k}")
            if err:
                return err
        else:
            return f"unsupported sampling key '{prefix}.{k}'"
    return None


def _validate_model_profile_key(key: str, val) -> str | None:
    """Validate key against _MODEL_PROFILE_KEY_SCHEMA; return error string or None."""
    if key == "model":
        return "cannot overwrite reserved identity key 'model'"
    expected = _MODEL_PROFILE_KEY_SCHEMA.get(key)
    if expected is None:
        return f"unsupported profile key '{key}'"
    if val is None:
        return None
    if expected == "bool" and not isinstance(val, bool):
        return f"key '{key}' requires a boolean or null"
    if expected == "int" and (isinstance(val, bool) or not isinstance(val, int)):
        return f"key '{key}' requires an integer or null"
    if expected == "float" and (
        isinstance(val, bool)
        or not isinstance(val, (int, float))
        or not math.isfinite(float(val))
    ):
        return f"key '{key}' requires a finite number or null"
    if expected == "str":
        if not isinstance(val, str):
            return f"key '{key}' requires a string or null"
        if key in ("mmproj_filename", "dspark_draft_filename", "chat_template"):
            if "/" in val or "\\" in val or ".." in val:
                return f"key '{key}' must be a filename, not a path"
        if key == "load_mode" and val not in ("auto", "none", "mmap", "mmap+mlock", ""):
            return "key 'load_mode' must be 'auto', 'none', 'mmap', 'mmap+mlock', or null"
    if expected == "str_list":
        toks = val.split() if isinstance(val, str) else val
        if not isinstance(toks, list) or not all(isinstance(x, str) for x in toks):
            return f"key '{key}' requires a list of strings, string, or null"
        if any("\n" in x or "\r" in x for x in toks):
            return f"key '{key}' items cannot contain newlines"
    if expected == "reasoning" and val not in ("on", "off"):
        return "key 'reasoning' must be 'on', 'off', or null"
    if expected == "dict":
        if not isinstance(val, dict):
            return f"key '{key}' requires an object or null"
        if key == "capabilities":
            for ck, cv in val.items():
                if ck not in ("thinking", "vision", "tool_call"):
                    return f"unsupported capability '{ck}'"
                if cv is not None and not isinstance(cv, bool):
                    return f"capabilities.{ck} requires boolean or null"
        elif key == "sampling":
            return _validate_sampling_block(val)
    return None


@router.post("/model-profile/key")
async def set_model_profile_key_endpoint(req: Request):
    """Patch or remove an allowed key in model_configs/models/<name>.json."""
    d = await req.json()
    m = str(d.get("model", "")).strip()
    key = str(d.get("key", "")).strip()
    if not m or not key:
        return JSONResponse({"ok": False, "error": "model and key required"}, status_code=400)
    val = d.get("value")
    err = _validate_model_profile_key(key, val)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    try:
        from model_configs.models_registry import set_profile_key
        _path = await asyncio.to_thread(set_profile_key, m, key, val)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError) as e:
        logger.warning("[config/model-profile/key] failed for %s (%s): %s", m, key, e, exc_info=True)
        return JSONResponse({"ok": False, "error": str(e)[:120]}, status_code=500)
    if _path is None:
        return JSONResponse({"ok": False, "error": f"no profile found for '{m}'"}, status_code=404)
    return {"ok": True, "model": m, "key": key, "value": val}


def _public_gguf_meta(model: str) -> dict:
    try:
        from backend.gguf_meta import read_gguf_metadata
        meta = dict(read_gguf_metadata(model) or {})
        meta.pop("chat_template", None)
        return meta
    except (ImportError, OSError, ValueError, TypeError) as exc:
        logger.debug("[config/gguf-meta] failed for %s: %s", model, exc)
        return {}


def _gguf_seed_defaults(meta: dict) -> dict:
    if not meta:
        return {}
    seed: dict = {
        "capabilities": {
            "thinking": bool(meta.get("has_thinking")),
            "vision": bool(meta.get("has_vision")),
            "tool_call": bool(meta.get("has_chat_template")),
        },
        "jinja": bool(meta.get("has_chat_template")),
        "reasoning": "on" if meta.get("has_thinking") else "off",
        "moe": bool(meta.get("is_moe")),
    }
    ctx_len = meta.get("context_length")
    if isinstance(ctx_len, int) and ctx_len > 0:
        seed["num_ctx"] = min(ctx_len, 32768)
    if meta.get("kv_bytes_per_token_q4"):
        seed["kv_bytes_per_token"] = int(meta["kv_bytes_per_token_q4"])
    return seed


@router.get("/gguf-meta")
async def get_gguf_meta_endpoint(model: str = ""):
    m = (model or "").strip()
    if not m:
        return JSONResponse({"ok": False, "error": "model parameter required"}, status_code=400)
    meta = await asyncio.to_thread(_public_gguf_meta, m)
    return {"ok": True, "model": m, "meta": meta}


def _build_model_profile_payload(model: str) -> dict:
    from model_configs.models_registry import find_profile_file, get_profile
    path = find_profile_file(model)
    prof = get_profile(model)
    gguf = _public_gguf_meta(model)
    return {
        "ok": True,
        "model": model,
        "exists": path is not None,
        "file": path.name if path else None,
        "profile": prof,
        "gguf": gguf,
    }


@router.get("/model-profile")
async def get_model_profile_query(model: str = ""):
    m = (model or "").strip()
    if not m:
        from model_configs.models_registry import refresh, _ensure_cache
        refresh()
        return {"ok": True, "models": _ensure_cache()}
    return await asyncio.to_thread(_build_model_profile_payload, m)


@router.get("/model-profile/{model:path}")
async def get_model_profile_path(model: str):
    m = (model or "").strip()
    if not m:
        return JSONResponse({"ok": False, "error": "model required"}, status_code=400)
    return await asyncio.to_thread(_build_model_profile_payload, m)


@router.post("/model-profile")
async def upsert_model_profile_endpoint(req: Request):
    """Create or update a per-model defaults JSON file (model_configs/models/<name>.json)."""
    d = await req.json()
    m = str(d.get("model", "")).strip()
    if not m:
        return JSONResponse({"ok": False, "error": "model required"}, status_code=400)
    raw_patch = d.get("patch") if isinstance(d.get("patch"), dict) else {
        k: v for k, v in d.items() if k not in ("model", "seed_from_gguf")
    }
    if not isinstance(raw_patch, dict):
        return JSONResponse({"ok": False, "error": "patch object required"}, status_code=400)
    clean_patch: dict = {}
    for k, v in raw_patch.items():
        err = _validate_model_profile_key(str(k), v)
        if err:
            return JSONResponse({"ok": False, "error": err}, status_code=400)
        if k == "extra_args" and isinstance(v, str):
            clean_patch[k] = [tok for tok in v.split() if tok]
        else:
            clean_patch[k] = v
    try:
        from model_configs.models_registry import find_profile_file, get_profile, upsert_profile
        existing = find_profile_file(m)
        final_patch = clean_patch
        if existing is None and bool(d.get("seed_from_gguf", False)):
            meta = await asyncio.to_thread(_public_gguf_meta, m)
            seeded = _gguf_seed_defaults(meta)
            seeded.update(clean_patch)
            final_patch = seeded
        p = await asyncio.to_thread(upsert_profile, m, final_patch)
        return {
            "ok": True,
            "model": m,
            "file": p.name,
            "profile": get_profile(m),
        }
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError) as e:
        logger.warning("[config/model-profile] failed for %s: %s", m, e, exc_info=True)
        return JSONResponse({"ok": False, "error": str(e)[:120]}, status_code=500)


@router.get("/registry")
async def get_registry():
    return registry_all()


@router.get("/presets/active")
async def get_presets_active():
    """The preset-visibility state for the UI (2026-11-01).

    active_preset persists even when the startup auto-load skipped the
    re-apply (settings edited after apply) — the UI must be able to show
    BOTH the name and the staleness, or "after a startup" you cannot tell
    whether you are inside a preset."""
    name = str(settings.get("active_preset", "") or "")
    applied_at = settings.get("preset_applied_at")
    saved_at = settings.get("settings_saved_at")
    stale = preset_autoload_stale(applied_at, saved_at) if name else False
    return {
        "name": name,
        "applied_at": applied_at or "",
        "stale": stale,
        "autoload_skipped": bool(stale),
        "message": (
            f"'{name}' was applied, but settings were edited afterwards — "
            f"the edited settings win." if stale and name else
            (f"'{name}' is applied." if name else "No preset active."))
    }


@router.get("/presets")
async def get_presets():
    return load_presets()


@router.post("/presets/{name}")
async def save_preset_ep(name: str, req: Request):
    try:
        _validate_preset_name(name)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    presets = load_presets()
    try:
        _body = await req.json()
    except Exception:
        _body = {}
    if not isinstance(_body, dict):
        _body = {}
    presets[name] = _preset_snapshot(extra=_body)
    if _state.pipeline:
        # PROMPT-CLOBBER-FIX (2026-10-28, preset audit): src_preset was always
        # None, so every re-save of an existing preset overwrote its saved
        # custom prompts with the built-in base prompts. Re-saving a preset
        # must copy FROM itself; only a NEW preset starts from the base.
        try:
            copy_prompts_to_preset(
                src_preset=name, dst_preset=name,
                agent_keys=list(_state.pipeline.agents.keys()), base_prompts=PROMPTS
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    save_presets(presets)
    return {"ok": True, "name": name}


@router.post("/presets/{name}/load")
async def load_preset_ep(name: str):
    try:
        _validate_preset_name(name)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    _warns: list = []
    if not await _apply_preset_internal(name, warnings=_warns):
        return JSONResponse({"error": "Not found"}, status_code=404)
    # the pipeline is what a run actually reads - refresh it, or the next run
    # (and the next preset snapshot) keeps the pre-load budgets.
    _synced = _sync_pipeline_agents()
    try:
        if _state.pipeline:
            judge_agent = _state.pipeline.agents.get("judge")
            if judge_agent:
                _wm3_ctx = _get_num_ctx(judge_agent.model)
                await _bk_load(judge_agent.model, keep_alive="10m", num_ctx=_wm3_ctx or None)
    except Exception:
        pass
    return {
        "ok": True,
        "name": name,
        # Summary so the UI can show what the preset actually restored —
        # "planner off + chunking off" used to skip the planner phase silently.
        "chunking": bool(settings.get("duo_chunking", False)),
        "planner_enabled": bool(settings.get("duo_planner_enabled", False)),
        "planner_model": settings.get("duo_planner_model") or "",
        # PRESET-WORKSPACE-GUARD: surfaced in the load alert, not only in logs
        "warnings": _warns,
        "pipeline_fields": _synced,
    }


@router.delete("/presets/{name}")
async def del_preset(name: str):
    try:
        _validate_preset_name(name)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    presets = load_presets()
    if name in presets:
        del presets[name]
        try:
            delete_custom_prompts_for_preset(name)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        save_presets(presets)
        # PRESET-HOUSEKEEPING (2026-09-30): deleting the ACTIVE preset must
        # clear active_preset (and persist), otherwise the UI shows it as
        # loaded and the next startup auto-loads a preset that no longer
        # exists.
        if str(settings.get("active_preset") or "") == name:
            settings["active_preset"] = None
            await asyncio.to_thread(save_settings, copy.deepcopy(settings))
    return {"ok": True}


# PRESET STARTUP AUTO-LOAD (2026-09-30, extracted from server.py so it is
# testable): re-apply the last explicitly loaded preset; when it no longer
# exists, CLEAR active_preset so the UI does not show a preset as active
# that was not loaded.
async def startup_preset_autoload() -> None:
    _ap_name = str(settings.get("active_preset") or "").strip()
    if not _ap_name:
        return
    # PRESET-AUTOLOAD-GUARD (2026-10-23): settings the user saved after the
    # last preset load must win — re-applying the snapshot here silently
    # reverted them on every boot. Re-load the preset explicitly to re-engage.
    if preset_autoload_stale(settings.get("preset_applied_at"),
                             settings.get("settings_saved_at")):
        logger.info(
            "[PRESET] startup auto-load skipped: settings were saved after "
            "'%s' was applied — keeping the newer settings", _ap_name)
        return
    if await _apply_preset_internal(_ap_name, persist=True):
        logger.info("[PRESET] startup auto-load: applied preset '%s'", _ap_name)
        return
    logger.warning(
        "[PRESET] startup auto-load: preset '%s' not found — clearing active_preset",
        _ap_name)
    settings["active_preset"] = None
    await asyncio.to_thread(save_settings, copy.deepcopy(settings))


@router.get("/presets/prompt-roles")
async def get_prompt_roles():
    """The prompt keys a preset can actually override (single source for the
    editor dropdown). Unknown keys used to fall back to the `direct` prompt,
    so the editor showed the wrong text for roles no runner reads."""
    return {"roles": [dict(r) for r in PRESET_PROMPT_ROLES]}


@router.get("/presets/{name}/prompts/{agent}")
async def get_prompt(name: str, agent: str):
    try:
        custom = get_custom_prompt(name, agent)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    # STRICT-FALLBACK (2026-11-01): only fall back to the role's own base
    # prompt. `PROMPTS.get(agent, PROMPTS["direct"])` made an unknown key
    # look like a real, editable prompt that was really the Direct prompt.
    default = PROMPTS.get(agent, "")
    if not default:
        for _r in PRESET_PROMPT_ROLES:
            if _r["key"] == agent:
                default = PROMPTS.get(_r["base"], "")
                break
    return {"content": custom or default, "is_custom": custom is not None}


@router.post("/presets/{name}/prompts/{agent}")
async def set_prompt(name: str, agent: str, req: Request):
    try:
        _validate_preset_name(name)
        _validate_agent_key(agent)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    try:
        d = await req.json()
    except (ValueError, TypeError):
        return JSONResponse({"ok": False, "error": "Invalid JSON"}, status_code=400)
    if not isinstance(d, dict):
        return JSONResponse({"ok": False, "error": "JSON object required"}, status_code=400)
    try:
        save_custom_prompt(name, agent, str(d.get("content", "") or ""))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True}


@router.get("/memory")
async def get_memory():
    if not _state.memory:
        return {"memories": []}
    return {
        "memories": [
            {"key": k, "value": v, "saved_at": d}
            for k, v, d in _state.memory.list_memories()
        ]
    }


@router.delete("/memory/{key}")
async def del_memory(key: str):
    if not _state.memory:
        return {"ok": False}
    return {"ok": _state.memory.forget(key)}


@router.post("/memory/clear_session")
async def clear_session(chat_id: str = Query(None, description="Optional: chat_id for ProjectState deletion")):
    if _state.memory:
        _state.memory.clear_session()
    if chat_id:
        try:
            from context.project_state import ProjectStateManager
            ProjectStateManager().delete(chat_id)
        except Exception:
            pass
    return {"ok": True}


@router.post("/memory/{key}")
async def set_memory(key: str, req: Request):
    data = await req.json()
    value = data.get("value", "").strip()
    if not key or not value:
        return JSONResponse({"error": "key and value required"}, status_code=400)
    if not _state.memory:
        return JSONResponse({"error": "Memory not available"}, status_code=500)
    try:
        _state.memory.remember(key, value)
    except Exception as e:
        return JSONResponse({"error": f"Memory error: {str(e)[:80]}"}, status_code=500)
    return {"ok": True, "key": key, "value": value}

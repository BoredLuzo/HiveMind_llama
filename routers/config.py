"""Config API-Router — Settings, Memory."""
import asyncio
import copy
import logging
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from settings import (
    load_settings, save_settings,
    load_presets, save_presets,
    get_custom_prompt, save_custom_prompt,
    delete_custom_prompts_for_preset, copy_prompts_to_preset,
    DEFAULT_AGENT_CFG, DEFAULT_SETTINGS,
    _now_iso,
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
# `_registry` is internal bookkeeping, and image_processing_mode /
# vision_agent_mode have no backend reader (the truth lives in
# vision_model.json via /vision/config).
_PRESET_NEVER_KEYS = {"git_token", "_registry", "models_dir",
                      "image_processing_mode", "vision_agent_mode",
                      # BOOKKEEPING (2026-10-28, preset audit): these three are
                      # written by the apply path itself (active_preset, both
                      # stamps). In the snapshot they froze the saving moment
                      # into every exported file and came back as stale state
                      # on load.
                      "active_preset", "settings_saved_at", "preset_applied_at"}

# PRESET-CTX-OVERLAY (2026-09-04): nur diese Keys darf der Client beim
# Preset-Save explizit uebergeben (verhindert das Race zwischen debounced
# postSettings und dem Snapshot). Alles andere bleibt Server-seitig gesnapshottet.
_PRESET_OVERLAY_KEYS = {
    "duo_coder_ctx_agentic",
    "duo_coder_ctx_normal",
    "duo_planner_ctx_target",
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
            if field not in cfg or cfg[field] is None:
                continue
            try:
                setattr(ag, field, cast(cfg[field]))
                applied += 1
            except (TypeError, ValueError):
                continue
        if "thinking" in cfg:
            ag.thinking = bool(cfg["thinking"])
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
    for key, value in p.items():
        if key in {"agents", "prompts", "active_preset"}:
            continue
        if key in _PRESET_NEVER_KEYS:
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
    _flag_keys = {"llama_cache_reuse", "llama_cache_ram_mib",
                  "llama_ctx_checkpoints", "llama_ubatch_size", "llama_mlock"}
    if _applied_keys & _flag_keys:
        try:
            import backend.llama_config as _lc_flags
            import backend.llama_server_manager as _lsm_flags
            import backend.manager_load as _ml_flags
        except ImportError as exc:
            logger.warning("[PRESET] llama flag sync skipped: %s", exc)
        else:
            _lc_flags.CACHE_REUSE = int(settings.get("llama_cache_reuse", 256) or 0)
            _lsm_flags.CACHE_REUSE = _lc_flags.CACHE_REUSE
            _lc_flags.CACHE_RAM_MIB = int(settings.get(
                "llama_cache_ram_mib", _lc_flags.CACHE_RAM_MIB))
            _lsm_flags.CACHE_RAM_MIB = _lc_flags.CACHE_RAM_MIB
            _lc_flags.CTX_CHECKPOINTS = int(settings.get(
                "llama_ctx_checkpoints", _lc_flags.CTX_CHECKPOINTS))
            _lsm_flags.CTX_CHECKPOINTS = _lc_flags.CTX_CHECKPOINTS
            _lc_flags.MLOCK_MODEL = bool(settings.get("llama_mlock", True))
            _lsm_flags.MLOCK_MODEL = _lc_flags.MLOCK_MODEL
            try:
                _lc_flags.LLAMA_UBATCH = int(settings.get("llama_ubatch_size", 256) or 256)
                # manager_load holds a by-value import from boot - sync it
                # too (same trap as the CACHE_REUSE fix, server.py:398)
                _ml_flags.LLAMA_UBATCH = _lc_flags.LLAMA_UBATCH
            except (TypeError, ValueError):
                pass
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
    # PRESET-LOAD-SYNC (2026-10-28, preset audit): a preset load used to skip
    # a live-sync that POST /settings performs, so a loaded value only took
    # effect after an engine restart: vram_preflight_gate kept the old
    # backend flag (the triple-sync below is the same one post_settings
    # does). Deliberately NOT synced here: _refresh_safe_profile_policy() —
    # it would reset agent budgets to profile defaults and defeat the merge
    # protection pinned by tests/test_preset_agent_merge.py.
    if "vram_preflight_gate" in settings:
        # SILENT-EXCEPT (2026-10-28): the imports are the only failure mode
        # (the assignments cannot raise); a missing module is logged instead
        # of swallowed - the no-new-silent-excepts guard counts this file.
        try:
            import backend.llama_config as _lc_pf
            import backend.manager_load as _ml_pf
            import backend.llama_server_manager as _lsm_pf
        except ImportError as exc:
            logger.warning("[PRESET] vram_preflight_gate sync skipped: %s", exc)
        else:
            _pf_val = bool(settings.get("vram_preflight_gate", True))
            _lc_pf.VRAM_PREFLIGHT_GATE = _pf_val
            _ml_pf.VRAM_PREFLIGHT_GATE = _pf_val
            _lsm_pf.VRAM_PREFLIGHT_GATE = _pf_val
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
        await asyncio.to_thread(save_settings, settings, saved_at=_stamp)
    registry_sync_from_pipeline()
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


@router.get("/settings")
async def get_settings():
    s = dict(settings)
    # SECURITY (UX-FIX 2026-10-07, user feedback: git fields must not reset on save): the masked '****' read as a broken tiny credential. The
    # field now arrives EMPTY with an explicit saved flag; the panel
    # shows the state as a placeholder instead of a fake value.
    _gt_saved = bool(s.get("git_token"))
    s["git_token"] = "" if _gt_saved else s.get("git_token", "")
    s["git_token_saved"] = _gt_saved
    _sa = s.setdefault("agents", {})
    for _ak, _av in DEFAULT_AGENT_CFG.items():
        _sa.setdefault(_ak, _av)
        if isinstance(_sa[_ak], dict):
            _sa[_ak].setdefault("model", _av.get("model", ""))
            _sa[_ak].setdefault("temperature", _av.get("temperature", 0.3))
            _sa[_ak].setdefault("max_tokens", _av.get("max_tokens", 400))
            _sa[_ak].setdefault("thinking", _av.get("thinking", False))
            _sa[_ak].setdefault("thinking_budget", _av.get("thinking_budget", 0))
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
            _key = str(_c).replace("-ud", "")
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
                _pk = str(_c).replace("-ud", "")
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
    try:
        _temp = float(_block.get("temperature"))
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
            _ui_rev_i = int(_ui_rev)
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
                    # explicit card sampling edit -> user value wins over any
                    # model recommendation (see _sync_recommended_sampling).
                    _sc_list = _sampling_choice.setdefault(_ak, [])
                    if "temperature" not in _sc_list:
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
        for _ak, _av in DEFAULT_AGENT_CFG.items():
            _sa.setdefault(_ak, _av)
            if isinstance(_sa[_ak], dict):
                _sa[_ak].setdefault("model", _av.get("model", ""))
                _sa[_ak].setdefault("temperature", _av.get("temperature", 0.3))
                _sa[_ak].setdefault("max_tokens", _av.get("max_tokens", 400))
                _sa[_ak].setdefault("thinking", _av.get("thinking", False))
                _sa[_ak].setdefault("thinking_budget", _av.get("thinking_budget", 0))
    _DEEP_MERGE_KEYS = {"soul_evolve_agent", "intent_agent", "exploration_agent"}
    for _dmk in _DEEP_MERGE_KEYS:
        if _dmk in data:
            _incoming = data.pop(_dmk)
            _existing = settings.setdefault(_dmk, {})
            if isinstance(_incoming, dict) and isinstance(_existing, dict):
                for _dk, _dv in _incoming.items():
                    if _dk == "model" and _dv == "" and _existing.get("model"):
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
    if isinstance(data, dict) and data.get("git_token") in ("****", ""):
        # mask echo or empty field: never destroy the stored credential
        data.pop("git_token", None)
    settings.update(data)
    # VRAM-PREFLIGHT-GATE (2026-10-11): live-sync the toggle into the
    # backend modules (same triple-sync as the startup path) so the
    # switch takes effect without a restart.
    if "vram_preflight_gate" in data:
        try:
            import backend.llama_config as _lc_pf
            import backend.manager_load as _ml_pf
            import backend.llama_server_manager as _lsm_pf
            _pf_val = bool(data.get("vram_preflight_gate", True))
            _lc_pf.VRAM_PREFLIGHT_GATE = _pf_val
            _ml_pf.VRAM_PREFLIGHT_GATE = _pf_val
            _lsm_pf.VRAM_PREFLIGHT_GATE = _pf_val
        except Exception:
            pass
    _xa = settings.get("exploration_agent")
    if isinstance(_xa, dict) and _xa.get("enabled") and not (_xa.get("model") or "").strip():
        _xa["model"] = DEFAULT_SETTINGS["exploration_agent"]["model"]
    if _model_changed_roles:
        _sync_recommended_sampling(settings, _model_changed_roles)
    _refresh_safe_profile_policy()
    apply_settings_to_pipeline(settings)
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
        await asyncio.to_thread(save_settings, settings)
    except Exception as e:
        logger.warning("[settings] save_settings failed: %s", e, exc_info=True)
        return JSONResponse({"error": f"Save failed: {str(e)[:120]}"}, status_code=500)
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


@router.post("/settings/agent")
async def set_agent(req: Request):
    d = await req.json()
    key = d.get("agent")
    if not _state.pipeline or key not in _state.pipeline.agents:
        return JSONResponse({"error": f"Unknown: {key}"}, status_code=400)
    _model = d.get("model") or ""
    try:
        if _model:
            registry_set(key, _model)
            settings.setdefault("agents", {}).setdefault(key, {})["model"] = _model
            _mc = settings.setdefault("agents_user_model_choice", {})
            if isinstance(_mc, dict):
                _mc[key] = _model
            _excluded = settings.setdefault("automap_excluded", [])
            if key not in _excluded:
                _excluded.append(key)
        if "temperature" in d:
            _sc = settings.setdefault("agents_user_sampling_choice", {})
            if isinstance(_sc, dict):
                _sc_list = _sc.setdefault(key, [])
                if "temperature" not in _sc_list:
                    _sc_list.append("temperature")
            v = float(d["temperature"])
            _state.pipeline.agents[key].temperature = v
            settings.setdefault("agents", {}).setdefault(key, {})["temperature"] = v
        if "max_tokens" in d:
            if d["max_tokens"] is None:
                raise ValueError("max_tokens missing")
            v = int(d["max_tokens"])
            _state.pipeline.agents[key].max_tokens = v
            settings.setdefault("agents", {}).setdefault(key, {})["max_tokens"] = v
        if "thinking" in d:
            _t = bool(d["thinking"])
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
    except Exception as e:
        logger.warning("[settings/agent] configuration failed (%s): %s", key, e, exc_info=True)
        return JSONResponse({"error": f"Configuration failed: {str(e)[:120]}"}, status_code=500)
    # NO model load on save (user decision 2026-08-19): only change the
    # settings, do not load. The model is loaded lazily on first real use
    # (pipeline/duo runners call ensure_loaded).
    try:
        await asyncio.to_thread(save_settings, settings)
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
    for key in settings["agents"]:
        settings["agents"][key]["model"] = m
    try:
        await asyncio.to_thread(save_settings, settings)
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


@router.get("/registry")
async def get_registry():
    return registry_all()


@router.get("/presets")
async def get_presets():
    return load_presets()


@router.post("/presets/{name}")
async def save_preset_ep(name: str, req: Request):
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
        copy_prompts_to_preset(
            src_preset=(name if name in presets else None), dst_preset=name,
            agent_keys=list(_state.pipeline.agents.keys()), base_prompts=PROMPTS
        )
    save_presets(presets)
    return {"ok": True, "name": name}


@router.post("/presets/{name}/load")
async def load_preset_ep(name: str):
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
    presets = load_presets()
    if name in presets:
        del presets[name]
        delete_custom_prompts_for_preset(name)
        save_presets(presets)
        # PRESET-HOUSEKEEPING (2026-09-30): deleting the ACTIVE preset must
        # clear active_preset (and persist), otherwise the UI shows it as
        # loaded and the next startup auto-loads a preset that no longer
        # exists.
        if str(settings.get("active_preset") or "") == name:
            settings["active_preset"] = None
            await asyncio.to_thread(save_settings, settings)
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
    await asyncio.to_thread(save_settings, settings)


@router.get("/presets/{name}/prompts/{agent}")
async def get_prompt(name: str, agent: str):
    custom = get_custom_prompt(name, agent)
    default = PROMPTS.get(agent, "")
    return {"content": custom or default, "is_custom": custom is not None}


@router.post("/presets/{name}/prompts/{agent}")
async def set_prompt(name: str, agent: str, req: Request):
    d = await req.json()
    save_custom_prompt(name, agent, d.get("content", ""))
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
        if hasattr(_state.memory, "remember"):
            _state.memory.remember(key, value)
        elif hasattr(_state.memory, "store"):
            _state.memory.store(key, value)
        elif hasattr(_state.memory, "set"):
            _state.memory.set(key, value)
        elif hasattr(_state.memory, "add"):
            _state.memory.add(key, value)
        elif _state.pipeline:
            await _state.pipeline._handle_memory_request(f"Remember: {key} is {value}")
    except Exception as e:
        return JSONResponse({"error": f"Memory error: {str(e)[:80]}"}, status_code=500)
    return {"ok": True, "key": key, "value": value}

"""Chat-run vision preprocessing (2026-11-06 split 3.2).

The image pipeline at the top of run_stream: per-image persistence into
the chat workspace, the vision-preprocess path (capable vision model
describes each image, raw images optionally kept for the vision gate),
the vision-model VRAM fit check with eviction, judge prefetch next and
the post-preprocess eviction toggle. Verbatim code motion — no logic
changes. Outputs flow back through `ps` (effective_images replaces the
raw images list for the rest of the run).
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import time

import httpx

from core import state as _state
from core.duo_helpers import (
    _build_duo_image_plan,
    duo_gate_status_text,
    effective_duo_image_mode,
    is_vision_error_text,
    resolve_duo_role_models,
    should_evict_vision_after_prepro,
)
from core.model_sampling import _model_profile
from core.state import _get_num_ctx, registry_get, settings
from routing.model_picker import _pick_direct_model
from utils.bg_tasks import spawn
from vision.preprocess import _preprocess_images_to_text

logger = logging.getLogger("hivemind.chat_run")
_logger = logger


@dataclasses.dataclass
class _ChatVisionPreState:
    """Vision-pre inputs/outputs (split 3.2); `carry` holds the runner
    closure `emit` and the lazy-import homes (server globals)."""
    _images_from_context: bool = False
    images: list = dataclasses.field(default_factory=list)
    user_input: str = ""
    mode: str = ""
    _ws_str: str = ""
    _vram_budget: float = 0.0
    _run_settings: dict = dataclasses.field(default_factory=dict)
    _judge_gb: float | None = None
    _saved_image_paths: list = dataclasses.field(default_factory=list)
    effective_images: list = dataclasses.field(default_factory=list)
    image_description: str = ""
    carry: dict = dataclasses.field(default_factory=dict)


async def _run_vision_prepro(images: list, user_input: str, _vision_model_name: str, emit):
    """Run vision preprocessing and drop error/fallback sentinels from output."""
    yield await emit({"type": "status", "content": "Vision model (" + _vision_model_name + ") describes images..."})
    _vp_t0 = time.monotonic()
    _vp_total_timeout = float(settings.get("vision_preprocess_timeout_seconds", 30.0) or 30.0)
    desc = ""
    try:
        desc = await asyncio.wait_for(
            _preprocess_images_to_text(images, user_input),
            timeout=_vp_total_timeout,
        )
    except asyncio.TimeoutError:
        yield await emit({"type": "status", "content":
            f"⚠ Vision preprocessing timed out after {_vp_total_timeout:.1f}s "
            "(VRAM busy? stop other model loads and retry). Images unusable this run"})
        _logger.warning(
            "[Vision-Prepro] Overall timeout after %.1fs (model=%s)",
            _vp_total_timeout, _vision_model_name,
        )
        desc = ""
    if desc and not is_vision_error_text(desc):
        yield await emit({"type": "image_description", "content": desc})
    elif desc:
        yield await emit({"type": "status", "content": desc
                         + " (VRAM busy? stop other model loads and retry)"})
        desc = ""
    yield {"_prepro_desc": desc, "_prepro_t0": _vp_t0}


async def _run_duo_preprocess_branch(
    images: list,
    user_input: str,
    emit,
    _effective_cfg: dict,
    _vision_cfg: dict,
    *,
    existing_description: str = "",
):
    """Run Agentic/Code-Duo preprocess describer (when selected) and status."""
    image_description = existing_description or ""
    _pm, _cm = resolve_duo_role_models(_effective_cfg)
    _duo_img_mode = effective_duo_image_mode(_effective_cfg, _vision_cfg)
    _vdbg_model = str((_vision_cfg or {}).get("model", "") or "").strip()
    if _duo_img_mode == "preprocess" and not (image_description and not is_vision_error_text(image_description)):
        if _vdbg_model:
            _vp_t0 = time.monotonic()
            async for _ev in _run_vision_prepro(images, user_input, _vdbg_model, emit):
                if isinstance(_ev, dict) and "_prepro_desc" in _ev:
                    image_description = _ev["_prepro_desc"]
                    _vp_t0 = _ev["_prepro_t0"]
                else:
                    yield _ev
            if should_evict_vision_after_prepro("code_duo", _vdbg_model, {_pm, _cm}):
                try:
                    from backend.llama_server_manager import manager as _vsm_e
                    await _vsm_e.evict(_vdbg_model)
                    yield await emit({"type": "status", "content":
                        f"[Image] vision model ({_vdbg_model}) evicted after "
                        f"{time.monotonic() - _vp_t0:.0f}s — VRAM free for planner/coder"})
                except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as _e:
                    logger.debug("[IMAGE] vision-evict status emit failed: %s", _e)
        else:
            yield await emit({"type": "status", "content":
                "⚠ Describe mode is selected for Agentic, but no Vision Describer model is configured."})
    _plan = _build_duo_image_plan(_pm, _cm, _effective_cfg, images, image_description, _vision_cfg)
    yield await emit({"type": "status", "content": duo_gate_status_text(_plan)})
    yield {"_duo_desc": image_description}


async def _chat_vision_pre(ps):
    from server import S_models_cache, _registry, _vision_cfg
    from vram.loader import _bk_evict
    emit = ps.carry["emit"]
    _judge_gb: float | None = None
    _images_from_context = ps._images_from_context
    images = ps.images
    user_input = ps.user_input
    mode = ps.mode
    _ws_str = ps._ws_str
    _vram_budget = ps._vram_budget
    _run_settings = ps._run_settings
    effective_images = ps.effective_images
    image_description = ps.image_description

    # -- IMAGE PERSISTENCE (2026-10-03, all modes) --
    _saved_image_paths: list = []
    _persist_on = bool(_run_settings.get(
        "image_uploads_persistent", settings.get("image_uploads_persistent", False)))
    if images and _ws_str and not _images_from_context and _persist_on:
        try:
            import base64 as _b64mod
            from datetime import datetime as _dt
            from pathlib import Path as _P
            _up_dir = _P(_ws_str) / ".hive_uploads"
            _up_dir.mkdir(parents=True, exist_ok=True)
            _stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
            for _ii, _b64 in enumerate(images):
                _raw = str(_b64 or "").strip()
                if not _raw:
                    continue
                if "," in _raw[:32]:  # strip data-url prefix if present
                    _raw = _raw.split(",", 1)[1]
                _ext = "png"
                try:
                    _magic = _b64mod.b64decode(_raw[:24] + "==")
                    if _magic[:2] == bytes([0xFF, 0xD8]):
                        _ext = "jpg"
                    elif _magic[:4] == bytes([0x89, 0x50, 0x4E, 0x47]):
                        _ext = "png"
                    elif _magic[:4] == bytes([0x47, 0x49, 0x46, 0x38]):
                        _ext = "gif"
                    elif _magic[:4] == bytes([0x52, 0x49, 0x46, 0x46]) and _magic[8:12] == b"WEBP":
                        _ext = "webp"
                except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as _e:
                    logger.debug("[IMAGE-SAVE] magic-byte sniff failed: %s", _e)
                _h8 = hashlib.sha256(_raw.encode("utf-8")).hexdigest()[:8]
                _fp = _up_dir / f"img_{_stamp}_{_ii + 1}_{_h8}.{_ext}"
                if not _fp.exists() or _fp.stat().st_size == 0:
                    _fp.write_bytes(_b64mod.b64decode(_raw))
                _saved_image_paths.append(str(_fp))
            if _saved_image_paths:
                yield await emit({"type": "status", "content":
                    "[Image] saved to " + ", ".join(
                        _P(x).name for x in _saved_image_paths)
                    + " (workspace/.hive_uploads)"})
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as _img_save_err:
            logger.warning("[IMAGE-SAVE] failed (non-fatal): %s", _img_save_err)

    # -- Vision Preprocessing / Gate (mode-aware) --
    _run_mode = str(mode or "").strip().lower()
    _effective_cfg = _run_settings if isinstance(_run_settings, dict) and _run_settings else settings
    _vdbg_has_images = bool(images)
    _vdbg_enabled    = bool(_vision_cfg.get("enabled"))
    _vdbg_model      = str(_vision_cfg.get("model", "") or "").strip()
    _logger.debug("[Vision-Trigger] mode=%r images=%s enabled=%s model=%r",
                  _run_mode, _vdbg_has_images, _vdbg_enabled, _vdbg_model)

    if images and _run_mode == "pipeline":
        effective_images = []
        _pipe_direct = bool(_effective_cfg.get("pipeline_vision_direct", False))
        _p_roles = (
            [k for k, v in (dict(_effective_cfg.get("pipeline_vision_roles") or {})).items() if v]
            if _pipe_direct else []
        )
        if _pipe_direct and not _p_roles:
            _p_roles = ["analyst"]
        _va_cfg_agent = _state.pipeline.agents.get("vision")
        _va_cfg_model = (_effective_cfg.get("vision_agent_model", "")
                         or (_va_cfg_agent.model if _va_cfg_agent else ""))
        _va_on = bool(_effective_cfg.get("vision_agent_enabled", False) and _va_cfg_model)
        if _p_roles:
            yield await emit({"type": "status",
                "content": f"[Image] pipeline raw roles: {', '.join(_p_roles)}"
                           + (f" (+ Vision-Agent: {_va_cfg_model})" if _va_on else "")})
        elif _va_on or _va_cfg_model:
            yield await emit({"type": "status",
                "content": f"[Image] pipeline Vision-Agent ({_va_cfg_model}) handles image"})
        else:
            yield await emit({"type": "status",
                "content": "[Image] pipeline has no raw roles or Vision-Agent configured"})

    elif images and _run_mode == "code_duo":
        effective_images = []
        async for _ev in _run_duo_preprocess_branch(images, user_input, emit, _effective_cfg, _vision_cfg):
            if isinstance(_ev, dict) and "_duo_desc" in _ev:
                image_description = _ev["_duo_desc"]
            else:
                yield _ev

    elif images and _vision_cfg.get("enabled") and _vision_cfg.get("model"):
        # Direct / Auto: explicit Preprocessor ("Describe first") enabled.
        _vision_model_name = _vision_cfg["model"]
        _configured_direct = str(_run_settings.get("direct_model") or "").strip() or (
            registry_get("direct") if "direct" in _state.pipeline.agents else "")
        async for _ev in _run_vision_prepro(images, user_input, _vision_model_name, emit):
            if isinstance(_ev, dict) and "_prepro_desc" in _ev:
                image_description = _ev["_prepro_desc"]
            else:
                yield _ev

        try:
            from backend.llama_server_manager import manager as _vsm
            from backend.llama_vram_table import vram_of as _vram_of_fn
            _direct_for_vram = _configured_direct or _vision_model_name
            _vision_gb = _vram_of_fn(_vision_model_name)
            _direct_gb = _vram_of_fn(_direct_for_vram)
            _judge_gb  = _vram_of_fn(_registry.get("judge", "granite-4.1:3b"))
            _budget_gb = _vram_budget
            _same_exact = _direct_for_vram == _vision_model_name
            if _same_exact:
                _logger.debug("[Vision-Evict] Reuse: %s == %s", _direct_for_vram, _vision_model_name)
            elif _vision_gb + _direct_gb + _judge_gb > _budget_gb - 0.3:
                await _bk_evict(_vision_model_name)
                _v_port = next(
                    (s.port for s in _vsm._slots if s.model == _vision_model_name), None
                )
                if _v_port:
                    for _ in range(50):  # max 7.5s
                        if not await _vsm._port_alive(_v_port):
                            break
                        await asyncio.sleep(0.15)
                await asyncio.sleep(3.0)
                _logger.debug("[Vision-Evict] Evicted %s - %.1f+%.1f+%.1f=%.1fGB > %.1fGB",
                              _vision_model_name, _vision_gb, _direct_gb, _judge_gb,
                              _vision_gb + _direct_gb + _judge_gb, _budget_gb)
            else:
                _logger.debug("[Vision-Evict] No evict - %.1f+%.1f+%.1f=%.1fGB <= %.1fGB budget",
                              _vision_gb, _direct_gb, _judge_gb,
                              _vision_gb + _direct_gb + _judge_gb, _budget_gb)
        except (OSError, RuntimeError, ValueError, TypeError, KeyError,
                AttributeError, IndexError, httpx.HTTPError) as _ve:
            _logger.debug("[Vision-Evict] Check failed (%s) - evicting to be safe", _ve)
            try:
                await _bk_evict(_vision_model_name)
                await asyncio.sleep(3.0)
            except (OSError, RuntimeError, ValueError, TypeError, httpx.HTTPError) as _e:
                logger.debug("[VISION-EVICT] background evict failed: %s", _e)
        if _configured_direct and _configured_direct != _vision_model_name:
            async def _prefetch_direct_post():
                try:
                    from backend import api_prefetch_next as _api_pf_next
                    await _api_pf_next(_configured_direct, num_ctx=_get_num_ctx(_configured_direct))
                except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as _e:
                    logger.debug("[VISION-PREFETCH] direct post-prefetch failed: %s", _e)
            spawn(_prefetch_direct_post())
        effective_images = []

    elif images:
        effective_images = []
        _va_cfg_agent = _state.pipeline.agents.get("vision")
        _va_cfg_model = _effective_cfg.get("vision_agent_model", "") or (_va_cfg_agent.model if _va_cfg_agent else "")
        _va_cfg_enabled = bool(_effective_cfg.get("vision_agent_enabled", False) and _va_cfg_model)
        # P9-override-aware: _run_settings.get("direct_model") beats registry card.
        _direct_eff2 = str(_run_settings.get("direct_model") or "").strip() or (
            registry_get("direct") if "direct" in _state.pipeline.agents else "")
        _direct_caps2 = _model_profile(_direct_eff2)
        _direct_is_vision2 = bool((_direct_caps2 or {}).get("vision", False))
        if _direct_is_vision2:
            _can_attach = True
            try:
                from core.duo_helpers import direct_vision_should_drop
                from backend.llama_server_manager import manager as _lsm_dvis
                from backend.llama_manager_utils import resolve_mmproj_strict as _mms_dir
                _dvis_slot = await _lsm_dvis._find_loaded(_direct_eff2) if _direct_eff2 else None
                _slot_vision = getattr(_dvis_slot, "vision_active", None) if _dvis_slot is not None else None
                _slot_model = getattr(_dvis_slot, "model", "") or ""
                if direct_vision_should_drop(_slot_vision, _slot_model, _direct_eff2):
                    if _mms_dir(_direct_eff2) is None:
                        _can_attach = False
                        _logger.warning("[VISION-GATE] direct model '%s' runs WITHOUT a "
                                        "projector (no mmproj found) - image dropped visibly",
                                        _direct_eff2)
                        yield await emit({"type": "status",
                            "content": "⚠ The direct model runs without a vision projector "
                                       "- the image was dropped. Pick a multimodal model or "
                                       "enable 'Describe first' in Image Processing."})
                    else:
                        yield await emit({"type": "status",
                            "content": f"🖼 Upgrading {_direct_eff2} with vision projector..."})
            except (ImportError, AttributeError, TypeError, ValueError) as _dge:
                _logger.debug("[VISION-GATE] direct pre-check skipped: %s", _dge)
            if _can_attach:
                effective_images = images
                yield await emit({"type": "status",
                    "content": f"Multimodal model ({_direct_eff2 or 'engine default'}) processes the image directly"})
        else:
            _user_dir_ov = str(_run_settings.get("direct_model") or "").strip()
            _vision_alt = (
                _pick_direct_model("simple", "vision", list(S_models_cache or []))
                if not _user_dir_ov and (S_models_cache or []) else ""
            )
            _alt_caps = _model_profile(_vision_alt) if _vision_alt else {}
            if _vision_alt and bool((_alt_caps or {}).get("vision", False)):
                effective_images = images
                yield await emit({"type": "status",
                    "content": f"↪ Direct model ({_direct_eff2 or 'none'}) is text-only — switching to multimodal model ({_vision_alt})"})
            elif _va_cfg_enabled and _run_mode in ("auto", "automap"):
                yield await emit({"type": "status", "content": "[Vision preprocessing off - pipeline vision-agent handles image if routed to pipeline]"})
            else:
                yield await emit({"type": "status", "content": "[Image ignored - no vision model active. "
                                 "Enable 'Describe first' in Image Processing or pick a multimodal model. "
                                 "Agentic runs receive the image description once a vision describer is configured.]"})
    # ── sync back: everything the runner consumes after this block ──
    ps._judge_gb = _judge_gb
    ps._saved_image_paths = _saved_image_paths
    ps.effective_images = effective_images
    ps.image_description = image_description

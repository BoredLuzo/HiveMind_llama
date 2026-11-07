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
from core.duo_helpers import should_evict_vision_after_prepro
from core.model_sampling import _model_profile
from core.state import _get_num_ctx, registry_get, settings
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
    # Attached images used to live only as base64 inside the request: after
    # the run they were gone. Save them once into the workspace so they
    # survive, are referenceable by the coder, and the UI note is honest.
    _saved_image_paths: list = []
    # PERSIST TOGGLE (2026-10-03): OFF (default) = images live only as base64
    # parts inside this run - no disk writes, nothing to clean up. ON = saved
    # under <workspace>/.hive_uploads/ and referenced by path afterwards.
    if (images and _ws_str and not _images_from_context
            and bool(settings.get("image_uploads_persistent", False))):
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
                # MAGIC SNIFF (2026-10-03 review): detect the real format,
                # not just JPEG - webp/gif/bmp used to land as .png. The
                # content hash in the name also makes same-second uploads
                # collision-free (two runs in one second used to lose the
                # second image via the skip-if-exists guard).
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

    # -- Vision Preprocessing --
    _vdbg_has_images = bool(images)
    _vdbg_enabled    = bool(_vision_cfg.get("enabled"))
    _vdbg_model      = _vision_cfg.get("model", "")
    _logger.debug("[Vision-Trigger] images=%s enabled=%s model=%r", _vdbg_has_images, _vdbg_enabled, _vdbg_model)
    if images and _vision_cfg.get("enabled") and _vision_cfg.get("model"):
        _vision_model_name = _vision_cfg["model"]
        # P9-override-aware (2026-10-06): judge the model this run will
        # actually use — the run body's direct_model beats the registry card.
        # _run_settings (not settings) carries the merged overrides.
        _configured_direct = str(_run_settings.get("direct_model") or "").strip() or (
            registry_get("direct") if "direct" in _state.pipeline.agents else "")
        _direct_caps = _model_profile(_configured_direct) if _configured_direct else {}
        _direct_is_vision = bool(_direct_caps.get("vision", False))
        _multimodal_available: list = []
        if not _direct_is_vision:
            _multimodal_available = [
                m for m in (S_models_cache or [])
                if bool((_model_profile(m) or {}).get("vision", False))
                and m.split(":")[0] in ("qwen3.5", "qwen3.6", "hermes3.6", "hermes", "gemma-4")
            ]
            # NO FLAG INFLATION (2026-11-02, live): this used to set
            # `_direct_is_vision = True` when ANY multimodal model was merely
            # INSTALLED. The run still used the text-only direct model, the
            # preprocessor was skipped, and the model answered "I cannot see an
            # image" (reproduced with a warm /models cache). Availability is
            # not capability: only the run's own direct model may claim vision;
            # otherwise the preprocessor below looks at the image.

        if _direct_is_vision:
            _direct_disp = _configured_direct or "multimodal model"
            yield await emit({"type": "status",
                "content": f"Multimodal model ({_direct_disp}) processes the image directly -- no vision preprocessing"})
            _logger.info("[Vision-Trigger] Image direct to multimodal model - prepro skipped (direct=%r, avail=%s)",
                         _configured_direct, _multimodal_available[:3] if _multimodal_available else "configured")
        else:
            yield await emit({"type": "status", "content": "Vision model (" + _vision_model_name + ") describes images..."})
            _vp_t0 = time.monotonic()
            _vp_total_timeout = float(settings.get("vision_preprocess_timeout_seconds", 30.0) or 30.0)
            try:
                image_description = await asyncio.wait_for(
                    _preprocess_images_to_text(images, user_input),
                    timeout=_vp_total_timeout,
                )
            except asyncio.TimeoutError:
                # TIMEOUT NOT A DESCRIPTION (2026-10-03): the old error string
                # had a different format than the [Vision-Preprocessing-Error
                # guard below, slipped through as a "description" and was
                # injected into planner task + coder sysprompt. Empty now:
                # resolve_image_plan warns about the missing description.
                image_description = ""
                yield await emit({"type": "status", "content":
                    f"⚠ Vision preprocessing timed out after {_vp_total_timeout:.1f}s "
                    "(VRAM busy? stop other model loads and retry). Images unusable this run"})
                _logger.warning(
                    "[Vision-Prepro] Overall timeout after %.1fs (model=%s)",
                    _vp_total_timeout, _vision_model_name,
                )
            if image_description and not image_description.startswith("[Vision-Preprocessing-Error"):
                yield await emit({"type": "image_description", "content": image_description})
            elif image_description and image_description.startswith("[Vision-Preprocessing-Error"):
                yield await emit({"type": "status", "content": image_description
                                 + " (VRAM busy? stop other model loads and retry)"})
            # DUO IMAGE EVICT (2026-10-01): in agentic duo runs the planner
            # and coder load RIGHT AFTER this — the preprocessing model must
            # not squat VRAM. Evict it unless it IS one of the duo models.
            _duo_models = {
                ((settings.get("agents") or {}).get("duo_planner") or {}).get("model", ""),
                ((settings.get("agents") or {}).get("duo_coder") or {}).get("model", ""),
            }
            _vm_name = _vision_cfg.get("model", "")
            if should_evict_vision_after_prepro(mode, _vm_name, _duo_models):
                try:
                    from backend.llama_server_manager import manager as _vsm_e
                    await _vsm_e.evict(_vm_name)
                    yield await emit({"type": "status", "content":
                        f"[Image] vision model ({_vm_name}) evicted after "
                        f"{time.monotonic() - _vp_t0:.0f}s — VRAM free for planner/coder"})
                except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError, IndexError) as _e:
                    logger.debug("[IMAGE] vision-evict status emit failed: %s", _e)

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
                # Too tight - evict vision
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
        if _direct_is_vision:
            # VISIBLE TRUTH CHECK (2026-10-04): the direct mode
            # used to strip silently inside chat_stream - the user saw
            # nothing and the model answered as if no image existed (the
            # same complaint as the duo downgrade). Check the loaded slot
            # HERE and say it out loud; chat_stream stays as the backstop.
            if effective_images:
                try:
                    from core.duo_helpers import direct_vision_should_drop
                    from backend.llama_server_manager import manager as _lsm_dvis
                    _dvis_model = registry_get("direct") if "direct" in _state.pipeline.agents else ""
                    _dvis_slot = None
                    if _dvis_model:
                        # _find_loaded matches BY MODEL NAME - a pinned planner
                        # on another slot can never be mistaken for the direct
                        # model (the false-warning case).
                        _dvis_slot = await _lsm_dvis._find_loaded(_dvis_model)
                    _slot_vision = getattr(_dvis_slot, "vision_active", None) if _dvis_slot is not None else None
                    _slot_model = getattr(_dvis_slot, "model", "") or ""
                    if direct_vision_should_drop(_slot_vision, _slot_model, _dvis_model):
                        effective_images = []
                        _logger.warning("[VISION-GATE] direct model '%s' runs WITHOUT a "
                                        "projector (vision_active=False) - image dropped "
                                        "visibly", _dvis_model)
                        yield await emit({"type": "status",
                            "content": "⚠ The direct model runs without a vision projector "
                                       "- the image was dropped. Pick a multimodal model or "
                                       "enable the vision preprocessor."})
                except (ImportError, AttributeError, TypeError, ValueError) as _dge:
                    _logger.debug("[VISION-GATE] direct pre-check skipped: %s", _dge)
            _logger.debug("[Vision-Trigger] Multimodal Direct model - raw images stay in effective_images (%d)", len(images))
        else:
            effective_images = []
    elif images:
        effective_images = []
        _va_cfg_agent = _state.pipeline.agents.get("vision")
        _va_cfg_model = settings.get("vision_agent_model", "") or (_va_cfg_agent.model if _va_cfg_agent else "")
        _va_cfg_enabled = bool(settings.get("vision_agent_enabled", False) and _va_cfg_model)
        # P9-override-aware (2026-10-06, live 11:45): the run body's
        # direct_model IS the model this run will use — the gate must judge
        # THAT one, not the registry card. Judging the registry model while
        # the runner applied the override emptied effective_images for a
        # multimodal override and produced a wrong "[Image ignored]" status.
        # _run_settings (not settings) carries the merged overrides.
        _direct_eff2 = str(_run_settings.get("direct_model") or "").strip() or (
            registry_get("direct") if "direct" in _state.pipeline.agents else "")
        _direct_caps2 = _model_profile(_direct_eff2)
        _direct_is_vision2 = bool((_direct_caps2 or {}).get("vision", False))
        # NO FLAG INFLATION (2026-11-02): an installed vision model does not
        # make THIS direct model multimodal; the image stays out of its request.
        if mode == "code_duo":
            # DUO IMAGE PLAN (2026-10-01): the gate must NOT judge by the
            # direct model — in agentic duo runs resolve_image_plan decides
            # per duo role (planner/coder). The attach-time projector check
            # in the runner has the final word; this status never claims
            # more than the plan says.
            from core.duo_helpers import _build_duo_image_plan, duo_gate_status_text, resolve_duo_role_models
            # ONE RESOLVER (2026-10-04): agent card + flat settings
            # key are both live paths - the ad-hoc fallback chain lived here
            # once and lied whenever only one side was set.
            _pm, _cm = resolve_duo_role_models(settings)
            _plan = _build_duo_image_plan(_pm, _cm, settings, images, image_description, _vision_cfg)
            yield await emit({"type": "status", "content": duo_gate_status_text(_plan)})
        elif _direct_is_vision2:
            effective_images = images
            yield await emit({"type": "status",
                "content": f"Multimodal model ({_direct_eff2 or 'engine default'}) processes the image directly"})
        elif _va_cfg_enabled:
            yield await emit({"type": "status", "content": "[Vision preprocessing off - vision-agent uses raw image]"})
        else:
            yield await emit({"type": "status", "content": "[Image ignored - no vision model active. "
                             "Enable the Vision agent in Settings (image preprocessing) or pick a multimodal model. "
                             "Agentic runs receive the image description once a vision model is configured.]"})
    # ── sync back: everything the runner consumes after this block ──
    ps._judge_gb = _judge_gb
    ps._saved_image_paths = _saved_image_paths
    ps.effective_images = effective_images
    ps.image_description = image_description

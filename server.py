# HiveMind — Local Multi-Agent AI Coding Assistant
# Author: Luzo (BoredLuzo) — https://github.com/BoredLuzo
from __future__ import annotations
import asyncio
# Windows: Proactor-Loop fÃ¼r subprocess-KompatibilitÃ¤t.
import sys as _sys_early
from pathlib import Path as _Path_early
_PROJECT_ROOT = str(_Path_early(__file__).parent.resolve())
if _PROJECT_ROOT not in _sys_early.path:
    _sys_early.path.insert(0, _PROJECT_ROOT)
if _sys_early.platform == "win32":
    import warnings as _warnings_early
    _warnings_early.filterwarnings("ignore", message=".*WindowsProactorEventLoopPolicy.*", category=DeprecationWarning)
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
import core.state as _state
from core.state import (
    detect_vision_need,
    registry_get, registry_set, registry_all,
    apply_settings_to_pipeline,
    _ws_configure, _refresh_safe_profile_policy,
)
import json
import copy
import logging
import sys
import os
import time
import re
import uuid
# platform removed ─ unused (all platform checks use sys.platform)
import threading
import contextvars as _contextvars

from utils.math import percentile_float as _percentile_float
from utils.httpx_utils import make_httpx_timeout as _make_httpx_timeout
from routing.agent_intent import detect_agent_intent, get_question_from_intent, detect_tool_request
from core.duo_helpers import DEFAULT_VRAM_BUDGET_GB

# ─── Shadow-Def Replacements: imports from extracted modules ───
from routing.model_picker import _pick_direct_model
from routing.complexity import _check_complexity_with_bias
from vram.loader import _bk_load, _bk_pin, _bk_evict
from vram.loader import _get_loaded_models_set, smart_preload_if_needed, _refresh_judge_keepalive
from vision.preprocess import _filter_vision_images, _preprocess_images_to_text, init_vision_preprocess
from vision.preprocess import _load_vision_model_cfg
from core.duo_config import DuoConfig
from core.run_context import RunContext
from core.stream import run_stream_orchestrated

# ─── Shadow-Def Imports from tools modules ───
from tools.definitions import _get_inline_tools
from tools.runner import _run_inline_tool as _run_inline_tool_runner
from context.chat import (
    init_chat_context,
    _get_chat_ctx_lock, _load_chat_context_locked, _save_chat_context_locked,
    _mutate_chat_context, _ctx_path_for_chat, _load_chat_context,
    _save_chat_context, _chat_context_valid,
)
from context.resume import (
    init_resume_deps,
    _write_resume_block, _load_resume_block, _clear_resume_block,
    _try_resume, _check_abort_and_maybe_save_resume,
)
from context.compression import init_compression, SESSION_COMPRESS_THRESHOLD, _compress_chat_session
from context.pause_state import init_pause_state
from infra.run_control import (
    init_run_control,
    _get_abort_lock, _get_abort_event, _clear_abort_event,
    _is_aborted, _cleanup_abort_registry, _abort_registry_cleanup_loop,
    _register_abort, _unregister_abort, _abort_event,
    _register_step_skip, _step_skip_event, _clear_step_skip, _unregister_step_skip,
)
from infra.run_counter import init_run_counter, _increment_run_counter
from infra.token_stats import (
    init_token_stats,
    _load_token_stats, _save_token_stats, _record_run,
    _estimate_tokens_from_content,
)
from learning.insights import init_insights, _run_insight_extractor, _run_skill_distillation

_files_read_in_run: _contextvars.ContextVar[set] = _contextvars.ContextVar("_files_read_in_run", default=None)
import httpx
from pathlib import Path
# deque removed ─ unused

HIVEMIND_VERSION = "1.0.4"

# ─── FrÃ¼he Logger-Definition ────────────────────────────────────────────────────
logger = logging.getLogger("hivemind.server")

# ─── sys.path Bootstrap ─────────────────────────────────────────────────────────
_THIS_DIR   = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
# llama.cpp-Backend
from backend import (
    BACKEND as _BACKEND, BACKEND_HOST as OLLAMA_HOST,
    api_tags as _api_tags, api_ps as _api_ps,
    api_generate_load as _api_gen_load,
)
try:
    from backend.llama_compat import force_kill_all as _force_kill_all
except ImportError:
    _force_kill_all = None
from hive_functions.planner import (
    run_planner, run_inloop_planner, PlannerResult,
    make_thinking_planner_sys, make_planner_sys,
    fallback_planner_steps,
)
from hive_functions.test_runner import run_tests as _run_test_suite, TestResult as _TestResult
from hive_functions.chunking import (
    build_chunk_context,
    ChunkState,
    ChunkAction,
    # Resume helpers
    resume_block_is_valid,
    load_resume_data,
    build_resume_block,
    # File signature helpers
    build_file_signature,
    file_signature_matches,
)
from infra.phase_timer import PhaseTimer
from hive_functions.ctx_utils import (
    run_context_pipeline,
    compute_content_budget,
    explore_to_planner_ctx,
    ContextBudget,
)
print(f"Hivemind v{HIVEMIND_VERSION} | Backend: {_BACKEND.upper()} ({OLLAMA_HOST})")

# ─── Pre-Explore Cache ───────────────────────────────────────────────────────────
# State + functions imported from explore.cache (via init_explore_cache dependency injection).

# ─── FrÃ¼he Stubs: _cache_lock / _chats_cache ────────────────────────────────────
_cache_lock: threading.Lock = threading.Lock()
_chats_cache: dict = {}
_state._cache_lock = _cache_lock
_state._chats_cache = _chats_cache


_chat_abort_registry: dict[str, asyncio.Event] = {}
_chat_abort_lock: asyncio.Lock | None = None


# ─── Until-Finished: Budget-Resolver ───────────────────────────────────────────

_TOOL_BUDGET_NORMAL = {"fast": 20, "balanced": 40, "heavy": 60}

from core.duo_helpers import (
    _resolve_duo_runtime_profile,
    _resolve_duo_run_timeout_seconds,
    _bucket_stop_reason,
)


from tools.websearch import (
    _get_websearch_timeout_seconds,
    _safe_web_search,
    _safe_web_fetch,
)

_state._safe_web_search = _safe_web_search


def _maybe_add_until_finished_block(base_prompt: str, until_finished: bool) -> str:
    """Appends the Until-Finished instruction block to the coder prompt when active."""
    if not until_finished:
        return base_prompt
    return base_prompt + UNTIL_FINISHED_BLOCK





# ─── Parallel Pre-Explore ───────────────────────────────────────────────────────
async def _bk_tags() -> list[str]:
    """All available models."""
    return await _api_tags()

async def _bk_ps() -> list[dict]:
    """Currently loaded models."""
    return await _api_ps()


from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

# ─── API-Router (endpoint organisation) ───
from routers import automap_router
from routers import chats_router
from routers import config_router
from routers import core_router
from routers import git_router
from routers import learning_router
from routers import soul_router
from routers import vision_router
from routers import vram_router
from routers import websearch_router
from routers import skills_router
from routers import debug_router, models_router
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response, FileResponse

_logger = logging.getLogger("hivemind.server")

# ─── Uvicorn Access-Log Filter ──────────────────────────────────────────────────
from infra.log_noise import _install_uvicorn_noise_filter

_install_uvicorn_noise_filter()

from hive_functions.pipeline import Pipeline
from hive_functions.tree_scout import (
    get_workspace_tree, partition_tree, partition_tree_async,
    parse_contract_summary, build_contract_prompt,
)
from hive_functions.hivemind_feature.ast_tools import (
    get_signatures_report,
    edit_ast_file,
    find_references_report,
)
from hive_functions.memory import Memory
from hive_functions.prompts import (
    PROMPTS, AGENT_ROLES,
    HIVEMIND_SOUL,          # moved from server.py inline definition
    PEER_RATING_PROMPT,     # moved from server.py inline definition
    VISION_AGENT_PROMPT,    # moved from server.py inline definition
    VISION_PREPROCESS_PROMPT,
    get_explore_analyst_prompt,
    build_partition_explore_prompt,
    EXPLORE_CODEBASE_PROMPT,
    STUCK_READER_INJECT,
    UNTIL_FINISHED_BLOCK,
    DUO_CRITIC_TOOLS_SYSTEM,
)

_MAX_TREE_DEPTH = 4
_MAX_TREE_FILES = 200
from hive_functions.language_config import LANGUAGE_RUNNERS as _LANGUAGE_RUNNERS, detect_language as _detect_language
from settings import (
    load_settings, save_settings,
    DEFAULT_AGENT_CFG, DEFAULT_SETTINGS
)
from model_configs import (
    init_base_configs, get_base_config, list_base_configs,
    get_learned_config, save_learned_config, get_effective_config,
    list_learned_models, list_learned_configs, delete_learned_config,
    reset_learned_configs, append_learning_log, read_learning_log,
    clear_learning_log
)
from routing.model_automap import (
    get_automap, get_model_display_map, detect_task_type,
    record_run_outcome, get_routing_weights_summary, get_routing_suggestion,
    is_valid_preprocessing_model, get_best_preprocessing_model,
    save_routing_weights,
    _VISION_PREPROCESSING_ALLOWLIST,
)

# ─── Model Capability Overrides (P1 FIX) ─────────────────────────────────────────
# Kanonische Implementierung: core/model_sampling._model_profile
# (Hardcoded-Hints + User-Config aus model_configs/models/*.json + Cap-Overrides).
from core.model_sampling import _model_profile

# ─── Websearch ───────────────────────────────────────────────────────────────────
try:
    from tools import websearch as _websearch  # type: ignore
    _WEBSEARCH_AVAILABLE = True
except ImportError:
    _websearch = None  # type: ignore
    _WEBSEARCH_AVAILABLE = False

from hive_functions.soul_engine import (
    load_soul, save_soul, build_soul_prompt_layer,
    maybe_evolve_soul, get_soul_summary, reset_soul,
    run_soul_cycle,
    MIN_RUNS_FOR_EVOLUTION, EVOLUTION_INTERVAL_RUNS,
    FORCED_EVOLUTION_COOLDOWN,
)
try:
    from hive_functions.git_tools import (
        exec_git_commit,
        get_git_diff_ctx,
        get_git_diff_ctx_async,
        get_git_log_oneline,
        get_git_log_oneline_async,
        exec_git_reset,
        exec_git_checkout,
        exec_git_stash,
        exec_git_status_detailed,
    )
    _GIT_TOOLS_AVAILABLE = True
except ImportError:
    _GIT_TOOLS_AVAILABLE = False
    exec_git_commit = None           # type: ignore[assignment,misc]
    get_git_diff_ctx = None          # type: ignore[assignment,misc]
    get_git_diff_ctx_async = None    # type: ignore[assignment,misc]
    get_git_log_oneline = None       # type: ignore[assignment,misc]
    get_git_log_oneline_async = None # type: ignore[assignment,misc]
    exec_git_reset = None
    exec_git_checkout = None
    exec_git_stash = None
    exec_git_status_detailed = None
    _logger.warning("[GIT] hive_functions/git_tools.py not found - Git features disabled")

# -- Bootstrap -------------------------------------------------

THIS_FILE = Path(__file__)
app      = FastAPI()

# ── CSRF/origin protection ────────────────────────────────────────────────────
# State-changing requests with an Origin header are only allowed from the same
# origin (origin host == host header). Blocks CSRF from arbitrary websites —
# including the "text/plain" variant without preflight, which would otherwise
# allow RCE via /internal/tool/exec. Non-browser clients (curl, httpx, MCP,
# CLI) send no Origin header and stay unaffected.

from infra.security import _csrf_origin_host, _csrf_origin_guard

app.middleware("http")(_csrf_origin_guard)

_static_dir = THIS_FILE.parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
memory   = Memory(THIS_FILE.parent / "memory.json")  # FIX: persistent
# Ensure object identity: core.state.settings and server.py must use the SAME
# settings basis.
settings = _state.settings
settings.clear()
settings.update(load_settings())
_SESSIONS_DIR = Path(os.environ.get("HIVEMIND_SESSIONS_DIR", THIS_FILE.parent / "sessions"))
_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
_state._SESSIONS_DIR = _SESSIONS_DIR
init_pause_state(_SESSIONS_DIR)
_logger.info("[VRAM-BUDGET] Startup: settings['vram_budget_gb'] = %s (type=%s)",
             settings.get("vram_budget_gb"), type(settings.get("vram_budget_gb")).__name__)
def _sync_backend_runtime_config() -> None:
    """Sync settings → backend runtime globals (llama_server_manager/llama_config)."""
    try:
        import backend.llama_server_manager as _lsm_init
        _raw = settings.get("vram_budget_gb")
        _lsm_init.VRAM_BUDGET_GB = float(_raw) if _raw is not None else DEFAULT_VRAM_BUDGET_GB
        _moe_cfg = settings.get("moe_cpu_experts", {})
        if isinstance(_moe_cfg, dict):
            _lsm_init.MOE_CPU_EXPERTS = {str(k): int(v or 0) for k, v in _moe_cfg.items()}
        else:
            _lsm_init.MOE_CPU_EXPERTS = {}
        _lsm_init.MLOCK_MODEL = bool(settings.get("llama_mlock", True))
        import backend.llama_config as _lc_init
        _gb = str(settings.get("gpu_backend", "") or "").strip().lower()
        if _gb in ("vulkan", "cuda"):
            _lc_init.GPU_BACKEND = _gb
            _lsm_init.GPU_BACKEND = _gb
            # LLAMA-BIN-RE-RESOLVE (2026-08-27, CUDA-VERSION-FIX): Auto-Discovery
            _lc_init.LLAMA_BIN = _lc_init._find_llama_server()
            _lsm_init.LLAMA_BIN = _lc_init.LLAMA_BIN
        _lsm_init.CACHE_REUSE = int(settings.get("llama_cache_reuse", 256) or 0)
    except Exception:
        pass


_sync_backend_runtime_config()
_refresh_safe_profile_policy()

init_base_configs(THIS_FILE)

_pipeline_run_counter_file = THIS_FILE.parent / "run_counter.json"
# In-Memory Counter-Cache.
_run_counter_cache: int | None = None
_run_counter_lock = threading.RLock()
_last_forced_evolution_run: int = 0


pipeline = Pipeline(memory=memory)

_state.init_state(pipeline_obj=pipeline, memory_obj=memory, settings_dict=settings)

# ─── Token Tracker ─────────────────────────────────────────────────────────────
# Tracks generated tokens per run and aggregates daily stats.
# Persisted to token_stats.json ─ survives restarts.

_TOKEN_STATS_FILE = THIS_FILE.parent / "token_stats.json"
_token_stats_lock = threading.Lock()


# -- Soul ------------------------------------------------------

# HIVEMIND_SOUL, PEER_RATING_PROMPT, VISION_AGENT_PROMPT etc. ─ imported from hive_functions.prompts above.


# -- Runtime Model Registry ------------------------------------

REGISTRY_FILE = THIS_FILE.parent / "runtime_models.json"

def _load_registry() -> dict:


    base = {k: a.model for k, a in pipeline.agents.items()}
    if REGISTRY_FILE.exists():
        try:
            stored = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
            for k, v in stored.items():
                if k not in base:   # adopt extra keys from runtime, never overwrite settings
                    base[k] = v
        except Exception:
            pass
    return base

_registry: dict = _load_registry()

_state.init_state(
    registry_dict=_registry, registry_file=REGISTRY_FILE,
    websearch_available=_WEBSEARCH_AVAILABLE, websearch_obj=_websearch,
)
_state._GIT_TOOLS_AVAILABLE = _GIT_TOOLS_AVAILABLE
_state.exec_git_reset = exec_git_reset
_state.exec_git_checkout = exec_git_checkout
_state.exec_git_stash = exec_git_stash
_state.exec_git_status_detailed = exec_git_status_detailed

# Websearch config after init_state (state._ws_configure reads state globals).
if _WEBSEARCH_AVAILABLE:
    _ws_configure(
        host     = settings.get("searxng_host", "http://localhost:8888"),
        enabled  = settings.get("pipeline_websearch_enabled", False)
                   or settings.get("duo_websearch_enabled", False),
        engines  = settings.get("searxng_engines"),
        language = settings.get("searxng_language"),
    )

apply_settings_to_pipeline(settings)

# (Ollama size_vram includes KV cache + activations ─ too high)
try:
    from backend.llama_vram_table import VRAM_GB as _VRAM_LOOKUP_GB
    if not _VRAM_LOOKUP_GB:
        raise ValueError("VRAM_LOOKUP_GB is empty")
except (ImportError, ValueError) as e:
    import logging as _vram_log
    _vram_log.warning(f"VRAM table import/load failed ({type(e).__name__}), using safe defaults")
    _VRAM_LOOKUP_GB: dict[str, float] = {
        # qwen3.5 ─ echte Messungen (Vulkan, 99 GPU-Layers)
        "qwen3.5:0.8b": 0.6, "qwen3.5:2b": 1.5, "qwen3.5:4b": 2.8,
        "qwen3.5:4b-ud": 3.0, "qwen3.5:4b-d": 3.2, "qwen3.5:9b": 5.5,
        "qwen3.5:9b-ud": 5.8,
        # Granite / Gemma
        "granite-4.1:3b": 2.1, "granite4:1b": 1.0,
        "gemma-4:e4b-it-obliterated": 3.0, "google-gemma-3:4b-it": 2.8,
        # OmniCoder / Ministral
        "omnicoder:9b": 5.5,
        "ministral:3b-instruct-2410": 2.0, "ministral:8b-instruct-2410": 4.8,
        "mistral:7b": 5.0, "llama3.1:8b": 5.5, "llama3:8b": 5.5,
        "phi3:3.8b": 2.8, "gemma3:4b": 3.0,
        "llava:7b": 5.0, "falcon3:7b": 5.0,
        "ternary-bonsai:8b": 4.8, "qwen-qwen3:14b-iq4-nl": 7.5,
    }

def _ctx_int(value) -> int | None:
    try:
        iv = int(value)
        return iv if iv > 0 else None
    except Exception:
        return None


def _ctx_override_from_settings(model: str, agent_role: str | None = None) -> int | None:
    ov = settings.get("ctx_overrides")
    if not isinstance(ov, dict):
        return None

    model_name = str(model or "")
    base_name = model_name.split(":")[0] if model_name else ""

    # Legacy flat shape support: {"duo_coder": 8192, "qwen3.5": 6144}
    if agent_role:
        flat_role = _ctx_int(ov.get(agent_role))
        if flat_role:
            return flat_role
    for mk in (model_name, base_name):
        if mk:
            flat_model = _ctx_int(ov.get(mk))
            if flat_model:
                return flat_model

    roles = ov.get("roles")
    if agent_role and isinstance(roles, dict):
        role_ctx = _ctx_int(roles.get(agent_role))
        if role_ctx:
            return role_ctx

    models = ov.get("models")
    if isinstance(models, dict):
        for mk in (model_name, base_name):
            if not mk:
                continue
            model_ctx = _ctx_int(models.get(mk))
            if model_ctx:
                return model_ctx

    return _ctx_int(ov.get("default"))


try:
    from hive_functions.num_ctx_config import get_num_ctx as _get_num_ctx_impl
    def _get_num_ctx(model: str, agent_role: str | None = None) -> int | None:
        base_ctx = _get_num_ctx_impl(model, agent_role)
        return _ctx_override_from_settings(model, agent_role) or base_ctx
except ImportError:
    import logging as _ncc_log
    _ncc_log.getLogger("hivemind").warning(
        "num_ctx_config.py not found ─ num_ctx limits disabled! "
        "KV-cache overflow possible. Please put num_ctx_config.py into the main directory."
    )
    def _get_num_ctx(model: str, agent_role: str | None = None) -> int | None:
        return _ctx_override_from_settings(model, agent_role)


async def _pipeline_chat_stream(model: str, msgs: list, temp: float, max_tok: int,
                                agent_role: str | None = None,
                                force_ctx: int | None = None,
                                think: bool | None = None,
                                thinking_budget: int | None = None,
                                split_thinking: bool = False,
                                no_cache: bool = False):


    if force_ctx:
        effective_ctx = force_ctx
    else:
        num_ctx = _get_num_ctx(model, agent_role)
        effective_ctx = num_ctx if num_ctx else 8192
    async for tok in pipeline.ollama.chat_stream(model, msgs, temp, max_tok, ctx=effective_ctx,
                                                  think=think, thinking_budget=thinking_budget,
                                                  split_thinking=split_thinking,
                                                  no_cache=no_cache):
        yield tok


@app.on_event("shutdown")
async def _shutdown():
    """Clean shutdown: terminate all llama-server processes."""
    _logger.info("[Hivemind] Shutdown ─ terminating all llama-server processes…")
    try:
        from backend.llama_server_manager import manager as _sm
        await _sm.shutdown()
    except Exception as _se:
        _logger.debug("[Hivemind] Manager-shutdown error: %s", _se)
    if _force_kill_all:
        try:
            _fk = _force_kill_all()
            if asyncio.iscoroutine(_fk):
                await _fk
        except Exception:
            pass
    # Pending Writes beim Shutdown flushen.
    try:
        save_settings(settings)
    except Exception as _e:
        _logger.debug("[Hivemind] flush_settings error: %s", _e)
    if _WEBSEARCH_AVAILABLE:
        try:
            await _websearch.shutdown()
        except Exception as _e:
            _logger.debug("[Hivemind] websearch.shutdown error: %s", _e)
    try:
        from tools.browser import close_browser
        close_browser()
    except Exception as _e:
        _logger.debug("[Hivemind] browser shutdown error: %s", _e)
    _logger.info("[Hivemind] Shutdown complete.")


@app.on_event("startup")
async def _startup():


    global S_models_cache

    # Batch 2.1: Extracted Module Dependency Injection
    def _init_extracted_modules():
        """Batch 2.1: Dependency injection for extracted modules."""
        try:
            from tools.definitions import init_websearch
            from tools.handlers import init_runtime_deps
            from explore.cache import init_explore_cache
            from vram.loader import init_vram_loader
            from routing.complexity import init_complexity
            from routing.model_picker import init_model_picker

            init_websearch(_WEBSEARCH_AVAILABLE, _websearch if _WEBSEARCH_AVAILABLE else None)
            init_runtime_deps(
                get_signatures_report_fn=get_signatures_report,
                find_references_report_fn=find_references_report,
                edit_ast_file_fn=edit_ast_file,
                detect_language=_detect_language,
                language_runners=_LANGUAGE_RUNNERS,
                safe_web_search=_safe_web_search if _WEBSEARCH_AVAILABLE else None,
                safe_web_fetch=_safe_web_fetch if _WEBSEARCH_AVAILABLE else None,
                websearch_available=_WEBSEARCH_AVAILABLE,
                git_available=_GIT_TOOLS_AVAILABLE,
                exec_git_commit_fn=exec_git_commit,
                run_test_suite_fn=_run_test_suite,
                TestResult_class=_TestResult,
            )
            init_explore_cache(
                file_signature_matches_fn=file_signature_matches,
                build_file_signature_fn=build_file_signature,
                settings_dict=settings,
            )
            init_vram_loader(
                api_gen_load=_api_gen_load,
                bk_ps=_bk_ps,
                settings=settings,
                registry=_registry,
                get_num_ctx=_get_num_ctx,
                vram_lookup_gb=_VRAM_LOOKUP_GB,
                vram_budget_default=DEFAULT_VRAM_BUDGET_GB,
            )
            init_complexity(pipeline_obj=pipeline)
            init_model_picker(pipeline_obj=pipeline, settings_dict=settings,
                             simple_direct=_SIMPLE_DIRECT_MODELS, complex_direct=_COMPLEX_DIRECT_MODELS)
            init_chat_context(
                sessions_dir=_SESSIONS_DIR,
                chats_cache=_chats_cache,
                cache_lock=_cache_lock,
                file_signature_matches=file_signature_matches,
                build_file_signature=build_file_signature,
            )
            init_resume_deps(
                build_resume_block_fn=build_resume_block,
                load_resume_data_fn=load_resume_data,
                clear_abort_event_fn=_clear_abort_event,
                is_aborted_fn=_is_aborted,
            )
            init_vision_preprocess(
                settings_dict=settings,
                get_num_ctx_fn=_get_num_ctx,
                is_valid_preprocessing_fn=is_valid_preprocessing_model,
                vision_preprocess_prompt=VISION_PREPROCESS_PROMPT,
            )
            init_compression(
                settings_obj=settings,
                registry_get_fn=registry_get,
                pipeline_chat_stream_fn=_pipeline_chat_stream,
            )
            init_run_control(
                chats_cache=_chats_cache,
                cache_lock=_cache_lock,
                abort_registry=_chat_abort_registry,
                abort_lock=_chat_abort_lock,
            )
            init_run_counter(
                counter_file=_pipeline_run_counter_file,
                lock=_run_counter_lock,
            )
            init_token_stats(
                stats_file=_TOKEN_STATS_FILE,
                lock=_token_stats_lock,
            )
            init_insights(
                insight_sem=_bg_insight_sem,
                registry_get_fn=registry_get,
                pipeline_obj=pipeline,
                memory_obj=memory,
            )
            # Peer-Ratings: shared semaphore aus server.py in extrahiertes Modul injizieren
            import learning.peer_ratings as _peer_ratings
            _peer_ratings._bg_rating_sem = _bg_rating_sem
        except Exception as _e:
                        logger.error("_init_extracted_modules() FAILED: %s ─ %s",
                         type(_e).__name__, str(_e))

    _init_extracted_modules()


    # ─── Cleanup stale ports from previous crashes ───
    try:
        from backend.llama_server_manager import manager as _lsm_startup
        await _lsm_startup.startup_cleanup()
    except Exception as _sc_err:
        logger.warning("startup_cleanup failed: %s", _sc_err)

    #  Planner Warmup: MoE cold-start prevention 
    # qwen3.6:35b-a3b-ud uses --no-warmup in _start_process() (saves ~300 MiB
    # compute). Without a warmup forward-pass the first Planner request must
    # compile Vulkan shaders + load MoE experts, exceeding the 600s timeout.
    # This warmup forces the forward-pass at startup so the Planner is hot.
    _planner_warmup_model = str(settings.get("duo_planner_model", "") or "").strip()
    if _planner_warmup_model and bool(settings.get("startup_preload_enabled", False)):
        try:
            from backend.llama_server_manager import manager as _lsm_warmup
            _warmup_ctx = int(settings.get("duo_planner_ctx_target", 0) or 8192)
            _warmup_timeout = float(settings.get("duo_planner_thinking_timeout_s", 600.0))
            _warmup_port = await _lsm_warmup.ensure_loaded(
                _planner_warmup_model, num_ctx=_warmup_ctx, n_parallel=1
            )
            logger.info(
                "Planner warmup: %s port=%d ctx=%d timeout=%.0fs ─ sending forward-pass",
                _planner_warmup_model, _warmup_port, _warmup_ctx, _warmup_timeout,
            )
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=_warmup_timeout, write=10.0, pool=5.0)
            ) as _wc:
                _wr = await _wc.post(
                    f"http://127.0.0.1:{_warmup_port}/v1/chat/completions",
                    json={
                        "messages": [{"role": "user", "content": "warmup"}],
                        "max_tokens": 1,
                        "temperature": 0.0,
                        "stream": False,
                    },
                )
            logger.info("Planner warmup OK: status=%d", _wr.status_code)
        except httpx.ConnectError:
            logger.warning(
                "Planner warmup ConnectError ─ cold model, first real request will warm it"
            )
        except Exception as _wu_err:
            logger.warning("Planner warmup failed (non-critical): %s", _wu_err)

_SIMPLE_DIRECT_MODELS = {
    "general":      "gemma3:4b",
    "creative":     "gemma3:4b",
    "factual":      "qwen3.5:2b",     # qwen3.5:2b: strukturiertes Wissen, neuere Gen als gemma3
    "reasoning":    "gemma3:4b",
    "code":         "qwen3.5:2b",     # qwen3.5:2b: stÃ¤rker als qwen2.5:3b, Vision+TC
    "math":         "qwen3.5:2b",
    "tool_use":     "qwen3.5:2b",     # TC-nativ
    "vision":       "granite3.2-vision:2b",  # Kleinste Vision
    "multilingual": "ministral-3:3b", # Ministral: specialized in multilingual
}
_COMPLEX_DIRECT_MODELS = {
    "general":      "qwen3.5:9b-ud",
    "creative":     "gemma3:4b",
    "factual":      "qwen3.5:4b",     # qwen3.5:4b: Faktenwissen
    "reasoning":    "qwen3.5:9b-ud", # qwen3.5: Thinking-Modus
    "code":         "qwen3.5:4b",     # qwen3.5:4b: Code-Benchmark, SOLO ~5.9GB
    "math":         "qwen3.5:9b-ud",
    "tool_use":     "qwen3.5:4b",     # TC-nativ
    "vision":       "granite3.2-vision:2b",
    "multilingual": "ministral-3:8b", # solo-only, 6GB
}


# ─── Code Duo ────────────────────────────────────────────────────────────────────
# Quality: qwen3.5:9b-ud(5.5) + qwen3.5:2b(1.5) = 7.0 GB ─ Strong Coder + Lightweight Critic


def _make_duo_coder_path_add(include_websearch: bool = False) -> str:
    _ws_block = (
        "2. RESEARCH ─ when you encounter an unknown API, library, error message, or need current\n"
        "   docs: web_search('library name usage example') then web_fetch(url) for the full page.\n"
        "   Use BEFORE guessing at an API signature. One targeted search beats three wrong attempts.\n"
    ) if include_websearch else ""
    _ws_step_offset = 1 if include_websearch else 0
    _impl  = 2 + _ws_step_offset
    _test  = 3 + _ws_step_offset
    _fix   = 4 + _ws_step_offset
    return (
        "\n\n## Agentic Workflow\n"
        "Work like an autonomous developer. Explore first, then implement, then test.\n\n"
        "Workflow:\n"
        "1. EXPLORE ─ list_dir / find_files / get_signatures / read_file to understand structure and find relevant files\n"
        + _ws_block +
        f"{_impl}. IMPLEMENT ─ edit_file for ALL file operations (create new files AND modify existing), edit_ast for node-level Python edits\n"
        "   edit_file: universal file editor ─ creates new files or patches existing ones\n"
        "   edit_ast: replace one Python function/class/variable by AST target name\n"
        f"{_test}. TEST ─ detect project type from files (package.json─npm test, Cargo.toml─cargo test, pom.xml─mvn test, pytest.ini/test_*.py─pytest, docker-compose.yml─docker compose up --build) and run the right command\n"
        f"{_fix}. FIX ─ if tests fail: read the error, edit_file, test again\n\n"
        "GET_SIGNATURES ─ use before large file reads to map structure quickly:\n"
        "  get_signatures(path[, max_items]) returns classes/functions/methods/variables with line numbers.\n"
        "  Then call read_file only for the specific line ranges you need.\n\n"
        "EDIT_AST ─ preferred for replacing whole Python nodes:\n"
        "  edit_ast(path, target_type, target_name, new_code)\n"
        "  target_type: function | class | variable\n"
        "  target_name: use qualified names when needed, e.g. ClassName.method\n\n"
        "EDIT_FILE ─ universal file editor (creates new files AND modifies existing ones):\n"
        "  edit_file(path, edits) where edits contains one or more blocks:\n"
        "    <<<<<<< SEARCH\n"
        "    exact text from the file (copy verbatim, correct indentation)\n"
        "    =======\n"
        "    new replacement text\n"
        "    >>>>>>> REPLACE\n"
        "  Multiple blocks per call OK ─ batches several changes in one tool call.\n"
        "  SEARCH must be unique and exact ─ use more context lines if needed.\n"
        "  For NEW files: pass the full file content in edits (no SEARCH/REPLACE markers needed).\n"
        "  Max chars per call: 5000 (2b), 7000 (3-6b), 10000 (7-9b), 20000 (14b+).\n"
        "  If too large: split with edit_file(first chunk) + write_file_append for remaining chunks.\n"
        "  The output token limit cuts off oversized calls mid-stream and discards them —\n"
        "  if your content is anywhere near the limit, ALWAYS split into chunks.\n\n"
        "Tool rules:\n"
        "  - Modifying existing file ─ read_file first (if not in context), then edit_file\n"
        "  - edit_file old text must be EXACT ─ copy verbatim from read_file output\n"
        "  - run_bash for anything shell-related: tests, installs, builds, grep, find\n"
        "  - find_files('**/*.py') to understand project structure\n"
        "  - All path formats accepted: C:\\\\..., /..., relative ─ never refuse a path\n"
        "  - Do not ask for confirmation ─ just act and report the result\n"
        "  - After edit_file: ALWAYS call run_bash to compile/run and verify output\n"
        + ("  - web_search / web_fetch available ─ use them for unknown APIs, errors, or missing docs\n"
           if include_websearch else "")
    )

_DUO_CODER_PATH_ADD = _make_duo_coder_path_add(include_websearch=False)

# PHASE B4-B6: Adaptive Thinking-Budget ─ delegiert an core.duo_helpers


# PHASE B2: Planner-Funktionen ─ delegiert an hive_functions.planner

_make_duo_thinking_planner_sys = make_thinking_planner_sys
_make_duo_planner_sys = make_planner_sys
_fallback_planner_steps = fallback_planner_steps

# ─── run_stream Kernfunktion ────────────────────────────────────────────────────

# DUO prompts: now in prompts.py ─ preset-overridable + separate learned configs
_DUO_CODER_SYS_DEFAULT   = PROMPTS.get("duo_coder", "Write complete, runnable code for the given task.")
_DUO_CRITIC_CODE_DEFAULT = PROMPTS.get("duo_critic_code",
    "Review the code. Respond ONLY in compact format:\n"
    "approved=true issues=[] verdict=max_10_words_with_underscores\n"
    "or: approved=false issues=[Problem_1;Problem_2] verdict=max_10_words_with_underscores\n"
    "approved=true ONLY if code is complete and correctly implemented.")
_DUO_CRITIC_GEN_DEFAULT  = PROMPTS.get("duo_critic_general",
    "Review the answer. Respond ONLY in compact format:\n"
    "approved=true issues=[] verdict=max_10_words_with_underscores\n"
    "or: approved=false issues=[Missing_Element_1;Logic_Error_2] verdict=max_10_words_with_underscores\n"
    "approved=true ONLY if the core task is fully fulfilled.")

# Critic answers stay in the compact JSON format.


def apply_learned_configs_for_run():
    for agent_key in list(pipeline.agents.keys()):
        model_name = registry_get(agent_key)
        cfg = get_effective_config(THIS_FILE, model_name, agent_key, use_learned=True)
        if "temperature" in cfg:
            pipeline.agents[agent_key].temperature = float(cfg["temperature"])
        if "max_tokens" in cfg:
            pipeline.agents[agent_key].max_tokens  = int(cfg["max_tokens"])

def reset_to_base_configs():
    """Resets pipeline agents to settings.json values when learning mode is off."""
    apply_settings_to_pipeline(settings)


def get_effective_prompt_with_override(agent_key, preset_name, use_learned):
    if use_learned:
        model_name = registry_get(agent_key)
        cfg        = get_effective_config(THIS_FILE, model_name, agent_key, use_learned=True)
        override   = cfg.get("system_prompt_override")
        if override:
            return override
    return get_effective_prompt(agent_key, preset_name)


from tools.errors import tool_error_response as _tool_error_response

from context.chat_util import (
    _extract_ws_query, _trim_query,
    get_effective_prompt, _make_messages,
    _extract_memory, _auto_memory_from_input,
)




_SYMBOL_HINT_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "your",
    "file", "files", "code", "project", "please", "write", "update", "change",
    "fix", "error", "issue", "test", "tests", "function", "class", "method",
    "python", "javascript", "typescript", "json", "yaml", "module", "build",
    "und", "oder", "bitte", "datei", "dateien", "projekt", "funktion", "klasse",
    "methode", "fehler", "testen", "ändere", "aendere", "schreibe", "nutze",
}


# NOTE: _run_inline_tool_legacy removed (~950 lines) ─ all call sites use _run_inline_tool


# _INLINE_TOOL_HANDLER_MAP removed ─ tool dispatch delegated to tools.runner._run_inline_tool


async def _run_inline_tool(
    name: str,
    args: dict,
    workspace_lock: str | None = None,
    *,
    tool_mode: str | None = None,
    include_websearch: bool = False,
) -> str:
    """Delegates to tools.runner._run_inline_tool (single dispatch path)."""
    return await _run_inline_tool_runner(
        name, args, workspace_lock,
        tool_mode=tool_mode, include_websearch=include_websearch,
    )








# -- Message builder -------------------------------------------



# -- Runtime Model Cache ---------------------------------------

S_models_cache: list = []
_state.S_models_cache = S_models_cache  # share ref with routers

# ─── Background-Task VRAM Guard ─────────────────────────────────
_bg_rating_sem = asyncio.Semaphore(1)

_bg_insight_sem = asyncio.Semaphore(1)

# ─── Runtime VRAM Planner Override ──────────────────────────────


# -- Vision Model Config ---------------------------------------

_vision_cfg: dict = _load_vision_model_cfg()
_state._vision_cfg = _vision_cfg

# ─── KV-Cache-Poisoning-Erkennung ───────────────────────────────────────────────
# Marker fÃ¼r offensichtliches Prompt-Leak/KV-Poisoning.
_VISION_POISON_MARKERS: tuple[str, ...] = (
    "ich bin hivemind",
    "bin hivemind",
    "hivemind ist ein lokales",
    "ein lokales ki-system, das vollstaendig",
    "ein lokales ki-system, das vollst",
    "nachrichtenherkunft",
    "[nutzer]\n",
    "i am hivemind",
)

# VISION_AGENT_PROMPT ─ imported from hive_functions.prompts


# -- Run Abort Registry ----------------------------------------
# Maps run_id -> asyncio.Event so the client can signal abort.

# -- Run-Abort-Registry via infra.run_control (imported above) --

def _get_avg_elapsed(agent_key: str) -> float:
    return float(settings.get("prefetch_agent_avgs", {}).get(agent_key, 0))


def _update_prefetch_lead(agent_key: str, actual_elapsed: float, state: dict | None = None):
    if actual_elapsed <= 0:
        return

    target = state if isinstance(state, dict) else settings

    # EMA avg pro Agent updaten
    avgs = target.setdefault("prefetch_agent_avgs", {})
    alpha = 0.25
    prev = avgs.get(agent_key, 0)
    new_avg = actual_elapsed if prev == 0 else round(alpha * actual_elapsed + (1 - alpha) * prev, 2)
    avgs[agent_key] = new_avg

    all_avgs = list(avgs.values())
    if all_avgs:
        target_lead = 3.0
        ideal_lead = round(max(2.0, min(15.0, max(all_avgs) - target_lead)), 1)
        current_lead = float(target.get("prefetch_lead_seconds", 8.0))
        if abs(ideal_lead - current_lead) > 1.0:
            target["prefetch_lead_seconds"] = ideal_lead


_prefetch_settings_lock = threading.Lock()


def _flush_prefetch_settings(run_prefetch_state: dict | None = None):
    """Persist prefetch_agent_avgs + prefetch_lead_seconds once after the run."""
    with _prefetch_settings_lock:
        if run_prefetch_state:
            src_avgs = dict(run_prefetch_state.get("prefetch_agent_avgs", {}) or {})
            dst_avgs = settings.setdefault("prefetch_agent_avgs", {})
            for k, v in src_avgs.items():
                try:
                    prev = float(dst_avgs.get(k, 0) or 0)
                    cur = float(v or 0)
                except Exception:
                    continue
                merged = cur if prev <= 0 else round((0.6 * cur) + (0.4 * prev), 2)
                dst_avgs[k] = merged

            try:
                lead_val = float(run_prefetch_state.get("prefetch_lead_seconds", settings.get("prefetch_lead_seconds", 8.0)))
                settings["prefetch_lead_seconds"] = max(2.0, min(20.0, lead_val))
            except Exception:
                pass

        save_settings(settings)


# -- SSE Stream ------------------------------------------------




# ─── Auto-Memory: erkennt Fakten in normalen GesprÃ¤chen ────────────────────────



# ─── run_stream (aus server.py extrahiert, M2b) ───
from core.chat_run import run_stream


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
        base_path           = THIS_FILE,
        ollama_client       = pipeline.ollama,
        model               = reflection_model,
        learning_log_reader = read_learning_log,
        registry_all_fn     = registry_all,
        total_runs          = max(run_count, MIN_RUNS_FOR_EVOLUTION),
    )
    if new_soul:
        append_learning_log(THIS_FILE, reflection_model, {
            "event":                      "soul_evolution_forced",
            "trigger":                    "low_peer_scores",
            "run_count":                  run_count,
            "evolution_count":            new_soul.get("evolution_count", 0),
            "selbstverstaendnis_preview": new_soul.get("selbstverstaendnis", "")[:100],
        })
    # NOTE: run_soul_cycle() is no longer called here ─ it now runs after
    # every completed Duo loop (see line ~12018). Calling it here too caused
    # double-decay: insights lost 0.16 instead of 0.08 per cycle.


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
        base_path           = THIS_FILE,
        ollama_client       = pipeline.ollama,
        model               = reflection_model,
        learning_log_reader = read_learning_log,
        registry_all_fn     = registry_all,
        total_runs          = run_count,
    )
    if new_soul:
        append_learning_log(THIS_FILE, reflection_model, {
            "event":                      "soul_evolution",
            "run_count":                  run_count,
            "evolution_count":            new_soul.get("evolution_count", 0),
            "selbstverstaendnis_preview": new_soul.get("selbstverstaendnis", "")[:100],
        })
    # NOTE: run_soul_cycle() removed here ─ runs after every Duo loop now.
    # Previously caused double-decay when soul evolution + duo completion coincided.


# -- REST Endpoints --------------------------------------------

@app.post("/stream")
async def stream(req: Request):
    body = await req.json()
    q = body.get("q", "")
    images = body.get("images", [])
    mode = body.get("mode", settings.get("mode", "auto"))
    iters = body.get("iterations", body.get("iters", 1))
    preset = ""  # presets removed (2026-09-02) — prompts come from built-ins only
    cmode = body.get("constraint_mode", body.get("cmode", False))
    force_complexity = body.get("force_complexity")
    skip_agents = body.get("skip_agents", [])
    judge_bias = int(body.get("judge_bias", 50))

    #   1. body (vom Frontend gesendet ─ User-Entscheidung)
    #   2. settings (settings.json ─ persistierte User-PrÃ¤ferenz)

    _MISSING = object()
    def _body_or_settings(key: str, settings_key: str | None = None, default=False):
        """body ─ settings ─ default. body ALWAYS wins (including explicit False)."""
        _sk = settings_key or key
        body_val = body.get(key, _MISSING)
        if body_val is not _MISSING:
            return body_val
        if _sk in settings:
            return settings[_sk]
        return default

    def _as_bool(v, default=False):
        """String/Bool/Zahl -> bool. Verhindert bool(\"false\") == True."""
        if v is None or v is _MISSING:
            return bool(default)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        _s = str(v).strip().lower()
        if _s in {"1", "true", "yes", "on", "y"}:
            return True
        if _s in {"0", "false", "no", "off", "n", ""}:
            return False
        return bool(default)

    _model_overrides = {}
    for _model_key in ("duo_planner_model", "duo_coder_model", "duo_critic_model"):
        if _model_key in body and str(body.get(_model_key) or "").strip():
            _model_overrides[_model_key] = str(body.get(_model_key)).strip()

    _duo_tool_rounds_raw = body.get("duo_tool_rounds", None)
    if _duo_tool_rounds_raw is None:
        _duo_tool_rounds_raw = settings.get("duo_tool_rounds", 0)
    duo_tool_rounds = int(_duo_tool_rounds_raw)
    duo_coding_mode = _as_bool(_body_or_settings("duo_coding_mode", default=True), default=True)
    duo_chunking    = _as_bool(_body_or_settings("duo_chunking",   default=False))
    logger.debug(
        "[DIAG-TOGGLE] duo_chunking: body=%r settings=%r final=%r",
        body.get("duo_chunking", "<ABSENT>"),
        settings.get("duo_chunking", "<ABSENT>"),
        duo_chunking,
    )
    duo_planner     = _as_bool(_body_or_settings("duo_planner", "duo_planner_enabled", default=False))
    duo_use_pipeline = bool(body.get("duo_use_pipeline", False))
    duo_pre_explore  = _as_bool(_body_or_settings("duo_pre_explore", default=False))
    duo_parallel_preexplore = _as_bool(_body_or_settings("duo_parallel_preexplore", default=False))
    duo_git_autocommit = _as_bool(_body_or_settings("duo_git_autocommit", default=False))

    duo_agentic_mode       = _as_bool(body.get("duo_agentic_mode",       settings.get("duo_agentic_mode",       False)))
    duo_thinking_per_chunk = _as_bool(_body_or_settings("duo_thinking_per_chunk", default=False))
    duo_agentic_thinking   = _as_bool(body.get("duo_agentic_thinking",   settings.get("duo_agentic_thinking",   False)))
    _duo_tf_legacy = _body_or_settings("duo_test_feedback", default=False)
    duo_test_feedback_chunk = _as_bool(_body_or_settings("duo_test_feedback_chunk", default=_duo_tf_legacy))
    duo_test_feedback_final = _as_bool(_body_or_settings("duo_test_feedback_final", default=_duo_tf_legacy))
    _duo_coder_tool_thinking_explicit = "duo_coder_tool_thinking" in body
    duo_coder_tool_thinking = _as_bool(_body_or_settings("duo_coder_tool_thinking", default=False))
    duo_coder_tool_thinking_auto_mode = str(_body_or_settings("duo_coder_tool_thinking_auto_mode", default="on_fail")).strip().lower()
    chat_id                = body.get("chat_id") or None
    until_finished         = _as_bool(_body_or_settings("until_finished",          default=False))
    duo_runtime_profile    = body.get("duo_runtime_profile") or settings.get("duo_runtime_profile") or "balanced"
    duo_runtime_profile_lock_override = _as_bool(_body_or_settings(
        "duo_runtime_profile_lock_override", default=False
    ))
    important_task         = bool(body.get(
        "important_task",
        body.get("critical_task", False),
    ))

    _stream_gen = run_stream(q, images, mode, int(iters), preset, bool(cmode),
                   force_complexity=force_complexity,
                   skip_agents=skip_agents,
                   judge_bias=judge_bias,
                   chat_id=chat_id,
                   model_overrides=_model_overrides,
                   duo_config=DuoConfig.from_legacy_params(
                       duo_tool_rounds=duo_tool_rounds,
                       duo_coding_mode=duo_coding_mode,
                       duo_chunking=duo_chunking,
                       duo_planner=duo_planner,
                       duo_use_pipeline=duo_use_pipeline,
                       duo_pre_explore=duo_pre_explore,
                       duo_agentic_mode=duo_agentic_mode,
                       duo_agentic_thinking=duo_agentic_thinking,
                       duo_thinking_per_chunk=duo_thinking_per_chunk,
                       duo_test_feedback_chunk=duo_test_feedback_chunk,
                       duo_test_feedback_final=duo_test_feedback_final,
                       duo_coder_tool_thinking=duo_coder_tool_thinking,
                       duo_coder_tool_thinking_explicit=_duo_coder_tool_thinking_explicit,
                       duo_coder_tool_thinking_auto_mode=duo_coder_tool_thinking_auto_mode,
                       until_finished=until_finished,
                       duo_runtime_profile=duo_runtime_profile,
                       duo_runtime_profile_lock_override=duo_runtime_profile_lock_override,
                        important_task=important_task,
                        duo_pass_explore_files=str(_body_or_settings("duo_pass_explore_files", default="touched")).lower(),
                        duo_parallel_preexplore=duo_parallel_preexplore,
                        duo_git_autocommit=duo_git_autocommit,
                    ))

    async def _safe_stream_gen():
        _ka_release = None
        try:
            from infra.keep_awake import acquire as _ka_acq, release as _ka_rel
            _ka_acq()
            _ka_release = _ka_rel
        except Exception:
            pass
        try:
            async for _chunk in _safe_stream_gen_inner():
                yield _chunk
        finally:
            if _ka_release:
                try:
                    _ka_release()
                except Exception:
                    pass

    async def _safe_stream_gen_inner():
        _sse_events = 0
        _saw_done = False
        try:
            async for _chunk in _stream_gen:
                _sse_events += 1
                if not _saw_done and isinstance(_chunk, str) and '"type": "done"' in _chunk:
                    _saw_done = True
                yield _chunk
        except BaseException as _se:
            if isinstance(_se, (GeneratorExit, asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                logger.warning(
                    "/stream aborted (client disconnect/cancel): type=%s mode=%s duo_agentic=%s pre_explore=%s chat_id=%s events=%d",
                    type(_se).__name__, mode, duo_agentic_mode, duo_pre_explore, chat_id, _sse_events,
                    exc_info=True,
                )
                try:
                    from infra.notify import notify
                    notify(
                        "HiveMind — Browser closed during run",
                        f"chat={chat_id or '?'} — stream aborted.",
                        dedup_sig=f"disconnect:{chat_id or 'none'}",
                    )
                except Exception:
                    pass
                logger.warning(
                    "⚠ BROWSER CLOSED — run interrupted (chat=%s, events=%d). "
                    "If a run was active it was parked — send a message to resume.",
                    chat_id, _sse_events,
                )
                raise
            logger.exception("/stream unhandled error: mode=%s duo_agentic=%s pre_explore=%s", mode, duo_agentic_mode, duo_pre_explore)
            _err = f"{type(_se).__name__}: {str(_se)[:160]}"
            yield f"data: {json.dumps({'type': 'status', 'content': '⚠️ Internal stream error — fallback answer sent.'}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'error', 'content': _err}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'elapsed': 0, 'stop_reason': 'error'}, ensure_ascii=False)}\n\n"
        else:
            if _saw_done:
                logger.info(
                    "[RUN-TRACE] /stream generator NORMAL end — %d events, done sent",
                    _sse_events,
                )
            else:
                logger.warning(
                    "[RUN-TRACE] /stream generator NORMAL end - %d events, NO done event (runner early exit?)",
                    _sse_events,
                )

    return StreamingResponse(
        _safe_stream_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    _ico = Path(__file__).parent / "static" / "favicon.ico"
    if _ico.exists():
        return FileResponse(_ico)
    from starlette.responses import Response
    return Response(status_code=204)

# -- OpenAI-Compatible Endpoints (/v1/*) --

async def _v1_agent_mode(msgs, raw_model, temp, max_tok, chat_id, created, do_stream):
    """AGENT MODE: tool-call loop for an OpenAI-compatible endpoint."""
    _TOOL_PRIORITY = ["granite-4.1:3b", "qwen3.5:2b", "qwen3.5:9b-ud", "rnj-1:8b"]
    explicit = raw_model[6:].lstrip(":") if ":" in raw_model.replace("agent", "", 1) else ""
    agent_model = explicit if explicit else next(
        (m for m in _TOOL_PRIORITY if m in S_models_cache),
        S_models_cache[0] if S_models_cache else "qwen3.5:9b-ud"
    )

    _OPENAI_WS_ENABLED = bool(settings.get("duo_websearch_enabled", False)) and _WEBSEARCH_AVAILABLE
    _CODING_TOOLS = _get_inline_tools(include_websearch=_OPENAI_WS_ENABLED, mode="openai_agent")
    _OPENAI_WORKSPACE_LOCK = str(Path(os.environ.get("HIVEMIND_WORKSPACE", ".")).resolve())

    async def _execute_tool(name: str, args: dict, *, model_for_limits: str) -> str:
        if name == "hivemind_pipeline":
            query = args.get("query", "")
            mode = args.get("mode", "auto")
            try:
                tokens = []
                _hivemind_port = os.environ.get("HIVEMIND_PORT", "8080")
                async with httpx.AsyncClient(timeout=180) as client:
                    async with client.stream("POST",
                        f"http://localhost:{_hivemind_port}/stream",
                        json={"q": query, "mode": mode}) as resp:
                        async for line in resp.aiter_lines():
                            if line.startswith("data:"):
                                try:
                                    d = json.loads(line[5:])
                                    if d.get("type") == "token":
                                        tokens.append(d["content"])
                                except Exception:
                                    pass
                return "".join(tokens).strip()
            except Exception as e:
                return _tool_error_response("PIPELINE_CALL_FAILED",
                    f"hivemind_pipeline failed: {type(e).__name__}: {str(e)[:220]}",
                    tool="hivemind_pipeline", mode="openai_agent", retryable=False)
        _tool_args = dict(args or {})
        _tool_args["__model__"] = model_for_limits
        try:
            return await _run_inline_tool(name, _tool_args,
                workspace_lock=_OPENAI_WORKSPACE_LOCK,
                tool_mode="openai_agent", include_websearch=_OPENAI_WS_ENABLED)
        except Exception as e:
            return _tool_error_response("TOOL_EXEC_EXCEPTION",
                f"{type(e).__name__}: {str(e)[:220]}",
                tool=name, mode="openai_agent", retryable=False)

    _agent_state = {
        "stop_reason": "completed", "tool_calls": 0, "tool_errors": 0,
        "last_tool_error": None,
    }
    system_msg = {"role": "system", "content": (
        "Use the available tools proactively for file analysis, code execution, and debugging.\\n"
        "For deep code analysis, combine get_signatures, find_files, and search_code before editing.\\n"
        "Prefer using tools over guessing when data or execution is required.\\n"
        "Be precise, concise, and solution-oriented.\\n"
        "Reply in the same language as the user's latest message.")}
    current_msgs = list(msgs)
    if not any(m.get("role") == "system" for m in current_msgs):
        current_msgs = [system_msg] + current_msgs

    async def _agent_loop():
        loop_msgs = list(current_msgs)
        _ag_ctx = _get_num_ctx(agent_model)
        _ag_opts: dict = {"temperature": temp, "num_predict": max_tok}
        if _ag_ctx:
            _ag_opts["num_ctx"] = _ag_ctx
        async with httpx.AsyncClient(timeout=120) as _agent_client:
            from backend.llama_server_manager import manager as _lsm4
            _ag_port = await _lsm4.ensure_loaded(agent_model, num_ctx=_ag_opts.get("num_ctx"))
            from core.tool_loop import ToolLoop, ToolLoopConfig
            _loop = ToolLoop(
                config=ToolLoopConfig(
                    stream=False, max_rounds=8, max_post_attempts=1,
                    model=agent_model, temperature=_ag_opts.get("temperature", 0.2),
                    max_tokens=_ag_opts.get("num_predict", 800),
                    num_ctx=_ag_opts.get("num_ctx", 4096),
                    tools=_CODING_TOOLS, tool_mode="openai_agent",
                    verify_guard=True,
                ),
                http_client=_agent_client, port=_ag_port,
                custom_executor=lambda name, args: _execute_tool(name, args, model_for_limits=agent_model),
            )
            async for _ev in _loop.run(loop_msgs):
                if _ev["type"] == "token":
                    yield _ev["content"]
            _agent_state["stop_reason"] = _loop.state.stop_reason
            _agent_state["tool_calls"] = _loop.state.tool_calls_made
            _agent_state["tool_errors"] = _loop.state.tool_errors

            if _loop.state.stop_reason == "completed":
                return
            if _loop.state.stop_reason in ("verification_required_after_write", "max_tool_rounds"):
                yield ("Verification required: latest file mutations were not followed by a successful run_bash."
                       if _loop.state.stop_reason == "verification_required_after_write"
                       else "[Max tool rounds reached]")
                return

        _agent_state["stop_reason"] = "max_tool_rounds"
        try:
            from backend.llama_server_manager import manager as _lsm4b
            _final_msgs = list(loop_msgs) + [{"role": "user", "content": (
                "Tool round limit reached. Provide the best possible final answer now. Do not call tools.")}]
            _ag_port2 = await _lsm4b.ensure_loaded(agent_model, num_ctx=_ag_opts.get("num_ctx"))
            async with httpx.AsyncClient(timeout=120) as _fc:
                _final_resp = await _fc.post(
                    f"http://127.0.0.1:{_ag_port2}/v1/chat/completions",
                    json={"model": agent_model, "messages": _final_msgs, "stream": False,
                          "temperature": _ag_opts.get("temperature", 0.2),
                          "max_tokens": _ag_opts.get("num_predict", 800)})
                _final_data = _final_resp.json()
            if "choices" in _final_data:
                _final_msg = _final_data["choices"][0].get("message", {}) if _final_data["choices"] else {}
            else:
                _final_msg = _final_data.get("message", {})
            _final_txt = str(_final_msg.get("content", "") or "").strip()
            if _final_txt:
                yield _final_txt; return
        except Exception:
            pass
        yield "[Max tool rounds reached]"

    def _stop_bucket(sr):
        return _bucket_stop_reason(sr)

    if do_stream:
        async def _agent_stream():
            async for chunk in _agent_loop():
                yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': raw_model, 'choices': [{'index': 0, 'delta': {'content': chunk}, 'finish_reason': None}]})}\\n\\n"
            yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': raw_model, 'stop_reason': _agent_state['stop_reason'], 'stop_reason_bucket': _stop_bucket(_agent_state['stop_reason']), '_meta': {'tool_calls': _agent_state['tool_calls'], 'tool_errors': _agent_state['tool_errors'], 'last_tool_error': _agent_state['last_tool_error']}, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\\n\\n"
            yield "data: [DONE]\\n\\n"
        return StreamingResponse(_agent_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    parts = []
    async for chunk in _agent_loop():
        parts.append(chunk)
    return {"id": chat_id, "object": "chat.completion", "created": created, "model": raw_model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(parts)}, "finish_reason": "stop"}],
            "stop_reason": _agent_state["stop_reason"],
            "stop_reason_bucket": _stop_bucket(_agent_state["stop_reason"]),
            "_meta": {"tool_calls": _agent_state["tool_calls"], "tool_errors": _agent_state["tool_errors"], "last_tool_error": _agent_state["last_tool_error"]},
            "usage": {"prompt_tokens": 0, "completion_tokens": len(parts), "total_tokens": len(parts)}}


async def _v1_direct_mode(msgs, raw_model, temp, max_tok, chat_id, created, do_stream):
    """DIRECT MODE (2026-08-31): simple chat via run_stream.

    Runs in-process through the same direct path as the UI (incl. optional
    chat tools) and returns OpenAI-compatible answers.
    """
    _user_msg = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
    if not _user_msg:
        return JSONResponse({"error": "no user message"}, status_code=400)

    async def _direct_loop():
        _parts: list = []
        try:
            async for _chunk in run_stream(
                _user_msg, [], "simple", 1, None,
                constraint_mode=True, chat_id=None,
            ):
                _s = _chunk if isinstance(_chunk, str) else ""
                if not _s.startswith("data: "):
                    continue
                try:
                    _d = json.loads(_s[6:])
                except Exception:
                    continue
                if _d.get("type") == "token" and _d.get("content"):
                    _txt = str(_d["content"])
                    _parts.append(_txt)
                    yield _txt
                elif _d.get("type") == "done":
                    return
        except Exception as _e:
            _err = f"[direct error: {type(_e).__name__}: {str(_e)[:200]}]"
            _parts.append(_err)
            yield _err

    if do_stream:
        async def _direct_stream():
            async for chunk in _direct_loop():
                yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': raw_model, 'choices': [{'index': 0, 'delta': {'content': chunk}, 'finish_reason': None}]})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(_direct_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    parts = []
    async for chunk in _direct_loop():
        parts.append(chunk)
    return {"id": chat_id, "object": "chat.completion", "created": created, "model": raw_model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(parts)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": len(parts), "total_tokens": len(parts)}}


@app.post("/v1/chat/completions")
async def v1_chat_completions(req: Request):
    """OpenAI-kompatibler /v1/chat/completions Endpunkt."""
    body      = await req.json()
    raw_model = body.get("model", settings.get("agents", {}).get("direct", {}).get("model", "qwen3.5:9b-ud"))
    msgs      = body.get("messages", [])
    do_stream = body.get("stream", False)
    max_tok   = int(body.get("max_tokens", 4096))
    temp      = float(body.get("temperature", 0.2))

    if not msgs:
        return JSONResponse({"error": "messages missing"}, status_code=400)

    chat_id = f"chatcmpl-{int(time.time())}"
    created = int(time.time())

    use_pipeline = raw_model.startswith("pipeline")
    use_agent    = raw_model == "agent" or raw_model.startswith("agent:")
    user_msg     = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
    pipeline_model = raw_model[9:] if raw_model.startswith("pipeline:") else None

    if use_agent:
        return await _v1_agent_mode(msgs, raw_model, temp, max_tok, chat_id, created, do_stream)
    elif use_pipeline:
        return JSONResponse({"error": "pipeline mode not yet implemented"}, status_code=501)
    else:
        return await _v1_direct_mode(msgs, raw_model, temp, max_tok, chat_id, created, do_stream)


@app.get("/", response_class=HTMLResponse)
async def index():
    _here = Path(__file__).parent
    for _candidate in [
        _here / "index.html",
        _here / "templates" / "index.html",
        Path.cwd() / "index.html",
    ]:
        if _candidate.exists():
            return HTMLResponse(
                _candidate.read_text(encoding="utf-8"),
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )
    return HTMLResponse(
        "<html><body><h2>index.html not found</h2>"
        f"<p>Expected at: {_here / 'index.html'}</p>"
        "<p>Please put index.html into the same folder as server.py.</p></body></html>",
        status_code=500
    )

# -- Abort Endpoint --------------------------------------------

# -- Health Endpoint -------------------------------------------

@app.get("/health")
async def health():
    """Global health probe: {status, version, llama_ok, model_count}."""
    try:
        from backend.llama_server_manager import manager as _hm_mgr
        _hm_slots = getattr(_hm_mgr, "_slots", None) or []
        _hm_running = [s for s in _hm_slots if getattr(s, "is_running", False)]
        _hm_models = {getattr(s, "model", None) for s in _hm_slots if getattr(s, "model", None)}
    except Exception:
        _hm_running, _hm_models = [], set()
    return {
        "status": "ok",
        "version": HIVEMIND_VERSION,
        "llama_ok": bool(_hm_running),
        "model_count": len(_hm_models),
    }

# -- Router Registration --
app.include_router(automap_router)
app.include_router(chats_router)
app.include_router(config_router)
app.include_router(core_router)
app.include_router(debug_router)
app.include_router(git_router)
app.include_router(learning_router)
app.include_router(models_router)
app.include_router(soul_router)
app.include_router(vision_router)
app.include_router(vram_router)
app.include_router(websearch_router)
app.include_router(skills_router)
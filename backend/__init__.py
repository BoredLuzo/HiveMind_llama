"""
backend — llama.cpp Backend Package
"""

import os
import sys
from pathlib import Path
from typing import Optional

# ── sys.path Bootstrap ────────────────────────────────────────────────────────
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ── Imports from root-level llama_* modules ───────────────────────────────────
from .llama_compat import (
    tags_list          as _llama_tags,
    ps_list            as _llama_ps,
    model_load         as _llama_load,
    model_prefetch     as _llama_prefetch,
)
from .llama_server_manager import manager as _llama_manager
from .llama_client import LlamaClient as _ClientClass, OllamaError  # noqa: F401
from .llama_config import BASE_PORT

BACKEND      = "llama"
BACKEND_HOST = f"http://127.0.0.1:{int(BASE_PORT)}"


def make_client() -> _ClientClass:
    return _ClientClass()


async def api_tags() -> list[str]:
    data = await _llama_tags()
    return [m["name"] for m in data["models"]]


async def api_ps() -> list[dict]:
    data = await _llama_ps()
    return data["models"]


async def api_generate_load(model: str, keep_alive: str,
                             num_ctx: Optional[int] = None):
    await _llama_load(model, keep_alive=keep_alive, num_ctx=num_ctx)


async def api_generate_pin(model: str, num_ctx: Optional[int] = None):
    await _llama_load(model, keep_alive="-1", num_ctx=num_ctx)


async def api_generate_evict(model: str):
    await _llama_load(model, keep_alive="0")


async def api_prefetch_next(model: str, num_ctx: Optional[int] = None):
    await _llama_prefetch(model, num_ctx=num_ctx)


def start_idle_monitor():
    _llama_manager.start_idle_monitor()


async def shutdown_backend():
    await _llama_manager.shutdown()

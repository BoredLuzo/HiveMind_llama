# -*- coding: utf-8 -*-
"""Welle 2 regressions: GGUF drop-in metadata, model-profile API, nullable UI presets,
and sampling/thinking/ctx parity across all execution paths.

Hermetic (local temp GGUF v3 binary files + mocked transport/loaders).
Run: python tests/test_gguf_dropin_and_profile_api.py
"""
from __future__ import annotations

import asyncio
import copy
import json
import struct
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(pad=True)
check = h.check


# ── Synthetic GGUF v3 writer ────────────────────────────────────────────────

def _gguf_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<Q", len(b)) + b


def _write_gguf_v3(path: Path, kv_items: list[tuple[str, int, object]]) -> None:
    """Write a minimal valid GGUF v3 header with typed KV entries."""
    out = bytearray()
    out += b"GGUF"
    out += struct.pack("<I", 3)       # version 3
    out += struct.pack("<Q", 0)       # tensor_count
    out += struct.pack("<Q", len(kv_items))
    for key, vtype, val in kv_items:
        out += _gguf_str(key)
        out += struct.pack("<I", vtype)
        if vtype == 4:      # UINT32
            out += struct.pack("<I", int(val))  # type: ignore[arg-type]
        elif vtype == 7:    # BOOL
            out += struct.pack("<?", bool(val))
        elif vtype == 8:    # STRING
            out += _gguf_str(str(val))
        elif vtype == 9:    # ARRAY: (etype, list)
            etype, items = val  # type: ignore[misc]
            out += struct.pack("<IQ", etype, len(items))
            for item in items:
                if etype == 4:
                    out += struct.pack("<I", int(item))
                elif etype == 8:
                    out += _gguf_str(str(item))
                else:
                    raise ValueError(f"unsupported array etype {etype}")
        else:
            raise ValueError(f"unsupported vtype {vtype}")
    path.write_bytes(bytes(out))


# ── 1. gguf_meta: dense/hybrid/vision/think + MoE + corrupt safety ──────────

import backend.gguf_meta as gm  # noqa: E402
from backend.llama_config import is_moe_model  # noqa: E402
from backend.llama_vram_table import vram_of, vram_of_with_ctx  # noqa: E402

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    td_path = Path(td)
    gm.clear_cache()

    hybrid_gguf = td_path / "BrandNew-Hybrid-4B-Q4_K_M.gguf"
    _write_gguf_v3(hybrid_gguf, [
        ("general.architecture", 8, "qwen35"),
        ("general.name", 8, "BrandNew Hybrid 4B"),
        ("qwen35.block_count", 4, 32),
        ("qwen35.context_length", 4, 65536),
        ("qwen35.embedding_length", 4, 2560),
        ("qwen35.attention.head_count", 4, 16),
        # GGUF array form where only 1/4 layers store KV (= mean of non-zero = 4):
        ("qwen35.attention.head_count_kv", 9, (4, [0, 0, 0, 4])),
        ("qwen35.attention.key_length", 4, 256),
        ("qwen35.attention.value_length", 4, 256),
        ("qwen35.full_attention_interval", 4, 4),
        ("qwen35.vision.block_count", 4, 12),
        ("tokenizer.chat_template", 8, "{% if enable_thinking %}<think>{% endif %}"),
        ("tokenizer.ggml.tokens", 9, (8, ["hello", "<think>"])),
    ])

    meta_hyb = gm.read_gguf_metadata(hybrid_gguf)
    check("gguf_meta parses architecture/context/head_kv array",
          meta_hyb.get("architecture") == "qwen35"
          and meta_hyb.get("context_length") == 65536
          and meta_hyb.get("head_count_kv") == 4.0)
    check("gguf_meta detects thinking + vision + chat_template",
          meta_hyb.get("has_thinking") is True
          and meta_hyb.get("has_vision") is True
          and meta_hyb.get("has_chat_template") is True
          and meta_hyb.get("is_moe") is False)
    # 8 full layers * 4 kv heads * (256+256) * (18/32) = 9216 bytes/token @ Q4_0
    check("gguf_meta derives hybrid KV bytes/token accurately",
          meta_hyb.get("kv_bytes_per_token_q4") == 9216
          and meta_hyb.get("kv_bytes_per_token_f16") == 32768,
          f"q4={meta_hyb.get('kv_bytes_per_token_q4')} f16={meta_hyb.get('kv_bytes_per_token_f16')}")

    moe_gguf = td_path / "DropIn-MoE-10B-A1.5B-Q4_K_M.gguf"
    _write_gguf_v3(moe_gguf, [
        ("general.architecture", 8, "qwen3moe"),
        ("qwen3moe.block_count", 4, 24),
        ("qwen3moe.context_length", 4, 32768),
        ("qwen3moe.attention.head_count", 4, 16),
        ("qwen3moe.attention.head_count_kv", 4, 4),
        ("qwen3moe.key_length", 4, 128),
        ("qwen3moe.attention.key_length", 4, 128),
        ("qwen3moe.attention.value_length", 4, 128),
        ("qwen3moe.expert_count", 4, 64),
        ("qwen3moe.expert_used_count", 4, 6),
    ])
    # Add dummy payload so file_size_gb >= 0.01 GB (~16 MiB)
    with moe_gguf.open("ab") as f:
        f.write(b"\x00" * (16 * 1024 * 1024))
    gm.clear_cache()

    with mock.patch("backend.llama_models.resolve_model_path", return_value=moe_gguf):
        check("is_moe_model detects GGUF expert_count fallback",
              is_moe_model("dropin-mystery:latest") is True)
        check("vram_of falls back to GGUF file_size_gb instead of defaulting big",
              0.0 < vram_of("dropin-mystery:latest") < 0.05)
        check("vram_of_with_ctx scales via gguf_meta kv_bytes_per_token",
              vram_of_with_ctx("dropin-mystery:latest", 32768) >= vram_of("dropin-mystery:latest"))

    check("is_moe_model decimal regex matches a3.5b and 8x7b",
          is_moe_model("custom-model:30b-a3.5b") is True
          and is_moe_model("mix:8x7b") is True)

    bad_gguf = td_path / "corrupt.gguf"
    bad_gguf.write_bytes(b"NOT_GGUF_12345")
    check("gguf_meta returns {} gracefully on bad magic",
          gm.read_gguf_metadata(bad_gguf) == {})


# ── 2. models_registry + routers.config /gguf-meta & /model-profile ─────────

import model_configs.models_registry as mr  # noqa: E402
import routers.config as rc  # noqa: E402


class _JsonReq:
    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


check("models_registry: dense profile moe_cpu_experts=0 returns None",
      mr.get_moe_cpu_experts("qwen3.5:4b-mtp") is None)
check("models_registry: set_profile_key unknown model returns None",
      mr.set_profile_key("nonexistent-dropin:999b", "gpu_layers", 0) is None)

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    tmp_models = Path(td)
    drop_gguf = tmp_models / "NewDropIn-7B-Q4_K_M.gguf"
    _write_gguf_v3(drop_gguf, [
        ("general.architecture", 8, "llama"),
        ("llama.block_count", 4, 32),
        ("llama.context_length", 4, 16384),
        ("llama.embedding_length", 4, 4096),
        ("llama.attention.head_count", 4, 32),
        ("llama.attention.head_count_kv", 4, 8),
        ("llama.expert_count", 4, 16),
        ("llama.expert_used_count", 4, 2),
        ("tokenizer.chat_template", 8, "{% if enable_thinking %}<think>{% endif %}"),
    ])
    gm.clear_cache()

    orig_models_dir = mr._MODELS_DIR
    try:
        mr._MODELS_DIR = tmp_models
        mr.refresh()

        with mock.patch("backend.llama_models.resolve_model_path", return_value=drop_gguf):
            resp_meta = asyncio.run(rc.get_gguf_meta_endpoint("NewDropIn:7b"))
            check("GET /gguf-meta strips raw chat_template and reports traits",
                  resp_meta.get("ok") is True
                  and "chat_template" not in (resp_meta.get("meta") or {})
                  and (resp_meta.get("meta") or {}).get("has_thinking") is True
                  and (resp_meta.get("meta") or {}).get("is_moe") is True)

            resp_up = asyncio.run(rc.upsert_model_profile_endpoint(_JsonReq({
                "model": "NewDropIn:7b",
                "seed_from_gguf": True,
                "patch": {
                    "gpu_layers": 0,
                    "moe_cpu_experts": 0,
                    "extra_args": "--flash-attn on",
                    "kv_cache_type": "q8_0",
                    "sampling": {
                        "thinking": {"temperature": 0.45, "top_p": 0.9, "repetition_penalty": 1.05}
                    },
                },
            })))
            check("POST /model-profile upserts + seeds integer kv_bytes_per_token",
                  resp_up.get("ok") is True
                  and mr.get_gpu_layers("newdropin:7b") == 0
                  and mr.get_moe_cpu_experts("NewDropIn:7B") == 0
                  and mr.get_extra_args("NewDropIn:7b") == ["--flash-attn", "on"]
                  and mr.get_kv_cache_type("NewDropIn:7b") == "q8_0"
                  and isinstance((mr.get_profile("NewDropIn:7b") or {}).get("kv_bytes_per_token"), int)
                  and ((mr.get_profile("NewDropIn:7b") or {})["kv_bytes_per_token"] > 1000))

            # Validation rejects unsafe mmproj path / bad temperature
            bad_res = asyncio.run(rc.upsert_model_profile_endpoint(_JsonReq({
                "model": "NewDropIn:7b",
                "patch": {"mmproj_filename": "../evil.gguf"},
            })))
            check("POST /model-profile rejects path traversal in mmproj_filename",
                  getattr(bad_res, "status_code", 200) == 400)

            bad_tmp = asyncio.run(rc.upsert_model_profile_endpoint(_JsonReq({
                "model": "NewDropIn:7b",
                "patch": {"sampling": {"thinking": {"temperature": 9.9}}},
            })))
            check("POST /model-profile rejects out-of-range temperature",
                  getattr(bad_tmp, "status_code", 200) == 400)

            # Case-insensitive set_profile_key + GET /model-profile/{model}
            patched = mr.set_profile_key("NEWDROPIN:7B", "num_ctx", 24576)
            check("set_profile_key matches case-insensitively", patched is not None)
            prof_ep = asyncio.run(rc.get_model_profile_path("NewDropIn:7b"))
            check("GET /model-profile/{model} returns saved profile + exists=True",
                  prof_ep.get("exists") is True
                  and (prof_ep.get("profile") or {}).get("num_ctx") == 24576)
    finally:
        mr._MODELS_DIR = orig_models_dir
        mr.refresh()

# Check _phase_model_flags appends extra_args from registry
with mock.patch("backend.llama_start_phases.get_extra_args", return_value=["--foo", "bar"], create=True):
    check("_phase_model_flags integrates get_extra_args into cmd",
          "get_extra_args(model)" in (ROOT / "backend" / "llama_start_phases.py").read_text(encoding="utf-8"))


# ── 3. Nullable "Model Default" agent overrides & parity across paths ─────────

import core.state as _cstate  # noqa: E402
from hive_functions.pipeline import AgentConfig  # noqa: E402
from core.model_sampling import resolve_agent_sampling  # noqa: E402

_orig_settings = copy.deepcopy(rc.settings)
try:
    _ag_test = AgentConfig(
        key="refiner", name="Refiner", model="qwen3.5:4b-mtp",
        temperature=None, max_tokens=500, thinking=None, thinking_budget=0,
    )
    _rt, _rth, _rb, _rsmp = resolve_agent_sampling(
        "qwen3.5:4b-mtp", agent=_ag_test, settings={}, role="refiner",
    )
    check("resolve_agent_sampling inherits thinking & temperature when None",
          _rth is True and abs(_rt - 0.6) < 1e-6 and _rsmp.get("top_k") == 20,
          f"temp={_rt} think={_rth}")

    # Test apply_settings_to_pipeline preserves None (Model Default)
    class _FakePipe:
        agents = {
            "direct": AgentConfig(
                key="direct", name="Direct", model="qwen3.5:4b-mtp",
                temperature=0.5, max_tokens=600, thinking=True, thinking_budget=0,
            ),
        }

    with mock.patch.object(_cstate, "pipeline", _FakePipe()):
        _cstate.apply_settings_to_pipeline({
            "agents": {"direct": {"temperature": None, "thinking": None}},
        })
        check("apply_settings_to_pipeline writes None cleanly (no float(None) crash)",
              _FakePipe.agents["direct"].temperature is None
              and _FakePipe.agents["direct"].thinking is None)
finally:
    rc.settings.clear()
    rc.settings.update(_orig_settings)

# Pipeline runner passes agent_role to get_num_ctx + think=False explicitly + Vision sampling_vl
_pr_src = (ROOT / "core" / "pipeline_runner.py").read_text(encoding="utf-8")
check("pipeline_runner resolves stage params via _resolve_stage_call (role-aware num_ctx)",
      "_resolve_stage_call(ctx, a," in _pr_src and "ctx.get_num_ctx(model, a.key)" in _pr_src)
check("pipeline_runner Vision-Agent uses sampling_vl profile instead of hardcoded repeat_penalty=1.3",
      'mode="sampling_vl"' in _pr_src and '"repeat_penalty": 1.3' not in _pr_src)

# Direct runner plain path passes sampler & resolves Model Default
_dr_src = (ROOT / "core" / "direct_runner.py").read_text(encoding="utf-8")
check("direct_runner plain path resolves sampler + passes sampler=_direct_smp",
      "_resolve_dir_smp" in _dr_src and _dr_src.count("sampler=_direct_smp") >= 2)

# ToolLoop explicit enable_thinking=False when cfg.thinking=False
from core.tool_loop import ToolLoop, ToolLoopConfig  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
import threading  # noqa: E402
import httpx  # noqa: E402

_tl_captured: list[dict] = []


class _TLH(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        _tl_captured.append(json.loads(self.rfile.read(n) or b"{}"))
        out = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


_tl_srv = ThreadingHTTPServer(("127.0.0.1", 0), _TLH)
threading.Thread(target=_tl_srv.serve_forever, daemon=True).start()
try:
    async def _run_tl():
        async with httpx.AsyncClient() as cl:
            loop = ToolLoop(
                config=ToolLoopConfig(
                    stream=False, max_rounds=1, max_post_attempts=1,
                    model="qwen3.5:4b-mtp", thinking=False, require_tool_call=False,
                ),
                http_client=cl, port=_tl_srv.server_address[1], workspace="",
            )
            async for _ in loop.run([{"role": "user", "content": "hi"}]):
                pass
    asyncio.run(_run_tl())
finally:
    _tl_srv.shutdown()
    _tl_srv.server_close()

check("ToolLoop(thinking=False) sends enable_thinking=False + thinking=False",
      bool(_tl_captured)
      and _tl_captured[0].get("thinking") is False
      and (_tl_captured[0].get("chat_template_kwargs") or {}).get("enable_thinking") is False,
      str(_tl_captured[:1]))

# Planner: Jinja models skip /think tag injection and honor sampling overrides
import hive_functions.planner as _pl  # noqa: E402

check("planner skips /think prompt tags for Jinja models via registry/metadata (no qwen3.6 hardcode)",
      _pl._should_inject_prompt_think_tags("qwen3.5:4b-mtp") is False
      and _pl._should_inject_prompt_think_tags("qwen3.6:35b-a3b-ud") is False
      and "startswith(\"qwen3.6\")" not in (ROOT / "hive_functions" / "planner.py").read_text(encoding="utf-8"))

_ptemp, _psmp = _pl._resolve_planner_sampling(
    "qwen3.5:4b-mtp", True,
    {"_model_sampling_overrides": {"qwen3.5:4b-mtp": {"sampling_thinking_code": {"temperature": 0.42}}}},
    fallback_temp=0.2,
)
check("planner sampling resolver honors UI/runtime override temperature",
      abs(_ptemp - 0.42) < 1e-6 and _psmp.get("top_k") == 20, f"temp={_ptemp}")

# Duo critic unified payload carries min_p, cache_prompt, seed, and thinking flags
from core.duo_runner import _build_critic_payload  # noqa: E402

_cp_tc = _build_critic_payload(
    "qwen3.5:4b-mtp", [{"role": "user", "content": "hi"}], [],
    tool_choice="auto", temp_override=None, max_tokens=400,
    critic_profile={"temperature": 0.15, "min_p": 0.0, "cache_prompt": True, "seed": 7},
    critic_thinking=True, critic_thinking_budget=512, settings={},
)
check("duo critic unified payload forwards min_p, cache_prompt, seed, and thinking",
      _cp_tc.get("min_p") == 0.0
      and _cp_tc.get("cache_prompt") is True
      and _cp_tc.get("seed") == 7
      and _cp_tc.get("thinking") is True
      and _cp_tc.get("thinking_budget") == 512
      and (_cp_tc.get("chat_template_kwargs") or {}).get("enable_thinking") is True)

# UI controls & slider bounds
_idx_src = (ROOT / "index.html").read_text(encoding="utf-8")
_app_src = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
check("UI exposes Defaults tab + 0..2.0 temp slider + duo_critic Follow Coder option",
      "cfgtab-defaults" in _idx_src
      and "cfg-defaults-panel" in _idx_src
      and 'max="2"' in _idx_src
      and "Follow Duo-Coder" in _app_src
      and "_isMoeModel" in _app_src)

sys.exit(h.finish("gguf_dropin_and_profile_api"))

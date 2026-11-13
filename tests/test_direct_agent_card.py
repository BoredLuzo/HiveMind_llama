# -*- coding: utf-8 -*-
"""Regression test: Direct Agent Card beats stale settings['direct_model'].

Covers the two fixes for settings.agents.direct.model vs stale
settings['direct_model']:
  1. POST /settings/agent and POST /settings clear stale direct_model when
     updating agents.direct.model (unless direct_model is explicitly posted).
  2. core.chat_run.run_stream strips stale settings['direct_model'] when
     agents.direct.model is configured and the request has no explicit
     model_overrides['direct_model'], while preserving explicit per-run
     direct_model overrides (Telegram /setModel, bench, smoke tests).
"""
import asyncio
import copy
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.chat_run as C  # noqa: E402
import core.state as S  # noqa: E402
import routers.config as rc  # noqa: E402
from infra import notify as NTF  # noqa: E402
from infra import run_control as RC  # noqa: E402
from infra import token_stats as TS  # noqa: E402
from tests._harness import Harness  # noqa: E402
from utils import workspace_resolve as WSR  # noqa: E402

h = Harness()
check = h.check


class _FakeReq:
    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return copy.deepcopy(self._body)


class _FakePipeline:
    def __init__(self):
        self.agents = {
            k: types.SimpleNamespace(
                model=f"m_{k}", temperature=0.2, max_tokens=512,
                thinking=False, thinking_budget=0, name=k,
            )
            for k in ("analyst", "refiner", "critic", "synthesizer",
                      "vision", "direct", "planner", "coder", "judge")
        }
        self.memory = types.SimpleNamespace(as_context_string=lambda: "")

    def _is_list_memory_request(self, _t):
        return False

    def _is_forget_request(self, _t):
        return False

    def _is_memory_request(self, _t):
        return False

    def _is_self_question(self, _t):
        return False


class _FakeMemory:
    _session_chat_id = ""

    def get_session_messages(self, *a, **k):
        return []

    def add_to_session(self, *a, **k):
        pass

    def clear_session(self):
        pass


def _fake_server():
    srv = types.ModuleType("server")
    srv.S_models_cache = []
    srv.THIS_FILE = ROOT / "server.py"
    srv._COMPLEX_DIRECT_MODELS = []
    srv._SIMPLE_DIRECT_MODELS = []
    srv._VRAM_LOOKUP_GB = {}
    srv._WEBSEARCH_AVAILABLE = False
    srv._flush_prefetch_settings = lambda state: None
    srv._get_avg_elapsed = lambda agent: 0.0
    srv._maybe_trigger_soul_evolution = lambda run_count: None
    srv._registry = {"judge": "", "direct": ""}
    srv._update_prefetch_lead = lambda *a, **k: None
    srv._vision_cfg = {}
    srv.apply_learned_configs_for_run = lambda: None
    srv.get_effective_prompt_with_override = lambda *a, **k: ""
    return srv


async def _capture_run_settings(overrides, ws_path: str, settings_patch: dict) -> dict:
    captured = {}

    async def _orch(ctx):
        captured["direct_model"] = ctx.settings.get("direct_model")
        captured["agents_direct"] = ((ctx.settings.get("agents") or {}).get("direct") or {}).get("model")
        yield {"type": "metrics", "metrics": dict(ctx.collect_done_metrics())}
        yield ctx.done_event(0.0, "completed")

    forced = {
        "smart_preload_enabled": False,
        "learning_preset_mode": False,
        "judge_prefetch_before_complexity": False,
        "intent_agent": {"enabled": False},
        "workspace": "",
        **settings_patch,
    }
    events = []
    with mock.patch.dict(sys.modules, {"server": _fake_server()}), \
            mock.patch.object(S, "pipeline", new=_FakePipeline()), \
            mock.patch.object(S, "memory", new=_FakeMemory()), \
            mock.patch.dict(S.settings, forced), \
            mock.patch.object(C, "run_stream_orchestrated", new=_orch), \
            mock.patch.object(WSR, "save_last_workspace", new=lambda ws: None), \
            mock.patch.object(WSR, "sync_env_workspace", new=lambda ws: None), \
            mock.patch.object(TS, "_record_run", new=lambda rec: None), \
            mock.patch.object(NTF, "notify_run_completed", new=lambda *a: None), \
            mock.patch.object(NTF, "notify_run_stopped", new=lambda *a: None):
        async for ev in C.run_stream(
            user_input="hi", images=[], mode="chat", iterations=1,
            active_preset="default", constraint_mode=True,
            force_complexity="simple", model_overrides=overrides,
            request_workspace=ws_path,
        ):
            events.append(ev)
    rid = next((e.get("run_id") for e in events if isinstance(e, dict) and e.get("type") == "run_id"), None)
    if rid:
        RC._unregister_abort(rid)
        RC._unregister_step_skip(rid)
    return captured


async def main():
    # 1. Config routes: set_agent and post_settings clear stale direct_model
    orig_settings = copy.deepcopy(rc.settings)
    orig_pipe = rc._state.pipeline
    try:
        rc._state.pipeline = _FakePipeline()
        with mock.patch.object(rc, "registry_set", new=lambda *a: None), \
                mock.patch.object(rc, "save_settings", new=lambda *a: None), \
                mock.patch.object(rc, "apply_settings_to_pipeline", new=lambda *a: None), \
                mock.patch.object(rc, "_sync_recommended_sampling", new=lambda *a: None), \
                mock.patch.object(rc, "_refresh_safe_profile_policy", new=lambda: None):
            rc.settings.clear()
            rc.settings.update({
                "direct_model": "qwen3.5:4b-mtp",
                "agents": {"direct": {"model": "qwen3.5:4b-mtp"}},
            })
            await rc.set_agent(_FakeReq({"agent": "direct", "model": "lfm3:1.7b-uncensored-ad"}))
            check("set_agent(direct) clears stale direct_model",
                  "direct_model" not in rc.settings
                  and rc.settings["agents"]["direct"]["model"] == "lfm3:1.7b-uncensored-ad",
                  rc.settings.get("direct_model"))

            rc.settings["direct_model"] = "qwen3.5:4b-mtp"
            await rc.post_settings(_FakeReq({"agents": {"direct": {"model": "gemma-4:e4b"}}}))
            check("post_settings(agents.direct.model) clears stale direct_model",
                  "direct_model" not in rc.settings
                  and rc.settings["agents"]["direct"]["model"] == "gemma-4:e4b",
                  rc.settings.get("direct_model"))

            await rc.post_settings(_FakeReq({"direct_model": "qwen3.5:4b-mtp"}))
            check("post_settings(explicit direct_model) preserves direct_model",
                  rc.settings.get("direct_model") == "qwen3.5:4b-mtp",
                  rc.settings.get("direct_model"))
    finally:
        rc._state.pipeline = orig_pipe
        rc.settings.clear()
        rc.settings.update(orig_settings)

    # 2. Runtime: chat_run strips stale direct_model unless overridden per-run
    with tempfile.TemporaryDirectory(prefix="hvm_direct_card_") as tmp:
        c1 = await _capture_run_settings(
            overrides={},
            ws_path=tmp,
            settings_patch={
                "direct_model": "qwen3.5:4b-mtp",
                "agents": {"direct": {"model": "lfm3:1.7b-uncensored-ad"}},
            },
        )
        check("chat_run strips stale direct_model when agents.direct.model is set",
              c1.get("direct_model") is None
              and c1.get("agents_direct") == "lfm3:1.7b-uncensored-ad",
              c1)

        c2 = await _capture_run_settings(
            overrides={"direct_model": "qwen3.5:4b-mtp"},
            ws_path=tmp,
            settings_patch={
                "direct_model": "stale:1b",
                "agents": {"direct": {"model": "lfm3:1.7b-uncensored-ad"}},
            },
        )
        check("chat_run preserves explicit model_overrides['direct_model']",
              c2.get("direct_model") == "qwen3.5:4b-mtp",
              c2)

        c3 = await _capture_run_settings(
            overrides={},
            ws_path=tmp,
            settings_patch={
                "direct_model": "qwen3.5:4b-mtp",
                "agents": {"direct": {"model": ""}},
            },
        )
        check("chat_run keeps settings['direct_model'] if agent card model is empty",
              c3.get("direct_model") == "qwen3.5:4b-mtp",
              c3)


asyncio.run(main())
sys.exit(h.finish("direct_agent_card"))

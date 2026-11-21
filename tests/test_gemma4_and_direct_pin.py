# -*- coding: utf-8 -*-
"""Regression suite for Gemma-4 uncensored/SWA configs, installer shipping, Direct pin handoff, and UI card sync.

Covers:
  1. GGUF header SWA (`sliding_window_pattern`) + `shared_kv_layers` KV calculation.
  2. Shipped Gemma-4 profiles (`e4b-uncensored-hauhaucs-aggressive`, `e4b-it-obliterated`,
     `e4b-it`, `e2b-it-qat`), VRAM estimates @60k ctx, and `_BUILTIN_FILENAME_TAGS`.
  3. Installer/updater shipping invariants (updater `_OBSOLETE_FILES`, Linux/Windows sync).
  4. Direct `pin_direct_after_response` unpin handoff across `direct_runner` and `routers/config`
     plus gating post-response pin on completed non-error runs.
  5. Agent card UI live capability/KV sync (`static/app.js` + `routers/models.py`).
"""
from __future__ import annotations

import asyncio
import copy
import json
import struct
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from backend import gguf_meta as GM  # noqa: E402
from backend import llama_models as LM  # noqa: E402
from backend import llama_vram_table as LVT  # noqa: E402
from core import direct_runner as DR  # noqa: E402
from deploy import updater as UPD  # noqa: E402
from model_configs import models_registry as REG  # noqa: E402
from routers import config as RC  # noqa: E402
from routing import model_automap as MA  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def _pack_gguf_str(s: str) -> bytes:
    raw = s.encode("utf-8")
    return struct.pack("<Q", len(raw)) + raw


def _write_synthetic_gemma4_gguf(path: Path) -> None:
    """Write a minimal valid GGUF v3 header mirroring Gemma-4 E4B SWA + shared KV."""
    swa_pattern = ([1, 1, 1, 1, 1, 0] * 7)  # 42 layers: 4 full in first 24, 3 in shared 18
    kvs: list[tuple[str, int, bytes]] = [
        ("general.architecture", 8, _pack_gguf_str("gemma4")),
        ("general.name", 8, _pack_gguf_str("Gemma-4-E4B-Synthetic")),
        ("gemma4.block_count", 4, struct.pack("<I", 42)),
        ("gemma4.context_length", 4, struct.pack("<I", 131072)),
        ("gemma4.embedding_length", 4, struct.pack("<I", 2560)),
        ("gemma4.attention.head_count", 4, struct.pack("<I", 8)),
        ("gemma4.attention.head_count_kv", 4, struct.pack("<I", 2)),
        ("gemma4.attention.key_length", 4, struct.pack("<I", 512)),
        ("gemma4.attention.value_length", 4, struct.pack("<I", 512)),
        ("gemma4.attention.sliding_window", 4, struct.pack("<I", 512)),
        ("gemma4.attention.shared_kv_layers", 4, struct.pack("<I", 18)),
        (
            "gemma4.attention.sliding_window_pattern",
            9,
            struct.pack("<IQ", 7, len(swa_pattern))
            + b"".join(struct.pack("<?", bool(x)) for x in swa_pattern),
        ),
    ]
    buf = bytearray()
    buf.extend(b"GGUF")
    buf.extend(struct.pack("<I", 3))
    buf.extend(struct.pack("<QQ", 0, len(kvs)))
    for key, vtype, payload in kvs:
        buf.extend(_pack_gguf_str(key))
        buf.extend(struct.pack("<I", vtype))
        buf.extend(payload)
    path.write_bytes(bytes(buf))


# ── 1) GGUF SWA + shared_kv_layers KV derivation ────────────────────────────
with tempfile.TemporaryDirectory(prefix="hm_g4_swa_") as _td:
    gguf_path = Path(_td) / "Gemma-4-E4B-Uncensored-Test.gguf"
    _write_synthetic_gemma4_gguf(gguf_path)
    GM.clear_cache()
    meta = GM.read_gguf_metadata(gguf_path)
    check(
        "1a GGUF SWA + shared_kv_layers yields exact 16384 B/tok f16 KV (not 172032)",
        meta.get("kv_bytes_per_token_f16") == 16384 and meta.get("kv_bytes_per_token_q4") == 4608,
        f"(got f16={meta.get('kv_bytes_per_token_f16')}, q4={meta.get('kv_bytes_per_token_q4')})",
    )

q4_dense, f16_dense = GM._derive_kv_bytes(32, 32, 8, 4096, 128, 128, None)
check(
    "1b dense _derive_kv_bytes without SWA still scales across all layers",
    f16_dense == 32 * 8 * 256 * 2 and q4_dense == round(32 * 8 * 256 * (18.0 / 32.0)),
    f"(got f16={f16_dense}, q4={q4_dense})",
)

# ── 2) Shipped Gemma-4 configs, VRAM @60k, and filename tag pins ────────────
REG.refresh()
LVT.reload_compat_views()

p_unc = REG.get_profile("gemma-4:e4b-uncensored-hauhaucs-aggressive")
check(
    "2a gemma-4 uncensored profile sets flash_attn=False, kv_bpt=16384, thinking+vision+tool_call, 32k ctx",
    p_unc.get("flash_attn") is False
    and p_unc.get("kv_bytes_per_token") == 16384
    and p_unc.get("reasoning") == "on"
    and p_unc.get("num_ctx") == 32768
    and p_unc.get("gpu_layers") is None
    and p_unc.get("mmproj_filename") == "mmproj-Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-f16.gguf"
    and p_unc.get("capabilities") == {"thinking": True, "vision": True, "tool_call": True},
    f"(got {p_unc})",
)

v_unc_60k = LVT.vram_of_with_ctx("gemma-4:e4b-uncensored-hauhaucs-aggressive", 60416)
check(
    "2b gemma-4 uncensored @60416 ctx estimates < 6.5 GB (fits 8 GB GPU with margin)",
    5.5 < v_unc_60k < 6.5,
    f"(got {v_unc_60k} GB)",
)

p_obl = REG.get_profile("gemma-4:e4b-it-obliterated")
check(
    "2c gemma-4 obliterated profile sets flash_attn=False, kv_bpt=16384, text-only capabilities",
    p_obl.get("flash_attn") is False
    and p_obl.get("kv_bytes_per_token") == 16384
    and p_obl.get("reasoning") == "on"
    and MA.get_model_capabilities("gemma-4:e4b-it-obliterated") == {"thinking": True, "vision": False, "tool_call": True}
    and abs(LVT.vram_of("gemma-4:e4b-it-obliterated") - 5.8) < 0.05,
    f"(got prof={p_obl}, vram={LVT.vram_of('gemma-4:e4b-it-obliterated')})",
)

v_obl_60k = LVT.vram_of_with_ctx("gemma-4:e4b-it-obliterated", 60416)
check(
    "2d gemma-4 obliterated @60416 ctx stays < 7.0 GB",
    6.0 < v_obl_60k < 7.0,
    f"(got {v_obl_60k} GB)",
)

check(
    "2e _BUILTIN_FILENAME_TAGS pins all 4 Gemma-4 GGUF filenames",
    LM._BUILTIN_FILENAME_TAGS.get("gemma-4-e4b-it-qat-ud-q4_k_xl.gguf") == "gemma-4:e4b-it-qat"
    and LM._BUILTIN_FILENAME_TAGS.get("gemma-4-e2b-it-qat-ud-q4_k_xl.gguf") == "gemma-4:e2b-it-qat"
    and LM._BUILTIN_FILENAME_TAGS.get("gemma-4-e4b-uncensored-hauhaucs-aggressive-q4_k_p.gguf")
    == "gemma-4:e4b-uncensored-hauhaucs-aggressive"
    and LM._BUILTIN_FILENAME_TAGS.get("gemma-4-e4b-it-obliterated-q5_k_m.gguf") == "gemma-4:e4b-it-obliterated",
)

# ── 3) Installer / Updater shipping invariants ──────────────────────────────
gemma_configs = [
    "gemma-4_e2b-it-qat.json",
    "gemma-4_e4b-it.json",
    "gemma-4_e4b-uncensored-hauhaucs-aggressive.json",
    "gemma-4_e4b-it-obliterated.json",
]
models_dir = ROOT / "model_configs" / "models"
obsolete_lower = {str(x).replace("/", "\\").lower() for x in UPD._LEGACY_PRUNE_FILES}
check(
    "3a all 4 Gemma-4 model configs exist in model_configs/models and are valid JSON",
    all((models_dir / fn).is_file() and isinstance(json.loads((models_dir / fn).read_text(encoding="utf-8")), dict)
        for fn in gemma_configs),
)
check(
    "3b deploy/updater.py does not list any Gemma-4 config in _LEGACY_PRUNE_FILES",
    all(f"model_configs\\models\\{fn}".lower() not in obsolete_lower for fn in gemma_configs),
)
check(
    "3c deploy/updater.py preserves user/learned while shipping model_configs/models",
    "model_configs/models" not in (ROOT / "deploy" / "install_linux.sh").read_text(encoding="utf-8")
    and "model_configs\\models" not in (ROOT / "deploy" / "update_prune.ps1").read_text(encoding="utf-8"),
)

# ── 4) Direct pin_direct_after_response handoff, unload & error gating ──────
async def _test_direct_pin_handoff() -> None:
    unpinned: list[str] = []
    evicted: list[str] = []

    class _FakeSlot:
        def __init__(self, model: str, *, pinned: bool = True, in_flight: int = 0, running: bool = True):
            self.model = model
            self.pinned = pinned
            self.in_flight = in_flight
            self.is_running = running
            self._loading = False

    class _FakeMgr:
        def __init__(self):
            self.slots: dict[str, _FakeSlot] = {}

        def _find_loaded_sync(self, model: str):
            return self.slots.get(model)

        async def unpin(self, model: str, keep_alive_seconds: float = 600.0):
            unpinned.append(model)
            s = self.slots.get(model)
            if s:
                s.pinned = False

        async def evict(self, model: str):
            evicted.append(model)
            self.slots.pop(model, None)

    fake_mgr = _FakeMgr()
    with mock.patch("backend.llama_client.manager", fake_mgr):
        fake_mgr.slots["gemma-4:e4b-it-qat"] = _FakeSlot("gemma-4:e4b-it-qat", pinned=True)
        DR._LAST_DIRECT_PINNED_MODEL = "gemma-4:e4b-it-qat"
        same = await DR.unpin_previous_direct_model("gemma-4:e4b-it-qat")
        check("4a same-model switch does not unpin or evict", same is False and unpinned == [] and evicted == [])

        switched = await DR.unpin_previous_direct_model("gemma-4:e4b-uncensored-hauhaucs-aggressive")
        check(
            "4b different-model switch unpins AND unloads idle pinned direct model",
            switched is True
            and unpinned == ["gemma-4:e4b-it-qat"]
            and evicted == ["gemma-4:e4b-it-qat"]
            and DR._LAST_DIRECT_PINNED_MODEL == "",
            f"(switched={switched}, unpinned={unpinned}, evicted={evicted}, tracked={DR._LAST_DIRECT_PINNED_MODEL!r})",
        )

        # In-flight pinned slot must only be unpinned, never killed mid-request
        unpinned.clear()
        evicted.clear()
        fake_mgr.slots["gemma-4:e4b-it-qat"] = _FakeSlot("gemma-4:e4b-it-qat", pinned=True, in_flight=1)
        DR._LAST_DIRECT_PINNED_MODEL = "gemma-4:e4b-it-qat"
        await DR.unpin_previous_direct_model("gemma-4:e4b-uncensored-hauhaucs-aggressive")
        check(
            "4b2 in-flight pinned direct model is unpinned without mid-stream eviction",
            unpinned == ["gemma-4:e4b-it-qat"] and evicted == [],
            f"(unpinned={unpinned}, evicted={evicted})",
        )

    # Test routers/config.py POST /settings/agent & POST /settings unpin+unload handoff
    unpinned.clear()
    evicted.clear()
    fake_mgr.slots.clear()

    class _FakeReq:
        def __init__(self, body: dict):
            self._body = body

        async def json(self):
            return copy.deepcopy(self._body)

    fake_pipe = types.SimpleNamespace(
        agents={
            "direct": types.SimpleNamespace(
                model="gemma-4:e4b-it-qat", temperature=1.0, max_tokens=600,
                thinking=True, thinking_budget=7000, name="direct",
            ),
            "judge": types.SimpleNamespace(
                model="minicpm5:2b-sharp", temperature=0.0, max_tokens=256,
                thinking=False, thinking_budget=0, name="judge",
            ),
        }
    )
    orig_settings = copy.deepcopy(RC.settings)
    orig_pipe = RC._state.pipeline
    try:
        RC._state.pipeline = fake_pipe
        RC.settings["agents"] = {
            "direct": {"model": "gemma-4:e4b-it-qat"},
            "judge": {"model": "minicpm5:2b-sharp"},
        }
        fake_mgr.slots["gemma-4:e4b-it-qat"] = _FakeSlot("gemma-4:e4b-it-qat", pinned=True)
        with (
            mock.patch("backend.llama_client.manager", fake_mgr),
            mock.patch.object(RC, "save_settings", lambda *_a, **_k: None),
            mock.patch.object(RC, "registry_set", lambda *_a, **_k: None),
            mock.patch.object(RC, "registry_all", lambda: {"judge": "minicpm5:2b-sharp"}),
        ):
            res = await RC.set_agent(_FakeReq({"agent": "direct", "model": "gemma-4:e4b-uncensored-hauhaucs-aggressive"}))
            check(
                "4c POST /settings/agent unpins AND unloads idle pinned direct model on switch",
                isinstance(res, dict)
                and res.get("ok") is True
                and unpinned == ["gemma-4:e4b-it-qat"]
                and evicted == ["gemma-4:e4b-it-qat"],
                f"(res={res}, unpinned={unpinned}, evicted={evicted})",
            )

            # Never unpin or unload if the old direct model is also the judge model
            unpinned.clear()
            evicted.clear()
            fake_mgr.slots["minicpm5:2b-sharp"] = _FakeSlot("minicpm5:2b-sharp", pinned=True)
            RC.settings["agents"]["direct"]["model"] = "minicpm5:2b-sharp"
            res2 = await RC.set_agent(_FakeReq({"agent": "direct", "model": "gemma-4:e4b-it-qat"}))
            check(
                "4d POST /settings/agent never unpins or unloads a model shared with judge",
                isinstance(res2, dict) and res2.get("ok") is True and unpinned == [] and evicted == [],
                f"(unpinned={unpinned}, evicted={evicted})",
            )

            # POST /settings turning off pin_direct_after_response unpins without killing active model
            unpinned.clear()
            evicted.clear()
            fake_mgr.slots["gemma-4:e4b-it-qat"] = _FakeSlot("gemma-4:e4b-it-qat", pinned=True)
            DR._LAST_DIRECT_PINNED_MODEL = "gemma-4:e4b-it-qat"
            with (
                mock.patch.object(RC, "apply_settings_to_pipeline", lambda *_a, **_k: None),
                mock.patch.object(RC, "_refresh_safe_profile_policy", lambda *_a, **_k: None),
            ):
                res3 = await RC.post_settings(_FakeReq({"pin_direct_after_response": False}))
            check(
                "4e POST /settings pin_direct_after_response=False unpins without evicting active model",
                isinstance(res3, dict)
                and res3.get("ok") is True
                and unpinned == ["gemma-4:e4b-it-qat"]
                and evicted == [],
                f"(res={res3}, unpinned={unpinned}, evicted={evicted})",
            )
    finally:
        RC.settings.clear()
        RC.settings.update(orig_settings)
        RC._state.pipeline = orig_pipe
        DR._LAST_DIRECT_PINNED_MODEL = ""


asyncio.run(_test_direct_pin_handoff())

dr_src = (ROOT / "core" / "direct_runner.py").read_text(encoding="utf-8")
check(
    "4f post-response pin is gated on completed non-error direct runs",
    'and _direct_stop_reason == "completed"' in dr_src and "and not _direct_had_err" in dr_src,
)

# ── 5) UI Agent Card live sync & /models kv_bytes_per_token ─────────────────
app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
models_py = (ROOT / "routers" / "models.py").read_text(encoding="utf-8")
check(
    "5a static/app.js updates agent explain badge and model-aware KV label on dropdown switch",
    "function _agentExplainHtml(" in app_js
    and "function _agentKvLabel(" in app_js
    and "data-agent-explain" in app_js
    and "prof.kv_bytes_per_token" in app_js,
)
check(
    "5b routers/models.py exposes kv_bytes_per_token in /models profiles",
    'p["kv_bytes_per_token"]' in models_py,
)

h.finish()

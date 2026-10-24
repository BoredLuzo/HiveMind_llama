"""Sprint-B audit fixes: queue-aware AMD wait, MoE KV override, GPU auto-fit,
warm whisper-server path, planner-handoff ctx pin, slow-independent tool
parallelism. Pure helpers get behavioral checks; the in-loop changes get
structural pins (the loop itself is covered by the executor suites)."""
import asyncio
import httpx
import sys
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── B2: MoE KV cache types ───────────────────────────────────────────────
from backend import llama_config as LC  # noqa: E402

check("moe kv default is q4_0", LC.moe_kv_cache_types("unknown-model:7b") == "q4_0")
LC._MOE_KV_CACHE_TYPES["manual-test:7b"] = "q8_0"
check("moe kv manual dict wins",
      LC.moe_kv_cache_types("manual-test:7b") == "q8_0")
del LC._MOE_KV_CACHE_TYPES["manual-test:7b"]

def _fake_gp(key):
    if key == "profile-test:7b":
        return {"kv_cache_type": "q5_1"}
    return {}


# patch the registry accessor the helper imports lazily
import model_configs.models_registry as _mr  # noqa: E402
_gp_orig = _mr.get_profile
_mr.get_profile = _fake_gp
try:
    check("moe kv profile override honored",
          LC.moe_kv_cache_types("profile-test:7b") == "q5_1")
finally:
    _mr.get_profile = _gp_orig

# ── B3: gpu layers resolution ────────────────────────────────────────────
TABLE = {"tabled-model:4b": 33}
check("gpu override wins over everything",
      LC.resolve_gpu_layers("any", 12, TABLE, auto=True) == 12)
check("gpu table entry survives auto",
      LC.resolve_gpu_layers("tabled-model:4b", None, TABLE, auto=True) == 33)
check("gpu auto omits the flag for unlisted models",
      LC.resolve_gpu_layers("unknown:7b", None, TABLE, auto=True) is None)
check("gpu default stays 99 without auto",
      LC.resolve_gpu_layers("unknown:7b", None, TABLE, auto=False) == 99)

import settings as _settings_mod  # noqa: E402
_orig_ls = _settings_mod.load_settings
_settings_mod.load_settings = lambda: {"llama_gpu_layers_auto": True}
try:
    check("gpu auto reads the setting", LC.llama_gpu_layers_auto() is True)
finally:
    _settings_mod.load_settings = _orig_ls

# ── B4: warm whisper-server path ─────────────────────────────────────────
import core.stt_engine as SE  # noqa: E402

import tempfile  # noqa: E402
_wav_file = Path(tempfile.mkdtemp(prefix="auditb_")) / "a.wav"
_wav_file.write_bytes(b"RIFF0000WAVEfmt ")   # only read_bytes() is called

_orig_warm = SE.warm_is_active
_orig_timeout = SE.resolve_timeout


def _with_warm(active, status, payload, raise_exc=None):
    SE.warm_is_active = lambda key="": active
    SE.resolve_timeout = lambda s: 60
    SE._WARM.update({"proc": object(), "port": 51111, "model": "ggml-base"})
    class _Resp:
        def __init__(self):
            self.status_code = status

        def json(self):
            return payload

    class _Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, files=None, data=None):
            if raise_exc is not None:
                raise raise_exc
            return _Resp()

    SE.httpx = NS(Client=_Client, Timeout=httpx.Timeout,
                  TimeoutException=httpx.TimeoutException,
                  HTTPError=httpx.HTTPError)


try:
    _with_warm(active=False, status=200, payload={})
    check("warm path returns None when no warm server",
          SE.transcribe_via_warm_server("ggml-base", _wav_file, {}) is None)

    _with_warm(active=True, status=200, payload={"text": "hello world"})
    check("warm server transcript returned",
          SE.transcribe_via_warm_server("ggml-base", _wav_file, {}) == "hello world")

    _with_warm(active=True, status=500, payload={})
    check("warm server HTTP error falls back to CLI",
          SE.transcribe_via_warm_server("ggml-base", _wav_file, {}) is None)

    _with_warm(active=True, status=200, payload={},
               raise_exc=httpx.ReadTimeout("timed out"))
    try:
        SE.transcribe_via_warm_server("ggml-base", _wav_file, {})
        check("warm server timeout raises STT_TIMEOUT", False)
    except SE.SttError as e:
        check("warm server timeout raises STT_TIMEOUT", getattr(e, "code", "") == "STT_TIMEOUT"
              or "TIMEOUT" in str(getattr(e, "code", "")) or "TIMEOUT" in str(e))

    _with_warm(active=True, status=200, payload={},
               raise_exc=httpx.ConnectError("refused"))
    check("warm server connect error falls back to CLI",
          SE.transcribe_via_warm_server("ggml-base", _wav_file, {}) is None)
finally:
    SE.warm_is_active = _orig_warm
    SE.resolve_timeout = _orig_timeout
    import httpx as _real_httpx
    SE.httpx = _real_httpx
    SE._WARM.update({"proc": None, "port": 0, "model": ""})

check("transcribe consults the warm path before the CLI",
      "transcribe_via_warm_server(key, wav, settings)" in
      (ROOT / "core" / "stt_engine.py").read_text(encoding="utf-8", errors="replace"))

# ── B1: queue-aware AMD commit wait (structural) ─────────────────────────
ml = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8", errors="replace")
check("ensure_loaded bumps the load sequence",
      "self._load_seq = getattr(self, \"_load_seq\", 0) + 1" in ml
      and "_my_load_seq" in ml)
check("the commit wait gates on a real successor",
      "_successor_pending" in ml and "self._load_seq != _entry_seq" in ml
      and "if _peers_active and _successor_pending:" in ml)

# ── B5: planner-handoff ctx pin (structural) ─────────────────────────────
dr = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
check("planner load ctx helper exists and covers the coder need",
      "def _planner_load_ctx(model: str, ctx_final: int) -> int:" in dr
      and "return max(ctx_final, int(_need)) if _need else ctx_final" in dr)
check("the primary planner load uses the pin",
      "num_ctx=_pin_ctx" in dr and "_pin_ctx = _planner_load_ctx(" in dr)
check("the pin is announced, not silent (audit F4)",
      "Planner ctx pinned" in dr)
check("the handoff reuse check still refuses degraded slots",
      "_plan_port_actual_ctx >= _coder_ctx_need" in dr)

# ── measurement-round fixes (2026-10-24): MTP env bypass, mlock no-op,
#    spec+load-mode-none crash guard ───────────────────────────────────────
ml_src = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8", errors="replace")
srv_src = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
check("the registry-MTP block honors the env off-switch",
      "ENV-BYPASS FIX" in ml_src
      and 'and os.environ.get("HIVEMIND_MTP_SPEC", "1") not in' in ml_src)
check("llama_mlock reaches the load path (dynamic read, canonical module)",
      "_llama_cfg.MLOCK_MODEL" in ml_src and "MLOCK_MODEL" not in ml_src.split("from .llama_config import")[1].split(")")[0]
      and "_lc_init.MLOCK_MODEL = bool(settings.get" in srv_src)
check("speculative models keep mmap even with mlock off (OOM guard)",
      "_speculation_planned" in ml_src
      and '"mmap+mlock" if (_mlock_now or _spec_planned) else "none"' in ml_src)

# behavioral: _speculation_planned
import backend.manager_load as ML
_patched_env = {"HIVEMIND_MTP_SPEC": "1"}
class _MLProbe:
    _speculation_planned = ML.LlamaLoadMixin._speculation_planned
probe = _MLProbe()

import model_configs.models_registry as _mr2
_gp2 = _mr2.get_profile
_dd_cfg = _mr2.get_dspark_draft_filename if hasattr(_mr2, "get_dspark_draft_filename") else None
try:
    _mr2.get_profile = lambda k: {}
    if _dd_cfg:
        _mr2.get_dspark_draft_filename = lambda k: None
    import os as _os2
    _os2.environ["HIVEMIND_MTP_SPEC"] = "1"
    check("an MTP name-list model plans speculation",
          probe._speculation_planned("qwen3.5:4b-mtp") is True)
    _os2.environ["HIVEMIND_MTP_SPEC"] = "0"
    check("env-off + plain model plans no speculation",
          probe._speculation_planned("some-unknown:7b") is False)
    _os2.environ["HIVEMIND_MTP_SPEC"] = "0"
    _mr2.get_profile = lambda k: {"mtp": True}
    check("registry-mtp model with env off plans no speculation",
          probe._speculation_planned("reg-mtp:7b") is False)
finally:
    _mr2.get_profile = _gp2
    if _dd_cfg:
        _mr2.get_dspark_draft_filename = _dd_cfg
    import os as _os3
    _os3.environ.pop("HIVEMIND_MTP_SPEC", None)


# ── B6: slow-independent tool parallelism (behavioral) ────────────────────
import core.tool_exec_helpers as TH  # noqa: E402


def _tc(name):
    return {"function": {"name": name, "arguments": {}}}


async def _run_gate(calls):
    started, released = [], {}
    lock = asyncio.Lock()

    async def _fake(name, args, **kw):
        async with lock:
            started.append(name)
        if name == "run_tests":
            # Prove the reads ran CONCURRENTLY: wait for the read to have
            # started, fail the scenario if it never does (serial would
            # deadlock into the timeout below).
            if "read_file" in calls:
                try:
                    await asyncio.wait_for(released.setdefault(
                        "read", asyncio.Event()).wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    return "[SERIAL-DEADLOCK]"
        elif name == "read_file":
            async with lock:
                if "run_tests" in started:
                    released.setdefault("read", asyncio.Event()).set()
        return f"ok:{name}"

    _orig = TH._run_inline_tool
    TH._run_inline_tool = _fake
    try:
        res = await TH._prefetch_readonly_tools(
            [_tc(n) for n in calls], "m", "ws", "tools", False)
    finally:
        TH._run_inline_tool = _orig
    return started, res


async def _b6():
    out = {}
    out["reads"] = await _run_gate(["read_file", "search_code"])
    out["test_read"] = await _run_gate(["run_tests", "read_file"])
    out["test_write"] = await _run_gate(["run_tests", "write_file"])
    out["two_tests"] = await _run_gate(["run_tests", "run_tests"])
    out["write_read"] = await _run_gate(["write_file", "read_file"])
    return out


_b6r = asyncio.run(_b6())
s, r = _b6r["reads"]
check("reads still run parallel (both prefetched)", len(r) == 2, f"({sorted(r)})")
s, r = _b6r["test_read"]
check("run_tests + read runs parallel (the measured 90s win)",
      len(r) == 2 and "[SERIAL-DEADLOCK]" not in str(r), f"({sorted(r)})")
s, r = _b6r["test_write"]
check("run_tests next to a write stays serial", r == {})
s, r = _b6r["two_tests"]
check("two run_tests never run together (pytest cache races)", r == {})
s, r = _b6r["write_read"]
check("write + read keeps the serial order guarantee", r == {})

print()
print(f"SUMMARY audit_b: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

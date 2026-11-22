# -*- coding: utf-8 -*-
"""Wave 3C regression suite: isolated deep-audit P0/P1/P2 fixes."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests._harness import Harness

h = Harness()
check = h.check


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ── Step 1: A100 / Autofit / Installers / Fetcher ──────────────────────
print("-- Step 1: Autofit, installers, fetcher --")
from deploy import hardware_autofit as HAF  # noqa: E402

_tier80, _p80 = HAF.compute_profile_patch(profile="autofit", vram_gb=80.0, backend="cuda")
check("80GB profile tier is datacenter_40gb_plus", _tier80 == "datacenter_40gb_plus", repr(_tier80))
check("80GB profile disables duo_parallel_preexplore", _p80.get("duo_parallel_preexplore") is False)
check("80GB profile sets llama_max_slots == 4", _p80.get("llama_max_slots") == 4)

_tier24, _p24 = HAF.compute_profile_patch(profile="autofit", vram_gb=24.0, backend="cuda")
check("24GB profile tier is high_vram_24gb", _tier24 == "high_vram_24gb", repr(_tier24))
check("24GB profile disables duo_parallel_preexplore", _p24.get("duo_parallel_preexplore") is False)
check("24GB profile sets llama_max_slots == 3", _p24.get("llama_max_slots") == 3)

import backend.llama_config as LC  # noqa: E402
check("MAX_SLOTS seeded cleanly in 1..16", isinstance(LC.MAX_SLOTS, int) and 1 <= LC.MAX_SLOTS <= 16)
_lc_src = _read("backend/llama_config.py")
check("MAX_SLOTS reads llama_max_slots from settings", 'get("llama_max_slots"' in _lc_src)

_sh_bytes = (_ROOT / "deploy/install_linux.sh").read_bytes()
_sh_txt = _sh_bytes.decode("utf-8")
check("install_linux.sh is LF-only", b"\r" not in _sh_bytes)
check("install_linux.sh avoids SIGPIPE grep -q on nvidia-smi", "nvidia-smi" in _sh_txt and "nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | grep" not in _sh_txt)
check("install_linux.sh scales MemoryMax via /proc/meminfo", "MemTotal" in _sh_txt and "MemoryMax=" in _sh_txt)

_bat_txt = _read("install.bat")
check("install.bat branches keep-vram-budget on VRAM_IN", "if defined VRAM_IN" in _bat_txt and "--keep-vram-budget" in _bat_txt)

_fm_txt = _read("deploy/fetch_models.py")
check("fetch_models sorts family keys longest-first", "sorted(families, key=len, reverse=True)" in _fm_txt)


# ── Step 2: Backend VRAM, MoE Lift, Runtime Sync, Slot Lifecycle ───────
print("-- Step 2: Backend VRAM, MoE lift, sync, slots --")
import backend.llama_vram_table as LVT  # noqa: E402
import backend.llama_slots as LS  # noqa: E402
import model_configs.models_registry as MR  # noqa: E402

with mock.patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
     mock.patch("subprocess.run") as _mrun:
    _mrun.return_value = mock.Mock(returncode=0, stdout=b"81920.5\n", stderr=b"")
    _free_mib = LVT._probe_nvidia_smi_free_mib()
    check("_probe_nvidia_smi_free_mib parses float MiB", _free_mib is not None and abs(_free_mib - 81920.5) < 0.01, repr(_free_mib))

with mock.patch("platform.system", return_value="Linux"), \
     mock.patch.object(LVT, "_current_gpu_backend", return_value="cuda"), \
     mock.patch.object(LVT, "_probe_nvidia_smi_free_mib", return_value=40960.0), \
     mock.patch.object(LVT, "_probe_linux_sysfs_free_mib", return_value=100.0):
    check("Linux CUDA prefers nvidia-smi over sysfs free probe", LVT.get_live_gpu_free_mib() == 40960.0)

_dummy_slot = LS.ModelSlot(0)
_dummy_slot.model = "m"
_dummy_slot._num_ctx = 8192
_dummy_slot._moe_all_gpu_vram_gb = 19.25
check("slot_effective_vram_gb uses max(est, _moe_all_gpu_vram_gb)",
      abs(LVT.slot_effective_vram_gb(_dummy_slot, vram_fn=lambda *_a, **_kw: 12.0) - 19.25) < 0.01)

_dummy_slot.process = None
_dummy_slot.kill()
check("ModelSlot.kill resets _moe_all_gpu_vram_gb to 0.0", _dummy_slot._moe_all_gpu_vram_gb == 0.0)

MR._ensure_cache()
with mock.patch.dict(MR._user_cache, {"demo-model": {"moe_cpu_experts": 12}}, clear=True):
    check("has_user_profile_key True for user override", MR.has_user_profile_key("demo-model", "moe_cpu_experts") is True)
    check("has_user_profile_key False for unset key", MR.has_user_profile_key("demo-model", "ctx_size") is False)
with mock.patch.dict(MR._user_cache, {}, clear=True):
    check("has_user_profile_key False when user layer empty", MR.has_user_profile_key("demo-model", "moe_cpu_experts") is False)

_rs_src = _read("backend/runtime_sync.py")
check("runtime_sync prefers canonical llama_kv_cache_type", 'settings.get("llama_kv_cache_type") or settings.get("kv_cache_type")' in _rs_src)
check("runtime_sync broadcasts to llama_vram_table", '"llama_vram_table"' in _rs_src)

_ml_src = _read("backend/manager_load.py")
check("manager_load emits --cache-ram 0 when CACHE_RAM_MIB >= 0", "if CACHE_RAM_MIB >= 0:" in _ml_src)

_mh_src = _read("backend/manager_health.py")
_mp_src = _read("backend/manager_prefetch.py")
check("manager_health unpin touches slot and force_kill_all covers _orphan_port",
      "slot.touch()" in _mh_src and "_orphan_port" in _mh_src)
check("manager_prefetch guards cancel/exception with _start_gen",
      "_start_gen" in _mp_src and "CancelledError" in _mp_src)


# ── Step 3: Core / Duo / Pre-explore / Compression ─────────────────────
print("-- Step 3: Core / duo / pre-explore / compression --")
_pe_src = _read("core/duo/_pre_explore.py")
check("_pre_explore defines non-blocking _xq_put_sentinel", "_xq_put_sentinel" in _pe_src)
check("_pre_explore handles (GeneratorExit, asyncio.CancelledError)", "(GeneratorExit, asyncio.CancelledError)" in _pe_src)

_per_src = _read("hive_functions/pre_explore/runner.py")
check("pre_explore runner cancels unfinished tasks in finally",
      "_wt.cancel()" in _per_src and "_gather_task.cancel()" in _per_src and "finally:" in _per_src)

_dr_src = _read("core/duo_runner.py")
check("duo_runner cancels _exec_task in finally on round exit",
      "if _exec_task is not None and not _exec_task.done():" in _dr_src)
check("duo_runner re-raises httpx transport/timeout errors before retry loop",
      "httpx.ReadError" in _dr_src and "httpx.RemoteProtocolError" in _dr_src and "if not is_ctx_aborted(ctx):" in _dr_src)

import core.duo_compress_cycle as DCC  # noqa: E402
import core.duo.preflight as CDP  # noqa: E402
_msgs: list[dict[str, str]] = [{"role": "user", "content": "first"}]
DCC._append_or_merge_user_msg(_msgs, "second")
check("_append_or_merge_user_msg merges adjacent user turns",
      len(_msgs) == 1 and "first" in _msgs[0]["content"] and "second" in _msgs[0]["content"])
_pin_txt = CDP._build_plan_pin_msg(["step 1", "step 2"], 0, 2)
_pin_msgs: list[dict[str, str]] = [{"role": "user", "content": "inventory"}]
check("_build_plan_pin_msg returns str for compress pin merge", isinstance(_pin_txt, str))
if _pin_txt:
    DCC._append_or_merge_user_msg(_pin_msgs, _pin_txt)
check("plan-pin string merges into adjacent user msg cleanly",
      len(_pin_msgs) == 1 and "[PLAN-PIN" in _pin_msgs[0]["content"] and "inventory" in _pin_msgs[0]["content"])
_dcc_src = _read("core/duo_compress_cycle.py")
check("duo_compress_cycle passes exclude_slot_id=None to compressor fit", "exclude_slot_id=None" in _dcc_src)

import core.duo_dtool_setup as DDS  # noqa: E402
_hist = [
    {"role": "assistant", "tool_calls": [
        {"id": "call_1", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}},
        {"id": "call_2", "function": {"name": "read_file", "arguments": '{"path":"b.py"}'}},
    ]},
    {"role": "tool", "name": "read_file", "tool_call_id": "call_2", "content": "read b"},
    {"role": "tool", "name": "read_file", "tool_call_id": "call_1", "content": "read a"},
]
_deduped = DDS._dedup_additional_explore_history(_hist, included_msgs=[], touched_paths={"a.py"})
check("_dedup_additional_explore_history preserves untouched call_2 and drops touched call_1",
      any(m.get("tool_call_id") == "call_2" and m.get("content") == "read b" for m in _deduped)
      and not any(m.get("tool_call_id") == "call_1" for m in _deduped),
      repr(_deduped))


# ── Step 4: Security, Tools, Cleanup & Frontend ────────────────────────
print("-- Step 4: Security, tools, cleanup, frontend --")
from utils.file import is_secret_path  # noqa: E402
check("is_secret_path blocks .env::$DATA", is_secret_path(".env::$DATA") is True)
check("is_secret_path blocks .env. with trailing dot/space", is_secret_path(".env. ") is True)
check("is_secret_path blocks credentials.json:$DATA", is_secret_path("sub/credentials.json:$DATA") is True)
check("is_secret_path blocks sessions./x", is_secret_path("sessions./chat.json") is True)
check("is_secret_path allows normal source file", is_secret_path("src/main.py") is False)

import core.duo_helpers as DH  # noqa: E402
with tempfile.TemporaryDirectory() as _td:
    _td_p = Path(_td)
    (_td_p / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (_td_p / "settings.json").write_text('{"secret": 1}\n', encoding="utf-8")
    (_td_p / "credentials.json").write_text('{"tok": "x"}\n', encoding="utf-8")
    (_td_p / "gateway.toml").write_text('key = "x"\n', encoding="utf-8")
    _blk, _pre_paths = DH.build_preseed_context(str(_td_p))
    _names = {Path(p).name for p in _pre_paths}
    check("preseed includes main.py and excludes secret files",
          _names == {"main.py"} and "settings.json" not in _blk and "credentials.json" not in _blk)

import tools.handlers.git_tools as TGH  # noqa: E402
import tools.handlers._shared as TGS  # noqa: E402
with tempfile.TemporaryDirectory() as _td:
    _td_p = Path(_td)
    (_td_p / "ok.py").write_text("x = 1\n", encoding="utf-8")
    with mock.patch.object(TGS, "_GIT_TOOLS_AVAILABLE", True), \
         mock.patch.object(TGS, "exec_git_commit", new=mock.AsyncMock(return_value="COMMITTED")):
        for _label, _bad_path in [
            ("pathspec magic :", ":(top)ok.py"),
            ("root dot-slash", "./"),
            ("parent traversal to root", "sub/.."),
            ("secret .env", ".env"),
            ("secret settings.json", "settings.json"),
        ]:
            _res = asyncio.run(TGH._inline_tool_git_commit(
                {"message": "msg", "paths": [_bad_path]}, _td_p, str(_td_p)
            ))
            check(f"git_commit rejects {_label}", "[TOOL_ERROR:INVALID_ARGUMENT]" in _res, _res)

import hive_functions.git_tools as HGT  # noqa: E402
check("exec_git_reset rejects dash target", "invalid target" in asyncio.run(HGT.exec_git_reset(".", "--hard")))
check("exec_git_checkout rejects dash target", "invalid target" in asyncio.run(HGT.exec_git_checkout(".", "--force")))
check("routers/git.py passes -- before git config keys", '["config", "--", "user.name"' in _read("routers/git.py"))

import tools.runner as TR  # noqa: E402
from infra import run_control as RC  # noqa: E402


async def _test_destructive_gate_order() -> None:
    _rid = "wave3c-gate-order"
    _seen: list[tuple[bool, str | None, str | None]] = []

    async def _fake_emit(ev: dict) -> None:
        if ev.get("type") == "agent_asking":
            _card = TR._pending_approvals.get(_rid)
            _paused = RC.is_pause_waiting(_rid)
            _seen.append((_paused, _card.get("decision_id") if _card else None, ev.get("decision_id")))

    async def _resume_soon() -> None:
        for _ in range(50):
            if RC.is_pause_waiting(_rid):
                RC.set_user_answer(_rid, "no")
                return
            await asyncio.sleep(0.01)

    t_rid = TR._current_run_id.set(_rid)
    t_snap = TR._run_settings_snapshot.set({"headless": False})
    t_emit = TR._tool_loop_emit.set(_fake_emit)
    t_to = TR._pause_timeout_s.set(5)
    try:
        resumer = asyncio.create_task(_resume_soon())
        await TR._destructive_gate("run_bash", {"cmd": "rm -rf ./tmp"})
        await resumer
    finally:
        RC.cleanup_pause(_rid)
        TR._pause_timeout_s.reset(t_to)
        TR._tool_loop_emit.reset(t_emit)
        TR._run_settings_snapshot.reset(t_snap)
        TR._current_run_id.reset(t_rid)

    check("destructive gate initiates pause + card with matching decision_id BEFORE emit",
          len(_seen) == 1 and _seen[0][0] is True and bool(_seen[0][1]) and _seen[0][1] == _seen[0][2],
          repr(_seen))


asyncio.run(_test_destructive_gate_order())

check("lsp.py uses open(..., newline='') not Path.read_text(newline=...)",
      "with open(p, encoding=\"utf-8\", errors=\"replace\", newline=\"\")" in _read("tools/handlers/lsp.py"))

import tools.handlers.exec_tools as ET  # noqa: E402


async def _test_stream_proc_cancel_reaps() -> None:
    _killed: list[bool] = []
    import tools.sandbox as _SB
    _orig_ktr = _SB.kill_tree_and_reap

    async def _wrapped_ktr(proc, taskkill_timeout=5):
        _killed.append(True)
        await _orig_ktr(proc, taskkill_timeout=taskkill_timeout)

    with mock.patch.object(_SB, "kill_tree_and_reap", new=_wrapped_ktr):
        task = asyncio.create_task(
            ET._stream_proc([sys.executable, "-c", "import time; time.sleep(30)"], 10, str(_ROOT), os.environ.copy())
        )
        await asyncio.sleep(0.2)
        task.cancel()
        got_cancel = False
        try:
            await task
        except asyncio.CancelledError:
            got_cancel = True
        check("_stream_proc re-raises CancelledError and reaps tree", got_cancel and bool(_killed))


asyncio.run(_test_stream_proc_cancel_reaps())

import tools.background as TB  # noqa: E402
_fake_entry = mock.Mock()
with mock.patch.object(TB, "_terminate") as _mterm:
    with TB._LOCK:
        TB._REGISTRY["__w3c_test__"] = _fake_entry
    _cnt = TB.stop_all_background()
    check("stop_all_background terminates registered entries", _cnt >= 1 and _mterm.called and "__w3c_test__" not in TB._REGISTRY)
check("server.py invokes stop_all_background in shutdown", "stop_all_background" in _read("server.py"))

_app_js = _read("static/app.js")
_idx_html = _read("index.html")
check("app.js uses Object.keys(_mm.cp).length > 0 guard", "typeof _mm.cp === 'object' && Object.keys(_mm.cp).length > 0" in _app_js)
check("app.js escapes soloModel with esc()", "esc(soloModel)" in _app_js)
check("index.html bumps app.js cache key to 20261122-01", "app.js?v=20261122-01" in _idx_html)

_node = shutil.which("node")
if _node:
    _js_test = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync('static/app.js', 'utf8');
const m = src.match(/let _settingsPatchQueue[\s\S]*?\nfunction _flushQueuedSettings\(\) \{[\s\S]*?\n\}/);
if (!m) { console.error('block not found'); process.exit(2); }
let fetchCount = 0;
let releaseFirst = null;
const sandbox = {
  console,
  S: { settingsRev: 1 },
  showInfo() {},
  fetch() {
    fetchCount++;
    if (fetchCount === 1) {
      return new Promise((res) => { releaseFirst = () => res({ json: async () => ({ settings_rev: 2 }) }); });
    }
    return Promise.resolve({ json: async () => ({ settings_rev: 3 }) });
  },
  getFetchCount: () => fetchCount,
  getReleaseFirst: () => releaseFirst,
};
const runner = `
${m[0]}
(async () => {
  _settingsPatchQueue = { a: 1 };
  const p1 = _flushQueuedSettings();
  await Promise.resolve();
  _settingsPatchQueue.b = 2;
  let p2Done = false;
  const p2 = _flushQueuedSettings().then(() => { p2Done = true; });
  await Promise.resolve();
  if (p2Done) throw new Error('p2 resolved early');
  getReleaseFirst()();
  await Promise.all([p1, p2]);
  if (getFetchCount() !== 2 || !p2Done) throw new Error('bad fetchCount ' + getFetchCount());
})();
`;
vm.runInNewContext(runner, sandbox).catch((err) => { console.error(err); process.exit(3); });
"""
    _nr = subprocess.run([_node, "-e", _js_test], cwd=str(_ROOT), capture_output=True, text=True, timeout=15)
    check("node test: concurrent _flushQueuedSettings chains until pending patch lands", _nr.returncode == 0, _nr.stderr)
else:
    check("node binary available for JS concurrency test", False, "node missing")

sys.exit(h.finish("audit_wave3c_deep_fixes"))

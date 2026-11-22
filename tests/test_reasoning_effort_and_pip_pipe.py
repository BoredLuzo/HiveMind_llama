# -*- coding: utf-8 -*-
"""Regressions for composer reasoning_effort propagation, Inkling medium default,
piped pip-install source-build hints on exit 0, and MoE CPU-offload load_mode none.

Run: python tests/test_reasoning_effort_and_pip_pipe.py
Exit 0 = all pass, Exit 1 = failures.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


# ── 1. normalize_reasoning_effort, resolve_reasoning_effort & schema meta ─────
import settings_defaults as SD  # noqa: E402
import core.model_sampling as MS  # noqa: E402

check(
    "DEFAULT_SETTINGS + SETTINGS_UI_META expose reasoning_effort='auto'",
    SD.DEFAULT_SETTINGS.get("reasoning_effort") == "auto"
    and SD.SETTINGS_UI_META.get("reasoning_effort", {}).get("choices")
    == ["auto", "none", "low", "medium", "high", "max"],
)

check(
    "normalize_reasoning_effort normalizes aliases cleanly",
    MS.normalize_reasoning_effort(None) == "auto"
    and MS.normalize_reasoning_effort("med") == "medium"
    and MS.normalize_reasoning_effort("xhigh") == "max"
    and MS.normalize_reasoning_effort("off") == "none"
    and MS.normalize_reasoning_effort("minimal") == "low"
    and MS.normalize_reasoning_effort("bogus") == "auto",
)

check(
    "resolve_reasoning_effort prioritizes user override > thinking=False > profile",
    MS.resolve_reasoning_effort({"reasoning_effort": "low"}, {"reasoning_effort": "high"}, thinking=False) == "low"
    and MS.resolve_reasoning_effort({"reasoning_effort": "auto"}, {"reasoning_effort": "medium"}, thinking=False) == "none"
    and MS.resolve_reasoning_effort({"reasoning_effort": "auto"}, {"reasoning_effort": "medium"}, thinking=True) == "medium"
    and MS.resolve_reasoning_effort({}, {}, thinking=True) == "",
)


# ── 2. Inkling profile default & config validation ────────────────────────────
import routers.config as RC  # noqa: E402

_ink = json.loads((_ROOT / "model_configs" / "models" / "inkling-2-mini.json").read_text(encoding="utf-8"))
_smp_ink = _ink.get("sampling") or {}
check(
    "inkling-2-mini ships medium effort for thinking/coding and none for non-thinking",
    _smp_ink.get("sampling_thinking_code", {}).get("reasoning_effort") == "medium"
    and _smp_ink.get("sampling_thinking_text", {}).get("reasoning_effort") == "medium"
    and _smp_ink.get("thinking", {}).get("reasoning_effort") == "medium"
    and _smp_ink.get("sampling_text", {}).get("reasoning_effort") == "none"
    and _smp_ink.get("non_thinking", {}).get("reasoning_effort") == "none",
)

check(
    "_validate_sampling_block accepts valid reasoning_effort and rejects invalid values",
    RC._validate_sampling_block({"thinking": {"reasoning_effort": "medium"}}) is None
    and RC._validate_sampling_block({"non_thinking": {"reasoning_effort": "none"}}) is None
    and RC._validate_sampling_block({"thinking": {"reasoning_effort": "turbo"}}) is not None,
)


# ── 3. End-to-end payload propagation across preflight, duo_helpers, ToolLoop, llama_client ──
from core.duo.preflight import _apply_thinking_kwargs  # noqa: E402
from core.duo_helpers import _apply_thinking_fields, _calculate_thinking_tokens  # noqa: E402
from core.tool_loop import ToolLoop, ToolLoopConfig  # noqa: E402
import backend.llama_client as LC  # noqa: E402

_p_off = _apply_thinking_kwargs({}, {"reasoning_effort": "medium", "preserve_thinking": True}, thinking=False, coder_tool_think=False)
_p_on_ink = _apply_thinking_kwargs({}, _smp_ink["sampling_thinking_code"], thinking=True, coder_tool_think=True, settings={"reasoning_effort": "auto"})
_p_on_user = _apply_thinking_kwargs({}, _smp_ink["sampling_thinking_code"], thinking=True, coder_tool_think=True, settings={"reasoning_effort": "high"})
check(
    "_apply_thinking_kwargs sets none when off, honors profile medium on auto, and honors user high",
    _p_off.get("reasoning_effort") == "none"
    and _p_off.get("chat_template_kwargs", {}).get("enable_thinking") is False
    and _p_on_ink.get("reasoning_effort") == "medium"
    and _p_on_ink.get("chat_template_kwargs", {}).get("preserve_thinking") is True
    and _p_on_user.get("reasoning_effort") == "high",
    repr((_p_off, _p_on_ink, _p_on_user)),
)

_f_off = _apply_thinking_fields({}, False, 1000)
_f_med = _apply_thinking_fields({}, True, 3072, reasoning_effort="medium")
check(
    "_apply_thinking_fields propagates reasoning_effort",
    _f_off.get("reasoning_effort") == "none" and _f_med.get("reasoning_effort") == "medium",
)

_b_off = LC._build_payload([{"role": "user", "content": "hi"}], 1.0, 64, False, thinking=False, sampler={"reasoning_effort": "medium"})
_b_on = LC._build_payload([{"role": "user", "content": "hi"}], 1.0, 64, False, thinking=True, sampler={"reasoning_effort": "medium"})
_b_auto = LC._build_payload([{"role": "user", "content": "hi"}], 1.0, 64, False, thinking=True, sampler={"reasoning_effort": "auto"})
check(
    "_build_payload enforces none when thinking=False, keeps medium when thinking=True, strips auto",
    _b_off.get("reasoning_effort") == "none"
    and _b_on.get("reasoning_effort") == "medium"
    and "reasoning_effort" not in _b_auto,
)

# ToolLoop real payload assembly check (mocked httpx)
_captured_tl_payloads: list[dict] = []


class _FakeResp:
    status_code = 200

    def __init__(self, payload_json: dict) -> None:
        self._j = payload_json

    async def aread(self) -> bytes:
        return json.dumps(self._j).encode("utf-8")

    def json(self) -> dict:
        return self._j


class _FakeStreamCtx:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp

    async def __aenter__(self) -> _FakeResp:
        return self._resp

    async def __aexit__(self, *_exc) -> bool:
        return False


class _FakeHttp:
    def stream(self, _method: str, _url: str, json: dict | None = None, timeout=None):  # noqa: A002
        _captured_tl_payloads.append(dict(json or {}))
        return _FakeStreamCtx(_FakeResp({"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}}))

    async def post(self, _url: str, json: dict | None = None, timeout=None):  # noqa: A002
        _captured_tl_payloads.append(dict(json or {}))
        return _FakeResp({"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}})


async def _run_tl_case(thinking: bool, settings_dict: dict) -> dict:
    _captured_tl_payloads.clear()
    tl = ToolLoop(
        config=ToolLoopConfig(
            stream=False, max_rounds=1, model="inkling-2-mini-ud",
            thinking=thinking, settings=settings_dict,
        ),
        http_client=_FakeHttp(),
        port=8101,
    )
    async for _ev in tl.run([{"role": "user", "content": "hi"}]):
        pass
    return dict(_captured_tl_payloads[0])


_tl_off = asyncio.run(_run_tl_case(False, {"reasoning_effort": "auto"}))
_tl_med = asyncio.run(_run_tl_case(False, {"reasoning_effort": "medium"}))
check(
    "ToolLoop emits reasoning_effort='none' on auto+thinking=False and overrides to medium when set",
    _tl_off.get("reasoning_effort") == "none"
    and _tl_off.get("thinking") is False
    and _tl_med.get("reasoning_effort") == "medium"
    and _tl_med.get("thinking") is True
    and _tl_med.get("thinking_budget") == MS.REASONING_EFFORT_BUDGETS["medium"],
    repr((_tl_off.get("reasoning_effort"), _tl_med.get("reasoning_effort"), _tl_med.get("thinking_budget"))),
)


# ── 4. Role isolation in resolve_agent_sampling + budget scaling ──────────────
class _DummyAgent:
    def __init__(self, thinking: bool | None = False, thinking_budget: int = 0) -> None:
        self.thinking = thinking
        self.thinking_budget = thinking_budget
        self.temperature = 0.5


_t_d, _th_d, _b_d, _s_d = MS.resolve_agent_sampling(
    "inkling-2-mini-ud", agent=_DummyAgent(False, 0),
    settings={"reasoning_effort": "high"}, role="direct",
)
_t_j, _th_j, _b_j, _s_j = MS.resolve_agent_sampling(
    "lfm3:1.7b-qad", agent=_DummyAgent(False, 0),
    settings={"reasoning_effort": "high"}, role="judge",
)
_tok_none = _calculate_thinking_tokens("inkling-2-mini-ud", {"reasoning_effort": "none"}, input_tokens=500, available_ctx=65000, agent_name="duo_coder")
_tok_low = _calculate_thinking_tokens("inkling-2-mini-ud", {"reasoning_effort": "low"}, input_tokens=500, available_ctx=65000, agent_name="duo_coder")
check(
    "composer effort scales direct/duo_coder budgets while leaving internal judge untouched",
    _th_d is True and _b_d == MS.REASONING_EFFORT_BUDGETS["high"] and _s_d.get("reasoning_effort") == "high"
    and _th_j is False and _b_j == 0 and _s_j.get("reasoning_effort") == "none"
    and _tok_none == 0 and _tok_low == MS.REASONING_EFFORT_BUDGETS["low"],
    repr((_b_d, _th_j, _s_j.get("reasoning_effort"), _tok_none, _tok_low)),
)


# ── 5. Piped pip install source-build hint even when exit code is 0 ───────────
import tools.handlers.exec_tools as ET  # noqa: E402

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as _td:
    _ws = Path(_td)
    _py_dir = _ws / ".venv" / "Scripts"
    _py_dir.mkdir(parents=True, exist_ok=True)
    (_py_dir / "python.exe").write_bytes(b"")

    _pip_err_tail = (
        "Getting requirements to build wheel: finished with status 'error'\n"
        "  error: subprocess-exited-with-error\n"
        "      FileNotFoundError: [WinError 2]\n"
    )
    with mock.patch.object(ET, "_stream_proc", return_value=(_pip_err_tail, "", 0, False)):
        _res_pipe_fail = asyncio.run(
            ET._inline_tool_run_bash({"cmd": ".venv/Scripts/python.exe -m pip install pygame 2>&1 | tail -5"}, _ws, None)
        )
    _pip_ok_out = (
        "Building wheel for somepurepkg (pyproject.toml): finished with status 'done'\n"
        "Successfully built somepurepkg\nSuccessfully installed somepurepkg-1.0\n"
    )
    with mock.patch.object(ET, "_stream_proc", return_value=(_pip_ok_out, "", 0, False)):
        _res_pipe_ok = asyncio.run(
            ET._inline_tool_run_bash({"cmd": ".venv/Scripts/python.exe -m pip install somepurepkg 2>&1 | tail -5"}, _ws, None)
        )

check(
    "piped pip install failure at exit 0 emits source-build hint without false-positiving clean builds",
    "[HINT] pip fell back to building from SOURCE" in _res_pipe_fail
    and "--only-binary=:all: pygame" in _res_pipe_fail
    and "[HINT]" not in _res_pipe_ok,
    repr((_res_pipe_fail[-160:], "[HINT]" in _res_pipe_ok)),
)


# ── 6. MoE CPU-offload profile load_mode='none' beats global mlock=True ───────
import backend.manager_load as ML  # noqa: E402

_dummy_gg = Path(tempfile.gettempdir()) / "_tmp_inkling_load_mode_test.gguf"
with open(_dummy_gg, "wb") as _gf:
    _gf.truncate(2 * 1024 * 1024 * 1024)

try:
    with mock.patch.object(ML, "_ram_guard_blocks", return_value=False):
        _lm_moe_none = ML._resolve_load_mode(
            str(_dummy_gg), spec_planned=False, mlock_now=True,
            profile_lm="none", free_ram_gb=32.0, large_moe=False, moe_cpu_offload=True,
        )
        _lm_dense_keep = ML._resolve_load_mode(
            str(_dummy_gg), spec_planned=False, mlock_now=True,
            profile_lm="none", free_ram_gb=32.0, large_moe=False, moe_cpu_offload=False,
        )
    with mock.patch.object(ML, "_ram_guard_blocks", return_value=True):
        _lm_tight_ram = ML._resolve_load_mode(
            str(_dummy_gg), spec_planned=False, mlock_now=True,
            profile_lm="none", free_ram_gb=4.0, large_moe=False, moe_cpu_offload=True,
        )
finally:
    _dummy_gg.unlink(missing_ok=True)

check(
    "_resolve_load_mode honors profile='none' for MoE CPU-offload unless tight RAM or spec",
    _lm_moe_none == "none" and _lm_dense_keep == "mmap+mlock" and _lm_tight_ram == "mmap",
    repr((_lm_moe_none, _lm_dense_keep, _lm_tight_ram)),
)


# ── 7. Frontend composer Reasoning Effort bar & Node VM test ──────────────────
_idx = (_ROOT / "index.html").read_text(encoding="utf-8")
_js = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")
_css = (_ROOT / "static" / "style.css").read_text(encoding="utf-8")

check(
    "index.html & style.css & app.js expose #effort-bar and pin reasoning_effort in /stream",
    'id="effort-bar"' in _idx
    and 'data-effort="auto"' in _idx
    and 'data-effort="none"' in _idx
    and 'data-effort="low"' in _idx
    and 'data-effort="medium"' in _idx
    and 'data-effort="high"' in _idx
    and 'data-effort="max"' in _idx
    and ".effort-btn.active" in _css
    and "reasoning_effort: S.reasoningEffort || 'auto'" in _js,
)

_node_eff = r"""
const fs = require('fs');
const vm = require('vm');
const appSrc = fs.readFileSync('static/app.js', 'utf8').replace(/\r\n/g, '\n');
const s = appSrc.indexOf('function _normalizeReasoningEffort(v)');
const e = appSrc.indexOf('\nfunction _duoCtxPayload', s);
const fnSrc = appSrc.slice(s, e);
const buttons = ['auto', 'none', 'low', 'medium', 'high', 'max'].map(lvl => ({
  _lvl: lvl,
  _cls: new Set(lvl === 'auto' ? ['effort-btn', 'active'] : ['effort-btn']),
  _attrs: { 'data-effort': lvl, 'aria-pressed': lvl === 'auto' ? 'true' : 'false' },
  getAttribute(k) { return this._attrs[k] || null; },
  setAttribute(k, v) { this._attrs[k] = String(v); },
  classList: {
    toggle(c, f) { if (f) this._s.add(c); else this._s.delete(c); },
    contains(c) { return this._s.has(c); },
  },
}));
for (const b of buttons) b.classList._s = b._cls;
const saved = [];
const ctx = {
  S: { reasoningEffort: 'auto' },
  document: {
    getElementById(id) {
      if (id !== 'effort-bar') return null;
      return { querySelectorAll: () => buttons };
    },
  },
  _postSettingsFF: (patch, lbl) => { saved.push({ patch, lbl }); },
};
vm.createContext(ctx);
vm.runInContext(fnSrc, ctx);
ctx.setReasoningEffort('med');
const afterMed = {
  state: ctx.S.reasoningEffort,
  active: buttons.filter(b => b._cls.has('active')).map(b => b._lvl),
  pressed: buttons.find(b => b._lvl === 'medium')._attrs['aria-pressed'],
  saved,
};
ctx.setReasoningEffort('xhigh');
const afterMax = {
  state: ctx.S.reasoningEffort,
  active: buttons.filter(b => b._cls.has('active')).map(b => b._lvl),
};
console.log(JSON.stringify({ afterMed, afterMax }));
"""
_ne = subprocess.run(["node", "-e", _node_eff], cwd=_ROOT, capture_output=True, text=True)
_ne_ok = _ne.returncode == 0
if _ne_ok:
    _nej = json.loads(_ne.stdout.strip())
    _ne_ok = (
        _nej["afterMed"]["state"] == "medium"
        and _nej["afterMed"]["active"] == ["medium"]
        and _nej["afterMed"]["pressed"] == "true"
        and _nej["afterMed"]["saved"][0]["patch"] == {"reasoning_effort": "medium"}
        and _nej["afterMax"]["state"] == "max"
        and _nej["afterMax"]["active"] == ["max"]
    )
check("Node VM confirms setReasoningEffort updates state, ARIA, active pill, and persists", _ne_ok, _ne.stdout.strip() or _ne.stderr.strip())

sys.exit(h.finish("reasoning_effort_and_pip_pipe"))

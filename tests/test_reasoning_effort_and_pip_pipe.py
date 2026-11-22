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
    "index.html & style.css & app.js expose #effort-trigger/#effort-menu and override badges",
    'id="effort-trigger"' in _idx
    and 'id="effort-menu"' in _idx
    and 'id="effort-menu-ovr"' in _idx
    and 'id="effort-bar"' not in _idx
    and 'data-effort="auto"' in _idx
    and 'data-effort="none"' in _idx
    and 'data-effort="low"' in _idx
    and 'data-effort="medium"' in _idx
    and 'data-effort="high"' in _idx
    and 'data-effort="max"' in _idx
    and "reasoning-override-slot" in _idx
    and 'data-reasoning-ovr-row="duo"' in _idx
    and ".effort-menu" in _css
    and ".effort-opt.disabled" in _css
    and '.effort-trigger[data-no-reasoning="1"]' in _css
    and ".reasoning-override-badge" in _css
    and ".reasoning-overridden" in _css
    and ".effort-btn.active" not in _css
    and "reasoning_effort: S.reasoningEffort || 'auto'" in _js,
)

_node_eff = r"""
const fs = require('fs');
const vm = require('vm');
const appSrc = fs.readFileSync('static/app.js', 'utf8').replace(/\r\n/g, '\n');
const s = appSrc.indexOf('var _REASONING_EFFORT_LABELS =');
const e = appSrc.indexOf('\n// removed: updateToolThinkingModeVis', s);
const fnSrc = appSrc.slice(s, e);
function makeCls(initial) {
  const set = new Set(initial || []);
  return {
    _s: set,
    add(c) { set.add(c); },
    remove(c) { set.delete(c); },
    toggle(c, f) { if (f === undefined) { if (set.has(c)) set.delete(c); else set.add(c); } else if (f) set.add(c); else set.delete(c); },
    contains(c) { return set.has(c); },
  };
}
const options = ['auto', 'none', 'low', 'medium', 'high', 'max'].map(lvl => ({
  _lvl: lvl,
  disabled: false,
  title: '',
  classList: makeCls(lvl === 'auto' ? ['effort-opt', 'active'] : ['effort-opt']),
  _attrs: { 'data-effort': lvl, 'aria-selected': lvl === 'auto' ? 'true' : 'false', 'aria-disabled': 'false' },
  getAttribute(k) { return this._attrs[k] || null; },
  setAttribute(k, v) { this._attrs[k] = String(v); },
}));
const trigAttrs = { 'data-effort': 'auto', 'aria-expanded': 'false' };
const trig = {
  title: '',
  setAttribute(k, v) { trigAttrs[k] = String(v); },
  getAttribute(k) { return trigAttrs[k] || null; },
  removeAttribute(k) { delete trigAttrs[k]; },
};
const lblEl = { textContent: 'Auto' };
const ctxEl = { textContent: '' };
const ovrEl = { hidden: true, classList: makeCls(['effort-menu-ovr']) };
const ovrTxt = { textContent: '' };
const menu = {
  hidden: true,
  querySelectorAll(sel) { return sel === '.effort-opt' ? options : []; },
};
function makeSlot(agentOvr) {
  return {
    _children: [],
    _html: '',
    getAttribute(k) { return (k === 'data-agent-override' && agentOvr) ? agentOvr : null; },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); if (!v) this._children = []; },
    querySelector(sel) {
      return sel === '.reasoning-override-badge' ? (this._children[0] || null) : null;
    },
    appendChild(ch) { this._children.push(ch); },
  };
}
const badgeSlots = [makeSlot(''), makeSlot('direct')];
const ovrRows = [
  { _key: 'duo', getAttribute(k) { return k === 'data-reasoning-ovr-row' ? 'duo' : null; }, classList: makeCls(['opt-row']) },
  { _key: 'direct', getAttribute(k) { return k === 'data-reasoning-ovr-row' ? 'direct' : null; }, classList: makeCls(['acard-cell']) },
];
const saved = [];
const ctx = {
  S: {
    reasoningEffort: 'auto',
    mode: 'code_duo',
    duoCoderModel: 'inkling-2-mini-ud',
    duoAgenticThinking: true,
    duoToolThinkingAlways: false,
    duoToolThinkingMode: 'on_fail',
    modelProfiles: {
      'inkling-2-mini-ud': { thinking: true, reasoning_effort: 'medium' },
      'lfm2.5:24b': { thinking: false, reasoning_effort: null },
    },
    currentAssignments: {},
    agentsCfg: {},
  },
  document: {
    createElement(tag) {
      return { tag, type: '', className: '', title: '', textContent: '', attrs: {}, setAttribute(k, v) { this.attrs[k] = v; } };
    },
    getElementById(id) {
      if (id === 'effort-trigger') return trig;
      if (id === 'effort-trig-lbl') return lblEl;
      if (id === 'effort-menu-ctx') return ctxEl;
      if (id === 'effort-menu-ovr') return ovrEl;
      if (id === 'effort-menu-ovr-txt') return ovrTxt;
      if (id === 'effort-menu') return menu;
      return null;
    },
    querySelectorAll(sel) {
      if (sel === '.reasoning-override-slot') return badgeSlots;
      if (sel === '[data-reasoning-ovr-row]') return ovrRows;
      return [];
    },
  },
  _shortModel: (m) => m,
  _postSettingsFF: (patch, lbl) => { saved.push({ patch, lbl }); },
};
vm.createContext(ctx);
vm.runInContext(fnSrc, ctx);
ctx._renderReasoningEffortUI('auto');
const initInkling = {
  label: lblEl.textContent,
  ovrHidden: ovrEl.hidden,
  noReasonAttr: trig.getAttribute('data-no-reasoning'),
  disabledLvls: options.filter(o => o.disabled).map(o => o._lvl),
};
ctx.toggleReasoningEffortMenu({ stopPropagation() {} });
const afterOpen = {
  menuHidden: menu.hidden,
  expanded: trig.getAttribute('aria-expanded'),
  ctxText: ctxEl.textContent,
};
ctx.setReasoningEffort('med');
const afterMed = {
  menuHidden: menu.hidden,
  expanded: trig.getAttribute('aria-expanded'),
  state: ctx.S.reasoningEffort,
  label: lblEl.textContent,
  trigEffort: trig.getAttribute('data-effort'),
  active: options.filter(b => b.classList.contains('active')).map(b => b._lvl),
  selected: options.find(b => b._lvl === 'medium')._attrs['aria-selected'],
  ovrHidden: ovrEl.hidden,
  ovrMuted: ovrEl.classList.contains('muted'),
  ovrTxt: ovrTxt.textContent,
  duoDimmed: ovrRows[0].classList.contains('reasoning-overridden'),
  badgeText: badgeSlots.map(s => s._children[0] && s._children[0].textContent),
  saved,
};
ctx.S.mode = 'auto';
ctx.S.currentAssignments = { direct: { model: 'lfm2.5:24b' } };
ctx.setReasoningEffort('xhigh');
const afterMaxNonThink = {
  state: ctx.S.reasoningEffort,
  label: lblEl.textContent,
  noReasonAttr: trig.getAttribute('data-no-reasoning'),
  ctxText: ctxEl.textContent,
  ovrHidden: ovrEl.hidden,
  ovrMuted: ovrEl.classList.contains('muted'),
  ovrTxt: ovrTxt.textContent,
  disabledLvls: options.filter(o => o.disabled).map(o => o._lvl),
  enabledLvls: options.filter(o => !o.disabled).map(o => o._lvl),
  directDimmed: ovrRows[1].classList.contains('reasoning-overridden'),
  badgeText: badgeSlots.map(s => s._children[0] && s._children[0].textContent),
};
ctx.setReasoningEffort('auto');
const afterAutoNonThink = {
  state: ctx.S.reasoningEffort,
  label: lblEl.textContent,
  noReasonAttr: trig.getAttribute('data-no-reasoning'),
  ovrHidden: ovrEl.hidden,
  duoDimmed: ovrRows[0].classList.contains('reasoning-overridden'),
  badgeCounts: badgeSlots.map(s => s._children.length),
};
console.log(JSON.stringify({ initInkling, afterOpen, afterMed, afterMaxNonThink, afterAutoNonThink }));
"""
_ne = subprocess.run(["node", "-e", _node_eff], cwd=_ROOT, capture_output=True, text=True)
_ne_ok = _ne.returncode == 0
if _ne_ok:
    _nej = json.loads(_ne.stdout.strip())
    _ne_ok = (
        _nej["initInkling"]["label"] == "Auto \u00b7 Med"
        and _nej["initInkling"]["ovrHidden"] is True
        and _nej["initInkling"]["noReasonAttr"] is None
        and _nej["initInkling"]["disabledLvls"] == []
        and _nej["afterOpen"]["menuHidden"] is False
        and _nej["afterOpen"]["expanded"] == "true"
        and "default: medium" in _nej["afterOpen"]["ctxText"]
        and _nej["afterMed"]["menuHidden"] is True
        and _nej["afterMed"]["expanded"] == "false"
        and _nej["afterMed"]["state"] == "medium"
        and _nej["afterMed"]["label"] == "Medium"
        and _nej["afterMed"]["trigEffort"] == "medium"
        and _nej["afterMed"]["active"] == ["medium"]
        and _nej["afterMed"]["selected"] == "true"
        and _nej["afterMed"]["ovrHidden"] is False
        and _nej["afterMed"]["ovrMuted"] is False
        and "Overriding" in _nej["afterMed"]["ovrTxt"]
        and _nej["afterMed"]["duoDimmed"] is True
        and _nej["afterMed"]["badgeText"] == ["Composer: Medium \u00d7", "Composer: Medium \u00d7"]
        and _nej["afterMed"]["saved"][0]["patch"] == {"reasoning_effort": "medium"}
        and _nej["afterMaxNonThink"]["state"] == "max"
        and _nej["afterMaxNonThink"]["label"] == "Max (n/a)"
        and _nej["afterMaxNonThink"]["noReasonAttr"] == "1"
        and "no native reasoning" in _nej["afterMaxNonThink"]["ctxText"]
        and _nej["afterMaxNonThink"]["ovrHidden"] is False
        and _nej["afterMaxNonThink"]["ovrMuted"] is True
        and "Skipped" in _nej["afterMaxNonThink"]["ovrTxt"]
        and _nej["afterMaxNonThink"]["disabledLvls"] == ["low", "medium", "high", "max"]
        and _nej["afterMaxNonThink"]["enabledLvls"] == ["auto", "none"]
        and _nej["afterMaxNonThink"]["directDimmed"] is False
        and _nej["afterMaxNonThink"]["badgeText"] == ["Composer: Max \u00d7", "Composer: Max (n/a) \u00d7"]
        and _nej["afterAutoNonThink"]["state"] == "auto"
        and _nej["afterAutoNonThink"]["label"] == "No reasoning"
        and _nej["afterAutoNonThink"]["noReasonAttr"] == "1"
        and _nej["afterAutoNonThink"]["ovrHidden"] is True
        and _nej["afterAutoNonThink"]["duoDimmed"] is False
        and _nej["afterAutoNonThink"]["badgeCounts"] == [0, 0]
    )
check("Node VM confirms dropdown lock/dimming, Auto label, override notices, and persistence", _ne_ok, _ne.stdout.strip() or _ne.stderr.strip())

# ── 6. Clash 1: Non-reasoning model guard ("lfm2.5:24b") ──────────────────────
from types import SimpleNamespace as _SN  # noqa: E402
_nr_temp, _nr_th_hi, _nr_b_hi, _nr_s_hi = MS.resolve_agent_sampling(
    "lfm2.5:24b", settings={"reasoning_effort": "high"}, role="direct",
)
_nr_temp2, _nr_th_none, _nr_b_none, _nr_s_none = MS.resolve_agent_sampling(
    "lfm2.5:24b", settings={"reasoning_effort": "none"}, role="direct",
)
_nr_tok_hi = _calculate_thinking_tokens(
    "lfm2.5:24b", {"reasoning_effort": "high"},
    input_tokens=500, available_ctx=65000, agent_name="duo_coder",
)
check(
    "Non-reasoning model guard blocks High/Max forcing while preserving explicit None",
    MS.model_supports_thinking("lfm2.5:24b") is False
    and MS.model_supports_thinking("inkling-2-mini-ud") is True
    and _nr_th_hi is False
    and _nr_b_hi == 0
    and "reasoning_effort" not in _nr_s_hi
    and _nr_th_none is False
    and _nr_b_none == 0
    and _nr_s_none.get("reasoning_effort") == "none"
    and _nr_tok_hi == 0,
    repr((_nr_th_hi, _nr_b_hi, _nr_s_hi.get("reasoning_effort"), _nr_s_none.get("reasoning_effort"), _nr_tok_hi)),
)

_nr_tl_payloads: list[dict] = []


class _NrFakeClient:
    def stream(self, _method: str, _url: str, json: dict | None = None, timeout=None):  # noqa: A002
        _nr_tl_payloads.append(dict(json or {}))
        return _FakeStreamCtx(_FakeResp({"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}}))

    async def post(self, url, json=None, **kw):
        _nr_tl_payloads.append(dict(json or {}))
        return _FakeResp({"choices": [{"message": {"role": "assistant", "content": "ok"}}], "usage": {}})


async def _run_nr_tl():
    _nr_tl = ToolLoop(
        config=ToolLoopConfig(
            stream=False, max_rounds=1, model="lfm2.5:24b",
            max_tokens=1200, thinking=False, settings={"reasoning_effort": "high"},
            tools=[], require_tool_call=False,
        ),
        http_client=_NrFakeClient(),
        port=18080,
    )
    async for _ev in _nr_tl.run([{"role": "user", "content": "hi"}]):
        pass


asyncio.run(_run_nr_tl())

_nr_tl_p = _nr_tl_payloads[0] if _nr_tl_payloads else {}
check(
    "ToolLoop never enables thinking or reasoning_effort='high' on non-reasoning lfm2.5:24b",
    _nr_tl_p.get("thinking") is False
    and _nr_tl_p.get("thinking_budget") == 0
    and "reasoning_effort" not in _nr_tl_p
    and (_nr_tl_p.get("chat_template_kwargs") or {}).get("enable_thinking") is False,
    repr(_nr_tl_p),
)

# ── 7. Clash 2: Planner propagation in hive_functions/planner.py ──────────────
import hive_functions.planner as _pl_mod  # noqa: E402

_, _ps_hi = _pl_mod._resolve_planner_sampling(
    "inkling-2-mini-ud", True, {"reasoning_effort": "high"}, fallback_temp=0.2,
)
_, _ps_nr = _pl_mod._resolve_planner_sampling(
    "lfm2.5:24b", False, {"reasoning_effort": "high"}, fallback_temp=0.2,
)

_pl_stream_payloads: list[dict] = []
_pl_post_payloads: list[dict] = []


class _PlFakeStreamResp:
    status_code = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"- [ ] Step 1"}}]}'
        yield "data: [DONE]"


class _PlFakeClient:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, json=None, **kw):
        _pl_stream_payloads.append(dict(json or {}))
        return _PlFakeStreamResp()

    async def post(self, url, json=None, **kw):
        _pl_post_payloads.append(dict(json or {}))
        return _SN(
            status_code=200,
            json=lambda: {"choices": [{"message": {"content": "## Plan:\n- [ ] Step 1", "reasoning_content": ""}}]},
        )


_orig_pl_httpx = _pl_mod.httpx
_pl_mod.httpx = _SN(
    AsyncClient=_PlFakeClient,
    Limits=_orig_pl_httpx.Limits,
    Timeout=_orig_pl_httpx.Timeout,
    HTTPError=_orig_pl_httpx.HTTPError,
    ConnectError=_orig_pl_httpx.ConnectError,
)
try:
    _pl_stream_payloads.clear()
    _res_hi = asyncio.run(_pl_mod.run_planner(
        task="Implement widget", explore_ctx="ctx", planner_model="inkling-2-mini-ud",
        planner_port=18081, use_thinking=False, thinking_budget=0,
        settings={"reasoning_effort": "high"},
    ))
    _pl_hi = dict(_pl_stream_payloads[0])

    _pl_stream_payloads.clear()
    _res_none = asyncio.run(_pl_mod.run_planner(
        task="Implement widget", explore_ctx="ctx", planner_model="inkling-2-mini-ud",
        planner_port=18081, use_thinking=True, thinking_budget=0,
        settings={"reasoning_effort": "none"},
    ))
    _pl_none = dict(_pl_stream_payloads[0])

    _pl_stream_payloads.clear()
    _res_nr = asyncio.run(_pl_mod.run_planner(
        task="Implement widget", explore_ctx="ctx", planner_model="lfm2.5:24b",
        planner_port=18081, use_thinking=False, thinking_budget=0,
        settings={"reasoning_effort": "high"},
    ))
    _pl_nr = dict(_pl_stream_payloads[0])

    _pl_post_payloads.clear()
    asyncio.run(_pl_mod.run_inloop_planner(
        task="Follow-up", explore_ctx="ctx", planner_model="inkling-2-mini-ud",
        planner_port=18081, use_thinking=False, thinking_budget=0,
        settings={"reasoning_effort": "low"},
    ))
    _inloop_low = dict(_pl_post_payloads[0])
finally:
    _pl_mod.httpx = _orig_pl_httpx

check(
    "Planner run_planner & run_inloop_planner propagate reasoning_effort safely",
    _ps_hi.get("reasoning_effort") == "high"
    and "reasoning_effort" not in _ps_nr
    and _res_hi.used_thinking is True
    and _pl_hi.get("thinking") is True
    and _pl_hi.get("thinking_budget") == MS.REASONING_EFFORT_BUDGETS["high"]
    and _pl_hi.get("reasoning_effort") == "high"
    and _res_none.used_thinking is False
    and _pl_none.get("thinking") is False
    and _pl_none.get("reasoning_effort") == "none"
    and _res_nr.used_thinking is False
    and _pl_nr.get("thinking") is False
    and _pl_nr.get("reasoning_effort") != "high"
    and _inloop_low.get("thinking") is True
    and _inloop_low.get("thinking_budget") == MS.REASONING_EFFORT_BUDGETS["low"]
    and _inloop_low.get("reasoning_effort") == "low",
    repr((_ps_hi.get("reasoning_effort"), _ps_nr.get("reasoning_effort"), _pl_hi.get("thinking_budget"), _pl_none.get("reasoning_effort"), _pl_nr.get("reasoning_effort"), _inloop_low.get("thinking_budget"))),
)

# ── 8. Clash 3: duo_tool_thinking_budget_cap auto-gate + 70% clamp ───────────
_dr_src = (_ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
check(
    "duo_runner gates duo_tool_thinking_budget_cap on auto and clamps to 70% of max_tokens_round",
    '_coder_user_eff == "auto" and _think_cap > 0 and _tool_thinking_budget > _think_cap' in _dr_src
    and '_budget_70_cap = max(1, int(_max_tokens_round * 0.70))' in _dr_src
    and _dr_src.index('_budget_70_cap') < _dr_src.index('_tool_payload = {'),
)

# ── 9. Clash 4: slot._reasoning_on + --reasoning off -> on reload ───────────
import asyncio as _aio  # noqa: E402
from types import SimpleNamespace as _SN  # noqa: E402
from backend.llama_slots import ModelSlot  # noqa: E402
import backend.manager_load as _ml_mod  # noqa: E402

_sl_check = ModelSlot(0)
_init_none = _sl_check._reasoning_on is None
_sl_check._reasoning_on = False
_sl_check.model = "inkling-2-mini-ud"
_sl_check.process = _SN(pid=99998, poll=lambda: None, terminate=lambda: None, wait=lambda **k: 0)
_sl_check.kill()
_kill_reset = _sl_check._reasoning_on is None


class _ReasoningReloadMgr(_ml_mod.LlamaLoadMixin):
    def __init__(self, slot):
        self._slots = [slot]
        self._lock = _aio.Lock()
        self._load_seq = 0
        self._last_orphan_scan_ts = 1e12
        self.start_count = 0
        self.kill_count = 0

    def _metric_inc(self, *_a, **_k):
        pass

    async def _find_loaded(self, model, **_kw):
        for sl in self._slots:
            if _ml_mod._nm(sl.model) == _ml_mod._nm(model) and (sl.is_running or sl._loading):
                return sl
        return None

    async def _find_free_slot(self):
        for sl in self._slots:
            if not sl.is_running and not sl._loading:
                return sl
        return None

    async def _kill_claimed_slot(self, sl):
        self.kill_count += 1
        sl.model = None
        sl._loading = False
        sl.process = None
        sl._reasoning_on = None

    async def _enforce_vram_budget(self, _slot):
        pass

    async def _port_alive(self, _port):
        return True

    async def _start_process(self, sl, model, num_ctx, vision=False, n_parallel=1, ctx_graceful=True, **_kw):
        self.start_count += 1
        sl.process = _SN(pid=222, poll=lambda: None)
        sl.model = model
        sl._num_ctx = num_ctx
        sl._vision = vision
        sl._jinja = True
        sl._n_parallel = n_parallel
        sl._reasoning_on = _ml_mod._wants_reasoning_on(self, model)
        sl._loading = False
        sl._ready_event.set()


async def _run_reload_tests():
    # Case A: --reasoning off -> on triggers clean reload when not busy
    s1 = ModelSlot(0)
    s1.model = "inkling-2-mini-ud"
    s1.process = _SN(pid=101, poll=lambda: None)
    s1._num_ctx = 16384
    s1._jinja = True
    s1._reasoning_on = False
    m1 = _ReasoningReloadMgr(s1)
    m1._reasoning_override = {"inkling-2-mini-ud": True}
    await m1.ensure_loaded("inkling-2-mini-ud", num_ctx=16384)

    # Case B: Busy slot (in_flight=1) must NOT be killed mid-request
    s2 = ModelSlot(0)
    s2.model = "inkling-2-mini-ud"
    s2.process = _SN(pid=102, poll=lambda: None)
    s2._num_ctx = 16384
    s2._jinja = True
    s2._reasoning_on = False
    s2.mark_in_flight()
    m2 = _ReasoningReloadMgr(s2)
    m2._reasoning_override = {"inkling-2-mini-ud": True}
    await m2.ensure_loaded("inkling-2-mini-ud", num_ctx=16384)

    # Case C: Already --reasoning on does NOT reload
    s3 = ModelSlot(0)
    s3.model = "inkling-2-mini-ud"
    s3.process = _SN(pid=103, poll=lambda: None)
    s3._num_ctx = 16384
    s3._jinja = True
    s3._reasoning_on = True
    m3 = _ReasoningReloadMgr(s3)
    await m3.ensure_loaded("inkling-2-mini-ud", num_ctx=16384)

    return (m1.kill_count, m1.start_count, s1._reasoning_on), (m2.kill_count, m2.start_count), (m3.kill_count, m3.start_count)


_r_a, _r_b, _r_c = _aio.run(_run_reload_tests())
check(
    "ModelSlot._reasoning_on and ensure_loaded reload on --reasoning off -> on (respecting busy guard)",
    _init_none and _kill_reset and _r_a == (1, 1, True) and _r_b == (0, 0) and _r_c == (0, 0),
    repr((_init_none, _kill_reset, _r_a, _r_b, _r_c)),
)

# ── 10. /models profile exposure of reasoning_effort ─────────────────────────
from routers.models import _build_models_profiles  # noqa: E402

_mp_list = _build_models_profiles(["inkling-2-mini-ud", "lfm2.5:24b"])
_mp_by_name = {p["name"]: p for p in _mp_list}
check(
    "_build_models_profiles exposes reasoning_effort='medium' for Inkling and None for non-reasoning LFM",
    _mp_by_name.get("inkling-2-mini-ud", {}).get("reasoning_effort") == "medium"
    and _mp_by_name.get("lfm2.5:24b", {}).get("reasoning_effort") is None,
    repr(_mp_by_name),
)

# ── 11. Live stream override status notifications (duo + direct) ─────────────
_vram_src = (_ROOT / "core" / "duo" / "_vram.py").read_text(encoding="utf-8")
_direct_src = (_ROOT / "core" / "direct_runner.py").read_text(encoding="utf-8")
check(
    "Live status notifications surface composer overrides in both duo and direct runners",
    "Reasoning effort: none (composer override" in _vram_src
    and "Reasoning effort: {_user_eff} (composer override)" in _vram_src
    and "Reasoning effort: none (composer override" in _direct_src
    and "Reasoning effort: {_dt_user_eff} (composer override)" in _direct_src
    and "Reasoning effort: {_dir_user_eff} (composer override)" in _direct_src
    and "does not support reasoning" in _direct_src,
)

sys.exit(h.finish("reasoning_effort_and_pip_pipe"))

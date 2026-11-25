# -*- coding: utf-8 -*-
"""Regression checks for metric accuracy, Direct-WS gating, [READ-STATS], and consolidated Reasoning card."""
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="summary")
check = h.check

# ── 1. PhaseTimer labels & RunContext.tool_round_durations ────────────────
from infra.phase_timer import PhaseTimer, _PHASE_LABELS  # noqa: E402
from core.run_context import RunContext  # noqa: E402

pt = PhaseTimer()
pt.start("direct_chat")
pt.end("direct_chat", status="completed")
pt_snap = pt.snapshot()
pt_sum = pt.ui_summary()
rc = RunContext(user_input="hi")
rc.tool_round_durations.append(1.25)

check(
    "PhaseTimer includes direct_chat label and RunContext exposes shared tool_round_durations",
    _PHASE_LABELS.get("direct_chat") == "Direct"
    and "Direct " in pt_sum
    and pt_snap.get("direct_chat", {}).get("status") == "completed"
    and rc.tool_round_durations == [1.25],
)

# ── 2. tools/runner read_file stats (disk read + [SKIP:] reuse + epilogue) ─
from tools.runner import (  # noqa: E402
    _files_read_in_run,
    _files_seen_in_run,
    _run_inline_tool,
    get_read_file_stats,
    reset_read_file_stats,
)

with tempfile.TemporaryDirectory() as td:
    ws = Path(td)
    (ws / "hello.py").write_text("print('hello world')\n", encoding="utf-8")
    _tok_read = _files_read_in_run.set(set())
    _tok_seen = _files_seen_in_run.set(set())
    try:
        reset_read_file_stats()
        r1 = asyncio.run(_run_inline_tool("read_file", {"path": "hello.py"}, str(ws)))
        r2 = asyncio.run(_run_inline_tool("read_file", {"path": "hello.py"}, str(ws)))
        stats = get_read_file_stats()
    finally:
        _files_read_in_run.reset(_tok_read)
        _files_seen_in_run.reset(_tok_seen)

check(
    "read_file stats track both fresh reads and [SKIP:] cache hits",
    "hello world" in r1
    and "[SKIP:" in r2
    and stats.get("calls") == 2
    and len(stats.get("paths") or ()) == 1
    and int(stats.get("bytes", 0)) > len("hello world"),
    repr(stats),
)

# ── 3. Direct-WS gating & usage_meta + direct_chat wiring ─────────────────
direct_src = (ROOT / "core" / "direct_runner.py").read_text(encoding="utf-8")
chat_run_src = (ROOT / "core" / "chat_run.py").read_text(encoding="utf-8")
planner_src = (ROOT / "hive_functions" / "planner.py").read_text(encoding="utf-8")
vram_src = (ROOT / "core" / "duo" / "_vram.py").read_text(encoding="utf-8")
duo_src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
epilogue_src = (ROOT / "core" / "duo_epilogue.py").read_text(encoding="utf-8")

check(
    "Direct-WS gates on _direct_tools_enabled and direct_runner instruments direct_chat + predicted_ms",
    "_direct_ws_tool = _direct_ws_enabled and _direct_tools_enabled and not _direct_tools_active and not _direct_tier_off" in direct_src
    and 'ctx.phase_timer.start("direct_chat")' in direct_src
    and 'ctx.phase_timer.end("direct_chat", status=_direct_stop_reason)' in direct_src
    and '"timer_key": "direct_chat"' in direct_src
    and '_d_tim_ms = float((_d_tim.get("predicted_ms")' in direct_src,
)

check(
    "chat_run, duo/_vram, duo_runner, and duo_epilogue wire direct_chat, tool_round_durations, and read stats",
    '("direct", "direct_chat")' in chat_run_src
    and "tool_round_durations=_tool_round_durations" in chat_run_src
    and "reset_read_file_stats" in chat_run_src
    and "ctx.tool_round_durations" in vram_src
    and "ctx.tool_round_durations" in duo_src
    and "get_read_file_stats" in epilogue_src,
)

from core.duo._vram import _format_ctx_label  # noqa: E402

check(
    "_format_ctx_label matches UI footer decimal k formatting",
    _format_ctx_label(65000) == "65k"
    and _format_ctx_label(60000) == "60k"
    and _format_ctx_label(65536) == "65.5k"
    and _format_ctx_label(32768) == "32.8k"
    and _format_ctx_label(512) == "512",
    repr([_format_ctx_label(x) for x in (65000, 60000, 65536, 32768, 512)]),
)

# ── 4. Planner _llm_stream pure-decode gen_ms, cache_n & final timings merge ─
import hive_functions.planner as planner_mod  # noqa: E402


class _FakeResp:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_lines(self):
        for ln in self._lines:
            yield ln


class _FakeClient:
    def __init__(self, lines, **kwargs):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, *args, **kwargs):
        return _FakeResp(self._lines)


_sse_lines = [
    "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "step 1 "}}]}),
    "data: " + json.dumps({"choices": [{"delta": {"content": "1. Do thing\n"}}]}),
    "data: " + json.dumps({
        "choices": [],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 24,
            "completion_tokens_details": {"reasoning_tokens": 10},
        },
        "timings": {
            "cache_n": 96,
            "predicted_ms": 1200.0,
            "predicted_per_second": 20.0,
            "prompt_per_second": 140.0,
        },
    }),
    "data: [DONE]",
]
_emitted = []


async def _emit_capture(ev):
    _emitted.append(ev)


async def _run_fake_planner_stream():
    with patch.object(planner_mod.httpx, "AsyncClient", side_effect=lambda **kw: _FakeClient(_sse_lines, **kw)):
        return await planner_mod._llm_stream(
            "demo",
            8101,
            [{"role": "user", "content": "hi"}],
            emit_fn=_emit_capture,
        )


_thk_txt, _pln_txt, _hit_to = asyncio.run(_run_fake_planner_stream())
_usage_evs = [e for e in _emitted if e.get("type") == "usage_meta"]

check(
    "Planner _llm_stream merges timings.cache_n, predicted_ms, predicted_per_second & prompt_per_second",
    not _hit_to
    and "step 1" in _thk_txt
    and "Do thing" in _pln_txt
    and len(_usage_evs) == 1
    and _usage_evs[0].get("cached_tokens") == 96
    and _usage_evs[0].get("gen_ms") == 1200
    and _usage_evs[0].get("decode_tps") == 20.0
    and _usage_evs[0].get("prompt_tps") == 140.0,
    repr(_usage_evs),
)

# ── 5. Consolidated Reasoning Card HTML/CSS/JS invariants + Node VM check ─
html_src = (ROOT / "index.html").read_text(encoding="utf-8")
css_src = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
stream_h_src = (ROOT / "static" / "js" / "stream_handlers.js").read_text(encoding="utf-8")
planner_ui_src = (ROOT / "static" / "js" / "planner_ui.js").read_text(encoding="utf-8")
settings_io_src = (ROOT / "static" / "js" / "settings_io.js").read_text(encoding="utf-8")

check(
    "Consolidated #reasoning-card structure, slot placement, flip-up CSS & live planner rate handlers exist",
    'id="chat-reasoning-section"' in html_src
    and 'id="reasoning-card"' in html_src
    and 'id="sidebar-main-effort-trigger"' in html_src
    and 'id="planner-effort-trigger"' in html_src
    and 'id="reasoning-adv-details"' in html_src
    and 'id="duo-reasoning-slot"' in html_src
    and html_src.index('id="chat-reasoning-section"') < html_src.index('id="chat-tools-section"')
    and html_src.index('id="dmo-agent-opts"') < html_src.index('id="duo-reasoning-slot"') < html_src.index('id="duo-tools-slot"')
    and ".sidebar-effort-wrap .effort-menu.open-up" in css_src
    and ".reasoning-card" in css_src
    and "function updateReasoningCardVisibility()" in settings_io_src
    and "if (S.perfDecodeEma > 0)" in planner_ui_src
    and "if (typeof _updatePlannerTokRate === 'function') _updatePlannerTokRate();" in stream_h_src,
)

_node_check = r"""
const vm = require('vm');
const { readAllUiJs } = require('./tests/ui_sources.cjs');
const appSrc = readAllUiJs({ normalizeNewlines: true });
const s = appSrc.indexOf('var _REASONING_EFFORT_LABELS =');
const e = appSrc.indexOf('\n// removed: updateToolThinkingModeVis', s);
const rsnSrc = appSrc.slice(s, e);
const mStart = appSrc.indexOf('function updateReasoningCardVisibility()');
const mEnd = appSrc.indexOf('\nfunction setDuoCoderToolTier(', mStart);
const visSrc = appSrc.slice(mStart, mEnd);

function makeCls(init) {
  const set = new Set(init || []);
  return {
    add(c){ set.add(c); },
    remove(c){ set.delete(c); },
    toggle(c, f){ if (f === undefined) { set.has(c) ? set.delete(c) : set.add(c); } else if (f) set.add(c); else set.delete(c); },
    contains(c){ return set.has(c); }
  };
}
function mkOpts() {
  return ['auto', 'none', 'on', 'minimal', 'low', 'medium', 'high', 'max'].map(l => ({
    _lvl: l, hidden: false, disabled: false, classList: makeCls(['effort-opt']),
    _a: { 'data-effort': l },
    getAttribute(k){ return this._a[k] || null; },
    setAttribute(k, v){ this._a[k] = String(v); },
  }));
}
function mkEl(extra) {
  return Object.assign({
    hidden: false, textContent: '', innerHTML: '', value: '', disabled: false,
    className: '', title: '', style: { display: '' }, classList: makeCls(),
    _a: {}, _children: [], _parent: null,
    setAttribute(k, v){ this._a[k] = String(v); },
    getAttribute(k){ return this._a[k] || null; },
    removeAttribute(k){ delete this._a[k]; },
    appendChild(c){ c._parent = this; this._children.push(c); },
    insertBefore(c){ c._parent = this; this._children.unshift(c); },
    get parentNode(){ return this._parent; },
  }, extra || {});
}

const mainOpts = mkOpts(), smOpts = mkOpts(), planOpts = mkOpts();
const byId = {
  'chat-reasoning-section': mkEl(),
  'reasoning-card': mkEl(),
  'duo-reasoning-slot': mkEl(),
  'reasoning-card-scope-sub': mkEl(),
  'reasoning-card-reset-btn': mkEl({ hidden: true }),
  'reasoning-adv-details': mkEl({ hidden: false }),
  'reasoning-adv-summary-val': mkEl(),
  'sidebar-main-role-lbl': mkEl(),
  'sidebar-main-effort-hint': mkEl(),
  'sidebar-main-effort-wrap': mkEl(),
  'sidebar-main-effort-trigger': mkEl(),
  'sidebar-main-effort-trig-lbl': mkEl(),
  'sidebar-main-effort-menu-ctx': mkEl(),
  'sidebar-main-effort-menu-ovr': mkEl({ hidden: true }),
  'sidebar-main-effort-menu-ovr-txt': mkEl(),
  'sidebar-main-effort-ovr-reset-btn': mkEl({ hidden: true }),
  'sidebar-main-effort-menu-empty': mkEl({ hidden: true }),
  'sidebar-main-effort-menu': mkEl({ hidden: true, querySelectorAll(sel){ return sel === '.effort-opt' ? smOpts : []; } }),
  'planner-effort-row': mkEl(),
  'planner-effort-inactive-pill': mkEl({ hidden: true }),
  'planner-effort-wrap': mkEl(),
  'planner-effort-trigger': mkEl(),
  'planner-effort-trig-lbl': mkEl(),
  'planner-effort-hint': mkEl(),
  'planner-effort-menu-ctx': mkEl(),
  'planner-effort-menu-ovr': mkEl({ hidden: true }),
  'planner-effort-menu-ovr-txt': mkEl(),
  'planner-effort-ovr-reset-btn': mkEl({ hidden: true }),
  'planner-effort-menu-empty': mkEl({ hidden: true }),
  'planner-effort-menu': mkEl({ hidden: true, querySelectorAll(sel){ return sel === '.effort-opt' ? planOpts : []; } }),
  'effort-trigger': mkEl(),
  'effort-trig-lbl': mkEl(),
  'effort-menu-ctx': mkEl(),
  'effort-menu-ovr': mkEl({ hidden: true }),
  'effort-menu-ovr-txt': mkEl(),
  'effort-ovr-reset-btn': mkEl({ hidden: true }),
  'effort-menu-empty': mkEl({ hidden: true }),
  'effort-menu': mkEl({ hidden: true, querySelectorAll(sel){ return sel === '.effort-opt' ? mainOpts : []; } }),
};
byId['chat-reasoning-section'].appendChild(byId['reasoning-card']);

const patches = [];
const ctx = {
  S: {
    mode: 'auto', routingPlanner: 'decider', duoAgenticMode: true, duoPlannerEnabled: true,
    duoAgenticThinking: true, duoChunking: false, duoToolThinkingAlways: false, duoToolThinkingMode: 'off',
    duoPlannerMaxTokens: 8000, duoPlannerUseCoderCtx: true,
    reasoningEffort: 'auto', plannerReasoningEffort: 'auto',
    duoCoderModel: 'inkling-2-mini-ud', duoPlannerModel: '',
    modelProfiles: {
      'inkling-2-mini-ud': { thinking: true, reasoning_mode: 'effort', reasoning_levels: ['none', 'minimal', 'low', 'medium', 'high', 'max'], reasoning_template_source: 'gguf', reasoning_effort: 'medium' },
    },
    currentAssignments: { direct: { model: 'inkling-2-mini-ud' } },
    agentsCfg: { direct: { model: 'inkling-2-mini-ud', thinking: true }, duo_coder: { model: 'inkling-2-mini-ud' } },
  },
  document: {
    getElementById(id){ return byId[id] || null; },
    querySelectorAll(){ return []; },
  },
  _shortModel: m => m,
  _postSettingsFF: (p) => { patches.push(p); },
  _ctxMoveTo: (el, slot) => { slot.appendChild(el); },
  _ctxRestore: (el) => { byId['chat-reasoning-section'].appendChild(el); },
};
vm.createContext(ctx);
vm.runInContext(rsnSrc + '\n' + visSrc, ctx);

// 1. Auto mode -> sits in chat-reasoning-section, Direct / Coder + Planner visible, Adv summary formatted
ctx.updateReasoningCardVisibility();
const c1 = {
  role: byId['sidebar-main-role-lbl'].textContent,
  sub: byId['reasoning-card-scope-sub'].textContent,
  planDisp: byId['planner-effort-row'].style.display,
  advHidden: byId['reasoning-adv-details'].hidden,
  advVal: byId['reasoning-adv-summary-val'].textContent,
  resetHidden: byId['reasoning-card-reset-btn'].hidden,
};

// 2. Setting Main effort to high updates BOTH composer & sidebar labels and reveals Reset button
ctx.setReasoningEffort('high');
const c2 = {
  compLbl: byId['effort-trig-lbl'].textContent,
  sideLbl: byId['sidebar-main-effort-trig-lbl'].textContent,
  resetHidden: byId['reasoning-card-reset-btn'].hidden,
};

// 3. Switching to code_duo with duoPlannerEnabled=false moves card to duo-reasoning-slot and shows 'off' pill
ctx.S.mode = 'code_duo';
ctx.S.duoPlannerEnabled = false;
ctx.updateReasoningCardVisibility();
const c3 = {
  role: byId['sidebar-main-role-lbl'].textContent,
  sub: byId['reasoning-card-scope-sub'].textContent,
  inDuoSlot: byId['reasoning-card']._parent === byId['duo-reasoning-slot'],
  planInactive: byId['planner-effort-row'].classList.contains('is-inactive'),
  pillHidden: byId['planner-effort-inactive-pill'].hidden,
};

// 4. Switching to simple (Chat) hides planner row & adv details
ctx.S.mode = 'simple';
ctx.updateReasoningCardVisibility();
const c4 = {
  sub: byId['reasoning-card-scope-sub'].textContent,
  planDisp: byId['planner-effort-row'].style.display,
  advHidden: byId['reasoning-adv-details'].hidden,
  restored: byId['reasoning-card']._parent === byId['chat-reasoning-section'],
};

// 5. resetAllReasoningEffort clears both overrides
ctx.setPlannerReasoningEffort('low');
ctx.resetAllReasoningEffort();
const c5 = {
  mainEff: ctx.S.reasoningEffort,
  planEff: ctx.S.plannerReasoningEffort,
  resetHidden: byId['reasoning-card-reset-btn'].hidden,
};

console.log(JSON.stringify({ c1, c2, c3, c4, c5 }));
"""
_nc = subprocess.run(["node", "-e", _node_check], cwd=ROOT, capture_output=True, text=True)
_nc_ok = _nc.returncode == 0
_nc_j = json.loads(_nc.stdout.strip()) if _nc_ok else {}
check(
    "Node VM verifies consolidated Reasoning card sync, inactive pill, slot move & resetAllReasoningEffort",
    _nc_ok
    and _nc_j["c1"]["role"] == "Direct / Coder"
    and _nc_j["c1"]["sub"] == "Direct + Planner"
    and _nc_j["c1"]["planDisp"] == ""
    and _nc_j["c1"]["advHidden"] is False
    and _nc_j["c1"]["advVal"] == "Plan: On \u00b7 Tools: Off \u00b7 8k"
    and _nc_j["c1"]["resetHidden"] is True
    and _nc_j["c2"]["compLbl"] == "High"
    and _nc_j["c2"]["sideLbl"] == "High"
    and _nc_j["c2"]["resetHidden"] is False
    and _nc_j["c3"]["role"] == "Coder"
    and _nc_j["c3"]["sub"] == "Coder (Planner off)"
    and _nc_j["c3"]["inDuoSlot"] is True
    and _nc_j["c3"]["planInactive"] is True
    and _nc_j["c3"]["pillHidden"] is False
    and _nc_j["c4"]["sub"] == "Direct"
    and _nc_j["c4"]["planDisp"] == "none"
    and _nc_j["c4"]["advHidden"] is True
    and _nc_j["c4"]["restored"] is True
    and _nc_j["c5"]["mainEff"] == "auto"
    and _nc_j["c5"]["planEff"] == "auto"
    and _nc_j["c5"]["resetHidden"] is True,
    _nc.stdout.strip() or _nc.stderr.strip(),
)

# ── 6. Live Metrics separation & in-flight output tracking (Node VM) ───────
_node_metrics = r"""
const vm = require('vm');
const fs = require('fs');
const path = require('path');

const transportSrc = fs.readFileSync(path.join(process.cwd(), 'static/js/stream_transport.js'), 'utf8');
const handlersSrc = fs.readFileSync(path.join(process.cwd(), 'static/js/stream_handlers.js'), 'utf8');

function extractRange(src, startMarker, endMarker) {
  const i = src.indexOf(startMarker);
  const j = endMarker ? src.indexOf(endMarker, i) : src.length;
  if (i < 0 || j < 0) throw new Error('Marker not found: ' + startMarker);
  return src.slice(i, j);
}

const perfSlice = extractRange(transportSrc, 'function _perfResetRuntimeState()', '// ── Phase Timing Pills');
const doneBlock = '  S.perfCompressing = false;\n  _perfRender(true);\n}';
const usageSlice = extractRange(handlersSrc, "  'usage_meta': function(d) {", "  'steer': function(d) {");
const toolGenSlice = extractRange(handlersSrc, "  'tool_gen': function(d) {", "  'tool_gen_reset': function(d) {");
const handleSlice = extractRange(handlersSrc, 'function handleEvent(d) {', '');

const domIds = ['pbb-ctx', 'pbb-ctxreuse', 'pbb-tok', 'pbb-rate', 'pbb-decode', 'pbb-in', 'pbb-cached', 'pbb-new', 'pbb-reqs', 'pbb-tools', 'pbb-summary'];
const els = {
  'perf-bar-bottom': { dataset: { built: '1' }, classList: { add() {} }, style: {}, textContent: '' },
  'agent-1': { textContent: '' },
};
for (const id of domIds) els[id] = { textContent: '', className: '' };

let fakeNow = 100000;
class FakeDate {
  static now() { return fakeNow; }
}
const ctx = {
  Date: FakeDate,
  Math, Number, parseInt, parseFloat, isNaN, isFinite, String, Object,
  S: {
    perfRunStartedAt: 0, perfFirstTokenAt: 0, perfActiveMs: 0, perfLastTokAt: 0, perfDecodeEma: 0,
    perfToolCalls: 0, perfToolMs: 0, perfToolLabel: '', perfToolStart: 0, perfLastToolTxt: '--',
    perfChars: 0, perfEstTokens: 0, perfRealTokens: 0, perfInFlightChars: 0, perfInFlightTokens: 0,
    perfCtxTokens: 0, perfCtxLimit: 0, perfCtxPct: 0, perfCtxReal: 0, perfCtxCached: 0,
    perfTokRate: 0, perfTokRateEma: 0, perfCompressing: false,
    runPromptTokens: 0, runCachedTokens: 0, runRequestCount: 0,
    curAgent: { tid: 'agent-1' },
  },
  document: { getElementById(id) { return els[id] || null; } },
  _perfPaintLastAt: 0,
  _pbbEnsure() {},
  _hmStorageGet() { return ''; },
  _hmStorageSet() {},
  _fmtTokens(n) { return n >= 1000 ? (n / 1000).toFixed(1) + 'K' : String(n); },
  _toolGenStream() {},
  _updatePlannerTokRate() {},
  scrollBtmIfNearBottom() {},
};

const script = `
var window = {};
${perfSlice}
${doneBlock}
function _perfEnsureUiRow() { return document.getElementById('perf-bar-bottom'); }
var EVENT_HANDLERS = {
${usageSlice}
${toolGenSlice}
  'ctx_meter': function(d) { _perfOnCtxMeter(d.est_tokens, d.ctx_limit, !!d.compressing, d.real_tokens, d.cached_tokens); },
  'thinking_token': function(d) { _perfOnToken(d.content); },
  'status': function() {}
};
${handleSlice}
`;
vm.createContext(ctx);
vm.runInContext(script, ctx);

ctx._perfResetRuntimeState();
// 1. Status + ctx_meter must NOT start perfFirstTokenAt / perfActiveMs or pollute perfEstTokens
ctx.handleEvent({ type: 'status', content: 'Loading coder...' });
ctx.handleEvent({ type: 'ctx_meter', est_tokens: 6200, ctx_limit: 65000, compressing: false, real_tokens: 6200, cached_tokens: 6138 });
const s1 = {
  firstTokAt: ctx.S.perfFirstTokenAt,
  activeMs: ctx.S.perfActiveMs,
  ctxTokens: ctx.S.perfCtxTokens,
  estTokens: ctx.S.perfEstTokens,
  ctxText: els['pbb-ctx'].textContent,
};

// 2. Previous round usage_meta (106 tok, 19.3 decode) followed by 10s later streaming 10560 chars of thinking + tool_gen
fakeNow = 105000;
ctx.handleEvent({ type: 'usage_meta', completion_tokens: 106, prompt_tokens: 6200, cached_tokens: 6138, decode_tps: 19.3 });
fakeNow = 120000; // long prefill gap before next turn's first output token
const chunk = 'x'.repeat(352);
for (let k = 0; k < 15; k++) {
  fakeNow += 500;
  ctx.handleEvent({ type: 'thinking_token', content: chunk });
  fakeNow += 500;
  ctx.handleEvent({ type: 'tool_gen', content: chunk });
}
const s2 = {
  inFlightTok: ctx.S.perfInFlightTokens,
  liveOut: ctx._perfLiveOutTokens(false),
  outText: els['pbb-tok'].textContent,
  ctxText: els['pbb-ctx'].textContent,
  rateText: els['pbb-rate'].textContent,
  wallRate: ctx._perfWallRate(),
};

// 3. Turn usage_meta finalizes round (no double count) + _perfOnDone stays consistent
fakeNow += 500;
ctx.handleEvent({ type: 'usage_meta', completion_tokens: 3000, prompt_tokens: 12500, cached_tokens: 6200, decode_tps: 19.3 });
const afterUsage = {
  realTok: ctx.S.perfRealTokens,
  inFlightTok: ctx.S.perfInFlightTokens,
  outText: els['pbb-tok'].textContent,
};
ctx._perfOnDone({
  metrics_ctx_peak_tokens: 15500,
  metrics_ctx_limit: 65000,
  metrics_ctx_pressure_peak: 0.24,
  elapsed: 30,
  phase_timings: { total: { real_tokens: 3106, gen_s: 160.0 } },
});
const afterDone = {
  ctxTokens: ctx.S.perfCtxTokens,
  estTokens: ctx.S.perfEstTokens,
  outText: els['pbb-tok'].textContent,
  ctxText: els['pbb-ctx'].textContent,
};

console.log(JSON.stringify({ s1, s2, afterUsage, afterDone }));
"""
_nm = subprocess.run(["node", "-e", _node_metrics], cwd=ROOT, capture_output=True, text=True)
_nm_ok = _nm.returncode == 0
_nm_j = json.loads(_nm.stdout.strip()) if _nm_ok else {}
check(
    "Node VM verifies perf metrics separate ctx vs output, count thinking+tool_gen live, and avoid double-booking",
    _nm_ok
    and _nm_j["s1"]["firstTokAt"] == 0
    and _nm_j["s1"]["activeMs"] == 0
    and _nm_j["s1"]["ctxTokens"] == 6200
    and _nm_j["s1"]["estTokens"] == 0
    and _nm_j["s1"]["ctxText"] == "ctx: 6.2K/65.0K (10%) \u00b7 real"
    and _nm_j["s2"]["inFlightTok"] == round((30 * 352) / 3.5)
    and _nm_j["s2"]["liveOut"] == 106 + _nm_j["s2"]["inFlightTok"]
    and _nm_j["s2"]["outText"] == "out: 3.1K"
    and _nm_j["s2"]["ctxText"] == "ctx: 9.2K/65.0K (14%) \u00b7 real"
    and _nm_j["s2"]["wallRate"] > 50.0
    and _nm_j["afterUsage"]["realTok"] == 3106
    and _nm_j["afterUsage"]["inFlightTok"] == 0
    and _nm_j["afterUsage"]["outText"] == "out: 3.1K"
    and _nm_j["afterDone"]["ctxTokens"] == 15500
    and _nm_j["afterDone"]["outText"] == "out: 3.1K"
    and _nm_j["afterDone"]["ctxText"] == "ctx: 15.5K/65.0K (24%) \u00b7 real",
    _nm.stdout.strip() or _nm.stderr.strip(),
)

sys.exit(h.finish("metrics_and_reasoning_ui"))

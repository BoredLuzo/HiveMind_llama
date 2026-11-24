"""Regression checks for direct chat empty-bubble bug, header model chip, and direct_tools run_meta.

Clean-install v1.6.3 screenshot + log bug report (2026-11-18):
  1. Refactoring `startAgent`/`startDuoAgent` extracted `function _createThinkBlock(tid)`
     above the already-existing inline `function _createThinkBlock()` in `static/app.js`.
     JS hoisting bound the caller to the parameterless inline helper, which read
     `S.curAgent.tid` while `S.curAgent` was still `null` — throwing a TypeError inside
     `startAgent()`, leaving `#chat` with zero assistant bubble and only `RUN COMPLETED`.
  2. `_syncAgentModelState()` only called `updateModelsChip()` for duo roles (not `direct` /
     `analyst` / `synthesizer`), `loadAgentMap()` / `showAutomapEvent()` did not call it,
     and `startAgent` did not reflect the actual routed model (`lfm3:1.7b-qad`) in `#h-models`.
  3. `_run_direct_tools` never emitted `run_meta` (`{"direct": _direct_model}`), causing
     `core.chat_run` `done` attribution to fall back to `analyst` (`gemma-4:e4b-it-qat`).

Run: python tests/test_direct_ui_and_thinkblock.py
"""
import asyncio
from collections import Counter
import contextlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import types
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tests.ui_sources import read_all_ui_js  # noqa: E402
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="summary")
check = h.check

_app_js = read_all_ui_js()
_html = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")

# ── 1) No duplicate top-level JS function names (hoisting overwrite guard) ──
_fn_names = re.findall(r"^function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(", _app_js, re.M)
_dups = sorted((name, cnt) for name, cnt in Counter(_fn_names).items() if cnt > 1)
check("no duplicate top-level JS function declarations in app.js",
      len(_dups) == 0, f" (dups={_dups})")

check("outer agent think helper renamed to _buildAgentThinkBlock",
      "function _buildAgentThinkBlock(tid)" in _app_js
      and "function _createThinkBlock()" in _app_js)

_cb_match = re.search(r"app\.js\?v=(\d{8})-(\d+)", _html)
check("index.html cache-buster bumped to >= 20261118-01",
      _cb_match is not None and (_cb_match.group(1), int(_cb_match.group(2))) >= ("20261118", 1),
      f" ({_cb_match.group(0) if _cb_match else '?'})")

# ── 2) Live Node VM execution of startAgent / startDuoAgent / _createThinkBlock / updateModelsChip ──
_node = shutil.which("node")
if _node:
    def _extract_fn(src: str, sig: str) -> str:
        idx = src.find(sig)
        assert idx >= 0, f"missing signature: {sig}"
        end = src.find("\n}", idx)
        assert end >= 0, f"unterminated function: {sig}"
        return src[idx:end + 2]

    _js_bundle = "\n\n".join([
        _extract_fn(_app_js, "function _shortModel(m)"),
        _extract_fn(_app_js, "function updateModelsChip()"),
        _extract_fn(_app_js, "function _syncAgentModelState(key, model, sel)"),
        _extract_fn(_app_js, "function startAgent(name, model, role, isRepair)"),
        _extract_fn(_app_js, "function _attachLoadTimer(hdr, tid, baseColor)"),
        _extract_fn(_app_js, "function _buildAgentThinkBlock(tid)"),
        _extract_fn(_app_js, "function startDuoAgent(role, label, model, roundInfo)"),
        _extract_fn(_app_js, "function _createThinkBlock()"),
    ])

    _harness_js = r"""
function mkEl(tag) {
  return {
    tagName: String(tag || 'div').toUpperCase(),
    id: '', className: '', textContent: '', _innerHTML: '',
    style: {}, dataset: {}, children: [], parentNode: null, parentElement: null,
    title: '', value: '',
    set innerHTML(v) { this._innerHTML = String(v || ''); },
    get innerHTML() { return this._innerHTML; },
    appendChild(ch) {
      if (!ch || typeof ch !== 'object' || !('tagName' in ch)) {
        throw new TypeError("parameter 1 is not of type 'Node'");
      }
      ch.parentNode = this; ch.parentElement = this;
      this.children.push(ch);
      _regIds(ch);
      return ch;
    },
    classList: {
      add() {}, remove() {}, toggle() { return true; }, contains() { return false; }
    },
    querySelector() { return null; },
  };
}
const _ids = {};
function _regIds(el) {
  if (!el || typeof el !== 'object') return;
  if (el.id) _ids[el.id] = el;
  (el.children || []).forEach(_regIds);
}
_ids['chat'] = mkEl('div'); _ids['chat'].id = 'chat';
_ids['h-models'] = mkEl('div'); _ids['h-models'].id = 'h-models';
global.document = {
  createElement: mkEl,
  getElementById: (id) => _ids[id] || null,
  querySelector: () => null,
};
global.setTimeout = () => 1;
global.setInterval = () => 1;
global.clearTimeout = () => {};
global.clearInterval = () => {};
global.cancelAnimationFrame = () => {};
global.rmEl = () => {};
global._tokenQueue = [];
global._tokenRafId = null;
global.agentColor = () => '#8858c0';
global._isNoTc = () => false;
global.scrollBtmIfNearBottom = () => {};
global._liveMdReset = () => {};
global.updateDuoImageUI = () => {};
global.updateDuoPairHint = () => {};
global.updateMoeVisibility = () => {};
global.renderAgentMap = () => { global._mapRendered += 1; };
global._updateVisionPreview = () => {};
global._mapRendered = 0;
global.S = {
  mode: 'auto',
  curAgent: null,
  _thinkBlockId: null,
  _thinkSeq: 0,
  currentAssignments: { direct: { model: 'gemma-4:e4b-it-qat' } },
  agentsCfg: { direct: { model: 'gemma-4:e4b-it-qat' } },
};
""" + _js_bundle + r"""
updateModelsChip();
const chipBefore = _ids['h-models'].textContent;
startAgent('Answer', 'lfm3:1.7b-qad', '', false);
const directTid = S.curAgent && S.curAgent.tid;
const chipAfterStart = _ids['h-models'].textContent;
const chatCount1 = _ids['chat'].children.length;
const hasTk1 = !!_ids['tk-' + directTid];
const inlineId = _createThinkBlock();
S.curAgent = null;
startDuoAgent('coder', 'Coder', 'qwen3.5:9b', '1/1');
const duoTid = S.curAgent && S.curAgent.tid;
const chatCount2 = _ids['chat'].children.length;
const hasTk2 = !!_ids['tk-' + duoTid];
_syncAgentModelState('direct', 'qwen3.5:4b', null);
const chipAfterSync = _ids['h-models'].textContent;
console.log(JSON.stringify({
  chipBefore,
  chipAfterStart,
  chatCount1,
  hasTk1,
  inlineIdOk: typeof inlineId === 'string' && inlineId.startsWith('thk-' + directTid + '-'),
  chatCount2,
  hasTk2,
  chipAfterSync,
  mapRendered: _mapRendered,
}));
"""
    _proc = subprocess.run([_node, "-"], input=_harness_js, capture_output=True, text=True, encoding="utf-8", timeout=15)
    check("node harness executes startAgent + startDuoAgent + _createThinkBlock cleanly",
          _proc.returncode == 0, f" (rc={_proc.returncode}, stderr={_proc.stderr[:240]})")
    _out = json.loads(_proc.stdout.strip()) if _proc.returncode == 0 and _proc.stdout.strip() else {}
    check("startAgent renders Answer bubble + tk-<tid> when S.curAgent is null",
          _out.get("chatCount1") == 1 and _out.get("hasTk1") is True, f" ({_out})")
    check("inline _createThinkBlock() creates thk-<tid>-<seq> inside abody",
          _out.get("inlineIdOk") is True, f" ({_out})")
    check("startDuoAgent renders Duo bubble + tk-<tid> cleanly",
          _out.get("chatCount2") == 2 and _out.get("hasTk2") is True, f" ({_out})")
    check("#h-models updates dynamically for Answer model and _syncAgentModelState('direct')",
          _out.get("chipBefore") == "gemma-4:e4b-it-qat"
          and _out.get("chipAfterStart") == "lfm3:1.7b-qad"
          and _out.get("chipAfterSync") == "qwen3.5:4b"
          and _out.get("mapRendered", 0) >= 1,
          f" ({_out})")
else:
    print("  SKIP node not installed — JS VM behavioral checks skipped")

# ── 3) Backend: run_direct with direct_tools emits run_meta for _direct_model ──
import backend.llama_client as _lc  # noqa: E402
import core.direct_runner as _dr  # noqa: E402
import core.tool_loop as _tl  # noqa: E402
from core.run_context import RunContext  # noqa: E402


class _FakeMgr:
    async def ensure_loaded(self, model, num_ctx=None, vision=False):
        return 8101


class _FakeClient:
    async def aclose(self):
        return None


class _FakeLoop:
    def __init__(self, *a, **kw):
        self._client = _FakeClient()
        self.state = None

    async def run(self, msgs):
        st = _tl.ToolLoopState()
        st.content_parts = ["Mir geht es gut!"]
        st.prompt_tokens = 1081
        st.completion_tokens = 19
        self.state = st
        yield {"type": "token", "content": "Mir geht es gut!"}


async def _drive_direct_tools_run():
    events = []

    async def _emit(ev):
        events.append(ev)
        return ev

    ctx = RunContext()
    ctx.complexity = "simple"
    ctx.complexity_source = "manual"
    ctx.effective_task_type = "general"
    ctx.active_preset = "default"
    ctx.models_cache = ["lfm3:1.7b-qad", "gemma-4:e4b-it-qat"]
    ctx.registry_get = lambda role: "gemma-4:e4b-it-qat" if role == "analyst" else ""
    ctx.model_profile = lambda m: {}
    ctx.settings = {
        "direct_tools_enabled": True,
        "direct_tools_tier": "research",
        "mode": "auto",
    }
    ctx.user_input = "Hey, wie gehts?"
    ctx.workspace = str(ROOT)
    ctx.run_id = "test-run-meta-1"
    ctx.memory = types.SimpleNamespace(add_to_session=lambda role, content: None)
    ctx.get_num_ctx = lambda *a, **k: 8192
    ctx.aborted = lambda: False
    ctx.unregister_abort = lambda *a: None
    ctx.unregister_step_skip = lambda *a: None
    ctx.increment_run_counter = lambda: 1
    ctx.collect_done_metrics = lambda: {}
    ctx.done_event = lambda elapsed, stop_reason, **kw: {
        "type": "done", "elapsed": elapsed, "stop_reason": stop_reason, **kw
    }
    ctx.get_effective_prompt_with_override = lambda *a, **k: "SYS"
    ctx.make_messages = lambda p, s, inp, *a, **k: [
        {"role": "system", "content": s},
        {"role": "user", "content": inp},
    ]

    async def _preload(model):
        return None
    ctx.maybe_preload = _preload

    async def _soul_evolve(n):
        return None
    ctx.maybe_trigger_soul_evolution = _soul_evolve

    async def _async_noop(*a, **kw):
        return None

    ctx.emit = _emit
    ctx.pipeline = types.SimpleNamespace(
        agents={"direct": types.SimpleNamespace(
            model="lfm3:1.7b-qad", temperature=0.4, max_tokens=600,
            thinking=False, thinking_budget=0,
        )}
    )

    orig_mgr = _lc.manager
    orig_loop = _tl.ToolLoop
    _lc.manager = _FakeMgr()
    _tl.ToolLoop = _FakeLoop
    try:
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(_dr, "_pick_direct_model", lambda *a, **k: "lfm3:1.7b-qad"))
            es.enter_context(mock.patch.object(_dr, "_refresh_judge_keepalive", _async_noop))
            es.enter_context(mock.patch.object(_dr, "run_peer_ratings", _async_noop))
            async for _ in _dr.run_direct(ctx):
                pass
    finally:
        _lc.manager = orig_mgr
        _tl.ToolLoop = orig_loop
    return events


_evs = asyncio.run(_drive_direct_tools_run())
_run_metas = [e for e in _evs if e.get("type") == "run_meta"]
_tokens = "".join(e.get("content", "") for e in _evs if e.get("type") == "token")
check("direct_tools run_direct emits run_meta with actual direct model",
      any((rm.get("models") or {}).get("direct") == "lfm3:1.7b-qad" for rm in _run_metas)
      and "Mir geht es gut!" in _tokens,
      f" (run_metas={_run_metas})")

sys.exit(h.finish("direct_ui_and_thinkblock"))

# -*- coding: utf-8 -*-
"""Regressions for silent test notifications, fast Saved Chats indexing/listing,
quiet speculative GGUF resolution, and chronological Think -> Tool -> Cut DOM order.

Run: python tests/test_notify_chats_speed_and_think_order.py
Exit 0 = all pass, Exit 1 = failures.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


# ── 1. Windows notification silence during tests ─────────────────────────────
import infra.notify as NTF  # noqa: E402

_orig_no_notify = os.environ.get("HIVEMIND_NO_NOTIFY")
_thread_calls: list = []
try:
    os.environ["HIVEMIND_NO_NOTIFY"] = "1"
    with mock.patch.object(NTF.sys, "platform", "win32"), \
         mock.patch.object(NTF, "_notifications_enabled", return_value=True), \
         mock.patch.object(NTF.threading, "Thread", side_effect=lambda *a, **kw: _thread_calls.append(a) or mock.Mock()):
        _suppressed = NTF.notify("Test Title", "Test Body", dedup_sig="")
    check("HIVEMIND_NO_NOTIFY=1 suppresses desktop notifications without spawning thread", _suppressed is False and len(_thread_calls) == 0)

    os.environ.pop("HIVEMIND_NO_NOTIFY", None)
    with mock.patch.dict(NTF._last, {"ts": 0.0, "sig": ""}), \
         mock.patch.object(NTF.sys, "platform", "win32"), \
         mock.patch.object(NTF, "_notifications_enabled", return_value=True), \
         mock.patch.object(NTF.threading, "Thread", side_effect=lambda *a, **kw: _thread_calls.append(a) or mock.Mock()):
        _allowed = NTF.notify("Test Title", "Test Body", dedup_sig="t2")
    check("notify still spawns thread normally when HIVEMIND_NO_NOTIFY is unset", _allowed is True and len(_thread_calls) == 1)
finally:
    if _orig_no_notify is not None:
        os.environ["HIVEMIND_NO_NOTIFY"] = _orig_no_notify
    else:
        os.environ["HIVEMIND_NO_NOTIFY"] = "1"

_rr_src = (_ROOT / "tests" / "run_regressions.py").read_text(encoding="utf-8")
_har_src = (_ROOT / "tests" / "_harness.py").read_text(encoding="utf-8")
_ps_src = (_ROOT / "deploy" / "prepush_check.ps1").read_text(encoding="utf-8")
check(
    "test runners configure HIVEMIND_NO_NOTIFY=1",
    "HIVEMIND_NO_NOTIFY" in _rr_src and "HIVEMIND_NO_NOTIFY" in _har_src and "HIVEMIND_NO_NOTIFY" in _ps_src,
)


# ── 2. Saved Chats persistent index & O(N) list_chats ────────────────────────
import core.state as ST  # noqa: E402
import context.chat as CC  # noqa: E402
import routers.chats as RC  # noqa: E402

_orig_st_sdir = ST._SESSIONS_DIR
_orig_cc_sdir = CC._SESSIONS_DIR
_orig_rc_loaded = RC._cache_loaded
_orig_rc_cache = dict(RC._chats_cache)
_orig_rc_lock = RC._cache_lock
_orig_blob_cache = dict(RC._blob_size_cache)
_orig_ctx_cache = dict(RC._ctx_interrupted_cache)

try:
    with tempfile.TemporaryDirectory(prefix="hm_chats_fast_") as _td:
        _sdir = Path(_td)
        _lock = threading.Lock()
        ST._SESSIONS_DIR = _sdir
        CC.init_chat_context(_sdir, cache_lock=_lock)
        RC._cache_lock = _lock
        RC._blob_size_cache.clear()
        RC._ctx_interrupted_cache.clear()

        _cid_a = "aaaaaaaa"
        _cid_b = "bbbbbbbb"
        _fa = _sdir / f"2026-11-22T10-00-00_ChatA_{_cid_a}.json"
        _fb = _sdir / f"2026-11-22T10-05-00_ChatB_{_cid_b}.json"
        _fa.write_text(json.dumps({
            "id": _cid_a, "title": "Alpha Chat", "created_at": "2026-11-22T10:00:00",
            "updated_at": "2026-11-22T10:00:00",
            "messages": [{"role": "user", "content": "hello alpha"}, {"role": "assistant", "content": "reply alpha"}],
        }), encoding="utf-8")
        _fb.write_text(json.dumps({
            "id": _cid_b, "title": "Beta Chat", "created_at": "2026-11-22T10:05:00",
            "updated_at": "2026-11-22T10:05:00",
            "messages": [{"role": "user", "content": "hello beta"}],
        }), encoding="utf-8")

        _bdir_a = _sdir / f"{_cid_a}.blobs"
        _bdir_a.mkdir()
        (_bdir_a / "0123456789abcdef").write_bytes(b"HIVEBLOB1\n" + b"X" * 600)
        (_sdir / f"2026-11-22T10-00-00_ChatA_{_cid_a}.context.json").write_text(
            json.dumps({"last_run": {"stop_reason": "disconnect"}}),
            encoding="utf-8",
        )

        RC._cache_loaded = False
        RC._chats_cache.clear()
        RC._ensure_cache()

        _idx_file = _sdir / RC._CHATS_INDEX_NAME
        _json_names = sorted(p.name for p in _sdir.glob("*.json"))
        check(
            "_ensure_cache creates .chats_index.cache without polluting *.json globs",
            _idx_file.is_file() and _idx_file.name not in _json_names and len(_json_names) == 3,
            repr(_json_names),
        )

        # Warm reload from persisted index should NOT read unchanged chat JSONs
        RC._cache_loaded = False
        RC._chats_cache.clear()
        _read_paths: list[str] = []
        _real_read_text = Path.read_text

        def _spy_read_text(self, *args, **kwargs):
            _read_paths.append(self.name)
            return _real_read_text(self, *args, **kwargs)

        with mock.patch.object(Path, "read_text", _spy_read_text):
            RC._ensure_cache()

        check(
            "warm _ensure_cache skips re-parsing unchanged chat JSONs",
            _fa.name not in _read_paths and _fb.name not in _read_paths
            and RC._chats_cache.get(_cid_a, {}).get("title") == "Alpha Chat"
            and RC._chats_cache.get(_cid_b, {}).get("_msg_count") == 1,
            repr(_read_paths),
        )

        # Modifying ChatB invalidates ONLY ChatB
        _fb.write_text(json.dumps({
            "id": _cid_b, "title": "Beta Chat", "created_at": "2026-11-22T10:05:00",
            "updated_at": "2026-11-22T10:06:00",
            "messages": [
                {"role": "user", "content": "hello beta"},
                {"role": "user", "content": "second user msg"},
            ],
        }), encoding="utf-8")
        _st_b = _fb.stat()
        os.utime(_fb, ns=(_st_b.st_atime_ns, _st_b.st_mtime_ns + 1_000_000_000))
        RC._cache_loaded = False
        RC._chats_cache.clear()
        _read_paths.clear()
        with mock.patch.object(Path, "read_text", _spy_read_text):
            RC._ensure_cache()
        check(
            "_ensure_cache re-parses modified chat JSON while skipping unchanged chat",
            _fb.name in _read_paths and _fa.name not in _read_paths
            and RC._chats_cache.get(_cid_b, {}).get("_msg_count") == 2,
            repr((_read_paths, RC._chats_cache.get(_cid_b))),
        )

        _listed = asyncio.run(RC.list_chats())["chats"]
        _by_id = {c["id"]: c for c in _listed}
        check(
            "list_chats accurately reports bytes (including .blobs) and interrupted flag",
            _by_id.get(_cid_a, {}).get("bytes", 0) > 600
            and _by_id.get(_cid_a, {}).get("interrupted") is True
            and _by_id.get(_cid_b, {}).get("interrupted") is False,
            repr(_by_id),
        )

        asyncio.run(RC.delete_chat(_cid_a))
        _listed2 = asyncio.run(RC.list_chats())["chats"]
        check(
            "delete_chat removes chat, context, and blob dir cleanly",
            len(_listed2) == 1 and _listed2[0]["id"] == _cid_b and not _bdir_a.exists(),
        )
finally:
    ST._SESSIONS_DIR = _orig_st_sdir
    CC._SESSIONS_DIR = _orig_cc_sdir
    RC._cache_loaded = _orig_rc_loaded
    RC._chats_cache.clear()
    RC._chats_cache.update(_orig_rc_cache)
    RC._cache_lock = _orig_rc_lock
    RC._blob_size_cache.clear()
    RC._blob_size_cache.update(_orig_blob_cache)
    RC._ctx_interrupted_cache.clear()
    RC._ctx_interrupted_cache.update(_orig_ctx_cache)


# ── 3. Quiet speculative GGUF metadata lookup ────────────────────────────────
import backend.llama_models as LM  # noqa: E402
import backend.gguf_meta as GM  # noqa: E402


class _ErrCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.msgs: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.msgs.append(self.format(record))


_lm_logger = logging.getLogger("llama_models")
_cap = _ErrCapture()
_lm_logger.addHandler(_cap)
try:
    _gm_none = GM.read_gguf_metadata("nonexistent-speculative-model-xyz")
    _err_after_meta = list(_cap.msgs)
    _rm_none = LM.resolve_model_path("nonexistent-speculative-model-xyz")
    _err_after_direct = list(_cap.msgs)
finally:
    _lm_logger.removeHandler(_cap)

check(
    "read_gguf_metadata uses quiet=True without emitting llama_models ERROR logs while direct resolve_model_path still errors",
    _gm_none == {} and len(_err_after_meta) == 0 and _rm_none is None and len(_err_after_direct) >= 1,
    repr((_err_after_meta, len(_err_after_direct))),
)


# ── 4. Frontend boot order + Think/Tool/Cut DOM order ────────────────────────
_js = (_ROOT / "static" / "app.js").read_text(encoding="utf-8")
_css = (_ROOT / "static" / "style.css").read_text(encoding="utf-8")
_html = (_ROOT / "index.html").read_text(encoding="utf-8")
_atl = (_ROOT / "core" / "agentic_tool_loop.py").read_text(encoding="utf-8")

_init_start = _js.index("async function init()")
_init_end = _js.index("\nfunction ", _init_start)
_init_body = _js[_init_start:_init_end]
check(
    "init() fires loadChatHistory before awaiting loadModels",
    _init_body.index("_step('loadChatHistory', loadChatHistory)")
    < _init_body.index("await _step('loadModels', loadModels)"),
)
check(
    "style.css defines .ctx-cut.warn and agentic_tool_loop attaches tool name on tool_gen_reset",
    ".ctx-cut.warn span" in _css and '"tool": _rst_tool' in _atl,
)

_m_css = re.search(r"/static/style\.css\?v=(\d{8})-(\d+)", _html)
_m_js = re.search(r"/static/app\.js\?v=(\d{8})-(\d+)", _html)
check(
    "index.html bumps style.css and app.js cache-busters to >= 20261122-03",
    _m_css is not None and (int(_m_css.group(1)), int(_m_css.group(2))) >= (20261122, 3)
    and _m_js is not None and (int(_m_js.group(1)), int(_m_js.group(2))) >= (20261122, 3),
)

# Real Node VM DOM test using the exact app.js handlers for status, tool_gen_reset, thinking_token, tool_call, tool_result
_node_test = r"""
const fs = require('fs');
const vm = require('vm');
const appSrc = fs.readFileSync('static/app.js', 'utf8').replace(/\r\n/g, '\n');

function sliceBetween(startNeedle, endNeedle) {
  const s = appSrc.indexOf(startNeedle);
  if (s < 0) throw new Error('missing ' + startNeedle);
  const e = appSrc.indexOf(endNeedle, s);
  if (e < 0) throw new Error('missing end ' + endNeedle);
  return appSrc.slice(s, e);
}

// Extract the handleEvent-style SSE branches + _createThinkBlock + _appendToolEl/_toolClusterAnchor
const statusBranch = sliceBetween("  else if (d.type === 'status') {", "  else if (d.type === 'planner_start')");
const resetBranch = sliceBetween("  else if (d.type === 'tool_gen_reset') {", "  else if (d.type === 'complexity')");
const thinkBranch = sliceBetween("  else if (d.type === 'thinking_token') {", "  else if (d.type === 'agent_done')");
const tcStart = appSrc.indexOf("  else if (d.type === 'tool_call') {");
const trEnd = appSrc.indexOf("\n  }\n}", appSrc.indexOf("  else if (d.type === 'tool_result') {"));
if (tcStart < 0 || trEnd < 0) throw new Error('missing tool_call/tool_result boundaries');
const tcTrBranches = appSrc.slice(tcStart, trEnd + 4);

function makeClassList(el) {
  const s = new Set();
  return {
    add: (...xs) => xs.forEach(x => s.add(x)),
    remove: (...xs) => xs.forEach(x => s.delete(x)),
    contains: x => s.has(x),
    toggle: (x, f) => {
      if (f === true) { s.add(x); return true; }
      if (f === false) { s.delete(x); return false; }
      if (s.has(x)) { s.delete(x); return false; }
      s.add(x); return true;
    },
    _sync: str => { s.clear(); String(str || '').split(/\s+/).filter(Boolean).forEach(x => s.add(x)); },
    _list: () => Array.from(s),
  };
}

const byId = {};
class Elem {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = { cssText: '' };
    this._id = '';
    this._innerHTML = '';
    this._textContent = '';
    this.classList = makeClassList(this);
  }
  get id() { return this._id; }
  set id(v) { this._id = v; if (v) byId[v] = this; }
  set className(v) { this.classList._sync(v); }
  get className() { return this.classList._list().join(' '); }
  get parentElement() { return this.parentNode; }
  get firstChild() { return this.children[0] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const idx = this.parentNode.children.indexOf(this);
    return (idx >= 0 && idx + 1 < this.parentNode.children.length) ? this.parentNode.children[idx + 1] : null;
  }
  get isConnected() { return !!this.parentNode || this._id === 'chat'; }
  appendChild(ch) {
    if (ch && ch.parentNode) ch.parentNode.removeChild(ch);
    if (ch && typeof ch === 'object') { ch.parentNode = this; this.children.push(ch); }
    return ch;
  }
  removeChild(ch) {
    const i = this.children.indexOf(ch);
    if (i >= 0) { this.children.splice(i, 1); ch.parentNode = null; }
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  insertBefore(newEl, refEl) {
    if (!refEl) return this.appendChild(newEl);
    if (newEl.parentNode) newEl.parentNode.removeChild(newEl);
    const i = this.children.indexOf(refEl);
    if (i < 0) return this.appendChild(newEl);
    newEl.parentNode = this;
    this.children.splice(i, 0, newEl);
    return newEl;
  }
  insertAdjacentElement(pos, el) {
    if (pos === 'afterend' && this.parentNode) {
      return this.parentNode.insertBefore(el, this.nextSibling);
    }
    return this.parentNode ? this.parentNode.appendChild(el) : null;
  }
  addEventListener() {}
  set innerHTML(v) { this._innerHTML = String(v); }
  get innerHTML() { return this._innerHTML; }
  set textContent(v) { this._textContent = String(v); }
  get textContent() { return this._textContent; }
  closest() { return null; }
  querySelector(sel) {
    if (sel === '.think-body') return { nodeType: 1, firstChild: null, appendChild() {}, textContent: '' };
    return null;
  }
  querySelectorAll(sel) {
    const tokens = sel.split(',').map(s => s.trim().replace(/^\./, ''));
    return this.children.filter(c => tokens.some(t => c.classList.contains(t)));
  }
}

const chatEl = new Elem('div'); chatEl.id = 'chat';
const agentBody = new Elem('div'); agentBody.id = 'ab-t1';
const statusEl = new Elem('div'); statusEl.id = 'status-el';
chatEl.appendChild(statusEl);

const fnSrc = `
function handleTestEvent(d) {
  if (false) {}
${statusBranch}
${resetBranch}
${thinkBranch}
${tcTrBranches}
}
`;

const ctx = {
  Node: { TEXT_NODE: 3 },
  S: { curAgent: { tid: 't1', body: agentBody }, _thinkBlockId: null, _coderHadToolCall: false, perfToolStart: 0 },
  _tgCards: {},
  _TOOL_ICONS: { run_bash: '⚡', write_file: '📝' },
  _cpCallSeq: 0,
  _chatFollow: false,
  document: {
    getElementById: (id) => byId[id] || null,
    createElement: (tag) => new Elem(tag),
    createTextNode: (t) => ({ nodeType: 3, length: t.length, appendData(s){ this.length += s.length; } }),
  },
  rmEl: (id) => { if (byId[id]) { byId[id].remove(); delete byId[id]; } },
  esc: (s) => String(s),
  showStatus: (txt) => { statusEl.textContent = txt; chatEl.appendChild(statusEl); },
  scrollBtmIfNearBottom: () => {},
  _scrollElToBottomSoon: () => {},
  _perfOnToken: () => {},
  _perfRender: () => {},
  _prexMarkDone: () => {},
  _liveMdReset: () => {},
  _flushTokenQueueSync: () => {},
  _toolGenDone: () => {},
  _pruneToolCallRows: () => {},
  _syncSpeakButtons: () => {},
  _createThinkBlock: () => {
    ctx.S._thinkSeq = (ctx.S._thinkSeq || 0) + 1;
    const tb = new Elem('div');
    tb.className = 'think-block live open';
    tb.id = 'thk-t1-' + ctx.S._thinkSeq;
    agentBody.appendChild(tb);
    return tb.id;
  },
  _toolClusterAnchor: (body) => {
    const els = body.querySelectorAll('.tool-call-row, .tool-act, .tool-result-block, .fc-preview, .write-sep, .tool-sep');
    return els.length ? els[els.length - 1] : null;
  },
  _appendToolEl: (body, el) => {
    const anchor = ctx._toolClusterAnchor(body);
    if (anchor) anchor.insertAdjacentElement('afterend', el);
    else body.appendChild(el);
  },
};
vm.createContext(ctx);
vm.runInContext(fnSrc, ctx);

// Simulate exact live sequence from user's screenshot:
// Round 11: Thinking #1 -> write_file hits truncation (tool_gen_reset + truncation status)
ctx.handleTestEvent({ type: 'thinking_token', content: 'round11 think' });
ctx.handleTestEvent({ type: 'tool_gen_reset', reason: 'length', tool: 'write_file' });
ctx.handleTestEvent({ type: 'status', content: '⚠ Tool call 1/3 truncated (write_file): JSON args malformed/truncated' });
// Round 12: Thinking #2 -> run_bash -> tool_result
ctx.handleTestEvent({ type: 'thinking_token', content: 'round12 think' });
ctx.handleTestEvent({ type: 'tool_call', name: 'run_bash', label: 'ls -la' });
ctx.handleTestEvent({ type: 'tool_result', ok: true, text: '5 lines' });
// Round 13: Thinking #3 + inline Tool-loop status notice
ctx.handleTestEvent({ type: 'thinking_token', content: 'round13 think' });
ctx.handleTestEvent({ type: 'status', content: '[Tool loop: 3x identical — re-injecting all tools]' });

const seq = agentBody.children.map(el => ({
  id: el.id || null,
  cls: el.className,
  tool: el.dataset.toolName || null,
}));
console.log(JSON.stringify({ seq, statusGone: !byId['status-el'] }));
"""
_np = subprocess.run(["node", "-e", _node_test], cwd=_ROOT, capture_output=True, text=True)
if _np.returncode != 0:
    check("node VM script executed cleanly", False, (_np.stderr or _np.stdout).strip())
    sys.exit(h.finish("notify_chats_speed_and_think_order"))
_nj = json.loads(_np.stdout.strip())
_seq = _nj["seq"]
_ok_order = (
    len(_seq) == 7
    and _seq[0]["id"] == "thk-t1-1"
    and "ctx-cut" in _seq[1]["cls"] and "warn" in _seq[1]["cls"]
    and _seq[2]["id"] == "thk-t1-2"
    and _seq[3]["tool"] == "run_bash" and "tool-call-row" in _seq[3]["cls"]
    and "tool-result-block" in _seq[4]["cls"]
    and _seq[5]["id"] == "thk-t1-3"
    and "ctx-cut" in _seq[6]["cls"] and "warn" in _seq[6]["cls"]
    and _nj["statusGone"]
)
check(
    "node VM confirms chronological [Think1, TruncCut, Think2, ToolCall, ToolResult, Think3, LoopCut] and clears status-el",
    _ok_order,
    repr(_nj),
)

# ── 5. Tool loop status migration, token sanitization & _isConnDrop journal reattach ──
_te_src = (_ROOT / "core" / "tool_executor.py").read_text(encoding="utf-8")
_te_loop_status = re.findall(r'hooks\.emit\(\{"type":\s*"([^"]+)",\s*"content":\s*f"[^"]*\[Tool loop:', _te_src)
check(
    "tool_executor emits [Tool loop: ...] strictly as status (never token)",
    bool(_te_loop_status) and all(t == "status" for t in _te_loop_status),
    repr(_te_loop_status),
)

_node_san = r"""
const fs = require('fs');
const vm = require('vm');
const appSrc = fs.readFileSync('static/app.js', 'utf8').replace(/\r\n/g, '\n');
const s = appSrc.indexOf('function _sanitizeRuntimeToken(t)');
const e = appSrc.indexOf('\n}\n', s);
const fnSrc = appSrc.slice(s, e + 3);
const ctx = {};
vm.createContext(ctx);
vm.runInContext(fnSrc, ctx);
const res = ctx._sanitizeRuntimeToken(
  'Before\n[Tool loop: 3x identical — re-injecting all tools]\n[Tool-Loop: ABAB pattern]\nAfter'
);
console.log(JSON.stringify({ res }));
"""
_ns = subprocess.run(["node", "-e", _node_san], cwd=_ROOT, capture_output=True, text=True)
_ns_ok = _ns.returncode == 0
if _ns_ok:
    _ns_out = json.loads(_ns.stdout.strip())["res"]
    _ns_ok = "Before" in _ns_out and "After" in _ns_out and "Tool loop" not in _ns_out and "Tool-Loop" not in _ns_out
check("_sanitizeRuntimeToken strips both [Tool loop: ...] and [Tool-Loop: ...] variants", _ns_ok, _ns.stdout.strip() or _ns.stderr.strip())

_tg_s = _js.index("function _toolGenStream(d)")
_tg_e = _js.index("\nfunction ", _tg_s)
_tg_body = _js[_tg_s:_tg_e]
check("_toolGenStream guards rmEl with typeof check", "if (typeof rmEl === 'function') rmEl('status-el');" in _tg_body)
check(
    "sendMsg _isConnDrop reattaches via _journalStartTail and run_id marks _attachedRunId",
    "if (_isConnDrop && !S._sawDoneEvent && S.currentRunId && !S._journalTailing)" in _js
    and "S._attachedRunId = d.run_id;" in _js
    and "if (typeof j.n === 'number') S._sseFrameSeq = Math.max(S._sseFrameSeq || 0, j.n);" in _js,
)

# ── 6. Per-element think-body mid-stream scroll follow ──────────────────────────
_node_scroll = r"""
const fs = require('fs');
const vm = require('vm');
const appSrc = fs.readFileSync('static/app.js', 'utf8').replace(/\r\n/g, '\n');
const s = appSrc.indexOf('function _elEnsureFollowListener(el)');
const e = appSrc.indexOf('\nfunction rmEl(', s);
if (s < 0 || e < 0) throw new Error('missing follow helpers');
const fnSrc = appSrc.slice(s, e);
let rafQueue = [];
const ctx = {
  _chatFollow: true,
  requestAnimationFrame: (cb) => { rafQueue.push(cb); return rafQueue.length; },
};
vm.createContext(ctx);
vm.runInContext(fnSrc, ctx);
function flushRaf() {
  const q = rafQueue;
  rafQueue = [];
  for (const cb of q) cb();
}
const listeners = {};
const el = {
  scrollHeight: 1000,
  clientHeight: 240,
  scrollTop: 760,
  isConnected: true,
  addEventListener: (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); },
  emit: (ev, payload) => { for (const fn of (listeners[ev] || [])) fn(payload || {}); },
};

// 1. Initial follow scrolls to bottom
ctx._scrollElToBottomSoon(el);
flushRaf();
const step1 = el.scrollTop;

// 2. User scrolls up mid-stream (chat itself stays at bottom -> _chatFollow=true)
el.scrollTop = 200;
el.emit('scroll');
el.scrollHeight = 1200;
ctx._scrollElToBottomSoon(el);
flushRaf();
const step2 = el.scrollTop;

// 3. User scrolls back down near bottom -> auto-follow resumes
el.scrollTop = 960;
el.emit('scroll');
el.scrollHeight = 1400;
ctx._scrollElToBottomSoon(el);
flushRaf();
const step3 = el.scrollTop;

// 4. Upward wheel event cancels even an already-queued rAF
el.scrollHeight = 1600;
ctx._scrollElToBottomSoon(el);
el.scrollTop = 900;
el.emit('wheel', { deltaY: -120 });
flushRaf();
const step4 = el.scrollTop;

// 5. Programmatic scroll guard: SSE appends lines between rAF and deferred scroll -> does NOT self-disarm
const el2L = {};
const el2 = {
  scrollHeight: 1000, clientHeight: 240, scrollTop: 760, isConnected: true,
  addEventListener: (ev, fn) => { (el2L[ev] = el2L[ev] || []).push(fn); },
  emit: (ev, p) => { for (const fn of (el2L[ev] || [])) fn(p || {}); },
};
ctx._scrollElToBottomSoon(el2);
flushRaf();
el2.scrollHeight = 1060; // +60px (>24px) arrives before browser fires deferred scroll
el2.emit('scroll');
const step5FollowAfterProg = el2._followBottom;

// 6. Small-delta (<24px) upward wheel followed immediately by scroll does NOT snap back
el2.emit('wheel', { deltaY: -10 });
el2.scrollTop = el2.scrollHeight - el2.clientHeight - 8;
el2.emit('scroll');
const step6FollowAfterSmallUp = el2._followBottom;

console.log(JSON.stringify({ step1, step2, step3, step4, step5FollowAfterProg, step6FollowAfterSmallUp }));
"""
_nsc = subprocess.run(["node", "-e", _node_scroll], cwd=_ROOT, capture_output=True, text=True)
_nsc_ok = _nsc.returncode == 0
if _nsc_ok:
    _nsc_j = json.loads(_nsc.stdout.strip())
    _nsc_ok = (
        _nsc_j["step1"] == 1000
        and _nsc_j["step2"] == 200
        and _nsc_j["step3"] == 1400
        and _nsc_j["step4"] == 900
        and _nsc_j["step5FollowAfterProg"] is True
        and _nsc_j["step6FollowAfterSmallUp"] is False
    )
check(
    "node VM confirms _scrollElToBottomSoon respects per-element scroll/wheel upward navigation",
    _nsc_ok,
    _nsc.stdout.strip() or _nsc.stderr.strip(),
)

_planner_think_slice = _js[_js.index("else if (d.type === 'planner_thinking_token')"):_js.index("else if (d.type === 'planner_plan_token')")]
_scroll_btm_slice = _js[_js.index("function scrollBtmIfNearBottom("):_js.index("function _elEnsureFollowListener(")]
check(
    "planner thinking uses _scrollElToBottomSoon and scrollBtmIfNearBottom re-checks _chatFollow in rAF",
    "_scrollElToBottomSoon(_thinkEl)" in _planner_think_slice
    and _scroll_btm_slice.count("if (!_chatFollow) return;") >= 2,
)

# ── 7. SSE keepalive heartbeat, detached queue short-circuit & reattach banner UX ──
import asyncio as _aio_sse  # noqa: E402
from unittest import mock as _mock_sse  # noqa: E402
import server as _srv_mod  # noqa: E402

_srv_text = (_ROOT / "server.py").read_text(encoding="utf-8")
check(
    "server.py emits SSE keepalive comments on idle wait and stops queueing after detach",
    "_SSE_KEEPALIVE_INTERVAL_S" in _srv_text
    and 'yield ": keepalive\\n\\n"' in _srv_text
    and '_state["detached"] = True' in _srv_text
    and 'if _state.get("detached"):' in _srv_text,
)


class _FakeReq:
    def __init__(self, body: dict):
        self._body = body
        self.headers = {}

    async def json(self):
        return dict(self._body)


async def _test_sse_keepalive_and_detach():
    _captured_overrides = {}

    # Case A: slow prefill (> keepalive interval) emits ': keepalive\n\n' before real frame
    async def _slow_gen(*_a, **kw):
        _captured_overrides.update(kw.get("model_overrides") or {})
        yield 'data: {"type": "run_id", "run_id": "test-ka-1"}\n\n'
        await _aio_sse.sleep(0.065)
        yield 'data: {"type": "token", "content": "hello"}\n\n'
        yield 'data: {"type": "done"}\n\n'

    with _mock_sse.patch.object(_srv_mod, "run_stream", side_effect=_slow_gen), \
         _mock_sse.patch.object(_srv_mod, "_SSE_KEEPALIVE_INTERVAL_S", 0.02):
        resp = await _srv_mod.stream(_FakeReq({
            "q": "hi",
            "mode": "simple",
            "duo_planner_separate_effort": True,
            "planner_reasoning_effort": "HIGH",
        }))
        chunks = []
        async for ch in resp.body_iterator:
            chunks.append(ch if isinstance(ch, str) else ch.decode("utf-8"))

    # Case B: consumer disconnects early -> producer finishes detached and records all frames in _RUN_JOURNAL
    _detach_done = _aio_sse.Event()

    async def _detach_gen(*_a, **_kw):
        yield 'data: {"type": "run_id", "run_id": "test-det-2"}\n\n'
        await _aio_sse.sleep(0.03)
        yield 'data: {"type": "token", "content": "after-detach-1"}\n\n'
        yield 'data: {"type": "token", "content": "after-detach-2"}\n\n'
        yield 'data: {"type": "done"}\n\n'
        _detach_done.set()

    with _mock_sse.patch.object(_srv_mod, "run_stream", side_effect=_detach_gen), \
         _mock_sse.patch.object(_srv_mod, "_SSE_KEEPALIVE_INTERVAL_S", 0.05):
        resp2 = await _srv_mod.stream(_FakeReq({"q": "hi", "mode": "simple"}))
        it2 = resp2.body_iterator
        first = await it2.__anext__()
        await it2.aclose()
        await _aio_sse.wait_for(_detach_done.wait(), timeout=2.0)
        await _aio_sse.sleep(0.02)
        jentry = dict(_srv_mod._RUN_JOURNAL.get("test-det-2") or {})
        _srv_mod._RUN_JOURNAL.pop("test-ka-1", None)
        _srv_mod._RUN_JOURNAL.pop("test-det-2", None)

    return chunks, first, jentry, _captured_overrides


_ka_chunks, _det_first, _det_j, _cap_ovr = _aio_sse.run(_test_sse_keepalive_and_detach())
check(
    "Live /stream yields ': keepalive\\n\\n' during slow prefill, forwards planner effort overrides & journals detached frames cleanly",
    ": keepalive\n\n" in _ka_chunks
    and any('"hello"' in c for c in _ka_chunks)
    and any('"done"' in c for c in _ka_chunks)
    and _cap_ovr.get("duo_planner_separate_effort") is True
    and _cap_ovr.get("planner_reasoning_effort") == "high"
    and '"test-det-2"' in (str(_det_first) if isinstance(_det_first, str) else _det_first.decode("utf-8"))
    and _det_j.get("done") is True
    and len(_det_j.get("frames") or []) == 4,
    repr((_ka_chunks, _cap_ovr, len(_det_j.get("frames") or []), _det_j.get("done"))),
)

_node_sse = r"""
const fs = require('fs');
const vm = require('vm');
const appSrc = fs.readFileSync('static/app.js', 'utf8').replace(/\r\n/g, '\n');
const s = appSrc.indexOf('function _sseFeedLines(text)');
const e = appSrc.indexOf('\nfunction _journalInitAttach()', s);
if (s < 0 || e < 0) throw new Error('missing sse/journal helpers');
const fnSrc = appSrc.slice(s, e);
const handled = [];
const chatCh = [];
const chatEl = {
  children: chatCh,
  appendChild(c){ c.parentNode = this; chatCh.push(c); },
  removeChild(c){ const i = chatCh.indexOf(c); if (i >= 0) chatCh.splice(i, 1); c.parentNode = null; },
};
let fetchCalls = 0;
const ctx = {
  S: { _sseBuf: '', _sseFrameSeq: 0, _journalTailing: null, _reattachBannerEl: null },
  window: { _journalTailIv: null },
  document: {
    hidden: false,
    getElementById(id){ return id === 'chat' ? chatEl : null; },
    createElement(tag){ return { tag, className: '', style: { cssText: '' }, textContent: '', parentNode: null }; },
  },
  handleEvent: d => handled.push(d),
  scrollBtmIfNearBottom: () => {},
  _finalizeStreamUi: () => {},
  setInterval: () => 99,
  clearInterval: () => {},
  fetch: () => { fetchCalls++; return new Promise(() => {}); },
};
vm.createContext(ctx);
vm.runInContext(fnSrc, ctx);

// 1. ': keepalive' comment lines must NOT increment _sseFrameSeq or dispatch events
ctx._sseFeedLines(': keepalive\n\n: keepalive\n\ndata: {"type":"token","content":"ok"}\n\n');
const seqAfterKa = ctx.S._sseFrameSeq;
const handledCount = handled.length;

// 2. Reattach banner updates to green checkmark when frames arrive
const b1 = ctx._journalNote('Connection lost - reattaching via the run journal ...', { reattachBanner: true });
const beforeTxt = b1.textContent;
ctx._journalClearReattachBanner(true);
const afterTxt = b1.textContent;
const clearedRef = ctx.S._reattachBannerEl === null;

// 3. Unresolved banner is removed on _journalStopTail
const b2 = ctx._journalNote('Connection lost - reattaching via the run journal ...', { reattachBanner: true });
const inChatBefore = chatCh.includes(b2);
ctx._journalStopTail();
const inChatAfter = chatCh.includes(b2);

// 4. _journalStartTail triggers immediate poll without waiting 2000ms
ctx._journalStartTail('run-imm-1');
const immFetch = fetchCalls;

console.log(JSON.stringify({ seqAfterKa, handledCount, beforeTxt, afterTxt, clearedRef, inChatBefore, inChatAfter, immFetch }));
"""
_nsse = subprocess.run(["node", "-e", _node_sse], cwd=_ROOT, capture_output=True, text=True)
_nsse_ok = _nsse.returncode == 0
if _nsse_ok:
    _nsse_j = json.loads(_nsse.stdout.strip())
    _nsse_ok = (
        _nsse_j["seqAfterKa"] == 1
        and _nsse_j["handledCount"] == 1
        and "Connection lost" in _nsse_j["beforeTxt"]
        and "Reattached via run journal" in _nsse_j["afterTxt"]
        and _nsse_j["clearedRef"] is True
        and _nsse_j["inChatBefore"] is True
        and _nsse_j["inChatAfter"] is False
        and _nsse_j["immFetch"] == 1
    )
check("Node VM confirms ': keepalive' preserves _sseFrameSeq, reattach banner transitions & immediate journal poll", _nsse_ok, _nsse.stdout.strip() or _nsse.stderr.strip())

sys.exit(h.finish("notify_chats_speed_and_think_order"))

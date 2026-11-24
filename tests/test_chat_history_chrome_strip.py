"""Regression suite for chat history DOM-chrome stripping and Direct timer fixes.

Covers:
  1. Legacy DOM-scraped assistant `content` (tool chips, [UNTRUSTED CONTENT]
     dumps, inline think blocks, pre-call reasoning) is cleaned via saved HTML
     (inline or HIVEBLOB1 blob) in `context.chat.load_chat_transcript()`.
  2. Code blocks (<pre class="coder-code-block lang-...">) and tables survive
     history extraction cleanly while void tags (<br>/<hr>/<img>) inside chrome
     never corrupt skip depth.
  3. Tool-only bubbles (empty answer after chrome strip) stay out of model
     history while keeping their `html` for UI reload.
  4. Invalid / traversal blob refs are safely rejected.
  5. `static/app.js` `_autosaveChatMessage` uses `_historyTextOnly(abody)`
     (tested end-to-end under Node when available) and `doneAgent` prefers
     `S.perfDecodeEma`.
  6. `core/direct_runner.py` starts the `agent_done` timer before
     `_run_direct_tools` so direct-tools turns do not report `0.0s`.

Run: python tests/test_chat_history_chrome_strip.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from tests.ui_sources import read_all_ui_js  # noqa: E402

passed = 0
failed = 0


def ok(name: str) -> None:
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name: str, detail: str = "") -> None:
    global failed
    failed += 1
    print(f"  FAIL  {name}  {detail}")


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        ok(name)
    else:
        fail(name, detail)


def _sample_poisoned_html() -> str:
    return (
        '<div class="tool-call-row" data-tool-name="web_search">'
        '<span class="tool-call-chip tc-read"><span class="tc-icon">🔍</span>'
        '<span class="tc-name">web_search</span><span class="tc-sep"> · </span>'
        '<span class="tc-label">example query</span></span>'
        '<div class="tc-extra"><table class="tc-extra-table">'
        '<tr><td>query</td><td>example query<br>extra</td></tr></table><hr></div>'
        '</div>'
        '<details class="tool-result-block"><summary>🔍 web_search 2 results</summary>'
        '<pre class="tool-result-pre">[UNTRUSTED CONTENT - source: web_search]\n'
        'raw search dump\n[END UNTRUSTED CONTENT]</pre></details>'
        '<div class="think-block"><div class="think-hdr">'
        '<span class="th-label">🧠 Thinking</span><span class="th-chevron">▲</span>'
        '</div><div class="think-body">secret reasoning<img src="x"></div></div>'
        '<div class="pre-call-reasoning"><div class="pcr-hdr">'
        '<span class="pcr-icon">💭</span><span class="pcr-toggle">▸</span>'
        '<span class="pcr-label">Reasoning</span></div>'
        '<div class="pcr-body">Let me search first.</div></div>'
        '<div class="coder-md-text">'
        '<p>Here are the direct links:</p>'
        '<ol class="coder-ol"><li value="1"><a href="https://example.com/a">https://example.com/a</a></li>'
        '<li value="2">Item two with <code class="coder-inline-code">inline_fn()</code></li></ol>'
        '<pre class="coder-code-block lang-python"><code>def add(a, b):\n    return a + b</code></pre>'
        '<table class="coder-tbl"><tr><th>Key</th><th>Val</th></tr>'
        '<tr><td>A</td><td>1</td></tr></table>'
        '</div>'
    )


def test_backend_transcript_sanitization() -> None:
    import context.chat as CC

    ws = Path(tempfile.mkdtemp(prefix="hvm_hist_chrome_"))
    prev_dir = CC._SESSIONS_DIR
    CC._SESSIONS_DIR = ws
    try:
        # 1. Clean normal messages stay untouched
        cid_clean = "1111aaaa"
        (ws / f"2026-11-21T07-00-00_clean_{cid_clean}.json").write_text(
            json.dumps({
                "id": cid_clean,
                "messages": [
                    {"role": "user", "content": "Hello"},
                    {"role": "assistant", "content": "Plain answer with `code`."},
                ],
            }),
            encoding="utf-8",
        )
        tr_clean = CC.load_chat_transcript(cid_clean)
        check(
            "clean messages pass through untouched",
            tr_clean == [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Plain answer with `code`."},
            ],
            repr(tr_clean),
        )

        # 2. Legacy poisoned content with inline HTML recovers clean prose + code + table
        cid_inline = "2222bbbb"
        poisoned_text = (
            "🔍web_search · example queryqueryexample query🔍 web_search"
            "[UNTRUSTED CONTENT - source: web_search]\nraw search dump\n"
            "[END UNTRUSTED CONTENT]🧠 Thinking▲secret reasoning"
            "Here are the direct links:https://example.com/a"
        )
        (ws / f"2026-11-21T07-01-00_inline_{cid_inline}.json").write_text(
            json.dumps({
                "id": cid_inline,
                "messages": [
                    {"role": "user", "content": "Find links"},
                    {
                        "role": "assistant",
                        "content": poisoned_text,
                        "html": _sample_poisoned_html(),
                    },
                ],
            }),
            encoding="utf-8",
        )
        tr_inline = CC.load_chat_transcript(cid_inline)
        ans = tr_inline[1]["content"] if len(tr_inline) == 2 else ""
        check(
            "inline HTML strips tool chips, untrusted content, think blocks, pre-call reasoning",
            len(tr_inline) == 2
            and "web_search" not in ans
            and "UNTRUSTED CONTENT" not in ans
            and "secret reasoning" not in ans
            and "Let me search first." not in ans
            and "Here are the direct links:" in ans
            and "https://example.com/a" in ans
            and "inline_fn()" in ans
            and "```python\ndef add(a, b):\n    return a + b\n```" in ans
            and "Key | Val" in ans
            and "A | 1" in ans,
            repr(ans),
        )

        # 3. Legacy poisoned content with HIVEBLOB1 blob ref (exact e81f64da repro)
        cid_blob = "3333cccc"
        html_payload = _sample_poisoned_html()
        blob_id = hashlib.sha256(html_payload.encode("utf-8")).hexdigest()[:16]
        bdir = ws / f"{cid_blob}.blobs"
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / blob_id).write_bytes(b"HIVEBLOB1\n" + html_payload.encode("utf-8"))
        (ws / f"2026-11-21T07-02-00_blob_{cid_blob}.json").write_text(
            json.dumps({
                "id": cid_blob,
                "messages": [
                    {"role": "user", "content": "Research links"},
                    {
                        "role": "assistant",
                        "content": poisoned_text,
                        "html": {"_ref": blob_id},
                    },
                ],
            }),
            encoding="utf-8",
        )
        tr_blob = CC.load_chat_transcript(cid_blob)
        ans_b = tr_blob[1]["content"] if len(tr_blob) == 2 else ""
        check(
            "blobified HIVEBLOB1 HTML recovers clean answer without tool chrome",
            len(tr_blob) == 2
            and "Here are the direct links:" in ans_b
            and "UNTRUSTED CONTENT" not in ans_b
            and "🔍web_search" not in ans_b
            and "```python" in ans_b,
            repr(ans_b),
        )

        # 4. Path-traversal blob ref rejected safely; fallback strips [UNTRUSTED CONTENT]
        cid_bad = "4444dddd"
        (ws / f"2026-11-21T07-03-00_badref_{cid_bad}.json").write_text(
            json.dumps({
                "id": cid_bad,
                "messages": [
                    {"role": "user", "content": "Q"},
                    {
                        "role": "assistant",
                        "content": (
                            "[UNTRUSTED CONTENT - source: web_search]\nsecret\n"
                            "[END UNTRUSTED CONTENT]\nReal answer."
                        ),
                        "html": {"_ref": "../../etc/passwd"},
                    },
                ],
            }),
            encoding="utf-8",
        )
        tr_bad = CC.load_chat_transcript(cid_bad)
        ans_bad = tr_bad[1]["content"] if len(tr_bad) == 2 else ""
        check(
            "invalid blob ref rejected and fallback strips [UNTRUSTED CONTENT]",
            ans_bad == "Real answer.",
            repr(ans_bad),
        )

        # 5. Tool-only bubble (empty content or chrome-only HTML) excluded from transcript
        cid_only = "5555eeee"
        chrome_only_html = (
            '<div class="tool-call-row"><span class="tool-call-chip">🔍web_search · q</span></div>'
            '<details class="tool-result-block"><pre>[UNTRUSTED CONTENT - source: web_search]\n'
            'x\n[END UNTRUSTED CONTENT]</pre></details>'
        )
        (ws / f"2026-11-21T07-04-00_only_{cid_only}.json").write_text(
            json.dumps({
                "id": cid_only,
                "messages": [
                    {"role": "user", "content": "Q1"},
                    {"role": "assistant", "content": "", "html": chrome_only_html},
                    {"role": "user", "content": "Q2"},
                    {
                        "role": "assistant",
                        "content": "🔍web_search · q[UNTRUSTED CONTENT - source: web_search]\nx\n[END UNTRUSTED CONTENT]",
                        "html": chrome_only_html,
                    },
                ],
            }),
            encoding="utf-8",
        )
        tr_only = CC.load_chat_transcript(cid_only)
        check(
            "tool-only bubbles stay out of transcript seed",
            tr_only == [{"role": "user", "content": "Q1"}, {"role": "user", "content": "Q2"}],
            repr(tr_only),
        )
    finally:
        CC._SESSIONS_DIR = prev_dir
        shutil.rmtree(ws, ignore_errors=True)


def test_frontend_history_extractor_and_header_rate() -> None:
    app = read_all_ui_js()

    check("app.js defines _HISTORY_CHROME_SEL", "var _HISTORY_CHROME_SEL" in app)
    check("app.js defines _historyTextOnly", "function _historyTextOnly(" in app)

    autosave_fn = app.split("function _autosaveChatMessage()", 1)[1].split("function chatAutosaveToggle", 1)[0]
    check(
        "_autosaveChatMessage uses _historyTextOnly(abody) for content",
        "_historyTextOnly(abody)" in autosave_fn
        and "content: _cleanContent" in autosave_fn
        and "html: abody.innerHTML" in autosave_fn
        and "content: abody.textContent" not in autosave_fn,
    )

    done_fn = app.split("function doneAgent(elapsed)", 1)[1].split("function renderCriticConstraints", 1)[0]
    check(
        "doneAgent prefers server S.perfDecodeEma over S.perfTokRate",
        "S.perfDecodeEma > 0" in done_fn and "S.perfTokRate" in done_fn,
    )

    # Execute the actual JS `_historyTextOnly` under Node.js when available
    if shutil.which("node"):
        start = app.index("var _HISTORY_CHROME_SEL")
        end = app.index("// Clamp TTS speed", start)
        snippet = app[start:end]
        js_harness = snippet + r"""
class MockNode {
  constructor(tag, cls, children, text) {
    if (tag === '#text') {
      this.nodeType = 3;
      this.nodeValue = text || '';
      this.childNodes = [];
    } else {
      this.nodeType = 1;
      this.tagName = tag.toUpperCase();
      this.className = cls || '';
      this.childNodes = children || [];
      this.parentNode = null;
      this.childNodes.forEach(c => { c.parentNode = this; });
    }
  }
  get textContent() {
    if (this.nodeType === 3) return this.nodeValue;
    return this.childNodes.map(c => c.textContent).join('');
  }
  cloneNode() {
    if (this.nodeType === 3) return new MockNode('#text', '', [], this.nodeValue);
    return new MockNode(this.tagName, this.className, this.childNodes.map(c => c.cloneNode(true)));
  }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.childNodes = this.parentNode.childNodes.filter(c => c !== this);
    this.parentNode = null;
  }
  _matchesOne(sel) {
    if (this.nodeType !== 1) return false;
    sel = sel.trim();
    if (sel.startsWith('.')) {
      const want = sel.slice(1);
      return this.className.split(/\s+/).includes(want);
    }
    return this.tagName === sel.toUpperCase();
  }
  querySelectorAll(selectorList) {
    const sels = selectorList.split(',').map(s => s.trim()).filter(Boolean);
    const hits = [];
    const visit = (n) => {
      if (n.nodeType !== 1) return;
      for (const c of n.childNodes) {
        if (c.nodeType === 1 && sels.some(s => c._matchesOne(s))) hits.push(c);
        visit(c);
      }
    };
    visit(this);
    return hits;
  }
}
const T = (s) => new MockNode('#text', '', [], s);
const E = (tag, cls, ch) => new MockNode(tag, cls, ch || []);
const body = E('div', 'abody', [
  E('div', 'tool-call-row', [
    E('span', 'tool-call-chip tc-read', [T('🔍web_search · test query')]),
    E('div', 'tc-extra', [T('querytest query')])
  ]),
  E('details', 'tool-result-block', [
    E('pre', 'tool-result-pre', [T('[UNTRUSTED CONTENT - source: web_search]\nnoise\n[END UNTRUSTED CONTENT]')])
  ]),
  E('div', 'think-block', [E('div', 'think-body', [T('hidden thought')])]),
  E('div', 'pre-call-reasoning', [E('div', 'pcr-body', [T('Let me check')])]),
  E('div', 'coder-md-text', [
    E('p', '', [T('Line one')]),
    E('p', '', [T('Line two with '), E('code', 'coder-inline-code', [T('foo()')])]),
    E('pre', 'coder-code-block lang-js', [E('code', '', [T('const x = 1;\n')])]),
  ])
]);
const res = _historyTextOnly(body);
console.log(JSON.stringify({ res, toolOnly: _historyTextOnly(E('div', 'abody', [E('div', 'tool-call-row', [T('🔍web_search · q')])])) }));
"""
        proc = subprocess.run(
            ["node", "-e", js_harness],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        check("Node executed _historyTextOnly cleanly", proc.returncode == 0, proc.stderr)
        if proc.returncode == 0:
            data = json.loads(proc.stdout.strip())
            res = data["res"]
            check(
                "JS _historyTextOnly strips all chrome and preserves prose + fenced code",
                "web_search" not in res
                and "UNTRUSTED" not in res
                and "hidden thought" not in res
                and "Let me check" not in res
                and "Line one\nLine two with foo()" in res
                and "```js\nconst x = 1;\n```" in res,
                repr(res),
            )
            check(
                "JS _historyTextOnly returns empty string for tool-only bubble",
                data["toolOnly"] == "",
                repr(data["toolOnly"]),
            )


def test_direct_runner_elapsed_includes_tool_loop() -> None:
    import core.direct_runner as DR

    src = (ROOT / "core" / "direct_runner.py").read_text(encoding="utf-8")
    idx_agent = src.index('yield await ctx.emit({"type": "agent", "content": "Answer"')
    idx_t = src.rfind("t = time.time()", 0, idx_agent)
    idx_dt = src.index("_dt_stream = _run_direct_tools(")
    check(
        "direct_runner initializes timer `t` before _run_direct_tools",
        0 <= idx_t < idx_agent < idx_dt,
        f"idx_t={idx_t} idx_agent={idx_agent} idx_dt={idx_dt}",
    )

    async def _run_fake_direct_tools():
        orig_run_dt = DR._run_direct_tools
        orig_unpin = DR.unpin_previous_direct_model
        orig_refresh = DR._refresh_judge_keepalive

        async def _fake_dt(ctx, model, msgs, tool_mode, result, vision=False):
            await asyncio.sleep(0.15)
            result.content = "clean tool answer"
            yield {"type": "token", "content": "clean tool answer"}

        async def _noop_unpin(*a, **kw):
            return False

        async def _noop_refresh():
            return None

        DR._run_direct_tools = _fake_dt
        DR.unpin_previous_direct_model = _noop_unpin
        DR._refresh_judge_keepalive = _noop_refresh
        try:
            agent = types.SimpleNamespace(model="m1", max_tokens=256, temperature=0.4, thinking_budget=0)
            mem = types.SimpleNamespace(
                add_to_session=lambda r, c: None,
                get_session_messages=lambda **kw: [],
            )
            ctx = types.SimpleNamespace(
                complexity="simple",
                models_cache=["m1"],
                vision_cfg={},
                prepro_success=False,
                effective_task_type="chat",
                images=[],
                effective_images=[],
                image_description="",
                image_paths=[],
                user_input="hi",
                workspace="",
                chat_id="",
                run_id="r1",
                t_total=0.0,
                active_preset="default",
                use_learned=False,
                pipeline_soul=None,
                pipeline_mem_ctx="",
                pipeline_sess_msgs=[],
                websearch_available=False,
                settings={"direct_tools_enabled": True, "direct_tools_tier": "readonly", "direct_model": "m1"},
                pipeline=types.SimpleNamespace(agents={"direct": agent}),
                memory=mem,
                model_profile=lambda m: {"tool_call": True, "vision": False},
                get_num_ctx=lambda m, role: 8192,
                maybe_preload=lambda m: asyncio.sleep(0),
                get_effective_prompt_with_override=lambda *a, **kw: "sys",
                make_messages=lambda *a, **kw: [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
                emit=lambda ev: asyncio.sleep(0, result=ev),
                done_event=lambda elapsed, reason, **kw: {"type": "done", "elapsed": elapsed, "stop_reason": reason},
                collect_done_metrics=lambda: {},
                increment_run_counter=lambda: 1,
                maybe_trigger_soul_evolution=lambda rc: asyncio.sleep(0),
                unregister_abort=lambda rid: None,
                unregister_step_skip=lambda rid: None,
            )
            events = []
            async for ev in DR.run_direct(ctx):
                events.append(ev)
            await asyncio.sleep(0)
            return events
        finally:
            DR._run_direct_tools = orig_run_dt
            DR.unpin_previous_direct_model = orig_unpin
            DR._refresh_judge_keepalive = orig_refresh

    events = asyncio.run(_run_fake_direct_tools())
    done_ev = next((e for e in events if e.get("type") == "agent_done"), None)
    check(
        "direct_tools run emits non-zero agent_done.elapsed covering tool-loop duration",
        done_ev is not None and float(done_ev.get("elapsed", 0)) >= 0.1,
        repr(done_ev),
    )


def main() -> int:
    print("== test_chat_history_chrome_strip ==")
    test_backend_transcript_sanitization()
    test_frontend_history_extractor_and_header_rate()
    test_direct_runner_elapsed_includes_tool_loop()
    print(f"Summary: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

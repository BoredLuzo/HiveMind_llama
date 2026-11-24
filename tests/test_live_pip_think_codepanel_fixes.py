# -*- coding: utf-8 -*-
"""Regressions for live Windows Git Bash pip/python, collapsible think blocks,
and resilient Code Panel streaming across truncation / pending rekey.

Run: python tests/test_live_pip_think_codepanel_fixes.py
Exit 0 = all pass, Exit 1 = failures.
"""
from __future__ import annotations

import asyncio
import json
import re
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


# ── 1. Windows Git Bash .venv backslash normalization & pip checks ─────
import tools.handlers.exec_tools as ET  # noqa: E402

check(
    "normalizes unquoted .venv\\Scripts\\python.exe for Git Bash",
    ET._normalize_win_bash_venv_paths(r".venv\Scripts\python.exe -m pip --version")
    == ".venv/Scripts/python.exe -m pip --version",
)
check(
    "normalizes chained venv Scripts paths in Git Bash",
    ET._normalize_win_bash_venv_paths(r"python -m venv .venv && .venv\Scripts\pip.exe install x")
    == "python -m venv .venv && .venv/Scripts/pip.exe install x",
)
check(
    "leaves quoted regex containing backslashes untouched",
    ET._normalize_win_bash_venv_paths(r'''python -c "import re; re.search(r'\.venv\\Scripts', s)"''')
    == r'''python -c "import re; re.search(r'\.venv\\Scripts', s)"''',
)

with tempfile.TemporaryDirectory(prefix="hm_venv_guard_") as _td:
    _ws = Path(_td)
    check(
        "allows chained venv create + source activate + pip install",
        ET._is_unscoped_pip_install(
            "python -m venv .venv && source .venv/Scripts/activate && pip install pygame-ce",
            _ws,
        ) is False,
    )
    check(
        "allows uv venv create + dot activate + uv pip install",
        ET._is_unscoped_pip_install(
            "uv venv .venv && . .venv/Scripts/activate && uv pip install pygame-ce",
            _ws,
        ) is False,
    )
    check(
        "still blocks venv create + pip install without activation",
        ET._is_unscoped_pip_install("python -m venv .venv && pip install pygame-ce", _ws) is True,
    )
    check(
        "still blocks bare pip install without .venv",
        ET._is_unscoped_pip_install("pip install pygame-ce", _ws) is True,
    )

check(
    "_pip_packages_from_cmd parses quoted space-containing python path",
    ET._pip_packages_from_cmd('"C:/Users/Test User/.venv/Scripts/python.exe" -m pip install --prefer-binary pygame')
    == "pygame",
)
check(
    "_pip_packages_from_cmd parses pip3 install",
    ET._pip_packages_from_cmd("pip3 install pygame-ce numpy") == "pygame-ce numpy",
)

_seen_bash_cmds: list[list[str]] = []


async def _fake_stream_proc(cmd, timeout_s, cwd, env, line_cap=300):
    _seen_bash_cmds.append(list(cmd))
    return "Python 3.14.0", "", 0, False


with tempfile.TemporaryDirectory(prefix="hm_bash_norm_") as _td2:
    with mock.patch.object(ET.sys, "platform", "win32"), \
         mock.patch.object(ET, "_run_bash_shell_mode", return_value="bash"), \
         mock.patch.object(ET, "_windows_bash_path", return_value=r"C:\Program Files\Git\bin\bash.exe"), \
         mock.patch.object(ET, "_stream_proc", side_effect=_fake_stream_proc):
        _out = asyncio.run(ET._inline_tool_run_bash({"cmd": r".venv\Scripts\python.exe --version"}, Path(_td2), None))
check(
    "_inline_tool_run_bash forwards normalized .venv/Scripts/python.exe to Git Bash",
    bool(_seen_bash_cmds) and ".venv/Scripts/python.exe --version" in _seen_bash_cmds[0][-1]
    and "[run_bash: exit 0]" in _out,
    repr((_seen_bash_cmds, _out)),
)
check(
    "shell_os_hint(bash, win32) teaches forward slashes for .venv/Scripts",
    ".venv/Scripts/python.exe" in ET.shell_os_hint("bash", "win32"),
)


# ── 2. CSS + JS + SSE thinking collapse & Code Panel resilience ─────────
_css = (_ROOT / "static" / "style.css").read_text(encoding="utf-8")
from tests.ui_sources import read_all_ui_js  # noqa: E402

_js = read_all_ui_js()
_html = (_ROOT / "index.html").read_text(encoding="utf-8")
_atl = (_ROOT / "core" / "agentic_tool_loop.py").read_text(encoding="utf-8")

check(
    "style.css overrides live think-body visibility with user-collapsed selector",
    ".think-block.live .think-body { display: block; }" in _css
    and ".think-block.live.user-collapsed .think-body { display: none; }" in _css
    and ".think-block.open .think-body { display: block; }" in _css,
)
check(
    "app.js defines _toggleThinkBlock setting dataset.userToggled and wires both builders",
    "function _toggleThinkBlock(tb)" in _js
    and "tb.dataset.userToggled = '1'" in _js
    and "_toggleThinkBlock(thinkBlock)" in _js
    and "_toggleThinkBlock(this.parentElement)" in _js,
)
check(
    "app.js respects userToggled during streaming think tokens and closes think block on tool_gen",
    "_tbEl.dataset.userToggled !== '1'" in _js
    and "thinkBlockEl.dataset.userToggled !== '1'" in _js
    and "if (Object.keys(_tgCards).length > 0) _toolGenDone();" in _js,
)
check(
    "agentic_tool_loop emits call_id on tool_gen and tool_gen_reset on length/drift",
    '"call_id":' in _atl
    and '"type": "tool_gen_reset"' in _atl,
)
check(
    "app.js handles tool_gen_reset, call_id rollover, active pending rekey, and rAF follow scroll",
    "'tool_gen_reset': function(d)" in _js
    and "_cid !== st.callId" in _js
    and "var _wasActive = (_cpActive === _k);" in _js
    and "job.plain && _cpFollowStream && document.body.classList.contains('code-panel-open')" in _js,
)
_m_css = re.search(r"/static/style\.css\?v=(\d{8})-(\d+)", _html)
_m_js = re.search(r"/static/app\.js\?v=(\d{8})-(\d+)", _html)
check(
    "index.html cache-busters bumped for style.css and app.js",
    _m_css is not None and (int(_m_css.group(1)), int(_m_css.group(2))) >= (20261122, 2)
    and _m_js is not None and (int(_m_js.group(1)), int(_m_js.group(2))) >= (20261122, 2),
)

# Run a real Node DOM test for _toggleThinkBlock + userToggled + _tgPendingRekey
_node_harness = r"""
const vm = require('vm');
const { readAllUiJs } = require('./tests/ui_sources.cjs');
const appSrc = readAllUiJs();

function extractFn(name) {
  const idx = appSrc.indexOf('function ' + name + '(');
  if (idx < 0) throw new Error('missing ' + name);
  let i = appSrc.indexOf('{', idx), depth = 0, inS = false, q = '';
  for (; i < appSrc.length; i++) {
    const ch = appSrc[i];
    if (inS) { if (ch === '\\') { i++; continue; } if (ch === q) inS = false; continue; }
    if (ch === '/' && appSrc[i + 1] === '/') { i = appSrc.indexOf('\n', i); if (i < 0) break; continue; }
    if (ch === "'" || ch === '"') { inS = true; q = ch; continue; }
    if (ch === '{') depth++;
    else if (ch === '}' && --depth === 0) return appSrc.slice(idx, i + 1);
  }
  throw new Error('unclosed ' + name);
}

function makeClassList(init) {
  const s = new Set(init || []);
  return {
    add: (...xs) => xs.forEach(x => s.add(x)),
    remove: (...xs) => xs.forEach(x => s.delete(x)),
    contains: x => s.has(x),
    toggle: (x, force) => {
      if (force === true) { s.add(x); return true; }
      if (force === false) { s.delete(x); return false; }
      if (s.has(x)) { s.delete(x); return false; }
      s.add(x); return true;
    },
  };
}

const cpBody = { scrollTop: 0, scrollHeight: 900 };
const ctx = {
  _TG_PENDING_KEY: '\u0000pending-write',
  _cpActive: '\u0000pending-write#0',
  _cpFollowStream: true,
  _cpFiles: {
    '\u0000pending-write#0': { content: '<html>', op: 'write', tab: { remove: () => {} } }
  },
  _cpAddCalls: [],
  _cpShowCalls: [],
  document: {
    body: { classList: makeClassList(['code-panel-open']) },
    getElementById: (id) => (id === 'code-panel-body' ? cpBody : null),
  },
  _cpAddOrUpdateFile: (p, c, o, r) => ctx._cpAddCalls.push([p, c, o, r]),
  _cpShowFile: (p, pl) => { ctx._cpActive = p; ctx._cpShowCalls.push([p, pl]); },
};
vm.createContext(ctx);
vm.runInContext(extractFn('_toggleThinkBlock') + '\n' + extractFn('_tgPendingRekey'), ctx);

const tb = { classList: makeClassList(['think-block', 'live', 'open']), dataset: {} };
ctx._toggleThinkBlock(tb);
const closedAfterClick = !tb.classList.contains('open')
  && tb.classList.contains('user-collapsed')
  && tb.dataset.userToggled === '1';
// Simulate streaming token check
if (tb.dataset.userToggled !== '1') tb.classList.add('open');
const stayedClosedOnToken = !tb.classList.contains('open') && tb.classList.contains('user-collapsed');
// Second click reopens it
ctx._toggleThinkBlock(tb);
const reopenedOnSecondClick = tb.classList.contains('open') && !tb.classList.contains('user-collapsed');
// Collapsing a .live-only block without .open succeeds in one click
const tbLiveOnly = { classList: makeClassList(['think-block', 'live']), dataset: {} };
ctx._toggleThinkBlock(tbLiveOnly);
const liveOnlyClosedInOneClick = !tbLiveOnly.classList.contains('open')
  && tbLiveOnly.classList.contains('user-collapsed')
  && tbLiveOnly.dataset.userToggled === '1';

ctx._tgPendingRekey('tetris.html', '\u0000pending-write#0');
console.log(JSON.stringify({
  closedAfterClick,
  stayedClosedOnToken,
  reopenedOnSecondClick,
  liveOnlyClosedInOneClick,
  rekeyActive: ctx._cpActive,
  showCalls: ctx._cpShowCalls,
  scrolledBottom: cpBody.scrollTop === cpBody.scrollHeight,
}));
"""
_np = subprocess.run(["node", "-e", _node_harness], cwd=_ROOT, capture_output=True, text=True, check=True)
_nj = json.loads(_np.stdout.strip())
check(
    "node harness verifies _toggleThinkBlock stays collapsed under stream and _tgPendingRekey preserves active tab + scroll",
    _nj["closedAfterClick"] and _nj["stayedClosedOnToken"]
    and _nj["reopenedOnSecondClick"] and _nj["liveOnlyClosedInOneClick"]
    and _nj["rekeyActive"] == "tetris.html"
    and _nj["showCalls"] == [["tetris.html", True]]
    and _nj["scrolledBottom"],
    repr(_nj),
)

sys.exit(h.finish("live_pip_think_codepanel_fixes"))

"""Test: direct chat tools (2026-08-31).

Verifies offline:
  1. _TOOL_MODE_ALLOWLISTS contains the three direct tiers
     (direct / direct_python / direct_full) with the correct tool sets.
  2. _tool_names_for_mode / _filter_tools_for_mode gate web tools correctly.
  3. MCP _tool_allowed: query is allowed over HTTP only with MCP_ALLOW_QUERY=1,
     exec stays blocked over HTTP always.
  4. settings.py DEFAULT_SETTINGS contains the new direct-tools keys.
  5. core.run_context.RunContext has a workspace field.
  6. core.direct_runner._run_direct_tools wires the ToolLoop correctly
     (mock: ensure_loaded + ToolLoop) and returns content + events.
"""
import asyncio
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools import definitions as _defs
from tools.websearch import WEB_SEARCH_TOOL_DEF, WEB_FETCH_TOOL_DEF
from settings import DEFAULT_SETTINGS
from core.run_context import RunContext

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


print("-- 1) Direct-Tool-Mode Allowlists --")
_allow = _defs._TOOL_MODE_ALLOWLISTS
check("modes present", {"direct", "direct_python", "direct_full"} <= set(_allow))
check("direct = websearch only (no read/python/write)",
      "web_search" in _allow["direct"]
      and "web_fetch" in _allow["direct"]
      and "read_file" not in _allow["direct"]
      and "search_code" not in _allow["direct"]
      and "run_python" not in _allow["direct"]
      and "run_bash" not in _allow["direct"])
check("direct_python adds run_python only",
      "run_python" in _allow["direct_python"]
      and "edit_file" not in _allow["direct_python"]
      and "run_bash" not in _allow["direct_python"])
check("direct_full = full inline + web",
      _allow["direct_full"] == set(_defs._INLINE_TOOL_NAMES) | {"web_search", "web_fetch"})

print("-- 2) Tool gating (web only if available) --")
# Without init_websearch => _WEBSEARCH_AVAILABLE=False => no web tools.
names_no_ws = _defs._tool_names_for_mode("direct", include_websearch=True)
check("no web tools when websearch unavailable",
      "web_search" not in names_no_ws and "web_fetch" not in names_no_ws)

# With a fake websearch module registered.
_fake_ws = types.SimpleNamespace(get_tool_defs=lambda: [WEB_SEARCH_TOOL_DEF, WEB_FETCH_TOOL_DEF])
_defs.init_websearch(True, _fake_ws)
names_ws = _defs._tool_names_for_mode("direct", include_websearch=True)
check("web tools appear when available",
      "web_search" in names_ws and "web_fetch" in names_ws)
names_ws_off = _defs._tool_names_for_mode("direct", include_websearch=False)
check("web tools stripped when include_websearch=False",
      "web_search" not in names_ws_off and "web_fetch" not in names_ws_off)

_tools = _defs._get_inline_tools(include_websearch=True, mode="direct")
_tool_names = {t["function"]["name"] for t in _tools}
check("_get_inline_tools(direct) websearch only",
      "web_search" in _tool_names and "web_fetch" in _tool_names
      and "read_file" not in _tool_names and "search_code" not in _tool_names
      and "run_python" not in _tool_names and "run_bash" not in _tool_names)
_tools_py = _defs._get_inline_tools(include_websearch=True, mode="direct_python")
_py_names = {t["function"]["name"] for t in _tools_py}
check("_get_inline_tools(direct_python) read + python",
      "read_file" in _py_names and "run_python" in _py_names
      and "run_bash" not in _py_names and "edit_file" not in _py_names)
_tools_full = _defs._get_inline_tools(include_websearch=True, mode="direct_full")
_full_names = {t["function"]["name"] for t in _tools_full}
check("_get_inline_tools(direct_full) full set",
      "run_bash" in _full_names and "edit_file" in _full_names and "web_search" in _full_names)
_defs.init_websearch(False, None)

print("-- 3) MCP query/exec Governance (HTTP) --")
import infra.mcp_server as _mcp
_orig_query = os.environ.get("MCP_ALLOW_QUERY")
try:
    os.environ.pop("MCP_ALLOW_QUERY", None)
    _ok, _reason = _mcp._tool_allowed("query", "http")
    check("query blocked over HTTP without opt-in", _ok is False)
    _ok2, _ = _mcp._tool_allowed("query", "stdio")
    check("query blocked over stdio without opt-in", _ok2 is False)
    _ok3, _ = _mcp._tool_allowed("shell", "http")
    check("exec always blocked over HTTP", _ok3 is False)
    os.environ["MCP_ALLOW_QUERY"] = "1"
    _ok4, _ = _mcp._tool_allowed("query", "http")
    check("query allowed over HTTP with MCP_ALLOW_QUERY=1", _ok4 is True)
    _ok5, _ = _mcp._tool_allowed("query", "stdio")
    check("query allowed over stdio with MCP_ALLOW_QUERY=1", _ok5 is True)
    _ok6, _ = _mcp._tool_allowed("shell", "http")
    check("exec stays blocked over HTTP even with flags", _ok6 is False)
finally:
    if _orig_query is None:
        os.environ.pop("MCP_ALLOW_QUERY", None)
    else:
        os.environ["MCP_ALLOW_QUERY"] = _orig_query

print("-- 4) Settings Defaults --")
check("direct_tools_enabled default True", DEFAULT_SETTINGS.get("direct_tools_enabled") is True)
check("direct_tools_tier default readonly", DEFAULT_SETTINGS.get("direct_tools_tier") == "readonly")
check("direct_tools_max_rounds default 12", DEFAULT_SETTINGS.get("direct_tools_max_rounds") == 12)

print("-- 5) RunContext workspace field --")
check("RunContext has workspace", "workspace" in RunContext.__dataclass_fields__)

print("-- 6) _run_direct_tools wiring (mocked ToolLoop) --")
import core.tool_loop as _tl
import core.direct_runner as _dr
import backend.llama_client as _lc


class _FakeMgr:
    def __init__(self):
        self.loaded = None
    async def ensure_loaded(self, model, num_ctx=None, vision=False):
        self.loaded = (model, num_ctx)
        return 8101


class _FakeClient:
    async def aclose(self):
        return None


_captured_configs = []


class _FakeLoop:
    def __init__(self, *a, **kw):
        self.config = kw.get("config")
        self._client = _FakeClient()
        self.state = None
        self._on_before_post = kw.get("on_before_post")
        _captured_configs.append(self.config)
    async def run(self, msgs):
        if self._on_before_post:
            await self._on_before_post(msgs, 0, None)
        self.state = _tl.ToolLoopState()
        self.state.content_parts = ["Hello answer"]
        yield {"type": "token", "content": "Hello answer"}


_fake_mgr = _FakeMgr()
_orig_mgr = _lc.manager
_orig_loop = _tl.ToolLoop
_lc.manager = _fake_mgr
_tl.ToolLoop = _FakeLoop

async def _run_direct_tools_collect(*a, **kw):
    _res = _dr._DirectToolsResult()
    _evs = []
    async for _e in _dr._run_direct_tools(*a, **kw, result=_res):
        _evs.append(_e)
    return _evs, _res


try:
    _ag = types.SimpleNamespace(model="m:9b", temperature=0.4, max_tokens=600,
                                thinking=False, thinking_budget=0)
    _pipe = types.SimpleNamespace(agents={"direct": _ag})
    _ctx = RunContext()
    # TIER-FIX (2026-09-02): the "direct" tier is websearch-only, so the wiring
    # test needs a registered (fake) websearch module — otherwise mode "direct"
    # yields zero tools and _run_direct_tools returns early.
    _defs.init_websearch(True, _fake_ws)
    _ctx.websearch_available = True
    _ctx.workspace = r"C:\fake"
    _ctx.settings = {"direct_tools_max_rounds": 3, "duo_llm_slow_timeout_s": 300}
    _ctx.get_num_ctx = lambda m, r=None: 8192
    _ctx.aborted = lambda: False
    _ctx.pipeline = _pipe

    _events, _res = asyncio.run(
        _run_direct_tools_collect(_ctx, "m:9b", [{"role": "user", "content": "hi"}], "direct")
    )
    check("tool loop returns content", _res.content == "Hello answer", f" ({_res.content!r})")
    check("tool loop returns token events",
          any(e.get("type") == "token" for e in _events))
    check("tool loop returns final msgs", _res.final_msgs is not None)
    check("ensure_loaded called with direct model+ctx",
          _fake_mgr.loaded == ("m:9b", 8192), f" ({_fake_mgr.loaded!r})")
    check("direct chat does not require tool calls",
          _captured_configs[-1].require_tool_call is False)
    check("direct chat uses stream mode",
          _captured_configs[-1].stream is True)
    check("direct chat tool_mode is direct",
          _captured_configs[-1].tool_mode == "direct")

    _ctx.settings = {"direct_tools_max_rounds": 0, "duo_llm_slow_timeout_s": 300}
    _events2, _res2 = asyncio.run(
        _run_direct_tools_collect(_ctx, "m:9b", [{"role": "user", "content": "hi"}], "direct")
    )
    check("rounds clamp (0 -> 1)", _events2 and _res2.content == "Hello answer")
    check("clamped max_rounds == 1", _captured_configs[-1].max_rounds == 1)
    _ctx.settings = {"direct_tools_max_rounds": 999, "duo_llm_slow_timeout_s": 300}
    asyncio.run(
        _run_direct_tools_collect(_ctx, "m:9b", [{"role": "user", "content": "hi"}], "direct")
    )
    check("max_rounds capped at 300", _captured_configs[-1].max_rounds == 300)
finally:
    _lc.manager = _orig_mgr
    _tl.ToolLoop = _orig_loop
    _defs.init_websearch(False, None)

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)

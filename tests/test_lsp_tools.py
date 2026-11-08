# -*- coding: utf-8 -*-
"""LSP tool-layer tests: formatting, conversion, caps, gates.

Companion to tests/test_lsp_client.py (which drives the REAL client against
a real child process). This suite pins the TOOL layer
(tools/handlers/lsp.py) against a FAKE LspManager - no server process, no
network:

  - diagnostic formatting "[ERROR] relpath:line:col message (source)"
  - 1-based tool args -> 0-based LSP positions
  - lsp_max_results / lsp_max_diagnostics caps and dedup
  - empty results and unmapped extensions answer clearly
  - LSP_DISABLED (lsp_enabled=false) and LSP_UNAVAILABLE (missing server)
  - lsp_rename is approval-class and summarizes the WorkspaceEdit without
    writing anything; lsp_code_actions lists actions without applying them
  - mode gates: read tools + code actions in the read tiers, lsp_rename only
    in the full tiers; handlers wired into the central dispatch funnel

Every flow runs in its own asyncio.run(); the handler keeps one manager per
(workspace, event loop), so each flow gets a fresh configured FakeManager.
"""
import asyncio
import os
import sys
import tempfile
import unittest.mock
from pathlib import Path

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


from infra import lsp_client as LC                          # noqa: E402
from tools.handlers import lsp as LH                        # noqa: E402
from tools import runner as R                               # noqa: E402
from tools import definitions as D                          # noqa: E402

SAMPLE = "def main():\n    pass\n\n\nx = 1\n"


class FakeManager:
    """Stand-in for infra.lsp_client.LspManager: records every call and
    returns the canned results a flow configures on the instance."""

    def __init__(self, workspace, settings=None):
        self.workspace = workspace
        self.settings = dict(settings or {})
        self.calls = []
        self.start_ok = True
        self.rows = [
            {"language": "python", "command": "pyright-langserver --stdio",
             "found": True},
            {"language": "rust", "command": "rust-analyzer", "found": False},
        ]
        self.diag_result = []
        self.definition_result = []
        self.references_result = []
        self.hover_result = None
        self.symbols_result = []
        self.wsymbols_result = []
        self.rename_result = None
        self.actions_result = []
        FakeManager.last = self

    def available(self):
        self.calls.append(("available",))
        return list(self.rows)

    def is_started(self, language_id):
        self.calls.append(("is_started", language_id))
        return bool(getattr(self, "started", True))

    def has_diagnostics(self, path):
        self.calls.append(("has_diagnostics", str(path)))
        return bool(getattr(self, "published", True))

    async def format(self, path, tab_size=4, insert_spaces=True):
        self.calls.append(("format", str(path), tab_size, insert_spaces))
        if getattr(self, "format_unsupported", False):
            return None
        return list(getattr(self, "format_edits", []))

    def broadcast_watched_change(self, paths):
        self.calls.append(("watched", list(paths)))
        return len(list(paths))

    async def start_language(self, language_id):
        self.calls.append(("start_language", language_id))
        if self.start_ok:
            self.started = True
        return self.start_ok

    async def diagnostics(self, path, wait_s=2.0, **kwargs):
        self.calls.append(("diagnostics", str(path), wait_s))
        return list(self.diag_result)

    async def definition(self, path, line, character):
        self.calls.append(("definition", str(path), line, character))
        return list(self.definition_result)

    async def references(self, path, line, character,
                         include_declaration=True):
        self.calls.append(("references", str(path), line, character))
        return list(self.references_result)

    async def hover(self, path, line, character):
        self.calls.append(("hover", str(path), line, character))
        return self.hover_result

    async def document_symbols(self, path):
        self.calls.append(("document_symbols", str(path)))
        return list(self.symbols_result)

    async def workspace_symbols(self, query):
        self.calls.append(("workspace_symbols", query))
        return list(self.wsymbols_result)

    async def rename(self, path, line, character, new_name):
        self.calls.append(("rename", str(path), line, character, new_name))
        return self.rename_result

    async def code_actions(self, path, start_line, start_char,
                           end_line, end_char):
        self.calls.append(("code_actions", str(path), start_line, start_char,
                           end_line, end_char))
        return list(self.actions_result)


# ── settings helpers (core.state.settings is the live dict) ────────────────
from core import state as _st                               # noqa: E402

_MISSING = object()
_TouchedSettings: dict = {}


def set_settings(**kwargs):
    for key, value in kwargs.items():
        _TouchedSettings.setdefault(key, _st.settings.get(key, _MISSING))
        _st.settings[key] = value


def restore_settings():
    for key, value in _TouchedSettings.items():
        if value is _MISSING:
            _st.settings.pop(key, None)
        else:
            _st.settings[key] = value
    _TouchedSettings.clear()


def new_ws() -> tuple:
    ws = Path(tempfile.mkdtemp(prefix="hm_lsp_tools_"))
    sample = ws / "sample.py"
    sample.write_text(SAMPLE, encoding="utf-8")
    sub = ws / "sub"
    sub.mkdir()
    other = sub / "other.rs"
    other.write_text("fn main() {}\n", encoding="utf-8")
    return ws, sample, other


async def call(tool, args, ws, lock=None):
    fn = getattr(LH, f"_inline_tool_{tool}")
    return await fn(args or {}, ws, lock)


def run(tool, args, ws, lock=None, setup=None):
    """One flow = one patched LspManager + one fresh cache entry.

    setup(mgr) configures the FakeManager instance the handler creates."""
    LH._MANAGERS.clear()
    created = []

    class ConfiguredFake(FakeManager):
        def __init__(self, workspace, settings=None):
            super().__init__(workspace, settings)
            created.append(self)
            if setup is not None:
                setup(self)

    with unittest.mock.patch.object(LC, "LspManager", ConfiguredFake):
        out = asyncio.run(call(tool, args, ws, lock))
    return out, (created[0] if created else None)


ws1, sample1, other1 = new_ws()

# ── 1. LSP_DISABLED ─────────────────────────────────────────────────────────
set_settings(lsp_enabled=False)
out, mgr = run("lsp_status", {}, ws1)
check("1a lsp_status answers LSP_DISABLED when switched off",
      out.startswith("[TOOL_ERROR:LSP_DISABLED]"), f"({out[:70]})")
out, mgr = run("lsp_diagnostics", {"path": "sample.py"}, ws1)
check("1b lsp_diagnostics answers LSP_DISABLED when switched off",
      out.startswith("[TOOL_ERROR:LSP_DISABLED]"))
check("1c no manager was constructed while disabled", mgr is None)
restore_settings()

# ── 2. lsp_status ───────────────────────────────────────────────────────────
out, mgr = run("lsp_status", {}, ws1)
check("2a lsp_status reports found and missing rows",
      "python: pyright-langserver --stdio [found]" in out
      and "rust: rust-analyzer [missing" in out, f"({out[:120]})")
check("2b the missing row carries the install hint",
      "rustup component add rust-analyzer" in out)
check("2c the head counts availability", "Language servers: 1/2 found" in out)
check("2d lsp_status never starts a server",
      all(c[0] != "start_language" for c in mgr.calls))
out2, mgr2 = run("lsp_status", {}, ws1)
check("2e a second call reports fine (cache + fresh flow)",
      "Language servers: 1/2 found" in out2 and mgr2 is not None)

set_settings(lsp_servers={"python": "custom-server --stdio"},
             lsp_client_timeout_s=7, lsp_idle_timeout_s=600)
out, mgr = run("lsp_status", {}, ws1)
check("2f lsp_servers + timeout settings are forwarded to the client",
      mgr.settings.get("lsp_servers") == {"python": "custom-server --stdio"}
      and mgr.settings.get("lsp_client_timeout_s") == 7,
      f"({mgr.settings})")
check("2f2 lsp_idle_timeout_s is forwarded too (audit R4)",
      mgr.settings.get("lsp_idle_timeout_s") == 600,
      f"({mgr.settings.get('lsp_idle_timeout_s')})")
restore_settings()

# ── 3. diagnostics: formatting, cap, wait forwarding ────────────────────────
DIAGS = [
    {"range": {"start": {"line": 4, "character": 0}},
     "message": "undefined variable 'x'\nsecond line ignored",
     "severity": 1, "source": "pyright"},
    {"range": {"start": {"line": 9, "character": 3}},
     "message": "hint message", "severity": 4},
]
set_settings(lsp_diagnostics_wait_s=0.5, lsp_max_diagnostics=50)
out, mgr = run("lsp_diagnostics", {"path": "sample.py"}, ws1,
               setup=lambda m: setattr(m, "diag_result", DIAGS))
check("3a error line formatted [ERROR] relpath:line:col message (source)",
      "[ERROR] sample.py:5:1 undefined variable 'x' (pyright)" in out,
      f"({out[:90]})")
check("3b only the first message line is shown",
      "second line ignored" not in out)
check("3c hint severity maps and a missing source adds no parens",
      "[HINT] sample.py:10:4 hint message" in out
      and "hint message )" not in out)
check("3d diagnostics wait_s forwarded from settings",
      ("diagnostics", str(sample1), 0.5) in mgr.calls)
out, _m = run("lsp_diagnostics", {"path": "sample.py"}, ws1)
check("3e no diagnostics -> clear note", out == "(no diagnostics)")
set_settings(lsp_max_diagnostics=1)
out, _m = run("lsp_diagnostics", {"path": "sample.py"}, ws1,
              setup=lambda m: setattr(m, "diag_result", DIAGS))
check("3f the diagnostics cap cuts with a note",
      out.count("[") == 1 and "+1 more diagnostics" in out
      and "lsp_max_diagnostics=1" in out, f"({out[:120]})")
restore_settings()

# ── 4. 1-based -> 0-based conversion ────────────────────────────────────────
out, mgr = run("lsp_definition",
               {"path": "sample.py", "line": 5, "character": 3}, ws1)
check("4a definition receives 0-based positions (5,3 -> 4,2)",
      ("definition", str(sample1), 4, 2) in mgr.calls, f"({mgr.calls})")
out, mgr = run("lsp_references",
               {"path": "sample.py", "line": 1, "character": 1}, ws1)
check("4b references conversion (1,1 -> 0,0)",
      ("references", str(sample1), 0, 0) in mgr.calls)
out, mgr = run("lsp_hover",
               {"path": "sample.py", "line": 2, "character": 6}, ws1)
check("4c hover conversion (2,6 -> 1,5)",
      ("hover", str(sample1), 1, 5) in mgr.calls)
out, mgr = run("lsp_code_actions",
               {"path": "sample.py", "start_line": 2, "start_char": 3,
                "end_line": 4, "end_char": 5}, ws1)
check("4d code_actions range conversion (2,3,4,5 -> 1,2,3,4)",
      ("code_actions", str(sample1), 1, 2, 3, 4) in mgr.calls)
out, mgr = run("lsp_definition",
               {"path": "sample.py", "line": 0, "character": -2}, ws1)
check("4e position 0/negative clamps to 1 (-> 0-based 0)",
      ("definition", str(sample1), 0, 0) in mgr.calls)
out, _m = run("lsp_definition", {"path": "sample.py", "line": "x"}, ws1)
check("4f a non-integer position is an INVALID_ARGUMENT",
      out.startswith("[TOOL_ERROR:INVALID_ARGUMENT]"), f"({out[:80]})")

# ── 5. caps, dedup and empty results ────────────────────────────────────────
def loc(path, line, char):
    return {"path": str(path),
            "range": {"start": {"line": line, "character": char}}}


LOCS = [loc(sample1, 0, 0), loc(sample1, 0, 0), loc(other1, 3, 1),
        loc(sample1, 7, 0)]
set_settings(lsp_max_results=2)
out, _m = run("lsp_references",
              {"path": "sample.py", "line": 1, "character": 1}, ws1,
              setup=lambda m: setattr(m, "references_result", LOCS))
check("5a results deduplicate, cap at lsp_max_results and show a more-note",
      out.startswith("sample.py:1:1\nsub/other.rs:4:2")
      and "+1 more" in out and "lsp_max_results" in out, f"({out!r})")
restore_settings()
out, _m = run("lsp_definition",
              {"path": "sample.py", "line": 5, "character": 3}, ws1)
check("5b empty definition -> named empty answer",
      out == "(no definition found at sample.py:5:3)")
out, _m = run("lsp_hover", {"path": "sample.py", "line": 1, "character": 1},
              ws1)
check("5c empty hover -> named empty answer",
      out == "(no hover information at sample.py:1:1)")
out, _m = run("lsp_symbols", {"path": "sample.py"}, ws1)
check("5d empty symbols -> named empty answer", out == "(no symbols)")
out, _m = run("lsp_workspace_symbols", {"query": "Foo"}, ws1)
check("5e empty workspace symbols -> named empty answer",
      out == "(no workspace symbols matching 'Foo')")
out, _m = run("lsp_workspace_symbols", {"query": "  "}, ws1)
check("5f empty query is an INVALID_ARGUMENT",
      out.startswith("[TOOL_ERROR:INVALID_ARGUMENT]"))

# ── 6. hover / symbols / workspace symbols formatting ───────────────────────
out, _m = run("lsp_hover", {"path": "sample.py", "line": 1, "character": 1},
              ws1, setup=lambda m: setattr(m, "hover_result", {
                  "contents": {"kind": "markdown",
                               "value": "```python\ndef main()\n```"}}))
check("6a MarkupContent hover value is rendered",
      "def main()" in out, f"({out[:60]})")
out, _m = run("lsp_hover", {"path": "sample.py", "line": 1, "character": 1},
              ws1, setup=lambda m: setattr(m, "hover_result",
                                           {"contents": "plain hover text"}))
check("6b a plain-string hover renders verbatim", out == "plain hover text")

SYMS = [
    {"name": "Klass", "kind": 5,
     "range": {"start": {"line": 9, "character": 0},
               "end": {"line": 19, "character": 0}},
     "children": [
         {"name": "meth", "kind": 6,
          "range": {"start": {"line": 10, "character": 4},
                    "end": {"line": 12, "character": 4}},
          "children": []},
     ]},
    {"name": "flat", "kind": 12,
     "location": {"path": str(other1),
                  "range": {"start": {"line": 2, "character": 0}}}},
]
out, _m = run("lsp_symbols", {"path": "sample.py"}, ws1,
              setup=lambda m: setattr(m, "symbols_result", SYMS))
check("6c document symbols render name, kind and 1-based range",
      "Klass (Class) L10-20" in out and "meth (Method) L11-13" in out,
      f"({out!r})")
check("6d nested children are indented", "\n  meth (Method)" in out)
check("6e SymbolInformation shows the (relative) location line",
      "flat (Function) @:3" in out)
out, _m = run("lsp_workspace_symbols", {"query": "main"}, ws1,
              setup=lambda m: setattr(m, "wsymbols_result", [
                  {"name": "ws_main", "kind": 12,
                   "location": {"path": str(other1),
                                "range": {"start": {"line": 3,
                                                    "character": 1}}}}]))
check("6f workspace symbols render relative path:line:col",
      out.startswith("ws_main (Function) sub/other.rs:4:2"), f"({out!r})")

# ── 7. missing server -> LSP_UNAVAILABLE with install hint ──────────────────
out, _m = run("lsp_diagnostics", {"path": "sub/other.rs"}, ws1,
              setup=lambda m: setattr(m, "start_ok", False))
check("7a a missing server answers LSP_UNAVAILABLE",
      out.startswith("[TOOL_ERROR:LSP_UNAVAILABLE]"), f"({out[:90]})")
check("7b the unavailable answer names the install command",
      "rustup component add rust-analyzer" in out)
check("7c the unavailable answer points at lsp_status and the override",
      "lsp_status" in out and "lsp_servers" in out)
out, _m = run("lsp_workspace_symbols", {"query": "main"}, ws1, setup=(
    lambda m: (setattr(m, "start_ok", True),
               setattr(m, "rows", [
                   {"language": "python", "command": "x", "found": False},
                   {"language": "rust", "command": "x", "found": False}]))))
check("7d workspace symbols with NO server at all answer LSP_UNAVAILABLE",
      out.startswith("[TOOL_ERROR:LSP_UNAVAILABLE]"))

ws2, _sample2, _o2 = new_ws()
(ws2 / "logo.png").write_bytes(b"\x89PNG\r\n")
out, mgr = run("lsp_diagnostics", {"path": "logo.png"}, ws2)
check("7e an unmapped extension answers with a clear note",
      out.startswith("(no language server mapping for '.png'"), f"({out})")
check("7f no server was started for the unmapped extension",
      mgr is None or all(c[0] != "start_language" for c in mgr.calls))

# ── 8. rename: summarize, never write, approval class ───────────────────────
ws3, sample3, other3 = new_ws()
RENAME_EDIT = {"changes": {str(sample3): [{"newText": "a"}, {"newText": "b"},
                                          {"newText": "c"}],
                           str(other3): [{"newText": "d"}]}}
out, _m = run("lsp_rename",
              {"path": "sample.py", "line": 1, "character": 5,
               "new_name": "renamed_main"}, ws3,
              setup=lambda m: setattr(m, "rename_result", RENAME_EDIT))
check("8a the rename plan is summarized (edits x files)",
      "Rename plan for 'renamed_main': 4 edit(s) across 2 file(s)" in out,
      f"({out[:110]})")
check("8b per-file edit counts are workspace-relative",
      "sample.py: 3 edit(s)" in out and "sub/other.rs: 1 edit(s)" in out)
check("8c the plan says NOT applied", "NOT applied" in out)
check("8d the files are untouched (rename never writes)",
      sample3.read_text(encoding="utf-8") == SAMPLE
      and other3.read_text(encoding="utf-8") == "fn main() {}\n")
out, _m = run("lsp_rename",
              {"path": "sample.py", "line": 1, "character": 5,
               "new_name": "n2"}, ws3,
              setup=lambda m: setattr(m, "rename_result", {
                  "documentChanges": [
                      {"textDocument": {"path": str(sample3)},
                       "edits": [{"newText": "x"}, {"newText": "y"}]}]}))
check("8e documentChanges are counted too",
      "2 edit(s) across 1 file(s)" in out, f"({out[:90]})")
out, _m = run("lsp_rename",
              {"path": "sample.py", "line": 1, "character": 5,
               "new_name": "n2"}, ws3,
              setup=lambda m: setattr(m, "rename_result", {
                  "changes": {str(sample3): [
                      {"newText": "x",
                       "range": {"start": {"line": 4, "character": 0},
                                 "end": {"line": 4, "character": 3}}},
                      {"newText": "y",
                       "range": {"start": {"line": 9, "character": 2},
                                 "end": {"line": 9, "character": 5}}}]}}))
check("8e2 rename plan includes the 1-based edit lines",
      "sample.py: 2 edit(s) @ line 5, 10" in out, f"({out[:140]})")
out, _m = run("lsp_rename",
              {"path": "sample.py", "line": 1, "character": 99,
               "new_name": "n3"}, ws3)
check("8f a refused rename (null) says so without error",
      out.startswith("(no symbol to rename at"), f"({out[:80]})")
check("8g lsp_rename is in the approval class",
      "lsp_rename" in R._APPROVAL_TOOLS)
check("8h the other lsp tools are deliberately NOT approval-gated",
      not {"lsp_status", "lsp_diagnostics", "lsp_definition",
           "lsp_references", "lsp_hover", "lsp_symbols",
           "lsp_workspace_symbols", "lsp_code_actions"}
      & R._APPROVAL_TOOLS)
check("8h2 only the two mutating lsp tools are gated",
      {"lsp_rename", "lsp_format"} <= R._APPROVAL_TOOLS)

# ── 9. code actions: listed only, nothing applied ───────────────────────────
ACTIONS = [{"title": "Add 'import os'", "kind": "quickfix",
            "isPreferred": True},
           {"title": "Extract method", "kind": "refactor.extract"}]
out, _m = run("lsp_code_actions",
              {"path": "sample.py", "start_line": 2, "start_char": 3,
               "end_line": 4, "end_char": 5}, ws3,
              setup=lambda m: setattr(m, "actions_result", ACTIONS))
check("9a actions render title, kind and the preferred flag",
      "Add 'import os' (quickfix) [preferred]" in out
      and "Extract method (refactor.extract)" in out, f"({out!r})")
check("9b the answer states nothing is applied",
      "nothing is applied" in out and "edit_file" in out)
out, _m = run("lsp_code_actions",
              {"path": "sample.py", "start_line": 1, "start_char": 1,
               "end_line": 1, "end_char": 1}, ws3)
check("9c empty actions -> clear note",
      out == "(no code actions for this range)")

# ── 10. transport errors and workspace confinement ──────────────────────────
class BoomManager(FakeManager):
    async def diagnostics(self, path, wait_s=2.0, **kwargs):
        raise LC.LspError("python: server exploded")


LH._MANAGERS.clear()
with unittest.mock.patch.object(LC, "LspManager", BoomManager):
    out = asyncio.run(call("lsp_diagnostics", {"path": "sample.py"}, ws3))
check("10a an LspError renders as LSP_FAILED with the reason",
      out.startswith("[TOOL_ERROR:LSP_FAILED]") and "server exploded" in out,
      f"({out[:80]})")

ws4, _sample4, _o4 = new_ws()
outside = Path(tempfile.mkdtemp(prefix="hm_lsp_out_")) / "evil.py"
outside.write_text("x = 1\n", encoding="utf-8")
out, _m = run("lsp_diagnostics", {"path": str(outside)}, ws4, lock=str(ws4))
check("10b a path outside the workspace lock is rejected",
      out.startswith("[TOOL_ERROR:PATH_OUTSIDE_WORKSPACE]"),
      f"({out[:70]})")
out, _m = run("lsp_diagnostics", {}, ws4)
check("10c a missing path argument is an INVALID_ARGUMENT",
      out.startswith("[TOOL_ERROR:INVALID_ARGUMENT]"))

# ── 11. mode gates + wiring ─────────────────────────────────────────────────
def names(mode):
    return D._tool_names_for_mode(mode, include_websearch=False)


READ = D._LSP_READ_TOOLS | D._LSP_CODE_ACTIONS
check("11a the read tiers offer the LSP read tools + code actions",
      all(READ <= names(m) for m in
          ("duo_readonly", "pre_explore", "critic_verify", "direct_python")))
FULL = READ | {"lsp_rename", "lsp_format"}
check("11b the full tiers offer everything incl. lsp_rename/lsp_format",
      all(FULL <= names(m) for m in
          ("duo_full", "tool_agent", "mcp_agent", "openai_agent",
           "direct_full")))
check("11c no read tier offers lsp_rename",
      all("lsp_rename" not in names(m) for m in
          ("duo_readonly", "pre_explore", "critic_verify", "direct_python")))
check("11d the websearch-only chat tier offers no lsp tools",
      not any(n.startswith("lsp_") for n in names("direct")))
check("11e fix_agent stays minimal (no lsp tools)",
      not any(n.startswith("lsp_") for n in names("fix_agent")))
check("11f every lsp tool has a handler in the central map",
      all(f"lsp_{n}" in R._INLINE_TOOL_HANDLER_MAP
          for n in ("status", "diagnostics", "definition", "references",
                    "hover", "symbols", "workspace_symbols",
                    "code_actions", "rename")))
check("11g the dead-name guard knows the lsp tools",
      all(n in R._refresh_offered_tool_names() for n in FULL))
check("11h the schemas exist in the master list",
      {t["function"]["name"] for t in D._INLINE_CODING_TOOLS
       if t["function"]["name"].startswith("lsp_")} == FULL)

out, _m = run("lsp_status", {}, ws3)  # warm nothing; funnel flows follow
LH._MANAGERS.clear()
with unittest.mock.patch.object(LC, "LspManager", FakeManager):
    async def _funnel():
        return await R._run_inline_tool("lsp_status", {}, str(ws3),
                                        tool_mode="mcp_agent")
    out = asyncio.run(_funnel())
check("11i lsp_status runs through the central funnel in mcp_agent mode",
      out.startswith("Language servers:"), f"({out[:60]})")
LH._MANAGERS.clear()
with unittest.mock.patch.object(LC, "LspManager", FakeManager):
    async def _funnel_denied():
        return await R._run_inline_tool(
            "lsp_rename",
            {"path": "sample.py", "line": 1, "character": 1,
             "new_name": "x"},
            str(ws3), tool_mode="duo_readonly")
    out = asyncio.run(_funnel_denied())
check("11j the dispatcher rejects lsp_rename in duo_readonly mode",
      out.startswith("[TOOL_ERROR:TOOL_NOT_ALLOWED]"), f"({out[:80]})")

# ── 12. automatic post-write diagnostics (2026-10-31) ────────────────────
def _auto_key(ws):
    loop = asyncio.get_running_loop()
    key = str(Path(ws).resolve())
    return (key.lower() if os.name == "nt" else key, id(loop))


def _auto_flow(ws, path, *, enabled=True, started=True, published=True,
               diag=None, warm=True, status=False):
    async def _go():
        set_settings(lsp_enabled=enabled, lsp_auto_diagnostics=True,
                     lsp_auto_warm_on_write=warm)
        LH._AUTO_DIAG_CACHE.clear()
        fake = FakeManager(ws)
        fake.started = started
        fake.published = published
        fake.diag_result = list(diag or [])
        LH._MANAGERS[_auto_key(ws)] = fake
        try:
            return await LH.auto_diagnostics_block(
                ws, path, return_status=status)
        finally:
            LH._MANAGERS.clear()
    return asyncio.run(_go())


_auto_ws, _auto_sample, _auto_other = new_ws()
_out = _auto_flow(_auto_ws, "sample.py", diag=DIAGS)
check("12a auto diagnostics attach a compact block",
      _out.startswith("[LSP auto-diagnostics") and "[ERROR]" in _out,
      f"({_out[:100]})")
_out = _auto_flow(_auto_ws, "sample.py", enabled=False, diag=DIAGS)
check("12b lsp_enabled=false disables automatic diagnostics", _out == "")
_out = _auto_flow(_auto_ws, "sample.py", started=False, diag=DIAGS)
check("12c a cold server is warmed from the write path",
      _out.startswith("[LSP auto-diagnostics") and "[ERROR]" in _out,
      f"({_out[:100]})")
_out = _auto_flow(_auto_ws, "sample.py", started=False, diag=DIAGS,
                  warm=False)
check("12c2 lsp_auto_warm_on_write=false keeps writes cold", _out == "")
_out = _auto_flow(_auto_ws, "sample.py", published=False, diag=DIAGS)
check("12d no publication yet -> no misleading auto block", _out == "")
_out = _auto_flow(_auto_ws, "sample.py", diag=[DIAGS[0]])
check("12e a warm server with a clean file -> no block",
      _auto_flow(_auto_ws, "sample.py", diag=[]) == "")

# 12f: status tells the write dispatcher whether LSP is authoritative.
_txt, _handled = _auto_flow(_auto_ws, "sample.py", diag=DIAGS, status=True)
check("12f LSP findings are handled", _handled is True and _txt)
_txt, _handled = _auto_flow(_auto_ws, "sample.py", diag=[], status=True)
check("12g LSP clean is handled (lint fallback can be dropped)",
      _handled is True and _txt == "")
_txt, _handled = _auto_flow(_auto_ws, "sample.py", started=False,
                            warm=False, diag=DIAGS, status=True)
check("12h LSP unavailable is not handled (lint stays)",
      _handled is False and _txt == "")
_txt, _handled = _auto_flow(_auto_ws, "sample.py", published=False,
                            diag=DIAGS, status=True)
check("12i LSP still analyzing is not handled",
      _handled is False and _txt == "")

_lint = "written\n[lint/python WARNINGS (pyright)]:\nfoo"
check("12j LSP wins: lint block is replaced",
      LH.merge_post_write_diagnostics(_lint, "LSP-OK", True)
      == "written\n\nLSP-OK")
check("12k LSP clean: lint block is dropped",
      LH.merge_post_write_diagnostics(_lint, "", True) == "written")
check("12l LSP unavailable: lint block stays",
      LH.merge_post_write_diagnostics(_lint, "LSP-OK", False) == _lint)
check("12m no lint block: LSP is appended",
      LH.merge_post_write_diagnostics("written", "LSP-OK", True)
      == "written\n\nLSP-OK")

# explicit lsp_diagnostics must not report "clean" before the first publish
def _pending_setup(mgr):
    mgr.published = False
    mgr.diag_result = []

_out, _mgr = run("lsp_diagnostics", {"path": "sample.py"}, _auto_ws,
                 setup=_pending_setup)
check("12f cold diagnostics says 'still analyzing', not 'no diagnostics'",
      "still analyzing" in _out, f"({_out[:100]})")

# ── 15. watched-change notify from the write path ────────────────────────
_ws_ws, _ws_sample, _ws_other = new_ws()
def _watch_flow(path):
    async def _go():
        set_settings(lsp_enabled=True)
        LH._MANAGERS.clear()
        fake = FakeManager(_ws_ws)
        LH._MANAGERS[_auto_key(_ws_ws)] = fake
        try:
            return fake, LH.notify_watched_change(_ws_ws, path)
        finally:
            LH._MANAGERS.clear()
    return asyncio.run(_go())

_fake, _sent = _watch_flow(["sample.py"])
check("15a a write notifies didChangeWatchedChanges through the manager",
      _sent == 1 and ("watched", ["sample.py"]) in _fake.calls,
      f"({_sent})")
_fake2, _sent2 = _watch_flow([])
check("15b an empty path list sends nothing", _sent2 == 0)
async def _no_mgr():
    LH._MANAGERS.clear()
    return LH.notify_watched_change(_ws_ws, ["sample.py"])
check("15c no manager -> silent 0", asyncio.run(_no_mgr()) == 0)

# ── 16. workspace/configuration answers from .vscode/settings.json ───────
from infra import lsp_client as LC2
_cfg_ws = Path(tempfile.mkdtemp(prefix="hm_lsp_cfg_"))
(_cfg_ws / ".vscode").mkdir()
(_cfg_ws / ".vscode" / "settings.json").write_text(
    '{"rust-analyzer": {"checkOnSave": {"enable": true}}, "python": {"analysis": {"typeCheckingMode": "basic"}}}',
    encoding="utf-8")
_ws_data = LC2.read_workspace_settings(_cfg_ws)
check("16a the reader loads .vscode/settings.json",
      _ws_data.get("rust-analyzer", {}).get("checkOnSave", {}).get("enable") is True)
(_cfg_ws / ".vscode" / "settings.json").write_text("{broken", encoding="utf-8")
check("16b a broken file answers {}", LC2.read_workspace_settings(_cfg_ws) == {})
check("16c a missing file answers {}",
      LC2.read_workspace_settings(Path(tempfile.mkdtemp(prefix="hm_lsp_nocfg_"))) == {})
_r = LC2._lookup_section({"a": {"b": 7}}, "a.b")
check("16d dotted sections resolve", _r == 7)
check("16e missing sections answer None",
      LC2._lookup_section({"a": 1}, "x.y") is None
      and LC2._lookup_section({}, None) == {})

# ── 19. LspManager.broadcast_watched_change at client level ──────────────

# ── 17. _apply_text_edits: order, UTF-16 astral columns, empty newText ───
from tools.handlers.lsp import _apply_text_edits as _apply

_NL = chr(10)
_txt = "alpha" + _NL + "beta" + _NL
_r = lambda l1, c1, l2, c2: {
    "start": {"line": l1, "character": c1},
    "end": {"line": l2, "character": c2}}
check("17a simple replace",
      _apply(_txt, [{"range": _r(0, 0, 0, 5), "newText": "ALPHA"}])
      == "ALPHA" + _NL + "beta" + _NL)
check("17b bottom-up order does not matter for correctness",
      _apply(_txt, [
          {"range": _r(1, 0, 1, 4), "newText": "BETA"},
          {"range": _r(0, 0, 0, 5), "newText": "ALPHA"},
      ]) == "ALPHA" + _NL + "BETA" + _NL)
check("17c empty newText deletes the line content",
      _apply(_txt, [{"range": _r(0, 0, 0, 5), "newText": ""}])
      == _NL + "beta" + _NL)
_emoji = chr(0x1F600)
_astral = 's = "' + _emoji + '"  # emoji is 2 UTF-16 units' + _NL
# UTF-16 col 14-15 sits past the emoji (2 units) -> code points 13-15,
# i.e. the "oj" of "emoji" is replaced by "!" — the astral shift by one.
check("17d a UTF-16 column past an astral char lands correctly",
      _apply(_astral, [{"range": _r(0, 14, 0, 15), "newText": "!"}])
      == 's = "' + _emoji + '"  # em!ji is 2 UTF-16 units' + _NL,
      f"({_apply(_astral, [{'range': _r(0, 14, 0, 15), 'newText': '!'}])!r})")
check("17e a column beyond the line clamps to the line end",
      _apply(_txt, [{"range": _r(0, 99, 0, 99), "newText": "!"}])
      == "alpha!" + _NL + "beta" + _NL)
check("17f inverted ranges are normalized",
      _apply(_txt, [{"range": _r(0, 5, 0, 0), "newText": ""}])
      == _NL + "beta" + _NL)
_CRLF = chr(13) + _NL
_crlf = "a" + _CRLF + "b" + _CRLF
check("17g CRLF lines keep the carriage return",
      _apply(_crlf, [{"range": _r(0, 1, 0, 1), "newText": "!"}])
      == "a!" + _CRLF + "b" + _CRLF)

def _broadcast_flow():
    async def _go():
        from infra.lsp_client import LspManager
        ws = Path(tempfile.mkdtemp(prefix="hm_lsp_bcast_"))
        (ws / "sub").mkdir()
        (ws / "f.py").write_text("x = 1" + chr(10), encoding="utf-8")
        mgr = LspManager(ws, {"lsp_idle_timeout_s": 0})

        class _Conn:
            def __init__(self):
                self.dead = False
                self.notes = []
            async def notify(self, method, params):
                self.notes.append((method, params))

        live, dead = _Conn(), _Conn()
        dead.dead = True
        mgr._conns = {"python": live, "rust": dead}
        n = mgr.broadcast_watched_change(["f.py", r"sub\g.rs", r"..\outside.py"])
        await asyncio.sleep(0.05)
        return live, n
    return asyncio.run(_go())

_live, _n = _broadcast_flow()
_bnote = next((p for m, p in _live.notes if m == "workspace/didChangeWatchedFiles"), None)
_buris = [c["uri"] for c in (_bnote or {}).get("changes", [])]
check("19a the broadcast fans out type-2 events with canonical URIs",
      _n == 1 and len(_buris) == 2
      and _buris[0].startswith("file:///") and _buris[0].endswith("/f.py"),
      f"({_n}, {_buris})")
check("19b dead connections are skipped", len(_live.notes) == 1)
check("19c a path outside the workspace is not broadcast",
      len(_buris) == 2 and all("/f.py" in u or "/sub/g.rs" in u
                               for u in _buris), f"({_buris})")

# ── 13. manager cap closes evicted servers (audit P1-2) ──────────────────
async def _evict_flow():
    LH._MANAGERS.clear()
    closed = []

    class ClosingFake(FakeManager):
        async def close_all(self):
            closed.append(self)

    with unittest.mock.patch.object(LC, "LspManager", ClosingFake):
        for _i in range(LH._MANAGERS_CAP + 1):
            _w = Path(tempfile.mkdtemp(prefix=f"hm_lsp_evict_{_i}_"))
            LH._get_manager(_w)
        await asyncio.sleep(0)
    return len(closed), len(LH._MANAGERS)


_evicted_n, _remaining_n = asyncio.run(_evict_flow())
check("13a cap eviction closes the evicted managers",
      _evicted_n == LH._MANAGERS_CAP, f"({_evicted_n})")
check("13b the fresh manager stays cached", _remaining_n == 1,
      f"({_remaining_n})")
LH._MANAGERS.clear()

# ── 14. schema gating + priority (audit P2-5) ───────────────────────────
import settings as _settings_mod
from core.budgets import schema_budget_for as _schema_budget

with unittest.mock.patch.object(_settings_mod, "load_settings",
                                return_value={"lsp_enabled": False}):
    _off = D._get_inline_tools(mode="duo_full")
check("14a lsp_enabled=false removes every lsp schema",
      not any(t["function"]["name"].startswith("lsp_") for t in _off))

with unittest.mock.patch.object(_settings_mod, "load_settings",
                                return_value={"lsp_enabled": True}):
    _on = D._get_inline_tools(mode="duo_full")
check("14b lsp_enabled=true keeps the lsp schemas",
      any(t["function"]["name"] == "lsp_diagnostics" for t in _on))

_fit = D.fit_tools_to_budget(_on, _schema_budget(8192, "m"))
_fit_names = {t["function"]["name"] for t in _fit}
check("14c lsp_status survives the 8k schema fit",
      "lsp_status" in _fit_names, f"({sorted(n for n in _fit_names if n.startswith('lsp_'))})")
check("14d lsp_diagnostics survives the 8k schema fit",
      "lsp_diagnostics" in _fit_names)

restore_settings()
print()
print(f"SUMMARY lsp_tools: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

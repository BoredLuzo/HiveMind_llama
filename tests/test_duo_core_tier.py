# -*- coding: utf-8 -*-
"""Behavior checks: core coder tier (17 tools), lean tier (10 tools) + research tier (2026-11-09).

- duo_core defaults to 17 tools (plus send_file when include_send_file=True).
- duo_lean provides the compact 10-tool set without get_datetime/send_file/lsp_*.
- duo_full opts into git/web/MCP/browser/full-LSP tools.
- direct_research gets web + read/search (no write/exec).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.duo_helpers import resolve_coder_tool_mode  # noqa: E402
from tools.definitions import _TOOL_PRIORITY, _get_inline_tools  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def _names(mode: str, **kw) -> set[str]:
    return {t["function"]["name"] for t in _get_inline_tools(mode=mode, **kw)}


_core = _names("duo_core")
check("coder core: read/edit/run/tests present",
      {"read_file", "edit_file", "write_file", "run_bash", "run_tests",
       "task_complete", "ask_user", "get_datetime", "lsp_diagnostics"} <= _core,
      f" ({sorted(_core)})")
check("coder core: no git/web/mcp/browser/subagent",
      not ({"git_status", "git_commit", "web_search", "web_fetch", "browser",
            "lsp_status", "subagent_research", "install_package"} & _core),
      f" ({sorted(_core & {'git_status', 'web_search', 'browser', 'lsp_status'})})")
check("coder core: send_file stays gated in core",
      "send_file" in _names("duo_core", include_send_file=True)
      and "send_file" not in _names("duo_core", include_send_file=False))
check("coder core: exact 17-tool set", len(_core) == 17, f" ({len(_core)})")
check("coder core: every duo_core tool is in _TOOL_PRIORITY",
      _core <= set(_TOOL_PRIORITY), f" (missing={sorted(_core - set(_TOOL_PRIORITY))})")

_lean = _names("duo_lean")
_expected_lean = {
    "read_file", "edit_file", "write_file", "run_bash", "run_tests",
    "search_code", "find_files", "list_dir", "get_signatures", "task_complete",
}
check("coder lean: exact 10-tool set", _lean == _expected_lean,
      f" ({sorted(_lean)})")
check("coder lean: excludes get_datetime, send_file, and lsp_references",
      not ({"get_datetime", "send_file", "lsp_references", "lsp_diagnostics"}
           & _names("duo_lean", include_send_file=True)))

_full = _get_inline_tools(mode="duo_full")
_core_json = len(json.dumps(_get_inline_tools(mode="duo_core")))
_lean_json = len(json.dumps(_get_inline_tools(mode="duo_lean")))
_full_json = len(json.dumps(_full))
check("coder core: schema is materially smaller than full",
      _core_json < _full_json * 0.65,
      f" ({_core_json} vs {_full_json} chars)")
check("coder lean: schema is smaller than core",
      _lean_json < _core_json,
      f" ({_lean_json} vs {_core_json} chars)")

check("resolve: default is core", resolve_coder_tool_mode({}) == "duo_core")
check("resolve: explicit full opts in",
      resolve_coder_tool_mode({"duo_coder_tool_tier": "full"}) == "duo_full")
check("resolve: explicit lean opts in",
      resolve_coder_tool_mode({"duo_coder_tool_tier": "lean"}) == "duo_lean")
check("resolve: unknown value fails safe to core",
      resolve_coder_tool_mode({"duo_coder_tool_tier": "junk"}) == "duo_core")
check("resolve: read-only wins over the tier",
      resolve_coder_tool_mode({"duo_coder_tool_tier": "full"}, read_only=True)
      == "duo_readonly")

_research = _names("direct_research")
check("direct research: read/search tools present",
      {"read_file", "search_code", "list_dir", "find_files"} <= _research,
      f" ({sorted(_research)})")
check("direct research: no write/exec/python",
      not ({"edit_file", "write_file", "run_bash", "run_python"} & _research),
      f" ({sorted(_research & {'edit_file', 'write_file', 'run_bash'})})")
check("direct research: no git/lsp",
      not ({"git_status", "lsp_status"} & _research))

sys.exit(h.finish("duo_core_tier"))

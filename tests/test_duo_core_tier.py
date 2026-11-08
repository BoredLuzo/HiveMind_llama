# -*- coding: utf-8 -*-
"""Behavior checks: core coder tier + research tier (2026-11-08).

The coder defaults to a 17-tool core set (read/edit/run/tests + LSP
diagnostics) instead of the 33-tool full set. Git/web/MCP/browser/LSP extras
are opt-in via `duo_coder_tool_tier=full`. Direct gets a research tier
(web + read/search, no write/exec).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.duo_helpers import resolve_coder_tool_mode  # noqa: E402
from tools.definitions import _get_inline_tools  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def _names(mode: str) -> set[str]:
    return {t["function"]["name"] for t in _get_inline_tools(mode=mode)}


_core = _names("duo_core")
check("coder core: read/edit/run/tests present",
      {"read_file", "edit_file", "write_file", "run_bash", "run_tests",
       "task_complete", "ask_user"} <= _core, f" ({sorted(_core)})")
check("coder core: no git/web/mcp/browser/lsp_status/subagent",
      not ({"git_status", "git_commit", "web_search", "web_fetch", "browser",
            "lsp_status", "subagent_research", "install_package"} & _core),
      f" ({sorted(_core & {'git_status', 'web_search', 'browser', 'lsp_status'})})")
check("coder core: LSP diagnostics included (warms auto-diagnostics)",
      "lsp_diagnostics" in _core)
check("coder core: exact 17-tool set", len(_core) == 17, f" ({len(_core)})")

_full = _get_inline_tools(mode="duo_full")
_core_json = len(json.dumps(_get_inline_tools(mode="duo_core")))
_full_json = len(json.dumps(_full))
check("coder core: schema is materially smaller than full",
      _core_json < _full_json * 0.65,
      f" ({_core_json} vs {_full_json} chars)")

check("resolve: default is core", resolve_coder_tool_mode({}) == "duo_core")
check("resolve: explicit full opts in",
      resolve_coder_tool_mode({"duo_coder_tool_tier": "full"}) == "duo_full")
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

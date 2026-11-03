# -*- coding: utf-8 -*-
"""LSP security hardening (audit 2026-11-03)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from settings import DEFAULT_SETTINGS            # noqa: E402

LC = (ROOT / "infra" / "lsp_client.py").read_text(encoding="utf-8")
LR = (ROOT / "infra" / "lsp_registry.py").read_text(encoding="utf-8")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


check("1 LSP children get a scrubbed environment",
      "_lsp_child_env" in LC and "env=_lsp_child_env()" in LC)
check("2 workspace .vscode settings are opt-in",
      DEFAULT_SETTINGS.get("lsp_forward_workspace_settings") is False
      and "if self._allow_ws_settings:" in LC)
check("3 shell metacharacters in lsp_servers are rejected",
      "contains shell" in LR)
check("4 the .cmd shim quotes the executable", "_exe_q" in LR)
check("5 the trust limitation is documented", "cargo check" in
      (ROOT / "docs" / "lsp.md").read_text(encoding="utf-8"))

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)

# -*- coding: utf-8 -*-
"""Real-server smoke test: pyright through the generic LSP client.

The fake-server suites cannot catch real dialect issues (URI encoding,
initialization order, delayed publication). This suite starts the pyright
that is installed on the machine; on a machine without it the suite SKIPs
with exit 0.

Run: python tests/test_lsp_real_pyright.py
"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="summary")
check = h.check

if shutil.which("pyright-langserver") is None:
    print("  SKIP  pyright-langserver is not installed")
    print()
    print("SUMMARY lsp_real_pyright: 0 passed, 0 failed (skipped)")
    sys.exit(0)

from infra.lsp_client import LspManager  # noqa: E402

BAD = 'def add(a: int, b: int) -> int:\n    return a + b\n\nx: int = add("one", 2)\n'
GOOD = 'def add(a: int, b: int) -> int:\n    return a + b\n\nx: int = add(1, 2)\n'


async def _flow():
    ws = Path(tempfile.mkdtemp(prefix="hm_lsp_pyright_"))
    sample = ws / "sample.py"
    sample.write_text(BAD, encoding="utf-8")
    mgr = LspManager(ws, {"lsp_client_timeout_s": 30})
    out = {}
    try:
        out["start"] = await mgr.start_language("python")
        out["bad"] = await mgr.diagnostics("sample.py", wait_s=20.0)
        sample.write_text(GOOD, encoding="utf-8")
        out["good"] = await mgr.diagnostics("sample.py", wait_s=20.0)
        out["clean_text_len"] = len(GOOD)
        # R6: sibling module created AFTER Pyright started must resolve once
        # broadcast_watched_change(..., created=True) fires (live bug 2026-11-19).
        (ws / "helper_mod.py").write_text("VALUE: int = 7\n", encoding="utf-8")
        mgr.broadcast_watched_change(["helper_mod.py"], created=True)
        await asyncio.sleep(0)
        sample.write_text("from helper_mod import VALUE\nx: int = VALUE + 1\n", encoding="utf-8")
        out["sibling"] = await mgr.diagnostics("sample.py", wait_s=20.0)
        # R7: built-in pythonPath default points at workspace .venv when present, else sys.executable
        py_conn = mgr._conns.get("python")
        out["python_path"] = ((getattr(py_conn, "config_settings", {}) or {}).get("python") or {}).get("pythonPath")
    finally:
        await mgr.close_all()
    # R8: real vscode-css-language-server validates broken CSS with built-in css.validate=True
    if shutil.which("vscode-css-language-server") is not None:
        css_ws = Path(tempfile.mkdtemp(prefix="hm_lsp_css_"))
        css_f = css_ws / "broken.css"
        css_f.write_text("body { color: ; }\n", encoding="utf-8")
        css_mgr = LspManager(css_ws, {"lsp_client_timeout_s": 20})
        try:
            out["css_started"] = await css_mgr.start_language("css")
            out["css_bad"] = await css_mgr.diagnostics("broken.css", wait_s=8.0)
            css_f.write_text("body { color: red; }\n", encoding="utf-8")
            out["css_good"] = await css_mgr.diagnostics("broken.css", wait_s=8.0)
        finally:
            await css_mgr.close_all()
    return out


res = asyncio.run(_flow())
check("R1 pyright starts through the registry", res["start"] is True)
_bad = " ".join(str(d.get("message") or "") for d in res["bad"])
check("R2 real pyright diagnostics reach the client", len(res["bad"]) >= 1,
      f"({len(res['bad'])} diagnostics)")
check("R3 the type error is the expected Pyright message",
      "cannot be assigned" in _bad or "not assignable" in _bad,
      f"({_bad[:120]!r})")
check("R4 the parser reports the correct 1-based line",
      any(d.get("range", {}).get("start", {}).get("line") == 3
          for d in res["bad"]), f"({[d.get('range') for d in res['bad']]})")
check("R5 the fixed file publishes no error diagnostics",
      not any(d.get("severity") == 1 for d in res["good"]),
      f"({res['good']})")
check("R6 newly created sibling module resolves after created=True watch notification",
      not any(d.get("severity") == 1 for d in res["sibling"]),
      f"({res['sibling']})")
check("R7 python LSP defaults python.pythonPath to active interpreter",
      bool(res.get("python_path")),
      f"({res.get('python_path')!r})")
if "css_started" in res:
    check("R8 real CSS LSP validates broken CSS and clears after fix",
          res["css_started"] is True
          and len(res["css_bad"]) >= 1
          and len(res["css_good"]) == 0,
          f"(bad={len(res['css_bad'])}, good={len(res['css_good'])})")

sys.exit(h.finish("lsp_real_pyright"))

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

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


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
    finally:
        await mgr.close_all()
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

print()
print(f"SUMMARY lsp_real_pyright: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

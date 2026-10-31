# -*- coding: utf-8 -*-
"""Real-server smoke test: rust-analyzer through the generic LSP client.

Pins the rust-analyzer dialect that the fake server cannot model: the
native type diagnostic arrives, is followed by a version-less empty clear,
and a later cargo-check publication. The client must keep the actionable
finding instead of returning "(no diagnostics)".

Skips cleanly when rust-analyzer is not installed.
"""
import asyncio
import shutil
import subprocess
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


if shutil.which("rust-analyzer") is None:
    print("  SKIP  rust-analyzer is not installed")
    print()
    print("SUMMARY lsp_real_rust: 0 passed, 0 failed (skipped)")
    sys.exit(0)

from infra.lsp_client import LspManager  # noqa: E402

BAD = '''fn add(a: i32, b: i32) -> i32 {
    a + b
}

fn main() {
    let value: i32 = "not a number";
    println!("{}", add(value, 2));
}
'''
GOOD = BAD.replace('"not a number"', "42")


async def _flow():
    ws = Path(tempfile.mkdtemp(prefix="hm_lsp_rust_"))
    (ws / "src").mkdir()
    (ws / "Cargo.toml").write_text(
        '[package]\nname = "hm_lsp_rust_test"\nversion = "0.1.0"\n'
        'edition = "2021"\n', encoding="utf-8")
    sample = ws / "src" / "main.rs"
    sample.write_text(BAD, encoding="utf-8")
    # Warm cargo's target dir first; rust-analyzer's flycheck is otherwise
    # occasionally slower than the wait on a cold registry.
    subprocess.run(["cargo", "check"], cwd=ws, capture_output=True,
                   text=True, timeout=120, check=False)
    mgr = LspManager(ws, {"lsp_client_timeout_s": 60})
    out = {}
    try:
        out["start"] = await mgr.start_language("rust")
        out["bad"] = await mgr.diagnostics("src/main.rs", wait_s=30.0)
        sample.write_text(GOOD, encoding="utf-8")
        out["good"] = await mgr.diagnostics("src/main.rs", wait_s=5.0)
    finally:
        await mgr.close_all()
    return out


res = asyncio.run(_flow())
check("R1 rust-analyzer starts through the registry", res["start"] is True)
_bad = " ".join(str(d.get("message") or "") for d in res["bad"])
check("R2 a native type diagnostic survives the empty clear",
      len(res["bad"]) >= 1, f"({len(res['bad'])} diagnostics)")
check("R3 the type mismatch is reported",
      "expected i32" in _bad or "mismatched types" in _bad,
      f"({_bad[:120]!r})")
check("R4 the fixed file no longer reports an error",
      not any(d.get("severity") == 1 for d in res["good"]),
      f"({res['good']})")

print()
print(f"SUMMARY lsp_real_rust: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

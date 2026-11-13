# -*- coding: utf-8 -*-
"""Auto-test-on-edit behavior tests (G#2 lever, 2026-11-11).

tools/auto_test.auto_test_note() runs the testmap-mapped tests of a just-
edited file IN THE SAME TOOL TURN and renders a compact [AUTO-TEST] note.
This suite pins the safety envelope:

  - DEFAULT OFF: nothing runs unless duo_auto_test_on_edit=true
  - mapped tests only: nothing maps -> nothing runs
  - bounded: per-run cap; unchanged files are never re-tested (signature)
  - silent degradation: missing runner / broken suite call -> "" (the edit
    result is never broken by the auto-test machinery)
  - success and failure renderings

Run: python tests/test_auto_test_on_edit.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools import auto_test                                    # noqa: E402
from tools.handlers import _shared as _sh                      # noqa: E402
from core.state import settings as _settings                   # noqa: E402
from tests._harness import Harness                             # noqa: E402

h = Harness(pad=True)
check = h.check


class _FakeResult:
    def __init__(self, *, success=True, failures=0, language="python",
                 command="pytest -x tests/test_app.py", errors=None,
                 inject=""):
        self.success = success
        self.failure_count = failures
        self.language = language
        self.command = command
        self.error_lines = errors or []
        self.raw_output = ""
        self.inject_msg = inject

    def is_clean(self):
        return self.success and self.failure_count == 0


class _FakeSuite:
    """Call recorder shared with the async fake injected below."""

    def __init__(self):
        self.calls: list[dict] = []


def _wire(fake: _FakeSuite, result: _FakeResult):
    async def _fn(*, workspace, timeout, chat_id="", paths=None, **kw):
        fake.calls.append({"workspace": workspace, "timeout": timeout,
                           "chat_id": chat_id, "paths": list(paths or [])})
        return result
    return _fn


def _mkws(with_test: bool = True) -> Path:
    td = tempfile.mkdtemp(prefix="hm_at_")
    ws = Path(td)
    (ws / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    if with_test:
        tdir = ws / "tests"
        tdir.mkdir()
        (tdir / "test_app.py").write_text(
            "import app\n\ndef test_value():\n    assert app.VALUE == 1\n",
            encoding="utf-8")
    return ws


async def _note(ws: Path) -> str:
    return await auto_test.auto_test_note([str(ws / "app.py")], ws, str(ws))


_ORIG_SUITE = _sh._run_test_suite
try:
    # ── 1. DEFAULT OFF: no suite call, empty note ──
    _settings.pop("duo_auto_test_on_edit", None)
    fake = _FakeSuite()
    ws = _mkws()
    _sh._run_test_suite = _wire(fake, _FakeResult())
    try:
        out = asyncio.run(_note(ws))
        check("default-off: empty note, suite never called",
              out == "" and fake.calls == [],
              f" ({out!r}, calls={fake.calls})")
    finally:
        _sh._run_test_suite = _ORIG_SUITE

    # ── 2. enabled + mapped test -> green note, scoped paths ──
    _settings["duo_auto_test_on_edit"] = True
    fake = _FakeSuite()
    ws = _mkws()
    _sh._run_test_suite = _wire(fake, _FakeResult())
    try:
        out = asyncio.run(_note(ws))
        check("enabled: green [AUTO-TEST] note rendered",
              "[AUTO-TEST] ✅ passed" in out and "test_app.py" in out,
              f" ({out!r})")
        check("suite called with the MAPPED test path only",
              len(fake.calls) == 1
              and fake.calls[0]["paths"] == ["tests/test_app.py"],
              f" ({fake.calls})")
        check("timeout bounded by setting",
              fake.calls[0]["timeout"] == 60)
    finally:
        _sh._run_test_suite = _ORIG_SUITE

    # ── 3. red suite -> failure note with the error text ──
    fake = _FakeSuite()
    ws = _mkws()
    _sh._run_test_suite = _wire(fake, _FakeResult(
        success=False, failures=1,
        errors=["FAILED tests/test_app.py::test_value - assert 1 == 2"]))
    try:
        out = asyncio.run(_note(ws))
        check("failure note names the failing test",
              "[AUTO-TEST] ❌ 1 failure(s)" in out
              and "assert 1 == 2" in out, f" ({out!r})")
    finally:
        _sh._run_test_suite = _ORIG_SUITE

    # ── 4. nothing mappable -> suite not called ──
    fake = _FakeSuite()
    ws2 = _mkws(with_test=False)
    _sh._run_test_suite = _wire(fake, _FakeResult())
    try:
        out = asyncio.run(_note(ws2))
        check("no mapped tests: empty note, suite never called",
              out == "" and fake.calls == [])
    finally:
        _sh._run_test_suite = _ORIG_SUITE

    # ── 5. per-run cap ──
    _settings["duo_auto_test_max_per_run"] = 1
    fake = _FakeSuite()
    ws = _mkws()
    _sh._run_test_suite = _wire(fake, _FakeResult())
    try:
        auto_test.reset_run_state("")
        out1 = asyncio.run(_note(ws))
        # touch the test file so the signature gate does not eat the 2nd call
        t = ws / "tests" / "test_app.py"
        t.write_text(t.read_text(encoding="utf-8") + "\n# x\n", encoding="utf-8")
        out2 = asyncio.run(_note(ws))
        check("cap=1: first call runs, second is suppressed",
              out1 != "" and out2 == "" and len(fake.calls) == 1,
              f" (out1={out1[:40]!r} out2={out2[:40]!r} calls={len(fake.calls)})")
    finally:
        _sh._run_test_suite = _ORIG_SUITE
    _settings.pop("duo_auto_test_max_per_run", None)

    # ── 6. signature gate: unchanged tests are not re-run ──
    fake = _FakeSuite()
    ws = _mkws()
    _sh._run_test_suite = _wire(fake, _FakeResult())
    try:
        auto_test.reset_run_state("")
        out1 = asyncio.run(_note(ws))
        out2 = asyncio.run(_note(ws))  # same files, no change -> skipped
        check("unchanged file not re-tested (loop protection)",
              out1 != "" and out2 == "" and len(fake.calls) == 1,
              f" (calls={len(fake.calls)})")
    finally:
        _sh._run_test_suite = _ORIG_SUITE
    auto_test.reset_run_state("")

    # ── 7. broken suite call degrades to "" ──
    async def _boom(**kw):
        raise RuntimeError("runner exploded")
    _sh._run_test_suite = _boom
    try:
        out = asyncio.run(_note(_mkws()))
        check("suite exception -> empty note, no raise", out == "")
    finally:
        _sh._run_test_suite = _ORIG_SUITE

    # ── 8. missing runner degrades to "" ──
    _sh._run_test_suite = None
    try:
        out = asyncio.run(_note(_mkws()))
        check("runner not wired -> empty note", out == "")
    finally:
        _sh._run_test_suite = _ORIG_SUITE

    # ── 9. "no tests found" scoped result -> neutral empty note ──
    fake = _FakeSuite()
    ws = _mkws()
    _sh._run_test_suite = _wire(fake, _FakeResult(
        success=False, failures=0,
        inject="⚠️ no tests found (scoped run)"))
    try:
        auto_test.reset_run_state("")
        out = asyncio.run(_note(ws))
        check("no-tests-found scoped result stays neutral",
              out == "", f" ({out!r})")
    finally:
        _sh._run_test_suite = _ORIG_SUITE
    auto_test.reset_run_state("")

    # ── 10. static-only edits never trigger ──
    fake = _FakeSuite()
    ws = _mkws()
    (ws / "style.css").write_text("body { color: red }\n", encoding="utf-8")
    _sh._run_test_suite = _wire(fake, _FakeResult())
    try:
        out = asyncio.run(auto_test.auto_test_note(
            [str(ws / "style.css")], ws, str(ws)))
        check("static asset edit: no auto-test", out == "" and fake.calls == [])
    finally:
        _sh._run_test_suite = _ORIG_SUITE

    # ── 11. timeout setting clamp ──
    _settings["duo_auto_test_timeout"] = 5000
    fake = _FakeSuite()
    ws = _mkws()
    _sh._run_test_suite = _wire(fake, _FakeResult())
    try:
        auto_test.reset_run_state("")
        asyncio.run(_note(ws))
        check("timeout clamped to 180 s max",
              fake.calls[0]["timeout"] == 180)
    finally:
        _sh._run_test_suite = _ORIG_SUITE
        _settings.pop("duo_auto_test_timeout", None)
        auto_test.reset_run_state("")

    # ── 12. source-edit hash bust: editing app.py invalidates signature ──
    fake = _FakeSuite()
    ws = _mkws()
    _sh._run_test_suite = _wire(fake, _FakeResult())
    try:
        auto_test.reset_run_state("")
        out_s1 = asyncio.run(_note(ws))
        (ws / "app.py").write_text("VALUE = 999\n", encoding="utf-8")
        out_s2 = asyncio.run(_note(ws))
        check("editing source file busts fresh signature even when test file is unchanged",
              out_s1 != "" and out_s2 != "" and len(fake.calls) == 2,
              f" (calls={len(fake.calls)})")
    finally:
        _sh._run_test_suite = _ORIG_SUITE
        auto_test.reset_run_state("")

    # ── 13. settings cleanup: key off again ──
    _settings.pop("duo_auto_test_on_edit", None)
    check("cleanup removed the setting", "duo_auto_test_on_edit" not in _settings)

    # ── 14. per-run _run_settings_snapshot overrides global core.state.settings ──
    from tools.runner import _run_settings_snapshot as _snap_var
    _settings["duo_auto_test_on_edit"] = False
    _tok = _snap_var.set({"duo_auto_test_on_edit": True})
    fake = _FakeSuite()
    ws = _mkws()
    _sh._run_test_suite = _wire(fake, _FakeResult())
    try:
        auto_test.reset_run_state("")
        out_snap = asyncio.run(_note(ws))
        check("per-run snapshot enables auto_test even when global setting is False",
              "[AUTO-TEST" in out_snap and len(fake.calls) == 1,
              f" ({out_snap!r}, calls={len(fake.calls)})")
    finally:
        _snap_var.reset(_tok)
        _sh._run_test_suite = _ORIG_SUITE
        _settings.pop("duo_auto_test_on_edit", None)
        auto_test.reset_run_state("")
finally:
    _sh._run_test_suite = _ORIG_SUITE

sys.exit(h.finish("auto_test_on_edit"))

"""All shipped .bat files must be pure ASCII with CRLF endings.

The special-char E2E showed cmd parses these files in the OEM codepage:
a UTF-8 umlaut inside a hard-coded path broke the copy step, and LF-only
line endings can break label jumps. Generation-side guard (2026-10-04):
this fails the regression BEFORE a broken bat ships in a release zip.

Run: python tests/test_bat_encoding.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


def main():
    bats = sorted(ROOT.rglob("*.bat"))
    bats = [b for b in bats
            if ".venv" not in b.parts and "llama" not in b.parts
            and "update_backup" not in b.name and ".legacy" not in b.parts]
    if len(bats) < 4:
        fail("discovery", f"only {len(bats)} bat files found")
    for b in bats:
        raw = b.read_bytes()
        try:
            raw.decode("ascii")
            ascii_ok = True
        except UnicodeDecodeError as e:
            ascii_ok = False
            fail(f"{b.name} ascii", f"byte {e.start}: {raw[e.start:e.start + 1]!r}")
        if b"\r\n" not in raw:
            fail(f"{b.name} crlf", "no CRLF line ending found")
        elif b"\n" in raw.replace(b"\r\n", b""):
            fail(f"{b.name} crlf", "mixed line endings (bare LF present)")
        if ascii_ok and b"\r\n" in raw:
            ok(f"{b.name} ({len(raw)} B, ascii+crlf)")

    # CONTROL CHARS (2026-10-27, live): a \f inside "deploy\fetch_models.py"
    # landed as form feed 0x0C in install.bat and setup_models.bat. The judge
    # model download therefore always failed ("file not found") and the
    # installer only warned. ASCII+CRLF alone does not catch that.
    _bad_cc = []
    for _b in bats:
        _raw = _b.read_bytes()
        for _c in (0x00, 0x07, 0x08, 0x0B, 0x0C, 0x1A):
            if bytes([_c]) in _raw:
                _bad_cc.append(f"{_b.name}:0x{_c:02x}")
    if _bad_cc:
        fail("no control characters in bat files", str(_bad_cc))
    else:
        ok("no control characters in bat files")

    # The judge is not a dedicated model download (2026-10-31): any
    # installed LLM can be assigned to the Judge role. The installer must not
    # offer lfm3 as a judge-specific install step.
    _inst = (ROOT / "install.bat").read_text(encoding="ascii", errors="replace")
    if (":judge_step" not in _inst
            and "--only-keys lfm3:1.7b-qad" not in _inst
            and "judge model" not in _inst.lower()):
        ok("no dedicated judge-model install step")
    else:
        fail("no dedicated judge-model install step",
             "judge_step / lfm3-only download still present")

    # DECIDER FLOW (2026-10-31): the optional CPU decider stays; both
    # branches must fall through to SearXNG, with no judge block in between.
    _dec_i = _inst.find(":decider_model_step")
    _sx_i = _inst.find(":searxng_step")
    _dec_block = _inst[_dec_i:_sx_i] if _dec_i >= 0 and _sx_i > _dec_i else ""
    if _dec_i >= 0 and _sx_i > _dec_i:
        ok("install.bat asks for the decider, then SearXNG")
    else:
        fail("install.bat asks for the decider, then SearXNG",
             "label order is wrong")
    if _dec_block.count("goto searxng_step") >= 2:
        ok("both decider branches reach SearXNG")
    else:
        fail("both decider branches reach SearXNG",
             "a branch still jumps to the removed judge block")
    if _inst.count("choice /c YN /n /d N") >= 1:
        ok("the decider is opt-in (Enter = skip)")
    else:
        fail("the decider is opt-in (Enter = skip)",
             "choice without /d N means the user cannot accept the default")
    if ":decider_done" not in _inst:
        ok("no duplicate decider block")
    else:
        fail("no duplicate decider block", ":decider_done still present")
    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)

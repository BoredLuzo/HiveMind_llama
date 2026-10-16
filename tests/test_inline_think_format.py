"""Inline reasoning (between tool calls) must be formatted and collapsed.

Owner report 2026-10-17: "reasoning during the coder: while streaming it is
well formatted, as an expanded tool call it is shit."

What the code actually did: the MAIN live think block of a coder bubble is
finalized with _renderThinkCodeBlocks() (fenced code -> styled <pre>) at the end
of the stream. The INLINE think blocks - the ones _createThinkBlock() inserts
between tool calls, i.e. exactly the reasoning that sits inside the expanded
tool-call flow - only lost their `live` class and got a check mark. Same
reasoning, no fence formatting, and they kept the `open` class forever, so
several tool rounds stacked up as a wall of expanded raw text.

Fix: the inline finalize loop now formats the body with the same
_renderThinkCodeBlocks() and collapses the block (CSS: .think-block.open shows
the body, .think-block.live shows it while streaming - neither class stays).

Run: python tests/test_inline_think_format.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")
_css = (ROOT / "static" / "style.css").read_text(encoding="utf-8", errors="replace")
_html = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")

# the inline finalize loop (endAgentStream)
_i = _js.find("Also finalize all inline think-blocks inside the body")
check("inline finalize loop exists", _i > 0)
_block = _js[_i:_i + 3200] if _i > 0 else ""

check("inline block is formatted with the shared fence renderer",
      "_renderThinkCodeBlocks(_tbb)" in _block)
check("inline block is collapsed (open removed)", "classList.remove('open')" in _block)
check("inline block still drops the live class", "classList.remove('live')" in _block)
check("inline block shows the char count",
      "' \\u00b7 ' + _tbb.textContent.length + ' chars \\u00b7 \\u2713'" in _block)
check("the char count is only set when the body has content",
      "_tbb && _tbb.textContent.trim()" in _block)

# the main streaming block stays as before (owner: streaming is fine)
_main = _js.find("THINK-CODE-FORMAT: fenced code in reasoning renders as code blocks")
_mblock = _js[_main:_main + 400] if _main > 0 else ""
check("main live block still formats its fences", "_renderThinkCodeBlocks(_tkBody)" in _mblock)
check("main live block is not force-collapsed",
      "classList.remove('open')" not in _mblock)

# _createThinkBlock is the inline creator and its body carries the styled class
_c = _js.find("function _createThinkBlock()")
_cblock = _js[_c:_c + 900] if _c > 0 else ""
check("_createThinkBlock builds header + think-body",
      "'<div class=\"think-body\"></div>'" in _cblock)
check("_createThinkBlock starts live+open", "'think-block live open'" in _cblock)

# CSS contract the fix relies on
check("CSS shows the body while live", ".think-block.live .think-body { display: block; }" in _css)
check("CSS shows the body when open", ".think-block.open .think-body { display: block; }" in _css)
check("CSS keeps reasoning whitespace", "white-space: pre-wrap" in _css)

# cache buster must move: app.js changed
_m = re.search(r"app\.js\?v=(\d{8})-(\d+)", _html)
check("cache buster bumped past 20261017-03",
      _m is not None and (_m.group(1), int(_m.group(2))) >= ("20261017", 4),
      f" ({_m.group(0) if _m else '?'})")

print()
print(f"SUMMARY inline_think_format: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

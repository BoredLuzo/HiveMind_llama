"""R3-web: date context in search results (Write-tool exact strings)."""
import ast

p = 'tools/websearch.py'
src = open(p, encoding='utf-8').read()

BS = chr(92)
old = '    lines = [f"Search results for: {query}' + BS + 'n"]'
new = (
    '    # DATE-CONTEXT (audit r3 live): search hits often carry old copyright\n'
    '    # stamps ("' + chr(0xA9) + ' 2025") - a small model concluded "2026 is not\n'
    '    # reached yet" from that and refused date-sensitive answers.\n'
    '    from datetime import datetime as _ws_dt\n'
    '    lines = [f"Search results for: {query}' + BS + 'n"\n'
    '             f"(fetched TODAY: {_ws_dt.strftime(' + BS + '"' + BS + '%Y-' + BS + '%m-' + BS + '%d' + BS + '")}; pages may\n'
    '             " carry older copyright dates - that does NOT mean today is\n'
    '             " in the past)' + BS + 'n"]'
)
c = src.count(old)
src = src.replace(old, new, 1)
open(p, 'w', encoding='utf-8').write(src)
ast.parse(src)
print("date-context:", c, "| syntax OK")

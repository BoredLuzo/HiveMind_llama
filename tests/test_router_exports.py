"""routers/__init__.py bleibt synchron zu den Modulen im Ordner.

Fund beim Live-Start: ein neuer Router (`routers/context.py`) war in server.py
importiert, aber im Paket nicht exportiert -> `ImportError: cannot import name
'context_router' from 'routers'` und die Engine startete nicht. Der Test prueft,
dass jede Router-Datei auch als `<name>_router` exportiert ist.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import routers  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


SKIP = {"__init__", "context_router"}  # context_router ist kein Modulname
modules = sorted(p.stem for p in (ROOT / "routers").glob("*.py") if p.stem not in {"__init__"})
check("router modules found", len(modules) >= 10, f"({len(modules)}: {', '.join(modules)})")

for mod in modules:
    attr = f"{mod}_router"
    check(f"routers exports {attr}", hasattr(routers, attr))

# server.py darf genau diese Namen importieren
import ast  # noqa: E402

server_src = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
tree = ast.parse(server_src)
imported = set()
for node in ast.walk(tree):
    if isinstance(node, ast.ImportFrom) and node.module == "routers":
        imported.update(a.name for a in node.names)
missing = sorted(n for n in imported if not hasattr(routers, n))
check("every router server.py imports exists", not missing, f"(missing: {missing})")
check("server.py imports the context router", "context_router" in imported, f"({sorted(imported)})")

print()
print(f"SUMMARY router_exports: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

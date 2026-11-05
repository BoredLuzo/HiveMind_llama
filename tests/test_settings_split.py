"""settings.py / settings_defaults.py split pin (2026-11-05 audit).

settings.py stays the import surface (63 importers); the pure data lives in
settings_defaults.py and must be I/O-free.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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


def main() -> int:
    from settings import DEFAULT_SETTINGS, DEFAULT_AGENT_CFG  # noqa: E402
    from settings_defaults import DEFAULT_SETTINGS as D2  # noqa: E402
    check("re-export is the same object", DEFAULT_SETTINGS is D2)
    check("defaults non-trivial", len(DEFAULT_SETTINGS) > 100
          and len(DEFAULT_AGENT_CFG) >= 8)

    src = (ROOT / "settings_defaults.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imports = [n for n in ast.walk(tree)
               if isinstance(n, (ast.Import, ast.ImportFrom))]
    check("settings_defaults is import-free (pure data)", not imports)
    check("settings_defaults has no functions",
          not [n for n in tree.body if isinstance(n, ast.FunctionDef)])
    check("settings.py re-exports both names",
          "from settings_defaults import DEFAULT_SETTINGS, DEFAULT_AGENT_CFG"
          in (ROOT / "settings.py").read_text(encoding="utf-8"))

    print(f"\nSUMMARY settings_split: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

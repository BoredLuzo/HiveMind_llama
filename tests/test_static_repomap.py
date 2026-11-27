# -*- coding: utf-8 -*-
"""Tests: static repo-map signature extraction & ast_tools JS/Python signatures.

Run: python tests/test_static_repomap.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _harness import Harness

try:
    import tree_sitter_language_pack  # noqa: F401
    _HAS_TS = True
except ImportError:
    _HAS_TS = False

from hive_functions.static_repomap import (
    _analyze_file,
    _build_sync,
    _extract_symbols_from_report,
)
from hive_functions.hivemind_feature.ast_tools import get_signatures_report

h = Harness()
check = h.check

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)

    # ── 1. Python: classes, methods, functions with params + return types + @L, no inner noise ──
    py_file = root / "engine.py"
    py_file.write_text(
        'VERSION = "1.0"\n'
        "MAX_ITEMS = 42\n"
        "\n"
        "class Engine:\n"
        "    def step(self, dt: float, substeps: int = 1) -> bool:\n"
        "        def _inner_helper(x):\n"
        "            return x + 1\n"
        "        return _inner_helper(substeps) > 0\n"
        "\n"
        "async def run_all(items: list[str]) -> int:\n"
        "    return len(items)\n",
        encoding="utf-8",
    )
    py_info = _analyze_file(str(py_file), "engine.py")
    check("python file analyzed", py_info is not None and len(py_info.symbols) >= 3, str(py_info))
    py_syms = py_info.symbols if py_info else []
    check("python class includes line number", any("class Engine @L4" in s for s in py_syms), str(py_syms))
    check(
        "python method includes class prefix, params, return type, line",
        any("Engine.step(self, dt: float, substeps: int = 1) -> bool @L5" in s for s in py_syms),
        str(py_syms),
    )
    check(
        "python top-level function includes params and return type",
        any("run_all(items: list[str]) -> int @L10" in s for s in py_syms),
        str(py_syms),
    )
    check("python nested inner helper is not leaked", not any("_inner_helper" in s for s in py_syms), str(py_syms))

    # Python ast_tools: variable headers do not bleed across subsequent lines
    py_rep = get_signatures_report(py_file)
    ver_lines = [ln for ln in py_rep.splitlines() if ln.startswith("variable VERSION")]
    check(
        "ast_tools python variable stays on its own line",
        len(ver_lines) == 1 and "MAX_ITEMS" not in ver_lines[0],
        str(ver_lines),
    )
    # Fallback parser preserves signatures from get_signatures_report
    fb_syms = _extract_symbols_from_report(py_rep)
    check(
        "fallback report extractor preserves function params and return type",
        any("run_all(items: list[str]) -> int @L10" in s for s in fb_syms),
        str(fb_syms),
    )
    check(
        "fallback report extractor preserves method params and return type",
        any("Engine.step(self, dt: float, substeps: int = 1) -> bool @L5" in s for s in fb_syms),
        str(fb_syms),
    )

    # ── 2. JavaScript: IIFE, var X = function, prototype methods, arrows, exports ──
    js_file = root / "camera.js"
    js_file.write_text(
        "window.Sim = window.Sim || {};\n"
        "(function () {\n"
        "  var Camera = function (opts) {\n"
        "    this.x = opts.x || 0;\n"
        "  };\n"
        "  Camera.prototype.worldToScreen = function (wx, wy, w, h) {\n"
        "    return { sx: wx, sy: wy };\n"
        "  };\n"
        "  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));\n"
        "  const sq = x => x * x;\n"
        "  window.Sim.Camera = Camera;\n"
        "  window.Sim.Constants = { G: 1, AU: 2 };\n"
        "})();\n",
        encoding="utf-8",
    )
    js_info = _analyze_file(str(js_file), "camera.js")
    js_syms = js_info.symbols if js_info else []
    check("js IIFE constructor extracted with params", any("Camera(opts) @L3" in s for s in js_syms), str(js_syms))
    check(
        "js prototype method extracted with params",
        any("Camera.worldToScreen(wx, wy, w, h) @L6" in s for s in js_syms),
        str(js_syms),
    )
    check("js arrow function extracted with params", any("clamp(v, lo, hi) @L9" in s for s in js_syms), str(js_syms))
    if _HAS_TS:
        check("js single-arg arrow extracted with params", any("sq(x) @L10" in s for s in js_syms), str(js_syms))
        check(
            "js namespace object export lists keys",
            any("export window.Sim.Constants {G, AU}" in s for s in js_syms),
            str(js_syms),
        )

    # ast_tools heuristic fallback also handles IIFE + prototype methods
    js_rep = get_signatures_report(js_file)
    check("ast_tools js IIFE finds Camera constructor", "function Camera @L3" in js_rep, js_rep)
    check("ast_tools js IIFE finds prototype method", "method Camera.worldToScreen @L6" in js_rep, js_rep)

    # ── 3. TypeScript & C++ (tree-sitter path) ──
    if _HAS_TS:
        ts_file = root / "sim.ts"
        ts_file.write_text(
            "export interface Vec2 { x: number; y: number; }\n"
            "export const step = (dt: number, n: number): void => {};\n"
            "export class Runner {\n"
            "  update(dt: number): number { return dt; }\n"
            "}\n",
            encoding="utf-8",
        )
        ts_info = _analyze_file(str(ts_file), "sim.ts")
        ts_syms = ts_info.symbols if ts_info else []
        check("ts interface extracted", any("Vec2 @L1" in s for s in ts_syms), str(ts_syms))
        check(
            "ts typed arrow includes params and return type",
            any("step(dt: number, n: number) -> void @L2" in s for s in ts_syms),
            str(ts_syms),
        )
        check(
            "ts class method includes class prefix, params, return type",
            any("Runner.update(dt: number) -> number @L4" in s for s in ts_syms),
            str(ts_syms),
        )

        # ── 4. C++: function_definition declarator unwrapping ──
        cpp_file = root / "math.cpp"
        cpp_file.write_text(
            "class Solver {\n"
            "public:\n"
            "    int compute(int a, float b) { return a; }\n"
            "};\n"
            "void run_sim(int n) {}\n",
            encoding="utf-8",
        )
        cpp_info = _analyze_file(str(cpp_file), "math.cpp")
        cpp_syms = cpp_info.symbols if cpp_info else []
        check(
            "cpp class method extracted with params",
            any("Solver.compute(int a, float b) @L3" in s for s in cpp_syms),
            str(cpp_syms),
        )
        check("cpp free function extracted with params", any("run_sim(int n) @L5" in s for s in cpp_syms), str(cpp_syms))

    # ── 5. _build_sync renders signatures & +N more symbols indicator ──
    many_file = root / "many.py"
    many_file.write_text(
        "\n".join(f"def fn_{i}(x: int) -> int:\n    return x + {i}" for i in range(20)) + "\n",
        encoding="utf-8",
    )
    repomap = _build_sync(
        str(root),
        [{"label": "root", "paths": ["engine.py", "camera.js", "many.py"]}],
        char_budget=8000,
    )
    check("repomap includes python signature", "Engine.step(self, dt: float, substeps: int = 1) -> bool @L5" in repomap, repomap)
    check("repomap includes js prototype signature", "Camera.worldToScreen(wx, wy, w, h) @L6" in repomap, repomap)
    check("repomap notes remaining symbols beyond cap", "(+4 more symbols)" in repomap, repomap)

sys.exit(h.finish("static_repomap"))

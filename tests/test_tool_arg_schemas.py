"""Eval: tool definitions & JSON-schema validation (tools/definitions.py).

Audit point: "no jsonschema validation of tool args". This suite checks
(a) structural integrity of all tool schemas, (b) consistency of allowlists
and subsets, (c) a minimal validator for required/type checks.
Deterministic, no LLM needed.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.definitions import (
    _INLINE_CODING_TOOLS,
    _INLINE_TOOL_NAMES,
    _TOOL_MODE_ALLOWLISTS,
    _TOOL_SUBSETS,
    _filter_tools_for_mode,
)

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


def _validate_args(schema_params: dict, args: dict) -> list[str]:
    """Minimaler JSON-Schema-Validator (Teilmenge). Liefert Fehlerliste."""
    errors = []
    props = schema_params.get("properties", {}) if isinstance(schema_params, dict) else {}
    required = schema_params.get("required", []) if isinstance(schema_params, dict) else []
    for key in required:
        if key not in args:
            errors.append(f"missing required: {key}")
    for key, val in args.items():
        spec = props.get(key)
        if not isinstance(spec, dict):
            errors.append(f"unknown arg: {key}")
            continue
        t = spec.get("type")
        _type_ok = {
            "string": isinstance(val, str),
            "integer": isinstance(val, int) and not isinstance(val, bool),
            "boolean": isinstance(val, bool),
            "number": isinstance(val, (int, float)) and not isinstance(val, bool),
            "array": isinstance(val, list),
            "object": isinstance(val, dict),
        }.get(t, True)
        if not _type_ok:
            errors.append(f"{key}: expected {t}, got {type(val).__name__}")
    return errors


# ── Schema-Struktur aller Tools ─────────────────────────────────────────
_tools = list(_INLINE_CODING_TOOLS)
check("tools non-empty", len(_tools) >= 20, f" ({len(_tools)})")
schema_bad = 0
for t in _tools:
    fn = t.get("function") if isinstance(t, dict) else None
    name = fn.get("name") if isinstance(fn, dict) else None
    params = fn.get("parameters") if isinstance(fn, dict) else None
    if not name or not isinstance(params, dict) or params.get("type") != "object":
        schema_bad += 1
        continue
    props = params.get("properties")
    req = params.get("required") or []
    if not isinstance(props, dict):
        schema_bad += 1
        continue
    for k in req:
        if k not in props:
            schema_bad += 1
            check(f"{name}: required '{k}' in properties", False)
    for pname, pspec in props.items():
        if not isinstance(pspec, dict) or "type" not in pspec:
            schema_bad += 1
check("all tool schemas structurally valid", schema_bad == 0, f" (bad={schema_bad})")

# ── Namen eindeutig ─────────────────────────────────────────────────────
_names = [t["function"]["name"] for t in _tools]
check("tool names unique", len(set(_names)) == len(_names))

# ── Allowlists / Subsets konsistent ─────────────────────────────────────
# Externe Tools, die NICHT in _INLINE_CODING_TOOLS liegen, aber real existieren.
_KNOWN_EXTERNAL = {"web_search", "web_fetch", "hivemind_pipeline"}
_bad_allow = []
for mode, allow in _TOOL_MODE_ALLOWLISTS.items():
    for name in allow:
        if name not in _INLINE_TOOL_NAMES and name not in _KNOWN_EXTERNAL:
            _bad_allow.append(f"{mode}:{name}")
check("allowlists only real tools", not _bad_allow, f" {_bad_allow[:5]}")

_bad_sub = [n for n, _ in _TOOL_SUBSETS.items() for x in _TOOL_SUBSETS[n] if x not in _INLINE_TOOL_NAMES]
check("subsets only real tools", not _bad_sub, f" {_bad_sub[:5]}")

# ── Validator-Verhalten an bekannten Schemas ────────────────────────────
def _params_of(name):
    for t in _tools:
        if t["function"]["name"] == name:
            return t["function"]["parameters"]
    return None

_rp = _params_of("read_file")
check("read_file: valid args", _validate_args(_rp, {"path": "a.py"}) == [])
check("read_file: missing required", _validate_args(_rp, {}) != [])
check("read_file: wrong type", _validate_args(_rp, {"path": 42}) != [])

_bp = _params_of("run_bash")
check("run_bash: valid cmd", _validate_args(_bp, {"cmd": "pytest -q"}) == [])
check("run_bash: missing cmd", _validate_args(_bp, {}) != [])

_lp = _params_of("list_dir")
check("list_dir: no required", _validate_args(_lp, {}) == [])

# ── Mode-Filtering ──────────────────────────────────────────────────────
_all = list(_tools)
_read_only = _filter_tools_for_mode(_all, "duo_readonly")
_run_present = any(t["function"]["name"] == "run_bash" for t in _read_only)
check("readonly excludes run_bash", not _run_present)
_full = _filter_tools_for_mode(_all, "duo_full")
check("full includes run_bash",
      any(t["function"]["name"] == "run_bash" for t in _full))

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)

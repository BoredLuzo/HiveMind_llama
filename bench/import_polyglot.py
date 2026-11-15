# -*- coding: utf-8 -*-
"""Import a fixed Python subset of the Aider polyglot benchmark.

Source: https://github.com/Aider-AI/polyglot-benchmark (Exercism exercises).
The generated task dirs are local benchmark fixtures; they are not part of the
shipped product. `.meta/example.py` is never copied — only the stub and the
hidden test file.
"""
from __future__ import annotations

import argparse
import ast
import json
import shutil
from pathlib import Path

DEFAULT_NAMES = (
    "affine-cipher", "beer-song", "bowling", "grade-school", "list-ops",
    "phone-number", "robot-name", "simple-linked-list", "tree-building", "wordy",
)


def _read_instructions(ex: Path) -> str:
    parts = []
    for name in ("instructions.md", "instructions.append.md"):
        p = ex / ".docs" / name
        if p.is_file():
            parts.append(p.read_text(encoding="utf-8", errors="replace").strip())
    return "\n\n".join(p for p in parts if p)


_SKIP_NAMES = frozenset({".docs", ".meta", ".approaches", "__pycache__"})


def _is_skipped_name(name: str) -> bool:
    return name in _SKIP_NAMES or name.endswith("_test.py")


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if _is_skipped_name(n)}


def _extract_test_contract(tests: list[Path], slug: str) -> str:
    """Extract imported symbols, instance/callable APIs, mocks, and exceptions via AST.

    Keeps concrete test inputs hidden while surfacing exact API names,
    callable arity/kwargs, return types, attribute read/write expectations,
    mocked targets, and ExceptionType('message') pairings.
    """
    import re as _re

    mod = slug.replace("-", "_")
    imported_names: list[str] = []
    seen_imports: set[str] = set()
    module_attrs: list[str] = []
    seen_mod_attrs: set[str] = set()
    patch_targets: list[str] = []
    seen_patches: set[str] = set()
    paired_errors: list[str] = []
    seen_paired_errors: set[str] = set()
    paired_exc_names: set[str] = set()
    unpaired_exc_types: list[str] = []
    seen_exc_types: set[str] = set()

    # key -> {"min_pos": int|None, "max_pos": int|None, "kwargs": list[str],
    #         "ret_types": list[str], "fmt": str}
    call_sigs: dict[str, dict] = {}
    # cls_name -> {attr_name -> {"read": bool, "writable": bool, "readonly": bool, "types": list[str]}}
    cls_attrs: dict[str, dict[str, dict]] = {}
    cls_methods: dict[str, list[str]] = {}

    def _add_import(name: str) -> None:
        if name and name != "*" and name not in seen_imports:
            seen_imports.add(name)
            imported_names.append(name)

    def _add_mod_attr(name: str) -> None:
        if name and name not in seen_mod_attrs:
            seen_mod_attrs.add(name)
            module_attrs.append(name)

    def _add_patch(target: str) -> None:
        cleaned = target.strip()
        if cleaned and cleaned not in seen_patches:
            seen_patches.add(cleaned)
            patch_targets.append(cleaned)

    def _add_exc_type(exc_name: str) -> None:
        cleaned = exc_name.strip()
        if cleaned and cleaned not in seen_exc_types:
            seen_exc_types.add(cleaned)
            unpaired_exc_types.append(cleaned)

    def _add_error(msg: str, exc_type: str = "") -> None:
        cleaned = msg.strip()
        if not cleaned:
            return
        if exc_type:
            paired_exc_names.add(exc_type.strip())
        entry = f"{exc_type}({cleaned!r})" if exc_type else repr(cleaned)
        if entry not in seen_paired_errors:
            seen_paired_errors.add(entry)
            paired_errors.append(entry)

    def _ensure_sig(key: str) -> dict:
        if key not in call_sigs:
            call_sigs[key] = {
                "min_pos": None,
                "max_pos": None,
                "kwargs": [],
                "ret_types": [],
                "fmt": "",
            }
        return call_sigs[key]

    def _record_call(key: str, node: ast.Call) -> None:
        sig = _ensure_sig(key)
        npos = sum(1 for a in node.args if not isinstance(a, ast.Starred))
        has_star = any(isinstance(a, ast.Starred) for a in node.args)
        if not has_star:
            sig["min_pos"] = npos if sig["min_pos"] is None else min(sig["min_pos"], npos)
            sig["max_pos"] = npos if sig["max_pos"] is None else max(sig["max_pos"], npos)
        for kw in node.keywords:
            if kw.arg and kw.arg not in sig["kwargs"]:
                sig["kwargs"].append(kw.arg)

    def _record_ret(key: str, rtype: str, fmt: str = "") -> None:
        if not rtype:
            return
        sig = _ensure_sig(key)
        if rtype not in sig["ret_types"]:
            sig["ret_types"].append(rtype)
        if fmt and not sig["fmt"]:
            sig["fmt"] = fmt

    def _record_attr(cls_name: str, attr: str, *, mode: str, rtype: str = "") -> None:
        bucket = cls_attrs.setdefault(cls_name, {}).setdefault(
            attr, {"read": False, "writable": False, "readonly": False, "types": []}
        )
        if mode == "read":
            bucket["read"] = True
        elif mode == "write":
            bucket["writable"] = True
        elif mode == "readonly":
            bucket["readonly"] = True
        if rtype and rtype not in bucket["types"]:
            bucket["types"].append(rtype)

    def _call_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    def _full_attr_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = _full_attr_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return ""

    def _expr_key(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = _expr_key(node.value)
            return f"{base}.{node.attr}" if base else ""
        return ""

    def _literal_type(node: ast.expr, local_types: dict[str, str]) -> tuple[str, str]:
        if isinstance(node, ast.Constant):
            val = node.value
            if val is None:
                return "None", ""
            if isinstance(val, bool):
                return "bool", ""
            if isinstance(val, int):
                return "int", ""
            if isinstance(val, float):
                return "float", ""
            if isinstance(val, str):
                fmt = ""
                if (
                    len(val) <= 32
                    and any(c.isdigit() for c in val)
                    and any(c in "-():/" for c in val)
                    and _re.fullmatch(r"[\d\s()\-.:/+]+", val)
                ):
                    fmt = _re.sub(r"\d", "#", val)
                return "str", fmt
            return type(val).__name__, ""
        if isinstance(node, ast.List):
            elem_types = {_literal_type(e, local_types)[0] for e in node.elts[:8]}
            elem_types.discard("")
            if len(elem_types) == 1:
                return f"list[{next(iter(elem_types))}]", ""
            return "list", ""
        if isinstance(node, ast.Tuple):
            return "tuple", ""
        if isinstance(node, ast.Dict):
            return "dict", ""
        if isinstance(node, ast.Set):
            return "set", ""
        if isinstance(node, ast.Name) and node.id in local_types:
            return local_types[node.id], ""
        return "", ""

    for t_file in tests:
        try:
            tree = ast.parse(
                t_file.read_text(encoding="utf-8", errors="replace"),
                filename=str(t_file),
            )
        except (OSError, SyntaxError):
            continue

        local_to_exported: dict[str, str] = {}
        mod_aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == mod or node.module.startswith(mod + "."):
                    for alias in node.names:
                        _add_import(alias.name)
                        if alias.name != "*":
                            local_to_exported[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == mod or alias.name.startswith(mod + "."):
                        mod_aliases.add(alias.asname or alias.name.split(".")[0])

        # Detect helper methods that instantiate and return an imported class
        helper_returns: dict[str, str] = {}
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            tmp_inst: dict[str, str] = {}
            for st in fn.body:
                if isinstance(st, ast.Assign) and isinstance(st.value, ast.Call):
                    cn = _call_name(st.value.func)
                    if isinstance(st.value.func, ast.Name) and cn in local_to_exported:
                        for tgt in st.targets:
                            if isinstance(tgt, ast.Name):
                                tmp_inst[tgt.id] = local_to_exported[cn]
                elif isinstance(st, ast.Return) and st.value is not None:
                    if isinstance(st.value, ast.Name) and st.value.id in tmp_inst:
                        helper_returns[fn.name] = tmp_inst[st.value.id]
                    elif (
                        isinstance(st.value, ast.Call)
                        and isinstance(st.value.func, ast.Name)
                        and st.value.func.id in local_to_exported
                    ):
                        helper_returns[fn.name] = local_to_exported[st.value.func.id]

        def _resolve_receiver(
            expr: ast.expr, local_inst: dict[str, str]
        ) -> str:
            ek = _expr_key(expr)
            if ek and ek in local_inst:
                return local_inst[ek]
            if isinstance(expr, ast.Call):
                if isinstance(expr.func, ast.Name) and expr.func.id in local_to_exported:
                    return local_to_exported[expr.func.id]
                if (
                    isinstance(expr.func, ast.Attribute)
                    and isinstance(expr.func.value, ast.Name)
                    and expr.func.value.id in mod_aliases
                ):
                    return expr.func.attr
                if isinstance(expr.func, ast.Attribute) and expr.func.attr in helper_returns:
                    return helper_returns[expr.func.attr]
            return ""

        def _call_target_key(
            call: ast.Call, local_inst: dict[str, str]
        ) -> tuple[str, str]:
            """Return (sig_key, cls_if_method)."""
            if isinstance(call.func, ast.Name) and call.func.id in local_to_exported:
                return local_to_exported[call.func.id], ""
            if isinstance(call.func, ast.Attribute):
                if (
                    isinstance(call.func.value, ast.Name)
                    and call.func.value.id in mod_aliases
                ):
                    _add_mod_attr(f"{mod}.{call.func.attr}")
                    return f"{mod}.{call.func.attr}", ""
                cls_name = _resolve_receiver(call.func.value, local_inst)
                if cls_name:
                    method = call.func.attr
                    mkey = f"{cls_name}.{method}"
                    if method not in cls_methods.setdefault(cls_name, []):
                        cls_methods[cls_name].append(method)
                    return mkey, cls_name
            return "", ""

        def _attr_target_key(
            node: ast.Attribute, local_inst: dict[str, str]
        ) -> tuple[str, str]:
            """Return (cls_name, attr_name) for instance attribute access."""
            if isinstance(node.value, ast.Name) and node.value.id in mod_aliases:
                _add_mod_attr(f"{mod}.{node.attr}")
                return "", ""
            cls_name = _resolve_receiver(node.value, local_inst)
            if cls_name and not node.attr.startswith("__"):
                return cls_name, node.attr
            return "", ""

        def _scan_expr(
            expr: ast.expr,
            local_inst: dict[str, str],
            local_types: dict[str, str],
        ) -> None:
            for sub in ast.walk(expr):
                if isinstance(sub, ast.Call):
                    fname = _full_attr_name(sub.func)
                    if fname == "patch" or fname.endswith(".patch"):
                        if (
                            sub.args
                            and isinstance(sub.args[0], ast.Constant)
                            and isinstance(sub.args[0].value, str)
                        ):
                            _add_patch(sub.args[0].value)
                    tkey, _ = _call_target_key(sub, local_inst)
                    if tkey:
                        _record_call(tkey, sub)
                elif isinstance(sub, ast.Attribute) and isinstance(sub.ctx, ast.Load):
                    if isinstance(sub.value, ast.Name) and sub.value.id in mod_aliases:
                        _add_mod_attr(f"{mod}.{sub.attr}")

            # Instance attribute reads (skip direct Call.func targets and Store targets)
            call_funcs = {
                id(n.func)
                for n in ast.walk(expr)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            }
            for sub in ast.walk(expr):
                if (
                    isinstance(sub, ast.Attribute)
                    and id(sub) not in call_funcs
                    and isinstance(sub.ctx, ast.Load)
                ):
                    cls_n, attr_n = _attr_target_key(sub, local_inst)
                    if cls_n and attr_n:
                        _record_attr(cls_n, attr_n, mode="read")

        def _handle_assert(
            call: ast.Call,
            local_inst: dict[str, str],
            local_types: dict[str, str],
            local_attr_reads: dict[str, tuple[str, str]],
            local_call_sources: dict[str, str],
            exc_vars: dict[str, str],
            last_exc_type: str,
        ) -> None:
            cname = _call_name(call.func)
            for kw in call.keywords:
                if (
                    kw.arg == "match"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    exc_t = _call_name(call.args[0]) if call.args else ""
                    _add_error(kw.value.value, exc_t)
            if cname in ("assertRaisesWithMessage", "assertRaisesRegex", "assertRaisesRegexp"):
                exc_t = _call_name(call.args[0]) if call.args else ""
                if (
                    len(call.args) >= 2
                    and isinstance(call.args[1], ast.Constant)
                    and isinstance(call.args[1].value, str)
                ):
                    _add_error(call.args[1].value, exc_t)
                elif exc_t:
                    _add_exc_type(exc_t)
                return
            if cname in ("assertRaises", "raises") and call.args:
                _add_exc_type(_call_name(call.args[0]))
                return
            if cname == "assertEqual" and len(call.args) >= 2:
                lhs, rhs = call.args[0], call.args[1]
                lhs_src = ast.unparse(lhs) if hasattr(ast, "unparse") else ""
                if "exception.args" in lhs_src or "str(err" in lhs_src or ".exception" in lhs_src:
                    if isinstance(rhs, ast.Constant) and isinstance(rhs.value, str):
                        ev_name = (
                            lhs_src[4:].split(".")[0] if lhs_src.startswith("str(")
                            else lhs_src.split(".")[0]
                        )
                        exc_t = exc_vars.get(ev_name, last_exc_type)
                        _add_error(rhs.value, exc_t)
                        return
                # Pair assertEqual(call/attr, literal) in either argument order
                for subj, obj in ((lhs, rhs), (rhs, lhs)):
                    lt, fmt = _literal_type(obj, local_types)
                    if not lt:
                        continue
                    if isinstance(subj, ast.Call):
                        tkey, _ = _call_target_key(subj, local_inst)
                        if tkey:
                            use_fmt = fmt if ("pretty" in tkey or "format" in tkey) else ""
                            _record_ret(tkey, lt, use_fmt)
                    elif isinstance(subj, ast.Attribute):
                        cls_n, attr_n = _attr_target_key(subj, local_inst)
                        if cls_n and attr_n:
                            _record_attr(cls_n, attr_n, mode="read", rtype=lt)
                    elif isinstance(subj, ast.Name):
                        if subj.id in local_call_sources:
                            _record_ret(local_call_sources[subj.id], lt)
                        if subj.id in local_attr_reads:
                            cn, an = local_attr_reads[subj.id]
                            _record_attr(cn, an, mode="read", rtype=lt)

        def _walk_stmts(
            stmts: list[ast.stmt],
            local_inst: dict[str, str],
            local_types: dict[str, str],
            local_attr_reads: dict[str, tuple[str, str]],
            local_call_sources: dict[str, str],
            exc_vars: dict[str, str],
            last_exc: list[str],
            in_attr_err: bool = False,
        ) -> None:
            for st in stmts:
                if isinstance(st, ast.Assign):
                    _scan_expr(st.value, local_inst, local_types)
                    for tgt in st.targets:
                        if isinstance(tgt, ast.Attribute):
                            cls_n, attr_n = _attr_target_key(tgt, local_inst)
                            if cls_n and attr_n:
                                _record_attr(
                                    cls_n, attr_n,
                                    mode="readonly" if in_attr_err else "write",
                                )
                        elif isinstance(tgt, ast.Name):
                            lt, _ = _literal_type(st.value, local_types)
                            if lt:
                                local_types[tgt.id] = lt
                            recv = _resolve_receiver(st.value, local_inst)
                            if recv and isinstance(st.value, ast.Call):
                                local_inst[tgt.id] = recv
                            elif isinstance(st.value, ast.Call):
                                tkey, _ = _call_target_key(st.value, local_inst)
                                if tkey:
                                    local_call_sources[tgt.id] = tkey
                            elif isinstance(st.value, ast.Attribute):
                                cn, an = _attr_target_key(st.value, local_inst)
                                if cn and an:
                                    local_attr_reads[tgt.id] = (cn, an)
                elif isinstance(st, ast.AnnAssign) and st.value is not None:
                    _scan_expr(st.value, local_inst, local_types)
                    if isinstance(st.target, ast.Attribute):
                        cls_n, attr_n = _attr_target_key(st.target, local_inst)
                        if cls_n and attr_n:
                            _record_attr(
                                cls_n, attr_n,
                                mode="readonly" if in_attr_err else "write",
                            )
                elif isinstance(st, ast.Expr):
                    _scan_expr(st.value, local_inst, local_types)
                    if isinstance(st.value, ast.Call):
                        _handle_assert(
                            st.value, local_inst, local_types,
                            local_attr_reads, local_call_sources,
                            exc_vars, last_exc[0],
                        )
                elif isinstance(st, ast.Assert):
                    _scan_expr(st.test, local_inst, local_types)
                    if st.msg is not None:
                        _scan_expr(st.msg, local_inst, local_types)
                elif isinstance(st, ast.With):
                    child_attr_err = in_attr_err
                    for item in st.items:
                        ctx_e = item.context_expr
                        _scan_expr(ctx_e, local_inst, local_types)
                        if isinstance(ctx_e, ast.Call):
                            _handle_assert(
                                ctx_e, local_inst, local_types,
                                local_attr_reads, local_call_sources,
                                exc_vars, last_exc[0],
                            )
                            cn = _call_name(ctx_e.func)
                            if cn in ("assertRaises", "raises", "assertRaisesRegex", "assertRaisesRegexp") and ctx_e.args:
                                exc_t = _call_name(ctx_e.args[0])
                                if exc_t:
                                    last_exc[0] = exc_t
                                    if exc_t == "AttributeError":
                                        child_attr_err = True
                                    if isinstance(item.optional_vars, ast.Name):
                                        exc_vars[item.optional_vars.id] = exc_t
                    _walk_stmts(
                        st.body, local_inst, local_types,
                        local_attr_reads, local_call_sources,
                        exc_vars, last_exc, child_attr_err,
                    )
                elif isinstance(st, (ast.For, ast.AsyncFor, ast.While, ast.If)):
                    for sub_body in (getattr(st, "body", []), getattr(st, "orelse", [])):
                        if sub_body:
                            _walk_stmts(
                                sub_body, local_inst, local_types,
                                local_attr_reads, local_call_sources,
                                exc_vars, last_exc, in_attr_err,
                            )
                elif isinstance(st, ast.Try):
                    for sub_body in (st.body, st.orelse, st.finalbody):
                        if sub_body:
                            _walk_stmts(
                                sub_body, local_inst, local_types,
                                local_attr_reads, local_call_sources,
                                exc_vars, last_exc, in_attr_err,
                            )

        # Collect decorator patches + scan each test function sequentially
        for fn in ast.walk(tree):
            if isinstance(fn, ast.FunctionDef):
                for dec in getattr(fn, "decorator_list", ()):
                    _scan_expr(dec, {}, {})
                _walk_stmts(fn.body, {}, {}, {}, {}, {}, [""], False)
            elif isinstance(fn, ast.ClassDef):
                for dec in getattr(fn, "decorator_list", ()):
                    _scan_expr(dec, {}, {})

    has_any = (
        imported_names or module_attrs or call_sigs
        or cls_attrs or paired_errors or unpaired_exc_types or patch_targets
    )
    if not has_any:
        return ""

    def _collapse_rtypes(raw_types: list[str]) -> list[str]:
        non_none = [t for t in raw_types if t != "None"] or raw_types
        if any(t.startswith("list[") for t in non_none):
            non_none = [t for t in non_none if t != "list"]
        return non_none

    def _fmt_call(name: str, sig: dict) -> str:
        parts: list[str] = []
        lo, hi = sig["min_pos"], sig["max_pos"]
        if lo is not None and hi is not None and hi > 0:
            unit = "arg" if lo == hi == 1 else "args"
            parts.append(f"{lo} {unit}" if lo == hi else f"{lo}..{hi} {unit}")
        for kw in sig["kwargs"][:5]:
            parts.append(f"{kw}=...")
        args_s = ", ".join(parts)
        rtypes = _collapse_rtypes(sig["ret_types"])
        ret_s = f" -> {' | '.join(rtypes[:2])}" if rtypes else ""
        fmt_s = f" (format {sig['fmt']!r})" if sig.get("fmt") else ""
        return f"`{name}({args_s}){ret_s}`{fmt_s}"

    lines = ["[Test suite API contract] (contract-v2)"]
    if imported_names:
        lines.append(f"- Imported from `{mod}`: {', '.join(imported_names)}")
    if module_attrs:
        lines.append(f"- Module attributes: {', '.join(module_attrs[:12])}")

    # Top-level callables (exclude classes that have instance APIs unless informative)
    top_calls = [
        _fmt_call(sym, call_sigs[sym])
        for sym in imported_names
        if sym in call_sigs and sym not in cls_attrs and sym not in cls_methods
    ]
    if top_calls:
        lines.append(f"- Signatures & return types: {', '.join(top_calls[:12])}")

    for cls_name in sorted(set(cls_attrs) | set(cls_methods)):
        c_parts: list[str] = []
        if cls_name in call_sigs:
            c_parts.append(_fmt_call(cls_name, call_sigs[cls_name]).strip("`"))
        attr_items: list[str] = []
        for aname, meta in sorted((cls_attrs.get(cls_name) or {}).items()):
            tags: list[str] = []
            if meta["types"]:
                rtypes = [t for t in meta["types"] if t != "None"] or meta["types"]
                tags.append(" | ".join(rtypes[:2]))
            if meta["readonly"]:
                tags.append("read-only property")
            elif meta["writable"]:
                tags.append("writable")
            tag_s = f" ({', '.join(tags)})" if tags else ""
            attr_items.append(f".{aname}{tag_s}")
        if attr_items:
            c_parts.append("attrs: " + ", ".join(attr_items[:10]))
        meth_items: list[str] = []
        for mname in cls_methods.get(cls_name, [])[:10]:
            mkey = f"{cls_name}.{mname}"
            msig = call_sigs.get(mkey)
            meth_items.append(
                _fmt_call(f".{mname}", msig) if msig else f"`.{mname}()`"
            )
        if meth_items:
            c_parts.append("methods: " + ", ".join(meth_items))
        if c_parts:
            lines.append(f"- Instance API `{cls_name}`: {'; '.join(c_parts)}")

    if paired_errors:
        lines.append(f"- Expected exception message patterns: {', '.join(paired_errors[:16])}")
    extra_exc = [e for e in unpaired_exc_types if e not in paired_exc_names]
    if extra_exc:
        lines.append(f"- Expected exception types: {', '.join(extra_exc[:8])}")
    if patch_targets:
        lines.append(f"- Mocked targets in tests: {', '.join(repr(p) for p in patch_targets[:8])}")
    return "\n".join(lines)


def import_exercise(src: Path, out: Path, slug: str) -> dict:
    ex = src / slug
    impl = sorted(p for p in ex.glob("*.py") if not p.name.endswith("_test.py"))
    tests = sorted(ex.glob("*_test.py"))
    if not impl or not tests:
        raise SystemExit(f"{slug}: expected one implementation and one test file")
    dst = out / f"polyglot_{slug.replace('-', '_')}"
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "repo").mkdir(parents=True)
    (dst / "tests").mkdir()
    for p in ex.iterdir():
        if _is_skipped_name(p.name):
            continue
        if p.is_dir():
            shutil.copytree(p, dst / "repo" / p.name, ignore=_ignore)
        elif p.is_file():
            shutil.copy2(p, dst / "repo" / p.name)
    for p in tests:
        shutil.copy2(p, dst / "tests" / p.name)
    mod = slug.replace("-", "_")
    files_list = ", ".join(p.name for p in impl)
    contract = _extract_test_contract(tests, slug)
    contract_block = f"\n\n{contract}" if contract else ""
    prompt = _read_instructions(ex)
    prompt += (
        f"{contract_block}\n\n"
        f"Implement the solution in `{files_list}` (starter stub is already in the workspace). "
        f"Verify your implementation before completing by importing `{mod}` and running "
        f"assertions on the examples and edge cases (e.g. via `python -c` or `run_python`). "
        f"Do not modify or create test files."
    )
    check = "python -m pytest -q " + " ".join(t.name for t in tests)
    data = {"id": f"polyglot_{slug.replace('-', '_')}", "prompt": prompt,
            "check": check, "timeout_s": 300}
    (dst / "task.json").write_text(json.dumps(data, indent=1) + "\n",
                                    encoding="utf-8")
    return {"id": data["id"], "source": slug, "tests": [t.name for t in tests]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True,
                    help="polyglot-benchmark/python/exercises/practice")
    ap.add_argument("--out", default="bench/tasks_polyglot")
    ap.add_argument("--names", default=",".join(DEFAULT_NAMES))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    src = Path(args.source)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.names.strip().lower() in ("all", "*"):
        names = sorted([d.name for d in src.iterdir() if d.is_dir() and not d.name.startswith(".")])
    else:
        names = [n.strip() for n in args.names.split(",") if n.strip()]
    if args.limit and args.limit > 0:
        names = names[:args.limit]
    imported = []
    for slug in names:
        if not (src / slug).is_dir():
            raise SystemExit(f"exercise not found: {src / slug}")
        imported.append(import_exercise(src, out, slug))
    manifest = {
        "source": "Aider-AI/polyglot-benchmark",
        "source_path": str(src),
        "license_note": "Exercism exercise content; see the source repository.",
        "tasks": imported,
    }
    (out / "SOURCE.json").write_text(json.dumps(manifest, indent=1) + "\n",
                                     encoding="utf-8")
    print(f"imported {len(imported)} tasks into {out}")
    for row in imported:
        print(" ", row["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

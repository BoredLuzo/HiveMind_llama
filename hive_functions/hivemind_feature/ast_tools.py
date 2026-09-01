
from __future__ import annotations
import ast
import re
import textwrap
from pathlib import Path


_JS_TS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_REFERENCE_EXTS = {
    ".py", ".pyi",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".cs",
    ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp",
    ".go", ".rs", ".rb", ".php", ".swift", ".scala",
}
_TEXT_FALLBACK_EXTS = {
    "", ".txt", ".md", ".markdown", ".rst",
    ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg",
    ".vue", ".svelte", ".astro", ".html", ".htm",
    ".css", ".scss", ".less", ".xml", ".svg",
    ".sql", ".sh", ".bash", ".bat", ".cmd", ".ps1", ".lua", ".pl",
}
_REFERENCE_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".idea", ".vscode",
}


def _detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _line_indent(line: str) -> str:
    m = re.match(r"[ \t]*", line)
    return m.group(0) if m else ""


def _python_header(lines: list[str], lineno: int, max_lines: int = 24) -> str:
    i = max(0, lineno - 1)
    parts: list[str] = []
    for j in range(i, min(len(lines), i + max_lines)):
        raw = lines[j].rstrip("\r\n")
        parts.append(raw.strip())
        if ":" in raw:
            break
    merged = " ".join(p for p in parts if p)
    merged = re.sub(r"\s+", " ", merged).strip()
    return merged[:220]


def _iter_py_assign_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                names.append(t.id)
    elif isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def _iter_reference_files(base: Path, max_files: int) -> list[Path]:
    if base.is_file():
        return [base]

    files: list[Path] = []
    for p in base.rglob("*"):
        if len(files) >= max_files:
            break
        if not p.is_file():
            continue
        if any(part in _REFERENCE_SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in _REFERENCE_EXTS:
            files.append(p)
        elif p.suffix.lower() in _TEXT_FALLBACK_EXTS:
            files.append(p)  # use-only Scan (AUDIT-FIX M6)
    return files


def _looks_like_definition_line(line: str, symbol: str, ext: str) -> bool:
    s = line.strip()
    if not s:
        return False

    esc = re.escape(symbol)
    if ext == ".py":
        return bool(re.search(rf"^(class|def|async\s+def)\s+{esc}\b", s))
    if ext in _JS_TS_EXTS:
        return bool(re.search(
            rf"^(?:export\s+)?(?:default\s+)?(?:class|function|const|let|var|interface|type)\s+{esc}\b",
            s,
        ))
    if ext in {".java", ".kt", ".kts", ".cs", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".go", ".rs", ".swift", ".scala"}:
        return bool(re.search(rf"\b(class|struct|interface|enum|fn|func|def)\b.*\b{esc}\b", s))
    return False


def find_references_report(path_or_root: str | Path, symbol: str, max_items: int = 160, max_files: int = 1200) -> str:
    """
    Lightweight LSP-like reference finder.

    Scans source files and returns compact hits with line numbers, tagged as
    definition/use via language heuristics.
    """
    base = Path(path_or_root)
    target = (symbol or "").strip()
    if not target:
        return "[find_references error: symbol is empty]"
    if not re.match(r"^[A-Za-z_$][A-Za-z0-9_$\.]*$", target):
        return f"[find_references error: invalid symbol '{target}']"
    if not base.exists():
        return f"[find_references error: path not found: {base}]"

    short = target.split(".")[-1]
    exact_pat = re.compile(re.escape(target))
    short_pat = re.compile(rf"\b{re.escape(short)}\b")

    files = _iter_reference_files(base, max_files=max(1, int(max_files)))
    if not files:
        return f"[references] symbol={target}\n(no source files found under {base})"

    max_items = max(20, min(2000, int(max_items or 160)))
    out: list[str] = [f"[references] symbol={target} root={base}"]
    hits = 0
    scanned = 0

    for fp in files:
        if hits >= max_items:
            break
        scanned += 1
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        ext = fp.suffix.lower()
        lines = text.splitlines()
        for lineno, raw in enumerate(lines, 1):
            if hits >= max_items:
                break

            line = raw.strip()
            if not line:
                continue

            has_exact = bool(exact_pat.search(line))
            has_short = bool(short_pat.search(line))
            if not has_exact and not has_short:
                continue

            if target != short and not has_exact and _looks_like_definition_line(line, short, ext) is False:
                # Qualified symbols should prefer exact hits; allow short-form only for definitions.
                continue

            kind = "def" if _looks_like_definition_line(line, short, ext) else "use"
            try:
                rel = fp.relative_to(base)
            except Exception:
                rel = fp
            snippet = re.sub(r"\s+", " ", line).strip()[:220]
            out.append(f"{kind} {rel}:{lineno}: {snippet}")
            hits += 1

    if hits == 0:
        out.append("(no references found)")
    out.append(f"[summary] files_scanned={scanned} hits={hits}")
    return "\n".join(out)


def _python_signatures(path: Path, text: str, max_items: int) -> str:
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return f"[get_signatures error: Python parse failed in {path} at line {e.lineno}: {e.msg}]"

    lines = text.splitlines(keepends=True)
    out: list[str] = [f"[signatures] {path}", "language=python"]
    count = 0

    for node in tree.body:
        if count >= max_items:
            break

        if isinstance(node, ast.ClassDef):
            header = _python_header(lines, node.lineno)
            out.append(f"class {node.name} @L{node.lineno}: {header}")
            count += 1
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if count >= max_items:
                        break
                    c_header = _python_header(lines, child.lineno)
                    out.append(f"  method {node.name}.{child.name} @L{child.lineno}: {c_header}")
                    count += 1
                elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                    for var_name in _iter_py_assign_names(child):
                        if count >= max_items:
                            break
                        v_header = _python_header(lines, child.lineno)
                        out.append(f"  variable {node.name}.{var_name} @L{child.lineno}: {v_header}")
                        count += 1

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            header = _python_header(lines, node.lineno)
            out.append(f"function {node.name} @L{node.lineno}: {header}")
            count += 1

        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for var_name in _iter_py_assign_names(node):
                if count >= max_items:
                    break
                v_header = _python_header(lines, node.lineno)
                out.append(f"variable {var_name} @L{node.lineno}: {v_header}")
                count += 1

    if count == 0:
        out.append("(no classes/functions/variables found)")
    return "\n".join(out)


def _js_ts_signatures(path: Path, text: str, max_items: int) -> str:
    lines = text.splitlines()
    out: list[str] = [f"[signatures] {path}", "language=js-ts-heuristic"]
    count = 0

    class_re = re.compile(r"^(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")
    fn_re = re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)")
    arrow_re = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>")
    fnexpr_re = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function\s*\(([^)]*)\)")
    var_re = re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\b")
    method_re = re.compile(
        r"^(?:(?:public|private|protected|static|readonly|async|get|set)\s+)*"
        r"([A-Za-z_$][\w$]*)\s*\(([^)]*)\)\s*(?::[^\{]+)?\{?"
    )

    keywords = {"if", "for", "while", "switch", "catch", "with", "return"}
    brace_level = 0
    active_class: str | None = None
    class_base_level: int = -1

    for idx, raw in enumerate(lines, 1):
        if count >= max_items:
            break
        stripped = raw.strip()
        if not stripped or stripped.startswith("//"):
            brace_level += raw.count("{") - raw.count("}")
            continue

        m_class = class_re.match(stripped)
        if m_class:
            cls = m_class.group(1)
            out.append(f"class {cls} @L{idx}: {stripped[:220]}")
            count += 1
            active_class = cls
            class_base_level = brace_level

        inside_class = active_class is not None and brace_level > class_base_level
        if inside_class and count < max_items:
            m_method = method_re.match(stripped)
            if m_method:
                m_name = m_method.group(1)
                if m_name not in keywords:
                    out.append(f"  method {active_class}.{m_name} @L{idx}: {stripped[:220]}")
                    count += 1

        if brace_level == 0 and count < max_items:
            m_fn = fn_re.match(stripped)
            if m_fn:
                out.append(f"function {m_fn.group(1)} @L{idx}: {stripped[:220]}")
                count += 1
            else:
                m_arrow = arrow_re.match(stripped)
                if m_arrow:
                    out.append(f"function {m_arrow.group(1)} @L{idx}: {stripped[:220]}")
                    count += 1
                else:
                    m_fnexpr = fnexpr_re.match(stripped)
                    if m_fnexpr:
                        out.append(f"function {m_fnexpr.group(1)} @L{idx}: {stripped[:220]}")
                        count += 1
                    else:
                        m_var = var_re.match(stripped)
                        if m_var:
                            out.append(f"variable {m_var.group(1)} @L{idx}: {stripped[:220]}")
                            count += 1

        brace_level += raw.count("{") - raw.count("}")
        if active_class is not None and brace_level <= class_base_level:
            active_class = None
            class_base_level = -1

    if count == 0:
        out.append("(no classes/functions/variables found)")
    return "\n".join(out)


def get_signatures_report(filepath: str | Path, max_items: int = 400) -> str:
    p = Path(filepath)
    if not p.exists():
        return f"[get_signatures error: file not found: {p}]"
    if not p.is_file():
        return f"[get_signatures error: not a file: {p}]"

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"[get_signatures error: {e}]"

    ext = p.suffix.lower()
    if ext == ".py":
        return _python_signatures(p, text, max_items=max(20, int(max_items)))
    if ext in _JS_TS_EXTS:
        return _js_ts_signatures(p, text, max_items=max(20, int(max_items)))

    lines = text.splitlines()
    out = [f"[signatures] {p}", f"language=generic({ext or 'unknown'})"]
    count = 0
    generic_re = re.compile(r"^\s*(class|def|async\s+def|function)\b")
    for idx, line in enumerate(lines, 1):
        if generic_re.search(line):
            out.append(f"L{idx}: {line.strip()[:220]}")
            count += 1
            if count >= max(20, int(max_items)):
                break
    if count == 0:
        out.append("(no signatures found for this extension)")
    return "\n".join(out)


def _find_python_target(tree: ast.Module, target_type: str, target_name: str) -> tuple[ast.AST | None, str, str]:
    target_type = (target_type or "").strip().lower()
    target_name = (target_name or "").strip()

    if target_type not in {"function", "class", "variable"}:
        return None, "", "[edit_ast error: target_type must be one of function|class|variable]"
    if not target_name:
        return None, "", "[edit_ast error: target_name is empty]"

    want_qualified = "." in target_name
    candidates: list[tuple[ast.AST, str]] = []

    def _match_name(actual: str) -> bool:
        if want_qualified:
            return actual == target_name
        short = actual.split(".")[-1]
        return short == target_name

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if target_type == "class" and _match_name(node.name):
                candidates.append((node, node.name))

            for child in node.body:
                if target_type == "function" and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qual = f"{node.name}.{child.name}"
                    if _match_name(qual):
                        candidates.append((child, qual))
                if target_type == "variable" and isinstance(child, (ast.Assign, ast.AnnAssign)):
                    for var_name in _iter_py_assign_names(child):
                        qual = f"{node.name}.{var_name}"
                        if _match_name(qual):
                            candidates.append((child, qual))

        elif target_type == "function" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _match_name(node.name):
                candidates.append((node, node.name))

        elif target_type == "variable" and isinstance(node, (ast.Assign, ast.AnnAssign)):
            for var_name in _iter_py_assign_names(node):
                if _match_name(var_name):
                    candidates.append((node, var_name))

    if not candidates:
        return None, "", f"[edit_ast error: target not found: {target_type} '{target_name}']"

    if len(candidates) > 1 and not want_qualified:
        choices = ", ".join(sorted(q for _, q in candidates)[:8])
        return None, "", (
            f"[edit_ast error: target '{target_name}' is ambiguous. "
            f"Use a qualified name like ClassName.{target_name}. Matches: {choices}]"
        )

    chosen, qual_name = candidates[0]
    if not hasattr(chosen, "lineno") or not hasattr(chosen, "end_lineno"):
        return None, "", "[edit_ast error: AST node has no line boundaries]"
    return chosen, qual_name, ""


def edit_ast_file(filepath: str | Path, target_type: str, target_name: str, new_code: str) -> tuple[bool, str]:
    p = Path(filepath)
    if not p.exists():
        return False, f"[edit_ast error: file not found: {p}]"
    if not p.is_file():
        return False, f"[edit_ast error: not a file: {p}]"
    if p.suffix.lower() != ".py":
        return False, (
            f"[edit_ast error: currently only .py is supported (got '{p.suffix or '(none)'}'). "
            "Use edit_file for non-Python files.]"
        )
    if not isinstance(new_code, str) or not new_code.strip():
        return False, "[edit_ast error: new_code is empty]"

    try:
        original = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return False, f"[edit_ast error: failed to read file: {e}]"

    try:
        tree = ast.parse(original)
    except SyntaxError as e:
        return False, f"[edit_ast error: cannot parse python file at line {e.lineno}: {e.msg}]"

    node, qual_name, err = _find_python_target(tree, target_type, target_name)
    if err:
        return False, err
    assert node is not None

    start = int(getattr(node, "lineno"))
    end = int(getattr(node, "end_lineno"))
    lines = original.splitlines(keepends=True)
    if start < 1 or end < start or end > len(lines):
        return False, "[edit_ast error: invalid node line range]"

    old_line = lines[start - 1]
    indent = _line_indent(old_line)
    normalized = textwrap.dedent(new_code).strip("\n")
    replacement_lines = normalized.split("\n")
    if indent:
        replacement_lines = [
            (indent + ln if ln.strip() else "")
            for ln in replacement_lines
        ]
    newline = _detect_newline(original)
    replacement = newline.join(replacement_lines) + newline

    patched = "".join(lines[: start - 1]) + replacement + "".join(lines[end:])

    try:
        ast.parse(patched)
    except SyntaxError as e:
        return (
            False,
            f"[edit_ast error: replacement produced invalid Python at line {e.lineno}: {e.msg}]",
        )

    tmp = p.with_suffix(p.suffix + ".tmp")
    try:
        tmp.write_text(patched, encoding="utf-8", newline="")
        tmp.replace(p)
    except Exception as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False, f"[edit_ast error: failed to write file: {e}]"

    return True, f"[edit_ast: {p} replaced {target_type} {qual_name} (lines {start}-{end})]"
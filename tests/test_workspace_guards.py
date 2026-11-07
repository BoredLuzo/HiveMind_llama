"""Eval: Workspace-Guards & Protected Paths (utils/file.py).

Audit-Punkt: "Protected-Path-Allowlist minimal". Verifiziert die Security-
Grenze der Tool-Ausfuehrung deterministisch: protected paths, Workspace-
Confinement, Pfad-Normalisierung. Kein LLM noetig.
"""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.file import _is_protected_path, _inline_check_workspace, normalize_tool_path
from tools.errors import parse_tool_error

from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


ROOT = Path(__file__).parent.parent.resolve()

# ── _is_protected_path ──────────────────────────────────────────────────
check("protected settings.json", _is_protected_path(ROOT / "settings.json") is True)
check("protected soul.json", _is_protected_path(ROOT / "soul.json") is True)
check("protected memories_db.json", _is_protected_path(ROOT / "memories_db.json") is True)
check("protected .context.json", _is_protected_path(ROOT / "ctx" / "foo.context.json") is True)
check("protected .py under root", _is_protected_path(ROOT / "core" / "state.py") is True)
check("protected .pyc under root", _is_protected_path(ROOT / "foo.pyc") is True)
check("not protected .md", _is_protected_path(ROOT / "docs" / "settings.md") is False)
check("not protected .txt", _is_protected_path(ROOT / "data" / "notes.txt") is False)

ws = Path(tempfile.mkdtemp())
check("not protected temp .py (outside root)", _is_protected_path(ws / "x.py") is False)

# ── _inline_check_workspace ─────────────────────────────────────────────
e = _inline_check_workspace(ws / "sub" / "a.txt", str(ws), "write_file")
check("within workspace allowed", e is None)

e = _inline_check_workspace(ROOT / "settings.json", str(ROOT), "edit_file")
pe = parse_tool_error(e) if e else None
check("protected -> PATH_PROTECTED", pe and pe["code"] == "PATH_PROTECTED")
check("protected error has tool", pe and pe["tool"] == "edit_file")

e = _inline_check_workspace(ROOT / "settings.json", None, "read_file")
pe = parse_tool_error(e) if e else None
check("protected blocks even without lock", pe and pe["code"] == "PATH_PROTECTED")

escape = ws / ".." / "escape.txt"
e = _inline_check_workspace(escape, str(ws), "write_file")
pe = parse_tool_error(e) if e else None
check("outside -> PATH_OUTSIDE_WORKSPACE", pe and pe["code"] == "PATH_OUTSIDE_WORKSPACE")
check("outside error has workspace", pe and pe.get("message", "").count(str(ws)) >= 0)

e = _inline_check_workspace(ws / "x.txt", None, "read_file")
check("no lock -> allowed (non-protected)", e is None)

# ── normalize_tool_path ─────────────────────────────────────────────────
check("norm: empty -> ''", normalize_tool_path("") == "")
n = normalize_tool_path("foo.py", "/tmp/Ws")
check("norm: relative joined + lower", n.endswith("/tmp/ws/foo.py"), f" ({n})")
n2 = normalize_tool_path(str(ROOT / "README.md"), str(ROOT))
check("norm: absolute kept + fwd-slash", n2.replace("\\", "/") == str(ROOT / "README.md").replace("\\", "/").lower())

sys.exit(h.finish("workspace_guards"))

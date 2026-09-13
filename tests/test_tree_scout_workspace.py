"""Tree-Scout workspace priority (2026-09-13).

Live: a run on workspace C:\\Users\\NtheP\\Desktop\\Test1 (source=force_ui)
logged "tree_scout Stufe 1 (Regex): C:\\Users\\Nicolas\\Desktop\\MyOwnTetris"
— a path from the copied task text won over the resolved workspace, and the
repo map analyzed the wrong (on this machine: non-existent) folder. Two
defects, both fixed:
1. _extract_path_from_text returned non-existent Windows paths (the
   `elif "\\\\" in candidate` escape hatch).
2. get_workspace_tree let the text-extracted path WIN over ws_str.

New priority: resolved ws_str wins whenever it is a valid dir; text
extraction is only a fallback for workspace-less chats.

Run: python tests/test_tree_scout_workspace.py
Exit 0 = all pass, Exit 1 = failures.
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hive_functions.tree_scout import _extract_path_from_text, get_workspace_tree

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


def test_nonexistent_win_path_rejected():
    fake = r"C:\Users\__definitely_not_existing__\SomeProject"
    text = f"Implement the task in {fake}"
    r = _extract_path_from_text(text)
    if r is None:
        ok("Nicht existierender Windows-Pfad aus Task-Text wird abgelehnt (None)")
    else:
        fail("nonexistent_rejected", f"returned {r!r}")


def test_existing_dir_and_file_resolution():
    tmp = tempfile.mkdtemp(prefix="hvm_ws_")
    file_path = os.path.join(tmp, "main.py")
    Path(file_path).write_text("x = 1\n", encoding="utf-8")
    r_dir = _extract_path_from_text(f"work in {tmp} please")
    r_file = _extract_path_from_text(f"look at {file_path}")
    if r_dir and os.path.samefile(r_dir, tmp) and r_file and os.path.samefile(r_file, tmp):
        ok("Existierender Dir -> Pfad, Datei -> parent-Dir")
    else:
        fail("existing", f"dir={r_dir!r} file={r_file!r}")


def test_get_workspace_tree_priority():
    ws = tempfile.mkdtemp(prefix="hvm_ws_p_")
    other = tempfile.mkdtemp(prefix="hvm_ws_q_")
    Path(ws, "ws_file.py").write_text("x = 1\n", encoding="utf-8")
    Path(other, "other_file.py").write_text("y = 2\n", encoding="utf-8")
    text = f"continue the project at {other}"
    import asyncio

    tree = asyncio.new_event_loop().run_until_complete(
        get_workspace_tree(task=text, ws_str=ws, enabled=True)
    )
    if "ws_file.py" in tree and "other_file.py" not in tree:
        ok("get_workspace_tree: ws_str gewinnt über Task-Text-Pfad (falscher Ordner ignoriert)")
    else:
        fail("priority", f"tree enthält falschen/keinen Inhalt: {tree[:200]!r}")


def test_text_fallback_without_ws():
    other = tempfile.mkdtemp(prefix="hvm_ws_t_")
    Path(other, "only_file.py").write_text("z = 3\n", encoding="utf-8")
    import asyncio

    tree = asyncio.new_event_loop().run_until_complete(
        get_workspace_tree(task=f"work in {other}", ws_str="", enabled=True)
    )
    if tree and "only_file.py" in tree:
        ok("Ohne ws_str greift der Text-Pfad weiterhin (Fallback erhalten)")
    else:
        fail("fallback", f"tree: {tree[:200]!r}")


if __name__ == "__main__":
    test_nonexistent_win_path_rejected()
    test_existing_dir_and_file_resolution()
    test_get_workspace_tree_priority()
    test_text_fallback_without_ws()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)

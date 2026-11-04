"""send_file tool + gateway /send guards (2026-11-04).

The agent may deliver files only when the Telegram gateway started the
run, only files it wrote itself, only inside the workspace, never secret
names, and never above Telegram's 50 MB document limit.

Run: python tests/test_send_file.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0
skipped = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def skip(name, why=""):
    global skipped
    skipped += 1
    print(f"  SKIP {name}{(' - ' + why) if why else ''}")


def _norm(p: Path) -> str:
    out = str(p.resolve()).replace("\\", "/")
    return out.lower() if os.name == "nt" else out


def main() -> int:
    import tools.definitions as D
    import tools.runner as R
    import hivemind_gateway.commands as C
    import hivemind_gateway.main as M  # noqa: F401  (dispatch imports)
    import core.send_files as SF
    from tools.handlers.send_file import _inline_tool_send_file

    names_plain = {t["function"]["name"]
                   for t in D._get_inline_tools(mode="duo_full")}
    names_telegram = {t["function"]["name"] for t in D._get_inline_tools(
        mode="duo_full", include_send_file=True)}
    check("send_file is hidden from desktop/API runs",
          "send_file" not in names_plain)
    check("send_file is offered to Telegram runs",
          "send_file" in names_telegram)
    check("send_file has a handler", "send_file" in R._INLINE_TOOL_HANDLER_MAP)
    check("send_file is in the duo_full allowlist",
          "send_file" in D._TOOL_MODE_ALLOWLISTS["duo_full"])
    check("the /send command exists", "send" in C.COMMAND_SPEC)
    check("/send asks for its path", C.wants_arg_first("send"))

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        good = ws / "index.html"
        good.write_text("<html>N64</html>", encoding="utf-8")
        (ws / "sub").mkdir()
        (ws / "sub" / "page.html").write_text("nested", encoding="utf-8")
        outside = ws.parent / "outside_send_file.txt"
        outside.write_text("outside", encoding="utf-8")
        try:
            check("a normal workspace file resolves",
                  SF.resolve_workspace_file(ws, "index.html") == good.resolve())
            check("a nested relative path resolves",
                  SF.resolve_workspace_file(ws, "sub/page.html").name == "page.html")
            try:
                SF.resolve_workspace_file(ws, "../outside_send_file.txt")
                check("parent traversal is refused", False)
            except SF.SendFileError:
                check("parent traversal is refused", True)
            try:
                SF.resolve_workspace_file(ws, str(outside))
                check("an absolute outside path is refused", False)
            except SF.SendFileError:
                check("an absolute outside path is refused", True)
            for bad in (".env", "id_rsa", "api_token.txt", ".ssh/config"):
                try:
                    SF.resolve_workspace_file(ws, bad)
                    check(f"secret name refused: {bad}", False)
                except SF.SendFileError:
                    check(f"secret name refused: {bad}", True)
            try:
                SF.resolve_workspace_file(ws, "sub")
                check("a directory is refused", False)
            except SF.SendFileError:
                check("a directory is refused", True)

            data, name = SF.read_sendable(ws, "index.html")
            check("read_sendable returns the bytes",
                  data == b"<html>N64</html>" and name == "index.html")

            check("explicit queued paths win",
                  SF.choose_deliverables(["a.html"], ["b.html"]) == ["a.html"])
            check("one deliverable is auto-picked",
                  SF.choose_deliverables([], ["index.html", "app.py"])
                  == ["index.html"])
            check("multiple deliverables stay manual",
                  SF.choose_deliverables([], ["a.html", "b.pdf"]) == [])
            check("non-deliverable files are ignored",
                  SF.choose_deliverables([], ["app.py", "style.css"]) == [])

            # Canonical spellings: the same file under different forms must
            # collapse to one entry, keeping the first spelling for delivery.
            _abs = str(good)
            _fwd = _abs.replace("\\", "/")
            check("duplicate spellings collapse to one queued entry",
                  SF.choose_deliverables([_abs, _fwd], [],
                                         workspace=str(ws)) == [_abs])
            check("relative and absolute spellings collapse",
                  SF.choose_deliverables(["index.html", _abs], [],
                                         workspace=str(ws)) == ["index.html"])
            check("written candidates collapse canonically",
                  SF.choose_deliverables([], [_abs, _fwd],
                                         workspace=str(ws)) == [_abs])

            # is_written_file matches every spelling of the same written
            # file, because the written set holds the canonical form.
            written = {SF.canonical_workspace_path(str(good), str(ws))}
            check("is_written_file matches the canonical spelling",
                  SF.is_written_file(str(good), ws, written))
            check("is_written_file matches forward-slash spelling",
                  SF.is_written_file(_fwd, ws, written))
            check("is_written_file matches a relative spelling",
                  SF.is_written_file("index.html", ws, written))
            check("is_written_file still refuses an outside path",
                  not SF.is_written_file(str(outside), ws, written))
            if os.name == "nt":
                check("is_written_file matches the MSYS /C:/ spelling",
                      SF.is_written_file("/" + _abs, ws, written))

            _old_max = SF.MAX_SEND_BYTES
            SF.MAX_SEND_BYTES = 8
            try:
                try:
                    SF.read_sendable(ws, "index.html")
                    check("the size cap is enforced", False)
                except SF.SendFileError:
                    check("the size cap is enforced", True)
            finally:
                SF.MAX_SEND_BYTES = _old_max

            # Handler: written-set membership is mandatory.
            tok = R._files_written_in_run.set({_norm(good)})
            try:
                out = asyncio.run(_inline_tool_send_file(
                    {"path": "index.html"}, ws, str(ws)))
                check("handler accepts a file the run wrote",
                      out.startswith("[FILE_QUEUED]"))
            finally:
                R._files_written_in_run.reset(tok)

            tok = R._files_written_in_run.set(set())
            try:
                out = asyncio.run(_inline_tool_send_file(
                    {"path": "index.html"}, ws, str(ws)))
                check("handler rejects an unwritten file",
                      "SEND_FILE_UNTRACKED" in out)
            finally:
                R._files_written_in_run.reset(tok)

            # A relative path and an absolute path to the same written file
            # both pass the handler.
            tok = R._files_written_in_run.set(written)
            try:
                out_rel = asyncio.run(_inline_tool_send_file(
                    {"path": "index.html"}, ws, str(ws)))
                out_abs = asyncio.run(_inline_tool_send_file(
                    {"path": _abs}, ws, str(ws)))
                check("handler passes the relative spelling of a written file",
                      out_rel.startswith("[FILE_QUEUED]"))
                check("handler passes the absolute spelling of the same file",
                      out_abs.startswith("[FILE_QUEUED]"))
            finally:
                R._files_written_in_run.reset(tok)

            # A file produced by run_python/run_bash never passes a write
            # tool; its mtime inside the run epoch is the fallback signal.
            import time as _t
            tok = R._files_written_in_run.set(set())
            etok = R._run_file_epoch.set(_t.time() - 60)
            try:
                out = asyncio.run(_inline_tool_send_file(
                    {"path": "index.html"}, ws, str(ws)))
                check("handler accepts a file written during the run",
                      out.startswith("[FILE_QUEUED]"))
            finally:
                R._run_file_epoch.reset(etok)
                R._files_written_in_run.reset(tok)

            etok = R._run_file_epoch.set(_t.time() + 60)
            try:
                out = asyncio.run(_inline_tool_send_file(
                    {"path": "index.html"}, ws, str(ws)))
                check("a pre-existing file stays blocked",
                      "SEND_FILE_UNTRACKED" in out)
            finally:
                R._run_file_epoch.reset(etok)

            # A symlink that points outside must not escape.
            try:
                link = ws / "escape.html"
                link.symlink_to(outside)
                try:
                    SF.resolve_workspace_file(ws, "escape.html")
                    check("a symlink escape is refused", False)
                except SF.SendFileError:
                    check("a symlink escape is refused", True)
            except (OSError, NotImplementedError):
                # Windows without Developer Mode/privilege cannot create
                # symlinks; Linux CI always can, so the escape check runs
                # there. Report the gap explicitly instead of a silent pass.
                skip("symlink escape", "symlinks not permitted here")
        finally:
            try:
                outside.unlink()
            except OSError:
                pass

    # Gateway delivery: a send_file event uploads via ms.send_document.
    from hivemind_gateway.bridge import RunBridge

    class _Ms:
        def __init__(self):
            self.docs = []
            self.msgs = []

        async def send_document(self, data, filename):
            self.docs.append((filename, data))

        async def send_message(self, text, **kwargs):
            self.msgs.append(text)

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "page.html").write_text("<html>ok</html>", encoding="utf-8")
        br = object.__new__(RunBridge)
        br.ms = _Ms()
        br._sent_files = set()
        asyncio.run(br._deliver_send_file(
            {"path": "page.html", "workspace": str(ws)}))
        check("gateway uploads a validated send_file event",
              br.ms.docs and br.ms.docs[0][0] == "page.html",
              f"({br.ms.docs})")
        check("gateway confirms the upload",
              any("Sent page.html" in m for m in br.ms.msgs))

        br.ms.docs.clear()
        asyncio.run(br._deliver_send_file(
            {"path": "../escape.html", "workspace": str(ws)}))
        check("gateway refuses an escaping event path", br.ms.docs == [])

    print(f"\nSUMMARY send_file: {passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

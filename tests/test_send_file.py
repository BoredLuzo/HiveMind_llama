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


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


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
                print("  SKIP symlink escape (not permitted here)")
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

    print(f"\nSUMMARY send_file: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

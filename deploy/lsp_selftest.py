# -*- coding: utf-8 -*-
"""Language-server self test: is a pack usable right now?

Starts each language server through HiveMind's own client, opens a tiny file
and waits for diagnostics. This is the compatibility check the installer runs
after a language pack is installed - a server counts as usable only when it
completes the LSP handshake and answers, not when its executable exists.

    python deploy/lsp_selftest.py            # every registered language
    python deploy/lsp_selftest.py python ts  # only these
    python deploy/lsp_selftest.py --install-hint

Exit code 0 when at least one requested language works, 1 otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger("hivemind.lsp_selftest")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# language id -> (file name, content that provokes at least one diagnostic)
SAMPLES: dict[str, tuple[str, str]] = {
    "python": ("probe.py", "def f() -> int:\n    return 'x'\n"),
    "typescript": ("probe.ts", "const n: number = 'x';\n"),
    "javascript": ("probe.js", "const n = ;\n"),
    "rust": ("probe.rs", "fn main() { let x: i32 = \"s\"; }\n"),
    "go": ("probe.go", "package main\nfunc main() { var x int = \"s\" }\n"),
    "c": ("probe.c", "int main(void) { int x = \"s\"; return x; }\n"),
    "cpp": ("probe.cpp", "int main() { int x = \"s\"; return x; }\n"),
    "bash": ("probe.sh", "if [ 1 -eq 1 ]; then\n  echo ok\n"),
    "lua": ("probe.lua", "local x = \n"),
    "json": ("probe.json", '{"a": }\n'),
    "yaml": ("probe.yaml", "a: [1, 2\n"),
    "html": ("probe.html", "<html><body><p>hi</body></html>\n"),
    "css": ("probe.css", "a { color: ; }\n"),
}

ALIASES = {"ts": "typescript", "js": "javascript", "rs": "rust",
           "py": "python", "sh": "bash"}

INSTALL_HINT = {
    "python": "npm i -g pyright",
    "typescript": "npm i -g typescript typescript-language-server",
    "javascript": "npm i -g typescript typescript-language-server",
    "rust": "rustup component add rust-analyzer",
    "go": "go install golang.org/x/tools/gopls@latest",
    "c": "install LLVM (clangd): winget install LLVM.LLVM",
    "cpp": "install LLVM (clangd): winget install LLVM.LLVM",
    "bash": "npm i -g bash-language-server",
    "lua": "npm i -g lua-language-server",
    "json": "npm i -g vscode-langservers-extracted",
    "yaml": "npm i -g yaml-language-server",
    "html": "npm i -g vscode-langservers-extracted",
    "css": "npm i -g vscode-langservers-extracted",
}


async def _probe(lang: str, timeout_s: float = 20.0) -> tuple[bool, str]:
    from infra.lsp_client import LspManager

    name, body = SAMPLES[lang]
    ws = Path(tempfile.mkdtemp(prefix=f"hm_lsp_{lang}_"))
    (ws / name).write_text(body, encoding="utf-8")
    mgr = LspManager(ws, {"lsp_client_timeout_s": 25})
    try:
        started = await mgr.start_language(lang)
        if not started:
            # a found command that fails to start is a different problem than
            # a missing one (e.g. typescript-language-server without the
            # "typescript" package) - name it instead of "not found"
            _found = {r["language"]: r["found"] for r in mgr.available()}
            if _found.get(lang):
                return False, ("command found but the server refused to start "
                               "(missing runtime package? see the engine log)")
            # the executable may exist without being on the engine's PATH
            # (npm global bin under %APPDATA%\npm, ~/.cargo/bin, ...)
            from infra.lsp_registry import SERVER_CANDIDATES
            import shutil as _sh
            _extra = [Path.home() / "AppData" / "Roaming" / "npm",
                      Path.home() / ".cargo" / "bin",
                      Path.home() / ".local" / "bin"]
            _hit = ""
            for _cmd in SERVER_CANDIDATES.get(lang, []):
                _bin = _cmd[0]
                if _sh.which(_bin):
                    continue
                for _d in _extra:
                    for _suffix in ("", ".cmd", ".exe"):
                        _cand = _d / (_bin + _suffix)
                        if _cand.exists():
                            _hit = str(_cand)
                            break
                    if _hit:
                        break
                if _hit:
                    break
            if _hit:
                return False, ("found but not on PATH: " + _hit
                               + " -> set lsp_servers for this language")
            return False, "server not found on PATH"
        try:
            diag = await asyncio.wait_for(mgr.diagnostics(name, wait_s=timeout_s - 4),
                                          timeout=timeout_s)
        except asyncio.TimeoutError:
            return False, f"no answer within {timeout_s - 4:.0f}s"
        n = len(diag or [])
        return True, f"{n} diagnostic(s)" if n else "answered, no diagnostics"
    except Exception as exc:                     # noqa: BLE001 - a broken server is a result, not a crash
        return False, f"{type(exc).__name__}: {exc}"[:120]
    finally:
        try:
            await mgr.close_all()
        except Exception:                        # noqa: BLE001 - teardown only
            logger.debug("selftest: manager close failed", exc_info=True)


async def main(argv: list[str]) -> int:
    if "--install-hint" in argv:
        for lang, hint in INSTALL_HINT.items():
            print(f"  {lang:<12} {hint}")
        return 0
    wanted = [a for a in argv if not a.startswith("-")]
    langs = [ALIASES.get(a.lower(), a.lower()) for a in wanted] or list(SAMPLES)
    langs = [l for l in langs if l in SAMPLES]
    if not langs:
        print("unknown language(s); known: " + ", ".join(sorted(SAMPLES)))
        return 1

    ok = 0
    print()
    for lang in langs:
        works, detail = await _probe(lang)
        mark = "[OK]  " if works else "[MISS]"
        print(f"  {mark} {lang:<12} {detail}")
        if works:
            ok += 1
        else:
            hint = INSTALL_HINT.get(lang, "")
            if hint:
                print(f"         install: {hint}")
    print()
    print(f"  {ok}/{len(langs)} language(s) usable")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))

# -*- coding: utf-8 -*-
"""B3: the MCP tool API must honor the secret denylist.

resources/read blocked settings.json / gateway.toml / .env, but read_file,
search_code and find_files resolved paths without that check. The default
workspace is the install root, so any MCP client could read the Telegram bot
token and the git token.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from infra import mcp_server as M            # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


ws = Path(tempfile.mkdtemp())
(ws / "settings.json").write_text('{"git_token": "tok-secret"}', encoding="utf-8")
(ws / "gateway.toml").write_text('bot_token = "tg-secret"', encoding="utf-8")
(ws / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
(ws / ".npmrc").write_text("//registry:_authToken=npm-secret\n", encoding="utf-8")
(ws / ".pypirc").write_text("[pypi]\npassword = pypi-secret\n", encoding="utf-8")
(ws / "vault.kdbx").write_bytes(b"kdbx-secret")
kube = ws / ".kube"
kube.mkdir()
(kube / "config").write_text("token: kube-secret\n", encoding="utf-8")
(ws / "ok.py").write_text("print('hi')\n", encoding="utf-8")
sub = ws / "sub"
sub.mkdir()
(sub / ".env").write_text("NESTED=secret\n", encoding="utf-8")
M.WORKSPACE = ws


def run(coro):
    return asyncio.run(coro)


_payload = {"settings.json": "tok-secret", "gateway.toml": "tg-secret",
            ".env": "API_KEY=", "sub/.env": "NESTED=",
            ".npmrc": "npm-secret", ".pypirc": "pypi-secret",
            "vault.kdbx": "kdbx-secret", ".kube/config": "kube-secret"}
for name in ("settings.json", "gateway.toml", ".env", "sub/.env",
             ".npmrc", ".pypirc", "vault.kdbx", ".kube/config"):
    out = run(M.tool_read_file(name))
    check(f"1 read {name} is denied",
          "SECRET_PATH_DENIED" in out and _payload[name] not in out, out[:70])

ok = run(M.tool_read_file("ok.py"))
check("2 a normal file still reads", "print('hi')" in ok, ok[:60])

before = (ws / "settings.json").read_text(encoding="utf-8")
out = run(M.tool_write_file("settings.json", '{"git_token": "pwned"}'))
check("3 writing a secret file is denied", "SECRET_PATH_DENIED" in out, out[:70])
check("3b the secret file is unchanged",
      (ws / "settings.json").read_text(encoding="utf-8") == before)

found = run(M.tool_find_files("*.json"))
check("4 find_files does not list settings.json",
      "settings.json" not in found, found[:80])

hits = run(M.tool_search_code("git_token"))
check("5 search_code does not return settings.json content",
      "tok-secret" not in hits and "settings.json" not in hits, hits[:90])

# P0 (deep audit 2026-11-03): the four remaining file tools took the same
# path without the denylist.
sig = run(M.tool_get_signatures("settings.json"))
check("6 get_signatures denies a secret file", "SECRET_PATH_DENIED" in sig, sig[:70])

refs = run(M.tool_find_references("git_token", "settings.json"))
check("7 find_references on a secret file is denied",
      "SECRET_PATH_DENIED" in refs, refs[:70])

refs_dir = run(M.tool_find_references("git_token", "."))
check("8 a directory scan filters secret files",
      "tok-secret" not in refs_dir, refs_dir[:80])

patched = run(M.tool_patch_file("settings.json", "tok-secret", "pwned"))
check("9 patch_file denies a secret file", "SECRET_PATH_DENIED" in patched, patched[:70])
check("9b the secret file is unchanged",
      (ws / "settings.json").read_text(encoding="utf-8") == before)

ast = run(M.tool_edit_ast("settings.json", "function", "x", "pass"))
check("10 edit_ast denies a secret file", "SECRET_PATH_DENIED" in ast, ast[:70])

glob_hit = run(M.tool_search_code("git_token", ".", "settings.json"))
check("11 search_code denies a secret file_glob",
      "SECRET_PATH_DENIED" in glob_hit, glob_hit[:70])

# P0 (audit 2026-11-03): shared tools are FORWARDED to the engine, so the
# local handler never runs. The dispatch path must guard before forwarding.
_forwarded = []


async def _fake_shared(name, args):
    _forwarded.append((name, args))
    return True, f"forwarded:{name}"


M._try_shared_tool_exec = _fake_shared
_out = run(M._dispatch_tool("read_file", {"path": "settings.json"}))
check("12 a forwarded read of a secret file is denied",
      "SECRET_PATH_DENIED" in _out and not _forwarded, _out[:70])

_old_write = os.environ.get("MCP_ALLOW_WRITE")
os.environ["MCP_ALLOW_WRITE"] = "1"
try:
    _out = run(M._dispatch_tool("write_file",
                                {"path": "gateway.toml", "content": "x"}))
finally:
    if _old_write is None:
        os.environ.pop("MCP_ALLOW_WRITE", None)
    else:
        os.environ["MCP_ALLOW_WRITE"] = _old_write
check("13 a forwarded write of a secret file is denied",
      "SECRET_PATH_DENIED" in _out and not _forwarded, _out[:70])

_out = run(M._dispatch_tool("read_file", {"path": "ok.py"}))
_sess = run(M.tool_read_file("sessions/x.json"))
check("13b transcript directories are denied", "SECRET_PATH_DENIED" in _sess, _sess[:70])
check("14 a normal forwarded read still passes",
      "forwarded" in _out and bool(_forwarded), _out[:70])

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)

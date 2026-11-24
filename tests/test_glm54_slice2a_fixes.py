# -*- coding: utf-8 -*-
"""Regression suite for verified GLM-5.4 Slice 2a fixes (tools/ & infra/).

Covers:
1. exec_tools.py: bounded run_python streaming + recursive rm blocklist variants
2. patch_2_fuzzy_edit.py / file_ops.py / patch_apply.py: fast O(1) sliding
   quick_ratio + time cap + asyncio.to_thread offloading
3. code_intel.py: ReDoS/time guards on search_code, capped subprocess drain,
   early-stop fast find_files
4. tools/runner.py: destructive bash pattern bypasses (rm, find, git, PowerShell)
5. tools/websearch.py: streamed web_fetch byte cap without pre-buffering body
6. infra/child_env.py: expanded secret key/passphrase/credential scrubbing
7. tools/background.py: chunked readline cap in _reader
8. infra/mcp_server.py: git --no-index/--contents/--git-dir blocks, strict
   same-host+port Origin CSRF check, non-loopback token requirement, bounded
   tool_run_python pipe drain

Run: python tests/test_glm54_slice2a_fixes.py
Exit 0 = all pass, Exit 1 = failure.
"""
from __future__ import annotations

import asyncio
import io
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if os.name == "nt":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


def test_exec_tools() -> None:
    import tools.handlers.exec_tools as et

    with tempfile.TemporaryDirectory(prefix="hms2a_py_") as td:
        ws = Path(td)
        res = asyncio.run(
            et._inline_tool_run_python(
                {"code": "for i in range(1000):\n    print('LINE_' + 'x' * 2500 + f'_{i}')"},
                ws,
                str(ws),
            )
        )
    check(
        "1a run_python bounds >2 MB output cleanly",
        "[exit code: 0]" in res and len(res) < 5000 and "[truncated]" in res,
        f"(len={len(res)})",
    )

    for bad in ("rm -fr ~", "rm -f -r ~", "rm --recursive --force /", "rm -rfv ."):
        check(
            f"1b _bash_blocklisted blocks {bad!r}",
            et._bash_blocklisted(bad) is not None,
        )
    for safe in ("rm -rf build", "rm -r sub", "rm single.txt"):
        check(
            f"1c _bash_blocklisted allows {safe!r}",
            et._bash_blocklisted(safe) is None,
        )


def test_fuzzy_edit_and_offload() -> None:
    from tools.patch_2_fuzzy_edit import best_match_location, fuzzy_replace

    filler = [f"line_{i:04d} = {i} * 2  # padding payload" for i in range(1500)]
    block = [f"def target_block_{j}(x): return x + {j}" for j in range(40)]
    drifted_block = list(block)
    drifted_block[10] = "def target_block_10(x): return x + 999  # drifted"
    doc_lines = filler[:700] + drifted_block + filler[700:]
    content = "\n".join(doc_lines) + "\n"
    old_str = "\n".join(block)

    t0 = time.monotonic()
    loc = best_match_location(content, old_str)
    dt_loc = time.monotonic() - t0
    check(
        "2a best_match_location finds 40-line block in 1500-line doc rapidly",
        loc is not None and loc[0] == 700 and loc[1] > 0.90 and dt_loc < 1.0,
        f"(loc={loc}, dt={dt_loc:.3f}s)",
    )

    t1 = time.monotonic()
    replaced = fuzzy_replace(content, old_str, "REPLACED_BLOCK = True")
    dt_rep = time.monotonic() - t1
    check(
        "2b fuzzy_replace applies cleanly and fast",
        replaced is not None and "REPLACED_BLOCK = True" in replaced and dt_rep < 1.0,
        f"(dt={dt_rep:.3f}s)",
    )

    check(
        "2c fuzzy_replace fail-safe returns None on max_seconds=0.0",
        fuzzy_replace(content, old_str, "X", max_seconds=0.0) is None,
    )

    file_ops_src = (ROOT / "tools" / "handlers" / "file_ops.py").read_text(encoding="utf-8")
    patch_apply_src = (ROOT / "tools" / "handlers" / "patch_apply.py").read_text(encoding="utf-8")
    check(
        "2d file_ops and patch_apply offload fuzzy helpers to thread",
        file_ops_src.count("await asyncio.to_thread(\n                _tolerant_replace") >= 1
        and "await asyncio.to_thread(_p2_fzr" in file_ops_src
        and "_build_edit_mismatch_anchor" in file_ops_src
        and "await asyncio.to_thread(\n                    _build_patch_mismatch_anchor" in patch_apply_src,
    )


def test_code_intel() -> None:
    import tools.handlers.code_intel as ci

    for bad_rx in ("(a+)+b", "(.*)+x", "((a+))+", ".*.*foo", r"(\w+)\1+"):
        check(f"3a _is_unsafe_regex catches {bad_rx!r}", ci._is_unsafe_regex(bad_rx))
    for ok_rx in (r"def\s+\w+\(", "(?:foo|bar)+", r"(\w\+\s)+", "foo.*bar", "[a*]+"):
        check(f"3b _is_unsafe_regex allows {ok_rx!r}", not ci._is_unsafe_regex(ok_rx))

    with tempfile.TemporaryDirectory(prefix="hms2a_ci_") as td:
        ws = Path(td)
        for idx in range(200):
            (ws / f"item_{idx:03d}.py").write_text(f"val = {idx}\n", encoding="utf-8")
        (ws / ".env").write_text("SECRET=1\n", encoding="utf-8")

        ff_out = asyncio.run(ci._inline_tool_find_files({"pattern": "**/*"}, ws, str(ws)))
        ff_lines = [l for l in ff_out.splitlines() if not l.startswith("…")]
        check(
            "3c find_files caps at 150 results and excludes .env",
            len(ff_lines) == 150 and "more than 150 matches" in ff_out and ".env" not in ff_out,
            f"(lines={len(ff_lines)})",
        )

        with mock.patch.object(ci.shutil, "which", return_value=None):
            bad_res = asyncio.run(ci._inline_tool_search_code({"pattern": "(a+)+b", "path": "."}, ws, str(ws)))
        check(
            "3d search_code returns INVALID_REGEX on ReDoS pattern",
            "INVALID_REGEX" in bad_res,
            f"({bad_res[:80]!r})",
        )

        # Verify bounded subprocess drain via fake search tool output (>300 KB)
        big_file = ws / "big.txt"
        big_file.write_text("needle " + ("a" * 200) + "\n", encoding="utf-8")
        huge_script = (
            "import sys; "
            "[sys.stdout.write(f'item_000.py:{i}: ' + 'z'*2000 + '\\n') for i in range(250)]"
        )
        orig_create = ci.asyncio.create_subprocess_exec

        async def _spawn_huge(*argv, **kw):
            return await orig_create(sys.executable, "-c", huge_script, **kw)

        with mock.patch.object(ci.shutil, "which", return_value="rg"), \
                mock.patch.object(ci.asyncio, "create_subprocess_exec", side_effect=_spawn_huge):
            sc_out = asyncio.run(ci._inline_tool_search_code({"pattern": "needle", "path": "."}, ws, str(ws)))
        check(
            "3e search_code caps huge subprocess stdout cleanly",
            "[search_code: needle]" in sc_out and len(sc_out) < 8200 and "... (" in sc_out,
            f"(len={len(sc_out)})",
        )


def test_destructive_bash_gate() -> None:
    from tools.runner import _is_destructive_bash

    bypasses = (
        "rm -fr ~",
        "rm -f -r ~",
        "rm --recursive --force ~",
        "cd ~ && rm -fr *",
        "find ~ -type f -delete",
        "git clean -fd",
        "git clean -xdf",
        "git -C repo clean -fd",
        "git push -f",
        "git push origin main -f",
        "git push --force-if-includes origin main",
        "git checkout -- .",
        "git checkout HEAD -- app.py",
        "git restore .",
        "git restore --worktree file.py",
        "git reset --har",
        "git -C repo reset --hard",
        "ri -Recurse -Force foo",
        "del -Recurse foo",
        "erase /s foo",
        "rd -r foo",
        "rmdir -Recurse foo",
    )
    for cmd in bypasses:
        check(
            f"4a destructive bypass blocked: {cmd!r}",
            _is_destructive_bash(cmd) is not None,
        )

    safe_cmds = (
        "rm file.txt",
        "rm -f single.txt",
        "find . -name '*.py'",
        "git push origin main",
        "git push origin feat-branch",
        "git checkout -b feat",
        "git checkout main",
        "git restore --staged a.py",
        "git clean -nd",
        "Remove-Item file.txt",
        "del file.txt",
    )
    for cmd in safe_cmds:
        check(
            f"4b safe command stays ungated: {cmd!r}",
            _is_destructive_bash(cmd) is None,
        )


def test_web_fetch_streaming_cap() -> None:
    import tools.websearch as ws

    chunks_yielded = 0

    class _HugeStreamResp:
        status_code = 200
        headers = {"content-type": "text/plain"}
        encoding = "utf-8"

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self, chunk_size: int = 65536):
            nonlocal chunks_yielded
            block = b"A" * (512 * 1024)
            for _ in range(100):  # 50 MB if unstopped
                chunks_yielded += 1
                yield block

    class _FakeStreamCtx:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *args):
            return False

    class _FakeHugeClient:
        def stream(self, *args, **kwargs):
            return _FakeStreamCtx(_HugeStreamResp())

    async def _fake_huge_get_client():
        return _FakeHugeClient()

    class _SmallStreamResp:
        status_code = 200
        headers = {"content-type": "text/plain"}
        encoding = "utf-8"

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self, chunk_size: int = 65536):
            yield b"Hello streamed world"

    class _FakeSmallClient:
        def stream(self, *args, **kwargs):
            return _FakeStreamCtx(_SmallStreamResp())

    async def _fake_small_get_client():
        return _FakeSmallClient()

    orig_gai = socket.getaddrinfo

    def _public_gai(host, *args, **kwargs):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    socket.getaddrinfo = _public_gai
    try:
        with mock.patch.object(ws, "_get_client_async", _fake_huge_get_client):
            out_huge = asyncio.run(ws.web_fetch("https://example.org/huge"))
        check(
            "5a web_fetch aborts >5 MB stream early without reading 50 MB",
            "response too large" in out_huge and chunks_yielded <= 12,
            f"(chunks={chunks_yielded}, out={out_huge!r})",
        )

        with mock.patch.object(ws, "_get_client_async", _fake_small_get_client):
            out_ok = asyncio.run(ws.web_fetch("https://example.org/small"))
        check(
            "5b web_fetch returns valid streamed response cleanly",
            "Hello streamed world" in out_ok,
            f"(out={out_ok!r})",
        )
    finally:
        socket.getaddrinfo = orig_gai


def test_child_env_and_background() -> None:
    from infra.child_env import scrubbed_env
    import tools.background as bg

    secret_vars = {
        "DJANGO_SECRET_KEY": "s1",
        "STRIPE_SECRET_KEY": "s2",
        "SECRET_KEY": "s3",
        "APP_SIGNING_KEY": "s4",
        "DB_ENCRYPTION_KEY": "s5",
        "GPG_PASSPHRASE": "s6",
        "CLOUD_CREDENTIALS": "s7",
        "PUBLIC_KEY": "keep-public",
    }
    with mock.patch.dict(os.environ, secret_vars, clear=False):
        env_out = scrubbed_env()
    for name in ("DJANGO_SECRET_KEY", "STRIPE_SECRET_KEY", "SECRET_KEY",
                 "APP_SIGNING_KEY", "DB_ENCRYPTION_KEY", "GPG_PASSPHRASE", "CLOUD_CREDENTIALS"):
        check(f"6a scrubbed_env strips {name}", name not in env_out)
    check("6b scrubbed_env preserves PATH and PUBLIC_KEY",
          "PATH" in env_out and env_out.get("PUBLIC_KEY") == "keep-public")

    class _TrackingStringIO(io.StringIO):
        def __init__(self, text: str):
            super().__init__(text)
            self.sizes: list[int | None] = []

        def readline(self, size: int | None = -1) -> str:
            self.sizes.append(size)
            return super().readline(size)

    fake_stream = _TrackingStringIO("Z" * 100_000)

    class _StubProc:
        stdout = fake_stream

    handle = "bg_slice2a_test"
    with bg._LOCK:
        bg._REGISTRY[handle] = {"proc": _StubProc(), "buf": "", "started": 0.0, "cmd": "x", "job": None}
    try:
        bg._reader(handle, _StubProc())
        with bg._LOCK:
            buf = bg._REGISTRY[handle]["buf"]
        check(
            "7a background _reader reads <=4096 chars per readline and caps buf at 20k",
            bool(fake_stream.sizes)
            and all(s is not None and 0 < s <= bg._READ_CHUNK_CHARS for s in fake_stream.sizes)
            and len(buf) == bg._MAX_OUTPUT_CHARS,
            f"(buf_len={len(buf)}, sizes[:3]={fake_stream.sizes[:3]})",
        )
    finally:
        with bg._LOCK:
            bg._REGISTRY.pop(handle, None)


def test_mcp_server_hardening() -> None:
    import infra.mcp_server as mcp

    for bad_git in (
        "git diff --no-index /etc/passwd /dev/null",
        "git blame --contents /etc/passwd f",
        "git status --git-dir=/etc",
        "git log --work-tree=/etc",
    ):
        check(f"8a _is_safe_git rejects {bad_git!r}", not mcp._is_safe_git(bad_git))

    orig_cors = os.environ.get("MCP_HTTP_CORS_ORIGINS")
    try:
        os.environ["MCP_HTTP_CORS_ORIGINS"] = "https://ide.example"
        check(
            "8b _origin_allowed rejects cross-port localhost CSRF",
            not mcp._origin_allowed({"origin": "http://localhost:3000", "host": "127.0.0.1:8090"}),
        )
        check(
            "8c _origin_allowed accepts exact same host+port Origin",
            mcp._origin_allowed({"origin": "http://127.0.0.1:8090", "host": "127.0.0.1:8090"}),
        )
        check(
            "8d _origin_allowed accepts explicit MCP_HTTP_CORS_ORIGINS",
            mcp._origin_allowed({"origin": "https://ide.example", "host": "127.0.0.1:8090"}),
        )
    finally:
        if orig_cors is None:
            os.environ.pop("MCP_HTTP_CORS_ORIGINS", None)
        else:
            os.environ["MCP_HTTP_CORS_ORIGINS"] = orig_cors

    orig_tok = os.environ.pop("MCP_HTTP_TOKEN", None)
    try:
        check("8e _auth_ok(require_token=True) denies unset token",
              not mcp._auth_ok({}, require_token=True))
        check("8f _auth_ok(require_token=False) keeps local loopback compatible",
              mcp._auth_ok({}, require_token=False))
    finally:
        if orig_tok is not None:
            os.environ["MCP_HTTP_TOKEN"] = orig_tok

    py_out = asyncio.run(
        mcp.tool_run_python("print('M' * 400000)", timeout=15)
    )
    check(
        "8g mcp.tool_run_python caps huge stdout cleanly",
        "[output truncated]" in py_out and len(py_out) < 300_000,
        f"(len={len(py_out)})",
    )


def main() -> int:
    test_exec_tools()
    test_fuzzy_edit_and_offload()
    test_code_intel()
    test_destructive_bash_gate()
    test_web_fetch_streaming_cap()
    test_child_env_and_background()
    test_mcp_server_hardening()
    return h.finish("glm54_slice2a_fixes")


if __name__ == "__main__":
    sys.exit(main())

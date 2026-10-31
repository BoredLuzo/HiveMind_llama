# -*- coding: utf-8 -*-
"""MCP phase 2 (2026-10-31): annotations, new tools, resources, prompts.

Behavioural, in-process, no network beyond loopback and no live model:
  - every tools/list entry carries correct annotations (readOnlyHint et al.);
  - git_diff / git_commit / run_tests / model_status: tiers, transports,
    containment, bounded output, explicit-paths-only commits;
  - resources/list + resources/read: workspace containment, size cap,
    secret denylist, binary rejection, templates;
  - prompts/list + prompts/get incl. bad input;
  - backward compatibility of initialize / tools/list.
"""
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


import infra.mcp_server as mcp  # noqa: E402

# ── 1. Annotations ────────────────────────────────────────────────────────
_HINTS = {"readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}

for _t in mcp.TOOL_SCHEMAS:
    _a = _t.get("annotations")
    check(f"1 {_t['name']} has the four hints",
          isinstance(_a, dict) and set(_a) == _HINTS
          and all(isinstance(_a[h], bool) for h in _HINTS),
          f"({_a})" if not (isinstance(_a, dict) and set(_a) == _HINTS) else "")

_RO = {t["name"] for t in mcp.TOOL_SCHEMAS if t["annotations"]["readOnlyHint"]}
for _n in ("read_file", "list_dir", "find_files", "search_code",
           "get_signatures", "find_references", "memory_get", "git_status",
           "git_diff", "model_status"):
    check(f"1 {_n} readOnlyHint=true", _n in _RO)

_NOT_RO = {t["name"] for t in mcp.TOOL_SCHEMAS if not t["annotations"]["readOnlyHint"]}
for _n in ("shell", "run_bash", "run_python", "write_file", "edit_file",
           "patch_file", "edit_ast", "replace_lines", "git_commit",
           "run_tests", "query", "memory_set", "write_file_append"):
    check(f"1 {_n} readOnlyHint=false", _n in _NOT_RO)

_DESTR = {t["name"] for t in mcp.TOOL_SCHEMAS if t["annotations"]["destructiveHint"]}
for _n in ("shell", "run_bash", "run_python", "write_file", "edit_file",
           "patch_file", "edit_ast", "replace_lines"):
    check(f"1 {_n} destructiveHint=true", _n in _DESTR)
for _n in ("git_commit", "run_tests", "write_file_append", "memory_set", "query"):
    check(f"1 {_n} destructiveHint=false", _n not in _DESTR,
          f"({_n in _DESTR})")

check("1 read-only tools are idempotentHint=true",
      all(t["annotations"]["idempotentHint"] for t in mcp.TOOL_SCHEMAS
          if t["annotations"]["readOnlyHint"]))
check("1 exec tools carry openWorldHint=true",
      all(mcp.TOOL_ANNOTATIONS[n]["openWorldHint"] for n in ("shell", "run_bash", "run_python")))
check("1 annotations are hints, schema shape unchanged",
      all({"name", "description", "inputSchema"} <= set(t) for t in mcp.TOOL_SCHEMAS))

# ── 2. Tier wiring ────────────────────────────────────────────────────────
check("2 git_diff/model_status are read tier",
      "git_diff" in mcp._TIER_READ and "model_status" in mcp._TIER_READ)
check("2 git_commit is write tier", "git_commit" in mcp._TIER_WRITE)
check("2 run_tests is exec tier", "run_tests" in mcp._TIER_EXEC)

_old = {k: os.environ.get(k) for k in ("MCP_ALLOW_WRITE", "MCP_ALLOW_EXEC")}
try:
    os.environ.pop("MCP_ALLOW_WRITE", None)
    os.environ.pop("MCP_ALLOW_EXEC", None)
    ok_d, _ = mcp._tool_allowed("git_diff", "http")
    ok_m, _ = mcp._tool_allowed("model_status", "http")
    ok_c, why_c = mcp._tool_allowed("git_commit", "http")
    ok_t, why_t = mcp._tool_allowed("run_tests", "http")
    ok_t_stdio, why_t2 = mcp._tool_allowed("run_tests", "stdio")
    check("2 git_diff allowed on http", ok_d)
    check("2 model_status allowed on http", ok_m)
    check("2 git_commit needs MCP_ALLOW_WRITE", not ok_c and "MCP_ALLOW_WRITE" in why_c)
    check("2 run_tests never on http", not ok_t and "HTTP" in why_t)
    check("2 run_tests needs MCP_ALLOW_EXEC on stdio", not ok_t_stdio and "MCP_ALLOW_EXEC" in why_t2)

    lst_http = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "http"))
    names_http = {t["name"] for t in lst_http["result"]["tools"]}
    check("2 http tools/list keeps git_diff+model_status",
          {"git_diff", "model_status"} <= names_http)
    check("2 http tools/list hides git_commit+run_tests",
          not ({"git_commit", "run_tests"} & names_http))
    os.environ["MCP_ALLOW_EXEC"] = "1"
    lst_stdio = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "stdio"))
    names_stdio = {t["name"] for t in lst_stdio["result"]["tools"]}
    check("2 stdio shows run_tests with MCP_ALLOW_EXEC", "run_tests" in names_stdio)

    # denied transports reach tools/call too
    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "run_tests", "arguments": {"cmd": "python -V"}}}, "http"))
    check("2 run_tests over http -> -32001", _r.get("error", {}).get("code") == -32001,
          f"({_r.get('error')})")
    os.environ.pop("MCP_ALLOW_EXEC", None)
    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "run_tests", "arguments": {"cmd": "python -V"}}}, "stdio"))
    check("2 run_tests without opt-in -> -32001", _r.get("error", {}).get("code") == -32001)
    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "git_commit", "arguments": {"message": "x", "paths": ["a"]}}}, "stdio"))
    check("2 git_commit without MCP_ALLOW_WRITE -> -32001",
          _r.get("error", {}).get("code") == -32001)
finally:
    for k, v in _old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ── 3. Temp git workspace for the new git/test tools ──────────────────────
_tmp = Path(tempfile.mkdtemp(prefix="mcp_phase2_"))
_orig_ws = mcp.WORKSPACE


def _git(*args):
    return subprocess.run(["git", *args], cwd=str(_tmp), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          check=False)


try:
    mcp.WORKSPACE = _tmp
    _git("init", "-q")
    _git("config", "user.email", "phase2@example.com")
    _git("config", "user.name", "Phase2 Test")
    (_tmp / "a.txt").write_text("line1\nline2\n", encoding="utf-8")
    _git("add", "a.txt")
    _git("commit", "-q", "-m", "base")

    # 3a git_diff: shows the modification, bounded
    (_tmp / "a.txt").write_text("line1\nCHANGED\n", encoding="utf-8")
    _d = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
         "params": {"name": "git_diff", "arguments": {}}}, "stdio"))
    _dtext = _d["result"]["content"][0]["text"]
    check("3a git_diff shows the change", "CHANGED" in _dtext and "a.txt" in _dtext,
          f"({_dtext[:80]!r})")
    check("3a git_diff is not isError", _d["result"]["isError"] is False)

    _d = asyncio.run(mcp.tool_git_diff(path="a.txt", ref="HEAD"))
    check("3a git_diff with ref+path works", "CHANGED" in _d, f"({_d[:80]!r})")
    _d = asyncio.run(mcp.tool_git_diff(ref="-oFun"))
    check("3a git_diff rejects leading-dash ref", "INVALID_ARGUMENT" in _d, f"({_d[:80]!r})")
    _d = asyncio.run(mcp.tool_git_diff(path="../outside.txt"))
    check("3a git_diff rejects paths outside the workspace", "PATH_OUTSIDE_WORKSPACE" in _d)
    (_tmp / "big.txt").write_text("x" * 40000, encoding="utf-8")
    _git("add", "big.txt")                       # tracked, so git diff sees it
    (_tmp / "big.txt").write_text("y" * 45000, encoding="utf-8")
    _d = asyncio.run(mcp.tool_git_diff(path="big.txt", max_chars=1000))
    check("3a git_diff output is bounded",
          len(_d) < 1500 and "truncated" in _d, f"(len={len(_d)})")
    _git("add", "a.txt")
    _git("commit", "-q", "-m", "3a: change a")
    _d = asyncio.run(mcp.tool_git_diff(ref="HEAD", path="a.txt"))
    check("3a clean tree vs HEAD gives no diff", _d.startswith("(no diff"), f"({_d[:60]!r})")

    # 3b git_commit: explicit paths only
    os.environ["MCP_ALLOW_WRITE"] = "1"
    (_tmp / "a.txt").write_text("line1\nCHANGED-AGAIN\n", encoding="utf-8")
    (_tmp / "untracked.txt").write_text("private\n", encoding="utf-8")
    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
         "params": {"name": "git_commit", "arguments": {
             "message": "phase2: change a", "paths": ["a.txt"]}}}, "stdio"))
    _ctext = _r["result"]["content"][0]["text"]
    check("3b git_commit succeeds with MCP_ALLOW_WRITE", "Committed" in _ctext,
          f"({_ctext[:100]!r})")
    _shown = _git("show", "--name-only", "--pretty=format:").stdout.strip().split()
    check("3b commit contains ONLY the listed path", _shown == ["a.txt"], f"({_shown})")
    check("3b untracked file was NOT swept in", "untracked.txt" not in _shown)

    _d = asyncio.run(mcp.tool_git_commit(message="empty paths", paths=[]))
    check("3b empty paths refused", "INVALID_ARGUMENT" in _d)
    _d = asyncio.run(mcp.tool_git_commit(message="root", paths=["."]))
    check("3b workspace root refused", "INVALID_ARGUMENT" in _d, f"({_d[:80]!r})")
    _d = asyncio.run(mcp.tool_git_commit(message="x", paths=["../escape.txt"]))
    check("3b path outside workspace refused", "PATH_OUTSIDE_WORKSPACE" in _d)
    (_tmp / "a.txt").write_text("line1\nCHANGED-AGAIN\nmore\n", encoding="utf-8")
    _d = asyncio.run(mcp.tool_git_commit(message="x", paths=["missing.txt", "a.txt"]))
    check("3b missing path is skipped, rest committed", "Committed" in _d and "missing.txt" in _d,
          f"({_d[:100]!r})")
    _d = asyncio.run(mcp.tool_git_commit(message="no changes", paths=["a.txt"]))
    check("3b commit without changes is an info, not an error",
          "no changes" in _d, f"({_d[:80]!r})")
    (_tmp / "sub").mkdir(exist_ok=True)
    (_tmp / "sub" / "b.txt").write_text("new file\n", encoding="utf-8")
    _d = asyncio.run(mcp.tool_git_commit(message="phase2: add sub/b", paths=["sub/b.txt"]))
    check("3b untracked-but-listed file gets committed", "Committed" in _d, f"({_d[:80]!r})")
    os.environ.pop("MCP_ALLOW_WRITE", None)

    # 3c run_tests: exec tier behaviour
    os.environ["MCP_ALLOW_EXEC"] = "1"
    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
         "params": {"name": "run_tests", "arguments": {
             "cmd": "python -c \"print('TESTS-OK-12345')\"", "timeout": 60}}}, "stdio"))
    _ttext = _r["result"]["content"][0]["text"]
    check("3c run_tests executes a plain command",
          "[TEST-RUN]" in _ttext and "TESTS-OK-12345" in _ttext, f"({_ttext[:120]!r})")
    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
         "params": {"name": "run_tests", "arguments": {
             "cmd": "python -c \"print(1)\" && echo chained"}}}, "stdio"))
    _ttext = _r["result"]["content"][0]["text"]
    check("3c chained cmd refused", "INVALID_ARGUMENT" in _ttext, f"({_ttext[:100]!r})")
    check("3c failing tests keep the tail",
          "FAILED (exit" in asyncio.run(mcp.tool_run_tests(
              cmd="python -c \"raise SystemExit(3)\"", timeout=30)))
    check("3c no suite detected is an info",
          "no test suite detected" in asyncio.run(mcp.tool_run_tests(path=".")))
    check("3c detection returns empty for a bare dir", mcp._detect_test_cmd(_tmp) == "",
          f"({mcp._detect_test_cmd(_tmp)!r})")
    (_tmp / "tests").mkdir(exist_ok=True)
    (_tmp / "tests" / "test_probe.py").write_text("def test_probe():\n    assert True\n",
                                                  encoding="utf-8")
    check("3c detection finds pytest for a tests/ dir",
          mcp._detect_test_cmd(_tmp) == "python -m pytest -q",
          f"({mcp._detect_test_cmd(_tmp)!r})")
    _d = asyncio.run(mcp.tool_run_tests(cmd="python -V", path="../elsewhere"))
    check("3c run_tests path containment", "PATH_OUTSIDE_WORKSPACE" in _d)
    os.environ.pop("MCP_ALLOW_EXEC", None)

    # 3d model_status: read-only, loads nothing
    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 14, "method": "tools/call",
         "params": {"name": "model_status", "arguments": {}}}, "http"))
    _mtext = _r["result"]["content"][0]["text"]
    check("3d model_status answers over http (read tier)",
          _r["result"]["isError"] is False and "engine:" in _mtext, f"({_mtext[:80]!r})")
    check("3d model_status reports ollama reachability honestly",
          "loaded models:" in _mtext, f"({_mtext[-120:]!r})")
finally:
    mcp.WORKSPACE = _orig_ws
    for _k in ("MCP_ALLOW_WRITE", "MCP_ALLOW_EXEC"):
        os.environ.pop(_k, None)

# ── 4. Resources ──────────────────────────────────────────────────────────
_res = Path(tempfile.mkdtemp(prefix="mcp_phase2_res_"))
_orig_ws2 = mcp.WORKSPACE
try:
    mcp.WORKSPACE = _res
    (_res / "docs").mkdir()
    (_res / "docs" / "hello.md").write_text("# hello world\n", encoding="utf-8")
    (_res / "top.txt").write_text("top level\n", encoding="utf-8")
    (_res / "settings.json").write_text("{}", encoding="utf-8")
    (_res / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (_res / "binary.bin").write_bytes(b"AB\x00CD")
    (_res / ".git").mkdir()
    (_res / ".git" / "config").write_text("gitconfig\n", encoding="utf-8")
    (_res / "node_modules").mkdir()
    (_res / "node_modules" / "dep.js").write_text("dep\n", encoding="utf-8")

    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 20, "method": "resources/list"}, "http"))
    _res_list = _r["result"]["resources"]
    _res_names = {x["name"] for x in _res_list}
    check("4 resources/list returns workspace files",
          {"docs/hello.md", "top.txt"} <= _res_names, f"({sorted(_res_names)})")
    check("4 resources/list prunes hidden/junk dirs",
          not any(n.startswith(".git") or "node_modules" in n for n in _res_names))
    check("4 resources/list hides likely-secret files",
          "settings.json" not in _res_names and ".env" not in _res_names)
    check("4 resources/list entries carry uri+name+mimeType",
          all({"uri", "name", "mimeType"} <= set(x) for x in _res_list))
    _hello = next(x for x in _res_list if x["name"] == "docs/hello.md")
    check("4 list uses file:// uris", _hello["uri"].startswith("file://"), f"({_hello['uri']})")

    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 21, "method": "resources/read",
         "params": {"uri": _hello["uri"]}}, "http"))
    _c0 = _r["result"]["contents"][0]
    check("4 resources/read returns text", _c0["text"].strip() == "# hello world", f"({_c0!r})")
    check("4 read echoes the uri", _c0["uri"] == _hello["uri"])

    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 22, "method": "resources/read",
         "params": {"uri": f"hivemind://workspace/docs/hello.md"}}, "stdio"))
    check("4 template scheme hivemind://workspace/<path> reads",
          _r["result"]["contents"][0]["text"].strip() == "# hello world", f"({_r!r:.120})")

    # containment + caps + denylist
    for _label, _uri in (
        ("4 absolute file uri outside the workspace",
         Path("C:/Windows/win.ini").as_uri() if os.name == "nt" else "file:///etc/passwd"),
        ("4 traversal via template scheme",
         "hivemind://workspace/../__outside__.txt"),
        ("4 unknown scheme", "ftp://example.com/x"),
        ("4 missing uri", ""),
    ):
        _params = {} if not _uri else {"uri": _uri}
        _r = asyncio.run(mcp.handle_request(
            {"jsonrpc": "2.0", "id": 23, "method": "resources/read",
             "params": _params}, "http"))
        check(_label, _r.get("error", {}).get("code") in (-32602, -32002),
              f"({_r.get('error')})")

    (_res / "huge.txt").write_text("y" * (mcp.MCP_RESOURCE_MAX_BYTES + 10), encoding="utf-8")
    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 24, "method": "resources/read",
         "params": {"uri": (_res / "huge.txt").as_uri()}}, "http"))
    check("4 oversized file refused with a clear error",
          _r.get("error", {}).get("code") == -32002
          and "too large" in _r["error"]["message"], f"({_r.get('error')!r:.120})")

    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 25, "method": "resources/read",
         "params": {"uri": (_res / "binary.bin").as_uri()}}, "http"))
    check("4 binary content refused",
          _r.get("error", {}).get("code") == -32002 and "binary" in _r["error"]["message"],
          f"({_r.get('error')!r:.120})")

    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 26, "method": "resources/read",
         "params": {"uri": (_res / ".env").as_uri()}}, "http"))
    check("4 secret denylist holds even for a direct uri",
          _r.get("error", {}).get("code") == -32002 and "secret" in _r["error"]["message"],
          f"({_r.get('error')!r:.120})")

    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 27, "method": "resources/read",
         "params": {"uri": (_res / "does-not-exist.txt").as_uri()}}, "http"))
    check("4 unknown resource -> -32002",
          _r.get("error", {}).get("code") == -32002, f"({_r.get('error')!r:.120})")

    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 28, "method": "resources/read",
         "params": {"uri": 42}}, "http"))
    check("4 non-string uri -> -32602", _r.get("error", {}).get("code") == -32602)

    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 29, "method": "resources/templates/list"}, "http"))
    _tpl = _r["result"]["resourceTemplates"]
    check("4 templates/list has the workspace-file template",
          len(_tpl) == 1 and _tpl[0]["uriTemplate"] == "hivemind://workspace/{path}",
          f"({_tpl})")

    # entry cap
    mcp.MCP_RESOURCE_MAX_ENTRIES = 2
    _r = asyncio.run(mcp.handle_request(
        {"jsonrpc": "2.0", "id": 30, "method": "resources/list"}, "http"))
    check("4 entry cap is honoured", len(_r["result"]["resources"]) <= 2,
          f"(n={len(_r['result']['resources'])})")
    mcp.MCP_RESOURCE_MAX_ENTRIES = 200
finally:
    mcp.WORKSPACE = _orig_ws2

# ── 5. Prompts ────────────────────────────────────────────────────────────
_r = asyncio.run(mcp.handle_request(
    {"jsonrpc": "2.0", "id": 40, "method": "prompts/list"}, "http"))
_plist = _r["result"]["prompts"]
check("5 prompts/list returns the three prompts",
      {p["name"] for p in _plist} == {"review_diff", "explain_file", "fix_tests"},
      f"({[p['name'] for p in _plist]})")
check("5 prompt arguments declared",
      any(a["name"] == "path" and a["required"] for p in _plist
          for a in p.get("arguments", [])))

_r = asyncio.run(mcp.handle_request(
    {"jsonrpc": "2.0", "id": 41, "method": "prompts/get",
     "params": {"name": "explain_file", "arguments": {"path": "docs/mcp.md"}}}, "http"))
_msg = _r["result"]["messages"][0]
check("5 prompts/get returns a user text message",
      _msg["role"] == "user" and _msg["content"]["type"] == "text", f"({_msg!r:.120})")
check("5 argument is substituted",
      "docs/mcp.md" in _msg["content"]["text"] and "{path}" not in _msg["content"]["text"])

_r = asyncio.run(mcp.handle_request(
    {"jsonrpc": "2.0", "id": 42, "method": "prompts/get",
     "params": {"name": "review_diff", "arguments": {}}}, "stdio"))
check("5 review_diff works without arguments (default ref HEAD)",
      "git_diff" in _r["result"]["messages"][0]["content"]["text"], f"({_r!r:.120})")

_r = asyncio.run(mcp.handle_request(
    {"jsonrpc": "2.0", "id": 43, "method": "prompts/get",
     "params": {"name": "fix_tests", "arguments": {"scope": "tests/test_mcp_phase2.py"}}}, "http"))
check("5 fix_tests substitutes the scope",
      "tests/test_mcp_phase2.py" in _r["result"]["messages"][0]["content"]["text"])

_r = asyncio.run(mcp.handle_request(
    {"jsonrpc": "2.0", "id": 44, "method": "prompts/get",
     "params": {"name": "nope"}}, "http"))
check("5 unknown prompt -> -32602", _r.get("error", {}).get("code") == -32602)
_r = asyncio.run(mcp.handle_request(
    {"jsonrpc": "2.0", "id": 45, "method": "prompts/get",
     "params": {"name": "explain_file", "arguments": {}}}, "http"))
check("5 missing required argument -> -32602", _r.get("error", {}).get("code") == -32602,
      f"({_r.get('error')})")
_r = asyncio.run(mcp.handle_request(
    {"jsonrpc": "2.0", "id": 46, "method": "prompts/get",
     "params": {"arguments": {}}}, "http"))
check("5 missing prompt name -> -32602", _r.get("error", {}).get("code") == -32602)
_r = asyncio.run(mcp.handle_request(
    {"jsonrpc": "2.0", "id": 47, "method": "prompts/get",
     "params": {"name": "explain_file", "arguments": "bad"}}, "http"))
check("5 non-object arguments -> -32602", _r.get("error", {}).get("code") == -32602)

# ── 6. Backward compatibility ─────────────────────────────────────────────
_r = asyncio.run(mcp.handle_request(
    {"jsonrpc": "2.0", "id": 50, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05"}}, "stdio"))
check("6 initialize still echoes 2024-11-05",
      _r["result"]["protocolVersion"] == "2024-11-05")
check("6 serverInfo unchanged",
      _r["result"]["serverInfo"]["name"] == "hivemind-mcp")
check("6 capabilities gained resources+prompts, tools kept",
      _r["result"]["capabilities"].get("tools") == {}
      and _r["result"]["capabilities"].get("resources") == {}
      and _r["result"]["capabilities"].get("prompts") == {})

_r = asyncio.run(mcp.handle_request(
    {"jsonrpc": "2.0", "id": 51, "method": "ping"}, "stdio"))
check("6 ping unchanged", _r["result"] == {})

os.environ["MCP_ALLOW_EXEC"] = "1"
os.environ["MCP_ALLOW_WRITE"] = "1"
_r = asyncio.run(mcp.handle_request(
    {"jsonrpc": "2.0", "id": 52, "method": "tools/list"}, "stdio"))
os.environ.pop("MCP_ALLOW_EXEC", None)
os.environ.pop("MCP_ALLOW_WRITE", None)
_tool = next(t for t in _r["result"]["tools"] if t["name"] == "read_file")
check("6 tools/list entries keep name/description/inputSchema",
      {"name", "description", "inputSchema"} <= set(_tool)
      and _tool["inputSchema"]["type"] == "object")
check("6 the four new tools are registered",
      {"git_diff", "git_commit", "run_tests", "model_status"}
      <= {t["name"] for t in _r["result"]["tools"]})
_r = asyncio.run(mcp.handle_request(
    {"jsonrpc": "2.0", "id": 53, "method": "tools/call",
     "params": {"name": "git_diff", "arguments": {"path": "../../etc/passwd"}}}, "stdio"))
check("6 unknown/legacy behaviour intact: bad path -> structured error, isError",
      _r["result"]["isError"] is True and "TOOL_ERROR" in _r["result"]["content"][0]["text"])

print()
print(f"SUMMARY mcp_phase2: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

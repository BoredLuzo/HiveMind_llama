# -*- coding: utf-8 -*-
"""LSP client tests: framing, URIs, diagnostics wait, timeouts, cleanup.

Drives the real client (infra/lsp_client.py) against a real child process
(tests/lsp_fake_server.py, a stdlib LSP server speaking Content-Length
framing), so protocol handling, URI round-trips, the diagnostics wait
window, server->client request answering and process cleanup are actually
exercised. No real language server, no network, no model.

Every client runs inside ONE event loop: a subprocess transport belongs to
the loop that created it - each flow builds its own manager inside its own
asyncio.run().
"""
import asyncio
import os
import sys
import tempfile
import time
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


from infra import lsp_client as LC  # noqa: E402
from infra import lsp_registry as LR  # noqa: E402

FAKE = Path(__file__).parent / "lsp_fake_server.py"
SAMPLE_TEXT = "def main():\n    pass\n\nx = 1\n"


def make_settings(mode: str, timeout_s: float | None = None) -> dict:
    settings = {"lsp_servers": {"python": [sys.executable, str(FAKE), mode]}}
    if timeout_s is not None:
        settings["lsp_client_timeout_s"] = timeout_s
    return settings


def new_ws() -> tuple[Path, Path]:
    ws = Path(tempfile.mkdtemp(prefix="hm_lsp_ws_"))
    sample = ws / "sample.py"
    sample.write_text(SAMPLE_TEXT, encoding="utf-8")
    return ws, sample


def same_path(a, b) -> bool:
    return str(a).lower() == str(b).lower()


# ── 1. Registry ---------------------------------------------------------
check("1a extension -> language id",
      LR.language_for_path("a.py") == "python"
      and LR.language_for_path("a.PY") == "python"
      and LR.language_for_path("b.ts") == "typescript"
      and LR.language_for_path("c.js") == "javascript"
      and LR.language_for_path("d.cpp") == "cpp"
      and LR.language_for_path("e.go") == "go"
      and LR.language_for_path("f.rs") == "rust"
      and LR.language_for_path("g.sh") == "bash"
      and LR.language_for_path("h.json") == "json"
      and LR.language_for_path("i.yaml") == "yaml"
      and LR.language_for_path("j.html") == "html"
      and LR.language_for_path("k.css") == "css"
      and LR.language_for_path("l.lua") == "lua")
check("1b unknown extension maps to None", LR.language_for_path("logo.png") is None)
check("1b2 tsx/jsx keep the server language but use the React document id",
      LR.language_for_path("a.tsx") == "typescript"
      and LR.language_for_path("b.jsx") == "javascript"
      and LR.document_language_for_path("a.tsx") == "typescriptreact"
      and LR.document_language_for_path("b.jsx") == "javascriptreact")

_r = LR.resolve("python")
check("1c resolve returns an existing binary or None",
      _r is None or _r[0].lower() in ("cmd", "cmd.exe")
      or Path(_r[0]).exists(), f"({_r})")

_ov = {"python": [sys.executable, "--version"]}
check("1d a found override replaces the defaults",
      LR.resolve("python", _ov) == [sys.executable, "--version"])
check("1e a missing override reports missing (no silent fallback)",
      LR.resolve("python", {"python": "definitely_missing_xyz_42"}) is None)
check("1f override strings split with backslashes intact",
      LR.split_command('"C:\\Program Files\\ls.exe" --stdio')
      == ["C:\\Program Files\\ls.exe", "--stdio"])
check("1g unknown language without override resolves to None",
      LR.resolve("brainfuck") is None)

_rows = LR.available({"rust": "definitely_missing_xyz_42"})
_names = {r["language"] for r in _rows}
_rust = next(r for r in _rows if r["language"] == "rust")
check("1h available lists all known languages plus override-only ones",
      {"python", "rust", "go", "css"} <= _names, f"({sorted(_names)})")
check("1i available rows carry language/command/found",
      all(set(r) == {"language", "command", "found"} for r in _rows))
check("1j available marks a missing override",
      _rust["found"] is False and _rust["command"] == "definitely_missing_xyz_42")
check("1k found rows show the logical command, not a cmd wrapper",
      all("cmd /d /c" not in r["command"] for r in _rows if r["found"]))

# ── 2. URI handling -----------------------------------------------------
_tricky = Path(tempfile.gettempdir()) / "täst file.py"
_uri = LC.path_to_uri(_tricky)
check("2a windows drive paths become file:///C:/... URIs",
      _uri.startswith("file:///")
      and "%20" in _uri and "t%C3%A4st" in _uri, f"({_uri})")
check("2b the URI decodes back to the same path",
      same_path(LC.uri_to_path(_uri), _tricky.resolve()))
check("2c non-file URIs pass through unchanged",
      LC.uri_to_path("untitled:Untitled-1") == "untitled:Untitled-1")
_loc = LC._decode_uris({"uri": _uri, "range": {"start": {"line": 1}}})
check("2d result locations decode uri -> path",
      same_path(_loc.get("path", ""), _tricky.resolve())
      and "uri" not in _loc and _loc["range"]["start"]["line"] == 1)
if os.name == "nt":
    # Pyright publishes file:///c%3A/... while the client opens
    # file:///C:/... - both must collapse onto one diagnostics key.
    _parts = _tricky.resolve().as_posix()
    _enc = "file:///" + _parts[0].lower() + "%3A" + _parts[2:]
    check("2e encoded drive URIs canonicalize to the client URI",
          LC.canonical_uri(_enc) == _uri, f"({LC.canonical_uri(_enc)} vs {_uri})")
    check("2f encoded drive URIs decode to the same path",
          same_path(LC.uri_to_path(_enc), _tricky.resolve()),
          f"({LC.uri_to_path(_enc)})")


# ── 3. Live flow: start, features, close --------------------------------
async def _live_flow():
    ws, sample = new_ws()
    mgr = LC.LspManager(ws, settings=make_settings("normal"))
    out = {"ws": ws, "sample": sample}
    out["start1"] = await mgr.start_language("python")
    out["start2"] = await mgr.start_language("python")
    conn = mgr._conns.get("python")
    out["pid1"] = conn._proc.pid if conn and conn._proc else None
    out["init_params"] = dict(conn.init_params) if conn else {}
    out["caps"] = dict(conn.server_capabilities) if conn else {}
    out["avail"] = mgr.available()
    out["diag"] = await mgr.diagnostics("sample.py")              # relative path
    out["diag_abs"] = await mgr.diagnostics(sample, wait_s=0.05)  # absolute path
    out["defn"] = await mgr.definition(sample, 0, 0)
    out["refs"] = await mgr.references(sample, 1, 4)
    out["hover"] = await mgr.hover(sample, 0, 0)
    out["syms"] = await mgr.document_symbols(sample)
    out["wsym"] = await mgr.workspace_symbols("main")
    out["rename"] = await mgr.rename(sample, 0, 4, "renamed")
    out["actions"] = await mgr.code_actions(sample, 0, 0, 0, 5)
    out["same_pid"] = (mgr._conns["python"]._proc.pid == out["pid1"])
    proc = mgr._conns["python"]._proc
    await mgr.close_all()
    out["rc"] = getattr(proc, "returncode", None)
    out["conns_after"] = dict(mgr._conns)
    return out


res = asyncio.run(_live_flow())
check("3a start_language succeeds and reuses the process",
      res["start1"] is True and res["start2"] is True and res["same_pid"],
      f"(pid={res['pid1']})")
_ip = res["init_params"]
check("3b initialize carries rootUri, processId and capabilities",
      same_path(LC.uri_to_path(_ip.get("rootUri", "")), res["ws"].resolve())
      and _ip.get("processId") == os.getpid()
      and "textDocument" in _ip.get("capabilities", {}),
      f"(rootUri={_ip.get('rootUri')!r})")
check("3c the server capabilities arrived",
      res["caps"].get("definitionProvider") is True)
_pyrow = next(r for r in res["avail"] if r["language"] == "python")
check("3d manager.available() reports the override command as found",
      _pyrow["found"] is True and "lsp_fake_server" in _pyrow["command"])

diag = res["diag"]
check("3e diagnostics wait for the publication (relative path)",
      len(diag) == 1 and diag[0]["message"] == f"fake diag sample.py|{len(SAMPLE_TEXT)}",
      f"({diag[0]['message'] if diag else '[]'})")
check("3f the diagnostic range is 0-based LSP-native",
      diag and diag[0]["range"]["start"] == {"line": 0, "character": 0})
check("3g the already-open document returns stored diagnostics at once",
      res["diag_abs"] == diag)

defn = res["defn"]
check("3h a single-object definition result normalizes to a list",
      len(defn) == 1 and same_path(defn[0].get("path", ""), res["sample"].resolve()),
      f"({defn})")
check("3i the definition range survived the decode",
      defn and defn[0]["range"]["start"]["line"] == 1)
refs = res["refs"]
check("3j references decode both locations",
      len(refs) == 2
      and all(same_path(r.get("path", ""), res["sample"].resolve()) for r in refs)
      and refs[0]["range"]["start"]["line"] == 2)
check("3k hover returns the server answer",
      isinstance(res["hover"], dict)
      and res["hover"]["contents"]["value"] == "fake hover sample.py")
syms = res["syms"]
check("3l document symbols decode DocumentSymbol and SymbolInformation",
      len(syms) == 2 and syms[0]["name"] == "fake_func"
      and same_path(syms[1]["location"].get("path", ""), res["sample"].resolve()))
wsym = res["wsym"]
check("3m workspace symbols carry a decoded location",
      len(wsym) == 1 and wsym[0]["name"] == "ws:main"
      and same_path(wsym[0]["location"].get("path", ""), res["sample"].resolve()))
ren = res["rename"]
_renkey = next(iter(ren["changes"]), None) if isinstance(ren, dict) else None
check("3n rename returns a WorkspaceEdit with local path keys",
      isinstance(ren, dict) and _renkey is not None
      and same_path(_renkey, res["sample"].resolve())
      and ren["changes"][_renkey][0]["newText"] == "renamed")
check("3o code actions receive the stored diagnostics as context",
      len(res["actions"]) == 1
      and res["actions"][0]["title"] == "fake quickfix (1 diag)",
      f"({res['actions'][0]['title'] if res['actions'] else '[]'})")
check("3p close_all stops the child cleanly (shutdown+exit -> rc 0)",
      res["rc"] == 0, f"(rc={res['rc']})")
check("3q close_all forgets the connections", res["conns_after"] == {})


# ── 4. didChange (full sync) after a file edit --------------------------
async def _change_flow():
    ws, sample = new_ws()
    sample.write_text("a = 1\n", encoding="utf-8")
    mgr = LC.LspManager(ws, settings=make_settings("normal"))
    first = await mgr.diagnostics(sample)
    sample.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
    second = await mgr.diagnostics(sample)
    conn = mgr._conns["python"]
    version = dict(conn.open_docs)
    proc = conn._proc
    await mgr.close_all()
    return first, second, version, getattr(proc, "returncode", None)


first, second, version, rc4 = asyncio.run(_change_flow())
check("4a the first sync reports the short content",
      len(first) == 1 and first[0]["message"].endswith("|6"),
      f"({first[0]['message'] if first else '[]'})")
check("4b didChange pushes the new content and fresh diagnostics arrive",
      len(second) == 1 and second[0]["message"].endswith("|18"),
      f"({second[0]['message'] if second else '[]'})")
check("4c the document version advanced to 2", list(version.values()) == [2],
      f"({version})")
check("4d the server exits cleanly after the change flow", rc4 == 0, f"(rc={rc4})")


# ── 4e. stale publication race (version guard) ─────────────────────────
async def _stale_flow():
    ws, sample = new_ws()
    mgr = LC.LspManager(ws, settings=make_settings("staleversion"))
    out = {}
    try:
        out["start"] = await mgr.start_language("python")
        out["first"] = await mgr.diagnostics("sample.py", wait_s=1.0)
        _new = "def main():\n    return 42\n\nprint(main())\n"
        sample.write_text(_new, encoding="utf-8")
        out["second"] = await mgr.diagnostics("sample.py", wait_s=1.0)
        out["new_len"] = len(_new)
    finally:
        await mgr.close_all()
    return out


_st = asyncio.run(_stale_flow())
check("4e the stale pre-change publication is ignored",
      _st["first"] and _st["second"]
      and _st["second"][0]["message"].endswith(f"|{_st['new_len']}"),
      f"({_st['second'][0]['message'] if _st['second'] else '[]'})")


# ── 4f. TSX sends the React document language id ──────────────────────
async def _tsx_flow():
    ws = Path(tempfile.mkdtemp(prefix="hm_lsp_tsx_"))
    sample = ws / "sample.tsx"
    sample.write_text("const x: number = 1;\n", encoding="utf-8")
    mgr = LC.LspManager(ws, settings={
        "lsp_servers": {"typescript": [sys.executable, str(FAKE), "normal"]}})
    try:
        out = {"start": await mgr.start_language("typescript")}
        out["diag"] = await mgr.diagnostics("sample.tsx", wait_s=1.0)
        return out
    finally:
        await mgr.close_all()


_tsx = asyncio.run(_tsx_flow())
check("4f TSX diagnostics arrive and the server saw typescriptreact",
      _tsx["start"] and _tsx["diag"]
      and _tsx["diag"][0].get("data", {}).get("language") == "typescriptreact",
      f"({_tsx['diag'][0].get('data') if _tsx['diag'] else '[]'})")


# ── 4g. LSP 3.17 pull diagnostics (rust-analyzer path) ─────────────────
async def _pull_flow():
    ws, sample = new_ws()
    mgr = LC.LspManager(ws, settings=make_settings("pullonly"))
    try:
        out = {"start": await mgr.start_language("python")}
        out["diag"] = await mgr.diagnostics("sample.py", wait_s=0.1)
        return out
    finally:
        await mgr.close_all()


_pull = asyncio.run(_pull_flow())
check("4g a server with diagnosticProvider answers via pull",
      _pull["start"] and _pull["diag"]
      and _pull["diag"][0]["message"].endswith(f"|{len(SAMPLE_TEXT)}"),
      f"({_pull['diag'][0]['message'] if _pull['diag'] else '[]'})")


# ── 4h. idle reaper closes unused servers ──────────────────────────────
async def _idle_flow():
    ws, sample = new_ws()
    mgr = LC.LspManager(ws, settings=make_settings("normal",
                                                   timeout_s=10))
    out = {}
    out["start"] = await mgr.start_language("python")
    conn1 = mgr._conns.get("python")
    out["pid1"] = conn1._proc.pid if conn1 else None
    out["reap_at_default"] = mgr.reap_idle(now=mgr._now() + 1)   # far below timeout
    # advance the injected clock beyond the 600 s default and reap
    out["reaped"] = mgr.reap_idle(now=mgr._now() + 10_000)
    out["conns_after"] = dict(mgr._conns)
    # next use restarts lazily
    diags = await mgr.diagnostics(sample, wait_s=1.0)
    conn2 = mgr._conns.get("python")
    out["pid2"] = conn2._proc.pid if conn2 else None
    out["diag_after_restart"] = len(diags)
    await mgr.close_all()
    return out


_idle = asyncio.run(_idle_flow())
check("4h1 nothing reaped below the idle timeout",
      _idle["reap_at_default"] == [] and _idle["start"] is True)
check("4h2 the reaper closes the idle server",
      _idle["reaped"] == ["python"] and _idle["conns_after"] == {})
check("4h3 the next call restarts the server lazily",
      _idle["pid2"] not in (None, _idle["pid1"])
      and _idle["diag_after_restart"] == 1,
      f"(pid {_idle['pid1']} -> {_idle['pid2']})")


async def _idle_off_flow():
    ws, sample = new_ws()
    mgr = LC.LspManager(ws, settings={**make_settings("normal"),
                                      "lsp_idle_timeout_s": 0})
    out = {"start": await mgr.start_language("python")}
    out["reaped"] = mgr.reap_idle(now=mgr._now() + 10_000)
    out["alive"] = "python" in mgr._conns
    await mgr.close_all()
    return out


_off = asyncio.run(_idle_off_flow())
check("4h4 lsp_idle_timeout_s=0 disables the reaper",
      _off["reaped"] == [] and _off["alive"])


# ── 4i. workspace settings are PUSHED after the handshake ──────────────
async def _cfgpush_flow():
    ws = Path(tempfile.mkdtemp(prefix="hm_lsp_push_"))
    (ws / ".vscode").mkdir()
    (ws / ".vscode" / "settings.json").write_text(
        '{"python": {"analysis": {"typeCheckingMode": "basic"}}}',
        encoding="utf-8")
    _cfg = make_settings("normal")
    # LSP-SECURITY (2026-11-03): repo settings are forwarded only on opt-in.
    _cfg["lsp_forward_workspace_settings"] = True
    mgr = LC.LspManager(ws, settings=_cfg)
    try:
        out = {"start": await mgr.start_language("python")}
        # the fake answers a non-LSP probe with its recorded state
        conn = mgr._conns.get("python")
        out["state"] = await conn.request("fake/state", {}, timeout=5)
        return out
    finally:
        await mgr.close_all()


_push = asyncio.run(_cfgpush_flow())
check("4i didChangeConfiguration carries the workspace settings",
      _push["start"] and _push["state"]["config_push"]
      == {"python": {"analysis": {"typeCheckingMode": "basic"}}},
      f"({_push['state']['config_push']})")


# ── 5. Server->client requests are answered -----------------------------
async def _cfgprobe_flow():
    ws, sample = new_ws()
    mgr = LC.LspManager(ws, settings=make_settings("cfgprobe"))
    diags = await mgr.diagnostics(sample, wait_s=3.0)
    await mgr.close_all()
    return diags


diags5 = asyncio.run(_cfgprobe_flow())
check("5a diagnostics arrive after the fake's configuration/register/progress"
      " probes -> the client answers server->client requests",
      len(diags5) == 1, f"({len(diags5)})")


# ── 6. The diagnostics wait window --------------------------------------
async def _slowdiag_flow():
    # two documents, so the checks cannot bleed into each other: file A's
    # late didOpen publication (0.6 s) must not satisfy file B's wait - a
    # publication for ANOTHER uri is not a fresh one for this document
    ws, sample = new_ws()
    sample.write_text("x = 1\n", encoding="utf-8")
    other = ws / "other.py"
    other.write_text("x = 1\ny = 2\n", encoding="utf-8")
    mgr = LC.LspManager(ws, settings=make_settings("slowdiag"))
    await mgr.start_language("python")   # exclude process startup from dt
    t0 = time.time()
    empty = await mgr.diagnostics(sample, wait_s=0.1)
    dt_empty = time.time() - t0
    t1 = time.time()
    fresh = await mgr.diagnostics(other, wait_s=3.0)
    dt_fresh = time.time() - t1
    await mgr.close_all()
    return empty, dt_empty, fresh, dt_fresh


empty6, dt_empty, fresh6, dt_fresh = asyncio.run(_slowdiag_flow())
check("6a a slow publication is not mistaken for a fast one (short wait -> [])",
      empty6 == [] and dt_empty < 1.0, f"(dt={dt_empty:.2f}s)")
check("6b a longer wait catches the slow publication",
      len(fresh6) == 1 and fresh6[0]["message"].endswith("|12"),
      f"({fresh6[0]['message'] if fresh6 else '[]'})")
check("6c the wait really waited", dt_fresh >= 0.5, f"(dt={dt_fresh:.2f}s)")


# ── 7. Timeouts ---------------------------------------------------------
async def _hang_flow():
    ws, _sample = new_ws()
    mgr = LC.LspManager(ws, settings=make_settings("hang", timeout_s=2.0))
    t0 = time.time()
    ok = await mgr.start_language("python")
    dt = time.time() - t0
    await mgr.close_all()
    return ok, dt


ok7, dt7 = asyncio.run(_hang_flow())
check("7a a hanging handshake fails the start without blocking",
      ok7 is False and dt7 < 15, f"(ok={ok7}, dt={dt7:.1f}s)")


async def _die_flow():
    ws, _sample = new_ws()
    mgr = LC.LspManager(ws, settings=make_settings("die", timeout_s=5.0))
    ok = await mgr.start_language("python")
    await mgr.close_all()
    return ok


check("7b a server dying during initialize reports False", asyncio.run(_die_flow()) is False)


async def _noanswer_flow():
    ws, sample = new_ws()
    mgr = LC.LspManager(ws, settings=make_settings("noanswer", timeout_s=1.5))
    await mgr.start_language("python")
    conn = mgr._conns["python"]
    proc_ref = conn._proc
    err = ""
    try:
        await mgr.hover(sample, 0, 0)
    except LC.LspError as exc:
        err = str(exc)
    rc_killed = getattr(proc_ref, "returncode", None)
    dead = conn.dead
    await mgr.close_all()
    return err, rc_killed, dead


err7, rc_killed, dead7 = asyncio.run(_noanswer_flow())
check("7c a request the server never answers times out",
      "timed out" in err7, f"({err7[:60]!r})")
check("7d the timed-out connection is dead and its process was killed"
      " (no zombie)",
      dead7 is True and rc_killed is not None, f"(rc={rc_killed})")


# ── 8. Self-healing: a dead connection restarts lazily ------------------
async def _restart_flow():
    ws, sample = new_ws()
    mgr = LC.LspManager(ws, settings=make_settings("normal"))
    await mgr.start_language("python")
    pid1 = mgr._conns["python"]._proc.pid
    mgr._conns["python"]._die("test: marked dead")
    hover = await mgr.hover(sample, 0, 0)
    conn2 = mgr._conns["python"]
    pid2 = conn2._proc.pid if conn2 and conn2._proc else None
    proc = conn2._proc
    await mgr.close_all()
    return hover, pid1, pid2, getattr(proc, "returncode", None)


hover8, pid1, pid2, rc8 = asyncio.run(_restart_flow())
check("8a a marked-dead connection is reaped and restarted on the next use",
      isinstance(hover8, dict) and pid2 is not None and pid1 != pid2,
      f"(pid {pid1} -> {pid2})")
check("8b the restarted server also closes cleanly", rc8 == 0, f"(rc={rc8})")


# ── 9. Unknown extension / missing server -------------------------------
async def _unknown_flow():
    ws, _sample = new_ws()
    mgr = LC.LspManager(ws, settings=make_settings("normal"))
    d = await mgr.diagnostics("logo.png")
    h = await mgr.hover("logo.png", 0, 0)
    r = await mgr.rename("logo.png", 0, 0, "x")
    a = await mgr.code_actions("logo.png", 0, 0, 0, 0)
    conns = dict(mgr._conns)
    await mgr.close_all()
    return d, h, r, a, conns


d9, h9, r9, a9, conns9 = asyncio.run(_unknown_flow())
check("9a an unmapped extension answers empty without starting a server",
      d9 == [] and h9 is None and r9 is None and a9 == []
      and conns9 == {})


async def _missing_flow():
    ws, _sample = new_ws()
    mgr = LC.LspManager(ws, settings={"lsp_servers": {
        "rust": "definitely_missing_xyz_42"}})
    ok = await mgr.start_language("rust")
    rows = mgr.available()
    rust = next(r for r in rows if r["language"] == "rust")
    await mgr.close_all()
    return ok, rust, rows


ok9, rust9, rows9 = asyncio.run(_missing_flow())
check("9b a missing server returns False without raising",
      ok9 is False, f"({ok9})")
check("9c the status row reports the missing override",
      rust9["found"] is False and rust9["command"] == "definitely_missing_xyz_42")

print()
print(f"SUMMARY lsp_client: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

"""Tests for the isolated placeholder workspace (default_workspace) + /new confirmation.

Covers:
  1. resolve_workspace / bootstrap_workspace / effective_run_workspace never
     fall back to the HiveMind install root (AGENTS.md / server.py).
  2. Direct/chat modes without an explicit chat/UI workspace skip stale
     last_workspace.json and poisoned HIVEMIND_WORKSPACE env vars.
  3. Placeholder helpers (list_placeholder_entries, clear_placeholder_workspace)
     handle nested files/empty subdirs and never follow symlinks outside.
  4. FastAPI routes GET /workspace/placeholder, POST /workspace/placeholder/clear,
     and PUT /chats/{id} clearing legacy sidecar workspace.
  5. Telegram Gateway resolves phone chats without /workspace to placeholder,
     renders accurate workspace labels (never lying 'engine default' when custom),
     and prompts before /new when default_workspace contains files.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from utils import workspace_resolve as WSR
from routers import chats as chats_router
from routers import core as core_router
from hivemind_gateway.bridge import RunBridge
from hivemind_gateway.config import GatewayConfig
from hivemind_gateway.state import GatewayState
from hivemind_gateway.auth import ParsedUpdate
from hivemind_gateway import main as gw_main

passed = 0
failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}{extra}")
    else:
        failed += 1
        print(f"  FAIL {name}{extra}")


tmp_root = Path(tempfile.mkdtemp(prefix="hvm_ph_test_"))
orig_ph_dir = WSR._PLACEHOLDER_WS_DIR
orig_last_ws = WSR._LAST_WS_FILE
orig_app_root = WSR._APP_ROOT

ph_dir = tmp_root / "default_workspace"
last_ws_file = tmp_root / "context" / "last_workspace.json"
old_proj_dir = tmp_root / "old_project"
old_proj_dir.mkdir(parents=True, exist_ok=True)
env_proj_dir = tmp_root / "env_project"
env_proj_dir.mkdir(parents=True, exist_ok=True)
custom_proj_dir = tmp_root / "custom_project"
custom_proj_dir.mkdir(parents=True, exist_ok=True)
outside_dir = tmp_root / "outside_do_not_delete"
outside_dir.mkdir(parents=True, exist_ok=True)
(outside_dir / "keep_me.txt").write_text("safe", encoding="utf-8")

WSR._PLACEHOLDER_WS_DIR = ph_dir
WSR._LAST_WS_FILE = last_ws_file

old_env = os.environ.get("HIVEMIND_WORKSPACE")

try:
    # ── 1. Workspace resolution isolation ───────────────────────────────────
    WSR.save_last_workspace(str(old_proj_dir))
    os.environ["HIVEMIND_WORKSPACE"] = str(env_proj_dir)

    for m in ("chat", "simple", "direct"):
        ws, src = WSR.resolve_workspace({"mode": m, "workspace": ""}, None, "hello")
        check(
            f"direct mode={m!r} without workspace skips stale last_used/env -> placeholder",
            src == "cwd_fallback" and Path(ws).resolve() == ph_dir.resolve()
            and not (Path(ws) / "AGENTS.md").exists(),
            f" (ws={ws}, src={src})",
        )

    ws_explicit, src_exp = WSR.resolve_workspace(
        {"mode": "chat", "workspace": str(custom_proj_dir)}, None, "hello",
    )
    check(
        "direct mode still honors explicit UI workspace",
        src_exp == "ui_setting" and Path(ws_explicit).resolve() == custom_proj_dir.resolve(),
        f" (ws={ws_explicit}, src={src_exp})",
    )

    ws_duo_last, src_duo_last = WSR.resolve_workspace(
        {"mode": "code_duo", "workspace": ""}, None, "build",
    )
    check(
        "non-direct mode can still fall back to last_used",
        src_duo_last == "last_used" and Path(ws_duo_last).resolve() == old_proj_dir.resolve(),
    )

    # Empty last_workspace and empty env -> never falls back to app root
    WSR._LAST_WS_FILE = tmp_root / "missing_last.json"
    os.environ.pop("HIVEMIND_WORKSPACE", None)
    ws_duo_empty, src_duo_empty = WSR.resolve_workspace(
        {"mode": "code_duo", "workspace": ""}, None, "build",
    )
    check(
        "empty fallback uses placeholder workspace, never app root",
        src_duo_empty == "cwd_fallback"
        and Path(ws_duo_empty).resolve() == ph_dir.resolve()
        and Path(ws_duo_empty).resolve() != orig_app_root.resolve(),
    )

    ws_boot, src_boot = WSR.bootstrap_workspace("", "", "")
    check(
        "bootstrap_workspace empty fallback uses placeholder",
        src_boot == "cwd_fallback" and Path(ws_boot).resolve() == ph_dir.resolve(),
    )
    check(
        "effective_run_workspace empty fallback uses placeholder",
        Path(WSR.effective_run_workspace("", "")).resolve() == ph_dir.resolve(),
    )

    # save_last_workspace & commit_allowed_for reject placeholder
    WSR._LAST_WS_FILE = last_ws_file
    WSR.save_last_workspace(str(ph_dir))
    check(
        "save_last_workspace never overwrites last project with placeholder",
        WSR.load_last_workspace() == str(old_proj_dir),
    )
    check(
        "commit_allowed_for stays False for placeholder workspace",
        WSR.commit_allowed_for(str(ph_dir), {"workspace": str(ph_dir)}) is False,
    )

    # ── 2. Placeholder listing & clearing safety ────────────────────────────
    (ph_dir / "note.txt").write_text("hi", encoding="utf-8")
    (ph_dir / "sub" / "nested.html").parent.mkdir(parents=True, exist_ok=True)
    (ph_dir / "sub" / "nested.html").write_text("<h1>x</h1>", encoding="utf-8")
    (ph_dir / "empty_dir").mkdir(exist_ok=True)
    try:
        os.symlink(outside_dir, ph_dir / "ext_link", target_is_directory=True)
        has_symlink = True
    except OSError:
        has_symlink = False

    entries = WSR.list_placeholder_entries()
    check(
        "list_placeholder_entries finds files, nested files, empty dir",
        "note.txt" in entries and "sub/nested.html" in entries and "empty_dir/" in entries,
        f" ({entries})",
    )
    cleared_n = WSR.clear_placeholder_workspace()
    check(
        "clear_placeholder_workspace deletes files and keeps placeholder dir",
        cleared_n >= 3 and ph_dir.is_dir() and WSR.list_placeholder_entries() == [],
        f" (cleared={cleared_n}, left={WSR.list_placeholder_entries()})",
    )
    if has_symlink:
        check(
            "clearing unlinks symlinks without deleting external target files",
            (outside_dir / "keep_me.txt").exists(),
        )

    bad_app_err = False
    try:
        WSR.clear_placeholder_workspace(orig_app_root)
    except ValueError:
        bad_app_err = True
    check("clear_placeholder_workspace refuses app root", bad_app_err)

    # ── 3. HTTP endpoints + legacy sidecar clear ────────────────────────────
    (ph_dir / "page.html").write_text("<h1>hello</h1>", encoding="utf-8")
    app = FastAPI()
    app.include_router(core_router.router)
    app.include_router(chats_router.router)
    client = TestClient(app)

    r_info = client.get("/workspace/placeholder")
    check(
        "GET /workspace/placeholder reports files",
        r_info.status_code == 200
        and Path(r_info.json()["path"]).resolve() == ph_dir.resolve()
        and r_info.json()["files"] == ["page.html"]
        and r_info.json()["count"] == 1
        and r_info.json()["is_empty"] is False,
        f" ({r_info.status_code}, {r_info.text})",
    )

    r_clr = client.post("/workspace/placeholder/clear", json={})
    check(
        "POST /workspace/placeholder/clear empties folder",
        r_clr.status_code == 200 and r_clr.json()["cleared"] == 1
        and WSR.list_placeholder_entries() == [],
    )

    # Legacy chat sidecar reset via PUT /chats/{id} with workspace=""
    sess_tmp = tmp_root / "sessions"
    sess_tmp.mkdir(parents=True, exist_ok=True)
    import core.state as _cstate
    import context.chat as cc_mod
    orig_sess_dir = _cstate._SESSIONS_DIR
    orig_cache_lock = chats_router._cache_lock
    orig_state_lock = _cstate._cache_lock
    orig_cache = dict(chats_router._chats_cache)
    orig_loaded = chats_router._cache_loaded
    orig_cc_sess = cc_mod._SESSIONS_DIR
    orig_cc_cache = cc_mod._chats_cache
    orig_cc_lock = cc_mod._cache_lock
    try:
        lk = orig_cache_lock or threading.Lock()
        chats_router._cache_lock = lk
        _cstate._cache_lock = lk
        _cstate._SESSIONS_DIR = sess_tmp
        chats_router._cache_loaded = False
        _cstate._cache_loaded = False
        cc_mod.init_chat_context(sess_tmp, chats_cache=chats_router._chats_cache,
                                 cache_lock=lk)
        with lk:
            chats_router._chats_cache.clear()
            chats_router._cache_loaded = True
            _cstate._cache_loaded = True

        cid = "deadbeef"
        chat_file = sess_tmp / f"2026-11-04_TG_{cid}.json"
        ctx_file = sess_tmp / f"2026-11-04_TG_{cid}.context.json"
        chat_file.write_text(json.dumps({
            "id": cid, "title": "[TG] Old", "created_at": "2026-11-04T10:00:00",
            "updated_at": "2026-11-04T10:00:00", "messages": [], "rev": 1,
        }), encoding="utf-8")
        ctx_file.write_text(json.dumps({"workspace": str(old_proj_dir)}), encoding="utf-8")
        with lk:
            chats_router._chats_cache[cid] = {
                "id": cid, "title": "[TG] Old", "created_at": "2026-11-04T10:00:00",
                "updated_at": "2026-11-04T10:00:00", "rev": 1, "_file": str(chat_file),
                "_msg_count": 0, "_tokens": 0, "_preview": "",
            }

        check("legacy chat inherits sidecar before reset",
              client.get(f"/chats/{cid}").json().get("workspace") == str(old_proj_dir))
        r_put = client.put(f"/chats/{cid}", json={"workspace": ""})
        check(
            "PUT /chats/{id} with workspace='' clears sidecar workspace too",
            r_put.status_code == 200 and not client.get(f"/chats/{cid}").json().get("workspace"),
        )
    finally:
        _cstate._SESSIONS_DIR = orig_sess_dir
        chats_router._cache_lock = orig_cache_lock
        _cstate._cache_lock = orig_state_lock
        chats_router._cache_loaded = orig_loaded
        _cstate._cache_loaded = orig_loaded
        cc_mod._SESSIONS_DIR = orig_cc_sess
        cc_mod._chats_cache = orig_cc_cache
        cc_mod._cache_lock = orig_cc_lock
        chats_router._chats_cache.clear()
        chats_router._chats_cache.update(orig_cache)

    # ── 4. Telegram Gateway: placeholder routing + /new confirmation flow ───
    class _FakeResp:
        def __init__(self, code=200, body=None):
            self.status_code = code
            self._body = body or {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return self._body

    class _FakeHive:
        def __init__(self, ph_path: Path):
            self.ph_path = str(ph_path)
            self.ph_files: list[str] = []
            self.created: list[str] = []
            self.chat_ws = ""
            self.stream_bodies: list[dict] = []
            self.clear_calls = 0

        async def settings(self):
            return {"mode": "simple", "workspace": str(custom_proj_dir),
                    "telegram_phone_restricted": False}

        async def journal(self, run_id="", after=0):
            return {"active": False}

        async def get_chats(self):
            return []

        async def get_chat(self, cid):
            return _FakeResp(200, {"id": cid, "workspace": self.chat_ws, "messages": [], "rev": 1})

        async def put_chat_messages(self, cid, msgs, rev):
            return _FakeResp(200, {"rev": rev + 1})

        async def put_chat_meta(self, cid, fields):
            if "workspace" in fields:
                self.chat_ws = str(fields["workspace"] or "")
            return _FakeResp(200, {"rev": 2})

        async def create_chat(self, title):
            self.created.append(title)
            self.chat_ws = ""
            return {"ok": True, "id": f"tg{len(self.created)}", "rev": 1,
                    "chat": {"created_at": "2026-11-18T20:00:00"}}

        async def placeholder_status(self):
            return {
                "ok": True,
                "path": self.ph_path,
                "files": list(self.ph_files),
                "count": len(self.ph_files),
                "is_empty": len(self.ph_files) == 0,
            }

        async def clear_placeholder(self):
            self.clear_calls += 1
            n = len(self.ph_files)
            self.ph_files.clear()
            return {"ok": True, "path": self.ph_path, "cleared": n}

        async def stream(self, q, cid, images=None, mode="", overrides=None, workspace=""):
            self.stream_bodies.append({"q": q, "cid": cid, "workspace": workspace})
            yield {"type": "run_id", "run_id": "r-ph-1"}
            yield {"type": "token", "content": "ok"}
            yield {"type": "done", "stop_reason": "completed"}

    class _FakeApi:
        def __init__(self):
            self.toasts: list[tuple[str, str]] = []
            self.edits: list[tuple[int, str]] = []

        async def answer_callback_query(self, cid, text=""):
            self.toasts.append((cid, text))
            return {"ok": True}

        async def edit_message_text(self, chat_id, msg_id, new_text):
            self.edits.append((msg_id, new_text))
            return {"ok": True}

    async def _test_gateway():
        st = GatewayState(tmp_root / "gw_state.json")
        st.set_owner(12345, "2026-11-18T20:00:00")
        gw = gw_main.Gateway.__new__(gw_main.Gateway)
        gw.owner_id = 12345
        gw.state = st
        gw.cfg = GatewayConfig()
        gw.api = _FakeApi()
        gw.hive = _FakeHive(ph_dir)
        gw.bridge = RunBridge(gw.hive, st, gw.cfg, gw)
        gw._pending_cmd = None
        gw.sent: list[dict] = []

        async def _send_message(text, parse_mode=None, reply_markup=None):
            gw.sent.append({"text": text, "markup": reply_markup})
            return {"message_id": len(gw.sent)}

        async def _reply(chat_id, text, reply_to_message_id=None, reply_markup=None):
            gw.sent.append({"text": text, "markup": reply_markup})

        async def _edit_message(mid, text):
            gw.api.edits.append((mid, text))

        gw.send_message = _send_message
        gw.reply = _reply
        gw.edit_message = _edit_message

        # Run without /workspace -> routes to placeholder & prints honest label
        await gw.bridge.start_text_run("hi there")
        info_msg = next((m["text"] for m in gw.sent if m["text"].startswith("ℹ️ Run")), "")
        check(
            "Telegram run without /workspace starts in default_workspace placeholder",
            gw.hive.stream_bodies[-1]["workspace"] == str(ph_dir)
            and "workspace: default_workspace (placeholder)" in info_msg,
            f" (ws={gw.hive.stream_bodies[-1]['workspace']}, info={info_msg!r})",
        )

        # Simulated legacy chat with custom workspace -> info bubble MUST NOT say workspace: engine default
        gw.sent.clear()
        gw.hive.chat_ws = str(old_proj_dir)
        await gw.bridge.start_text_run("which workspace")
        info_msg2 = next((m["text"] for m in gw.sent if m["text"].startswith("ℹ️ Run")), "")
        check(
            "Telegram info card displays actual chat workspace instead of lying 'engine default'",
            f"workspace: {old_proj_dir.name}" in info_msg2 and "workspace: engine default" not in info_msg2,
            f" (info={info_msg2!r})",
        )

        # /workspace default resets back to placeholder
        rst_msg = await gw.bridge.workspace_text("default")
        check(
            "/workspace default resets custom workspace back to placeholder",
            "default_workspace" in rst_msg and gw.hive.chat_ws == ""
            and "workspace: default_workspace (placeholder)" in await gw.bridge.status_text(),
        )

        # /new when placeholder is EMPTY -> creates new chat immediately, no prompt
        before_empty_n = len(gw.hive.created)
        gw.sent.clear()
        await gw.cmd_new(ParsedUpdate(1, "message", 12345, 12345, "private", "/new", 0, False, 10, None))
        check(
            "/new on empty placeholder starts new chat without confirmation",
            len(gw.hive.created) == before_empty_n + 1
            and gw.sent[-1]["markup"] is None
            and "New HiveMind chat" in gw.sent[-1]["text"],
        )

        # /new when placeholder HAS FILES -> asks before clearing!
        before_prompt_n = len(gw.hive.created)
        before_prompt_clr = gw.hive.clear_calls
        gw.hive.ph_files = ["app.py", "notes.md"]
        gw.sent.clear()
        await gw.cmd_new(ParsedUpdate(2, "message", 12345, 12345, "private", "/new", 0, False, 11, None))
        ask_msg = gw.sent[-1]
        kb = (ask_msg.get("markup") or {}).get("inline_keyboard") or [[]]
        cb_yes = kb[0][0]["callback_data"]
        cb_no = kb[0][1]["callback_data"]
        check(
            "/new with placeholder files prompts user with file list and confirm/cancel buttons",
            len(gw.hive.created) == before_prompt_n
            and gw.hive.clear_calls == before_prompt_clr
            and "app.py" in ask_msg["text"]
            and cb_yes.startswith("newchat:y:")
            and cb_no.startswith("newchat:n:")
            and len(cb_yes.encode()) <= 64,
            f" (created={len(gw.hive.created)} vs {before_prompt_n}, clr={gw.hive.clear_calls}, yes={cb_yes!r}, no={cb_no!r})",
        )

        # Cancel keeps files and chat
        gw.sent.clear()
        await gw.handle_newchat_callback(
            ParsedUpdate(3, "callback_query", 12345, 12345, "private", cb_no, 0, False, 12, "cb-no"),
        )
        check(
            "tapping Cancel keeps files and does not start new chat",
            len(gw.hive.created) == before_prompt_n
            and gw.hive.clear_calls == before_prompt_clr
            and gw.hive.ph_files == ["app.py", "notes.md"]
            and any("cancelled" in m["text"].lower() for m in gw.sent),
            f" (sent={gw.sent!r}, toasts={gw.api.toasts!r}, ph={gw.hive.ph_files!r})",
        )

        # Stale confirm nonce after cancel is rejected safely
        gw.sent.clear()
        await gw.handle_newchat_callback(
            ParsedUpdate(4, "callback_query", 12345, 12345, "private", cb_yes, 0, False, 12, "cb-stale"),
        )
        check(
            "stale/replayed confirm button is rejected",
            gw.hive.clear_calls == before_prompt_clr
            and gw.hive.ph_files == ["app.py", "notes.md"]
            and any("expired" in t.lower() for _, t in gw.api.toasts),
            f" (clr={gw.hive.clear_calls}, ph={gw.hive.ph_files!r}, toasts={gw.api.toasts!r})",
        )

        # Confirm via full process_update clears placeholder and creates new chat
        gw.sent.clear()
        gw.limiter = gw_main.RateLimiter(20)
        await gw.cmd_new(ParsedUpdate(5, "message", 12345, 12345, "private", "/new", 0, False, 13, None))
        cb_yes2 = gw.sent[-1]["markup"]["inline_keyboard"][0][0]["callback_data"]
        gw.sent.clear()
        await gw_main.process_update(gw, {
            "update_id": 6,
            "callback_query": {
                "id": "cb-yes",
                "from": {"id": 12345, "is_bot": False},
                "data": cb_yes2,
                "message": {
                    "message_id": 14,
                    "chat": {"id": 12345, "type": "private"},
                    "date": 0,
                },
            },
        })
        check(
            "tapping Confirm clears placeholder workspace and creates new chat",
            len(gw.hive.created) == before_prompt_n + 1
            and gw.hive.clear_calls == before_prompt_clr + 1
            and gw.hive.ph_files == []
            and any("Cleared 2 file(s)" in m["text"] and "New HiveMind chat" in m["text"] for m in gw.sent),
            f" (created={len(gw.hive.created)} vs {before_prompt_n+1}, clr={gw.hive.clear_calls}, ph={gw.hive.ph_files!r}, sent={gw.sent!r})",
        )

    asyncio.run(_test_gateway())

    # ── 5. Packaging & UI wiring checks ─────────────────────────────────────
    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    check(
        "UI newChat checks and clears placeholder workspace and retry skips prompt",
        "/workspace/placeholder" in app_js
        and "/workspace/placeholder/clear" in app_js
        and "S._skipPlaceholderClear = true;" in app_js,
    )
    check(
        "packaging/updater excludes protect default_workspace",
        "/default_workspace/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
        and "'default_workspace'" in (ROOT / "deploy" / "update_prune.ps1").read_text(encoding="utf-8")
        and '"default_workspace"' in (ROOT / "deploy" / "updater.py").read_text(encoding="utf-8")
        and "--exclude '/default_workspace'" in (ROOT / "deploy" / "install_linux.sh").read_text(encoding="utf-8")
        and "--exclude='./default_workspace'" in (ROOT / "deploy" / "install_linux.sh").read_text(encoding="utf-8"),
    )

finally:
    WSR._PLACEHOLDER_WS_DIR = orig_ph_dir
    WSR._LAST_WS_FILE = orig_last_ws
    if old_env is None:
        os.environ.pop("HIVEMIND_WORKSPACE", None)
    else:
        os.environ["HIVEMIND_WORKSPACE"] = old_env

print()
print(f"SUMMARY placeholder_workspace: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

"""Regression suite for GLM-5.4 Slice 1 audit fixes.

Covers:
  1. Medium: Corrupt `settings.json` backup + automatic-clobber guard (`settings.py`, `server.py`).
  2. A1: Meta-only `/run/journal` idle polling in both Telegram Gateway (`bridge.py`)
     and Web UI (`static/js/stream_transport.js`).
  3. A3: Event-loop offload (`asyncio.to_thread`) in `routers/chats.py` (`create_chat`,
     `get_chat`) and `routers/websearch.py` (`websearch_config`).
  4. A6: Resolved literal bot token scrubbing in `hivemind_gateway/render.py`,
     `send.py`, and `main.py`.

Run: python tests/test_glm54_slice1_fixes.py
Exit 0 = all pass, Exit 1 = failure.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import settings as S  # noqa: E402
from hivemind_gateway import render as gw_render  # noqa: E402
from hivemind_gateway import send as gw_send  # noqa: E402
from hivemind_gateway.bridge import JOURNAL_META_ONLY_AFTER, RunBridge  # noqa: E402
from hivemind_gateway.config import GatewayConfig  # noqa: E402
from hivemind_gateway.state import GatewayState  # noqa: E402
from tests._harness import Harness  # noqa: E402
from tests.ui_sources import read_ui_module  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


def test_corrupt_settings_guard_and_backup() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="glm54_set_"))
    orig_file = S.SETTINGS_FILE
    orig_key = S._load_cache_key
    orig_data = S._load_cache_data
    try:
        sf = tmp / "settings.json"
        S.SETTINGS_FILE = sf
        S._load_cache_key = None
        S._load_cache_data = None

        check("1a missing settings.json is not corrupt", not S.is_settings_file_corrupt(sf))

        sf.write_text('{"server_port": 8123, "bad": [}', encoding="utf-8")
        check("1b invalid JSON triggers is_settings_file_corrupt", S.is_settings_file_corrupt(sf))

        arr_f = tmp / "arr.json"
        arr_f.write_text("[1, 2, 3]", encoding="utf-8")
        check("1c non-dict JSON triggers is_settings_file_corrupt", S.is_settings_file_corrupt(arr_f))

        mutate_threw = False
        try:
            S.mutate_settings(lambda d: d.__setitem__("x", 1))
        except json.JSONDecodeError:
            mutate_threw = True
        check("1d mutate_settings refuses to clobber corrupt JSON", mutate_threw)
        check("1e corrupt file untouched after mutate_settings failure", sf.read_text(encoding="utf-8") == '{"server_port": 8123, "bad": [}')

        S.save_settings({"server_port": 8001})
        backups = sorted(tmp.glob("settings.json.corrupt-*"))
        check(
            "1f explicit save_settings backs up corrupt file verbatim before replacing",
            len(backups) == 1 and backups[0].read_text(encoding="utf-8") == '{"server_port": 8123, "bad": [}' and json.loads(sf.read_text(encoding="utf-8"))["server_port"] == 8001,
            f"backups={[b.name for b in backups]}",
        )
        check("1g overwritten valid settings.json is no longer corrupt", not S.is_settings_file_corrupt(sf))

        srv_src = (ROOT / "server.py").read_text(encoding="utf-8")
        check(
            "1h server.py guards automatic background saves on corrupt disk file",
            "is_settings_file_corrupt" in srv_src
            and srv_src.count("is_settings_file_corrupt()") >= 3,
        )
    finally:
        S.SETTINGS_FILE = orig_file
        S._load_cache_key = orig_key
        S._load_cache_data = orig_data
        shutil.rmtree(tmp, ignore_errors=True)


async def test_gateway_idle_meta_only_journal() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="glm54_gw_"))
    try:
        class _FakeMs:
            def __init__(self) -> None:
                self.messages: list[str] = []

            async def send_message(self, text: str, **_kw):
                self.messages.append(text)
                return None

            async def flush_status(self, *_a, **_kw):
                return None

        class _FakeHive:
            def __init__(self) -> None:
                self.calls: list[tuple[str, int]] = []
                self.responses: dict[tuple[str, int], dict] = {}
                self.default_idle: dict = {"active": False}

            async def settings(self) -> dict:
                return {"telegram_mirror_enabled": True}

            async def journal(self, run_id: str = "", after: int = 0) -> dict:
                self.calls.append((run_id, after))
                if (run_id, after) in self.responses:
                    return dict(self.responses[(run_id, after)])
                return dict(self.default_idle)

        def _make_bridge(hive: _FakeHive) -> tuple[RunBridge, _FakeMs]:
            st = GatewayState(tmp / f"state_{time.monotonic_ns()}.json")
            cfg = GatewayConfig(hive_base_url="http://127.0.0.1:8001")
            ms = _FakeMs()
            br = RunBridge(hive, st, cfg, ms)
            return br, ms

        # 2a: pure idle tick uses JOURNAL_META_ONLY_AFTER and makes 1 request
        hive_idle = _FakeHive()
        br_idle, _ = _make_bridge(hive_idle)
        await br_idle.mirror_tick()
        check(
            "2a idle mirror_tick sends JOURNAL_META_ONLY_AFTER (no full frame download)",
            hive_idle.calls == [("", JOURNAL_META_ONLY_AFTER)],
            repr(hive_idle.calls),
        )

        # 2b: fresh live run peeked meta-only triggers exactly one full fetch (rid, 0)
        hive_live = _FakeHive()
        hive_live.responses[("", JOURNAL_META_ONLY_AFTER)] = {
            "active": True,
            "run_id": "live-99",
            "done": False,
            "aborted": False,
            "n": 2,
            "ts": time.time(),
            "frames": [],
        }
        hive_live.responses[("live-99", 0)] = {
            "active": True,
            "run_id": "live-99",
            "done": False,
            "aborted": False,
            "n": 2,
            "ts": time.time(),
            "frames": [
                'data: {"type": "run_id", "run_id": "live-99"}',
                'data: {"type": "token", "content": "Hello live"}',
            ],
        }
        br_live, ms_live = _make_bridge(hive_live)
        await br_live.mirror_tick()
        mirror_live = br_live._mirror()
        check(
            "2b fresh live takeover peeks meta-only then fetches full frames once",
            hive_live.calls == [("", JOURNAL_META_ONLY_AFTER), ("live-99", 0)]
            and mirror_live.get("run_id") == "live-99"
            and "".join(mirror_live.get("answer_parts") or []) == "Hello live",
            repr((hive_live.calls, mirror_live.get("run_id"), ms_live.messages)),
        )

        # 2c: R6 late result peeks meta-only then fetches full frames once and relays result
        hive_late = _FakeHive()
        late_ts = time.time() + 10.0
        hive_late.responses[("", JOURNAL_META_ONLY_AFTER)] = {
            "active": True,
            "run_id": "late-77",
            "done": True,
            "aborted": False,
            "n": 3,
            "ts": late_ts,
            "frames": [],
        }
        hive_late.responses[("late-77", 0)] = {
            "active": True,
            "run_id": "late-77",
            "done": True,
            "aborted": False,
            "n": 3,
            "ts": late_ts,
            "frames": [
                'data: {"type": "run_id", "run_id": "late-77"}',
                'data: {"type": "token", "content": "Late answer ok"}',
                'data: {"type": "done", "stop_reason": "completed"}',
            ],
        }
        br_late, ms_late = _make_bridge(hive_late)
        await br_late.mirror_tick()
        check(
            "2c R6 late result fetches full frames after meta peek and delivers answer",
            hive_late.calls == [("", JOURNAL_META_ONLY_AFTER), ("late-77", 0)]
            and any("Late answer ok" in m for m in ms_late.messages),
            repr((hive_late.calls, ms_late.messages)),
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_ui_idle_meta_peek_and_async_routes() -> None:
    st_js = read_ui_module("static/js/stream_transport.js", normalize_newlines=True)
    check(
        "3a stream_transport.js uses meta-only idle peek before full journal fetch",
        "'/run/journal?after=1000000000'" in st_js
        and "'/run/journal?run_id=' + encodeURIComponent(j.run_id) + '&after=0'" in st_js,
    )

    node = shutil.which("node")
    if node:
        fn_block = st_js[st_js.index("function _journalApplyAttachSnapshot"):st_js.index("// -- Stop Stream")]
        probe = f"""
const vm = require('vm');
const urls = [];
let fed = [];
const ctx = {{
  S: {{ streaming: false, _journalTailing: null, _chatSwitchEpoch: 0, currentChatId: 'c1', _attachedRunId: '' }},
  Date, Number, String, Promise, encodeURIComponent,
  _journalNote: () => {{}},
  _sseFeedLines: (x) => fed.push(x),
  _journalStartTail: () => {{}},
  _finalizeStreamUi: () => {{}},
  fetch: (url) => {{
    urls.push(url);
    if (url === '/run/journal?after=1000000000') {{
      return Promise.resolve({{ json: () => Promise.resolve({{ active: true, run_id: 'r-ui-1', done: false, aborted: false, n: 1, ts: Date.now()/1000, chat_id: 'c1', frames: [] }}) }});
    }}
    return Promise.resolve({{ json: () => Promise.resolve({{ active: true, run_id: 'r-ui-1', done: false, aborted: false, n: 1, ts: Date.now()/1000, chat_id: 'c1', frames: ['data: {{"type":"status","content":"hi"}}'] }}) }});
  }},
}};
vm.createContext(ctx);
vm.runInContext({json.dumps(fn_block)}, ctx);
Promise.resolve(ctx._journalInitAttach()).then(() => new Promise(r => setTimeout(r, 30))).then(() => {{
  console.log(JSON.stringify({{ urls, fedCount: fed.length }}));
}});
"""
        r = subprocess.run(
            [node, "-e", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        ok_vm = r.returncode == 0
        parsed = json.loads(r.stdout.strip()) if ok_vm and r.stdout.strip() else {}
        check(
            "3b Node VM confirms _journalInitAttach peeks meta-only then fetches full frames",
            ok_vm
            and parsed.get("urls") == ["/run/journal?after=1000000000", "/run/journal?run_id=r-ui-1&after=0"]
            and parsed.get("fedCount") == 1,
            repr((r.returncode, r.stderr, parsed)),
        )

    chats_src = (ROOT / "routers" / "chats.py").read_text(encoding="utf-8")
    ws_src = (ROOT / "routers" / "websearch.py").read_text(encoding="utf-8")
    check(
        "3c routers/chats.py and routers/websearch.py offload sync saves/hydration to thread",
        "await asyncio.to_thread(_save_chat, cid, chat)" in chats_src
        and "await asyncio.to_thread(_hydrate)" in chats_src
        and "await asyncio.to_thread(lambda: mutate_settings(_apply))" in ws_src,
    )


async def test_literal_token_scrubbing() -> None:
    gw_render.clear_registered_secrets()
    bare_secret = "999888777:AAFakeBareSecretWithoutPrefix_XYZ123"
    gw_render.register_secret(bare_secret)

    class _CapApi:
        def __init__(self) -> None:
            self.sent_texts: list[str] = []
            self.sent_docs: list[bytes] = []

        async def send_message(self, _chat_id, text, **_kw):
            self.sent_texts.append(text)
            return {}

        async def send_document(self, _chat_id, data, _fn, **_kw):
            self.sent_docs.append(data)
            return {}

    try:
        scrubbed = gw_render.filter_secrets(f"token leak -> {bare_secret}")
        check("4a filter_secrets scrubs registered bare token", bare_secret not in scrubbed and "***" in scrubbed)

        api = _CapApi()
        await gw_send.send(api, 123, 123, text=f"hello {bare_secret}")
        await gw_send.send(api, 123, 123, document_bytes=f"cfg={bare_secret}".encode(), filename="note.txt")
        check(
            "4b gw_send scrubs bare token in outbound text and text documents",
            bool(api.sent_texts) and bare_secret not in api.sent_texts[0]
            and bool(api.sent_docs) and bare_secret.encode() not in api.sent_docs[0],
        )

        main_src = (ROOT / "hivemind_gateway" / "main.py").read_text(encoding="utf-8")
        check("4c hivemind_gateway/main.py registers resolved token with render.register_secret", "register_secret(" in main_src)
    finally:
        gw_render.clear_registered_secrets()


async def _run_async_parts() -> None:
    await test_gateway_idle_meta_only_journal()
    await test_literal_token_scrubbing()


def main() -> int:
    test_corrupt_settings_guard_and_backup()
    asyncio.run(_run_async_parts())
    test_ui_idle_meta_peek_and_async_routes()
    return h.finish("glm54_slice1_fixes")


if __name__ == "__main__":
    sys.exit(main())

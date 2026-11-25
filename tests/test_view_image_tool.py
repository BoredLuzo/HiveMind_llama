# -*- coding: utf-8 -*-
"""Tests for the conditional `view_image` tool and multimodal loop injection."""
from __future__ import annotations

import asyncio
import json
import struct
import sys
import tempfile
import zlib
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

from core.duo_helpers import (  # noqa: E402
    build_view_image_user_message,
    resolve_view_image_enabled,
)
from core.tool_exec_helpers import ToolRoundState  # noqa: E402
from core.agentic_duo_state import DuoRoundState  # noqa: E402
import core.tool_executor as TE  # noqa: E402
from core.tool_loop import ToolLoop, ToolLoopConfig  # noqa: E402
from sse.events import make_tool_call_event  # noqa: E402
from tools import browser as TB  # noqa: E402
from tools.definitions import _get_inline_tools  # noqa: E402
from tools.handlers import _shared as TSHARED  # noqa: E402
from tools.handlers import file_ops as TFILEOPS  # noqa: E402
from tools.handlers.view_image import (  # noqa: E402
    _inline_tool_view_image,
    _pending_view_images,
    _sniff_image_stdlib,
    drain_viewed_images,
    reset_pending_view_images,
)
from utils.token import estimate_ctx_tokens  # noqa: E402


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def _write_solid_png(path: Path, width: int, height: int, rgb: tuple[int, int, int]) -> None:
    """Create a valid RGB PNG using pure Python stdlib (no Pillow required)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * width
    raw = zlib.compress(row * height, level=1)
    blob = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", raw)
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(blob)


def _make_jpeg_bytes(width: int, height: int) -> bytes:
    """Build a minimal synthetic JPEG header with a SOF0 segment."""
    sof0_payload = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    sof0 = b"\xff\xc0" + struct.pack(">H", len(sof0_payload) + 2) + sof0_payload
    return b"\xff\xd8" + sof0 + b"\xff\xd9"


def _make_webp_vp8x_bytes(width: int, height: int) -> bytes:
    """Build a minimal synthetic VP8X WEBP header."""
    vp8x_payload = (
        b"\x00\x00\x00\x00"
        + int(width - 1).to_bytes(3, "little")
        + int(height - 1).to_bytes(3, "little")
    )
    riff_body = b"WEBP" + b"VP8X" + struct.pack("<I", len(vp8x_payload)) + vp8x_payload
    return b"RIFF" + struct.pack("<I", len(riff_body)) + riff_body


# ── 1. Conditional schema gating ─────────────────────────────────────────
_MODES_WITH_VIEW = (
    "duo_full", "duo_core", "duo_lean", "duo_readonly",
    "direct_research", "direct_python", "direct_full",
)
for _m in _MODES_WITH_VIEW:
    _t_off = {t["function"]["name"] for t in _get_inline_tools(mode=_m, include_view_image=False)}
    _t_on = {t["function"]["name"] for t in _get_inline_tools(mode=_m, include_view_image=True)}
    check(f"mode {_m}: excluded when include_view_image=False", "view_image" not in _t_off)
    check(f"mode {_m}: included when include_view_image=True", "view_image" in _t_on)

_t_plain_direct = {t["function"]["name"] for t in _get_inline_tools(mode="direct", include_view_image=True)}
check("plain 'direct' mode never exposes view_image", "view_image" not in _t_plain_direct)

# ── 2. resolve_view_image_enabled truth table ────────────────────────────
check("images_off always disables view_image",
      resolve_view_image_enabled(slot_vision_active=True, coder_images_off=True, wants_vision=True, model_has_mmproj=True) is False)
check("slot vision_active=False disables view_image",
      resolve_view_image_enabled(slot_vision_active=False, wants_vision=True, model_has_mmproj=True) is False)
check("slot vision_active=True enables view_image",
      resolve_view_image_enabled(slot_vision_active=True, wants_vision=False, model_has_mmproj=False) is True)
check("slot None + wants_vision + mmproj enables view_image",
      resolve_view_image_enabled(slot_vision_active=None, wants_vision=True, model_has_mmproj=True) is True)
check("slot None + no wants_vision keeps view_image disabled",
      resolve_view_image_enabled(slot_vision_active=None, wants_vision=False, model_has_mmproj=True) is False)
check("slot None + wants_vision without mmproj keeps view_image disabled",
      resolve_view_image_enabled(slot_vision_active=None, wants_vision=True, model_has_mmproj=False) is False)


# ── 3. Handler behavior, stdlib sniffing, containment & slot guard ───────
class _MockManager:
    def __init__(self, vision_active):
        self._slots = [SimpleNamespace(is_running=True, vision_active=vision_active)]


async def _test_handler() -> None:
    import backend.llama_server_manager as _lsm_mod
    orig_mgr = _lsm_mod.manager
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        outside = ws.parent / f"outside_{ws.name}.png"
        _write_solid_png(outside, 100, 80, (255, 0, 0))
        big_png = ws / "screen.png"
        _write_solid_png(big_png, 2400, 1600, (40, 120, 200))
        jpg_path = ws / "photo.jpg"
        jpg_path.write_bytes(_make_jpeg_bytes(640, 480))
        bad_png = ws / "corrupt.png"
        bad_png.write_bytes(b"not a real png")
        txt_path = ws / "notes.txt"
        txt_path.write_text("hello", encoding="utf-8")
        oversized_png = ws / "oversized.png"
        with oversized_png.open("wb") as fh:
            fh.seek(10 * 1024 * 1024)
            fh.write(b"\x00")

        try:
            _lsm_mod.manager = _MockManager(False)
            res_unavail = await _inline_tool_view_image({"path": "screen.png"}, ws, str(ws))
            check("non-vision running slot returns VIEW_IMAGE_UNAVAILABLE",
                  "VIEW_IMAGE_UNAVAILABLE" in res_unavail)

            _lsm_mod.manager = _MockManager(True)

            # Pure-stdlib sniffing across PNG, JPEG, GIF, WEBP, BMP + corrupt
            sniff_png = _sniff_image_stdlib(big_png.read_bytes())
            check("stdlib sniffer parses PNG dimensions",
                  sniff_png == ("image/png", "PNG", 2400, 1600))
            sniff_jpg = _sniff_image_stdlib(_make_jpeg_bytes(640, 480))
            check("stdlib sniffer parses JPEG SOF0 dimensions",
                  sniff_jpg == ("image/jpeg", "JPEG", 640, 480))
            sniff_gif = _sniff_image_stdlib(b"GIF89a" + struct.pack("<HH", 320, 240) + b"\x00\x00")
            check("stdlib sniffer parses GIF dimensions",
                  sniff_gif == ("image/gif", "GIF", 320, 240))
            sniff_webp = _sniff_image_stdlib(_make_webp_vp8x_bytes(800, 600))
            check("stdlib sniffer parses WEBP VP8X dimensions",
                  sniff_webp == ("image/webp", "WEBP", 800, 600))
            sniff_bmp = _sniff_image_stdlib(b"BM" + (b"\x00" * 16) + struct.pack("<ii", 123, -45))
            check("stdlib sniffer parses BMP dimensions",
                  sniff_bmp == ("image/bmp", "BMP", 123, 45))
            try:
                _sniff_image_stdlib(b"invalid header")
                check("stdlib sniffer rejects corrupt bytes", False)
            except ValueError:
                check("stdlib sniffer rejects corrupt bytes", True)

            tok = reset_pending_view_images()
            try:
                res_png = await _inline_tool_view_image({"path": "screen.png"}, ws, str(ws))
                queued_png = drain_viewed_images()
            finally:
                _pending_view_images.reset(tok)
            check("pure-stdlib PNG loads cleanly without base64 in tool text",
                  "[view_image]" in res_png
                  and "2400x1600" in res_png
                  and "data:image/" not in res_png
                  and len(queued_png) == 1
                  and queued_png[0]["width"] == 2400
                  and queued_png[0]["height"] == 1600
                  and queued_png[0]["data_url"].startswith("data:image/png;base64,"))

            tok = reset_pending_view_images()
            try:
                res_jpg = await _inline_tool_view_image({"path": "photo.jpg"}, ws, str(ws))
                queued_jpg = drain_viewed_images()
            finally:
                _pending_view_images.reset(tok)
            check("pure-stdlib JPEG queues valid data_url cleanly",
                  "[view_image]" in res_jpg
                  and "640x480" in res_jpg
                  and len(queued_jpg) == 1
                  and queued_jpg[0]["width"] == 640
                  and queued_jpg[0]["height"] == 480
                  and queued_jpg[0]["data_url"].startswith("data:image/jpeg;base64,"))

            res_big = await _inline_tool_view_image({"path": "oversized.png"}, ws, str(ws))
            check("> 10 MB image returns VIEW_IMAGE_TOO_LARGE",
                  "VIEW_IMAGE_TOO_LARGE" in res_big and "10.0 MB" in res_big)

            res_esc = await _inline_tool_view_image({"path": f"../{outside.name}"}, ws, str(ws))
            check("workspace escape blocked", "TOOL_ERROR" in res_esc)
            if outside.exists():
                outside.unlink()

            res_bad_ext = await _inline_tool_view_image({"path": "notes.txt"}, ws, str(ws))
            check("non-image suffix -> VIEW_IMAGE_INVALID_FORMAT",
                  "VIEW_IMAGE_INVALID_FORMAT" in res_bad_ext)

            res_corrupt = await _inline_tool_view_image({"path": "corrupt.png"}, ws, str(ws))
            check("corrupt png -> VIEW_IMAGE_INVALID_FORMAT",
                  "VIEW_IMAGE_INVALID_FORMAT" in res_corrupt)

            res_missing = await _inline_tool_view_image({"path": "missing.png"}, ws, str(ws))
            check("missing file -> FILE_NOT_FOUND", "FILE_NOT_FOUND" in res_missing)

            # Browser screenshot + read_file binary error hints
            check("coder_vision_truth True with MockManager(True)",
                  TSHARED.coder_vision_truth() is True)
            _lsm_mod.manager = _MockManager(True)
            bin_err_vis = TFILEOPS._read_binary_error(big_png)
            check("read_file binary error mentions view_image when vision active",
                  "view_image" in bin_err_vis)
            _lsm_mod.manager = _MockManager(False)
            bin_err_novis = TFILEOPS._read_binary_error(big_png)
            check("read_file binary error omits view_image when vision inactive",
                  "view_image" not in bin_err_novis)
        finally:
            _lsm_mod.manager = orig_mgr

    # ── 4. execute_tool_round & message builder integration ──────────────
    class _Hooks:
        def __init__(self):
            self.events = []

        async def emit(self, ev):
            self.events.append(ev)

        def is_aborted(self, _cid):
            return False

        async def on_tool_result(self, _n, _a, _r):
            return None

        async def remember_insight(self, *a, **k):
            pass

        async def evict_model(self, _m):
            pass

    class _LRU:
        default_ttl = 600

        def decay(self, _p):
            pass

        def register(self, _i, **_kw):
            pass

    with tempfile.TemporaryDirectory() as td2:
        ws2 = Path(td2)
        shot = ws2 / "ui.png"
        _write_solid_png(shot, 320, 200, (15, 90, 210))
        orig_mgr2 = _lsm_mod.manager
        try:
            _lsm_mod.manager = _MockManager(True)
            tc = [{"id": "c1", "type": "function",
                   "function": {"name": "view_image", "arguments": json.dumps({"path": "ui.png"})}}]
            msgs_off: list[dict] = []
            r_off = await TE.execute_tool_round(
                tool_calls=tc,
                dtool_msgs=msgs_off,
                round_state=DuoRoundState(exec_model="m:9b"),
                hooks=_Hooks(),
                trs=ToolRoundState(tool_ctx_lru=_LRU(), allow_view_image=False),
                tool_mode="duo_core",
                duo_ws=False,
                workspace_lock=str(ws2),
                exec_model="m:9b",
                exec_has_thinking=False,
                tool_think_auto_mode="off",
                run_id_global="r1",
                chat_id="c1",
                subtask_index=0,
            )
            check("execute_tool_round blocks view_image when allow_view_image=False",
                  not r_off.viewed_images
                  and any("VIEW_IMAGE_UNAVAILABLE" in str(m.get("content", "")) for m in msgs_off))

            msgs_on: list[dict] = []
            r_on = await TE.execute_tool_round(
                tool_calls=tc,
                dtool_msgs=msgs_on,
                round_state=DuoRoundState(exec_model="m:9b"),
                hooks=_Hooks(),
                trs=ToolRoundState(tool_ctx_lru=_LRU(), allow_view_image=True),
                tool_mode="duo_core",
                duo_ws=False,
                workspace_lock=str(ws2),
                exec_model="m:9b",
                exec_has_thinking=False,
                tool_think_auto_mode="off",
                run_id_global="r2",
                chat_id="c2",
                subtask_index=0,
            )
            check("execute_tool_round queues viewed_images when allow_view_image=True",
                  len(r_on.viewed_images) == 1 and r_on.viewed_images[0]["path"] == "ui.png")
        finally:
            _lsm_mod.manager = orig_mgr2

        # 5. build_view_image_user_message + cap + token estimate
        fake_items = [
            {"path": f"img{i}.png", "data_url": "data:image/png;base64," + ("A" * 12000), "width": 800, "height": 600}
            for i in range(6)
        ]
        vi_msg = build_view_image_user_message(fake_items, extra_text="Next step note")
        check("build_view_image_user_message caps at 4 images + merges extra_text",
              vi_msg is not None
              and vi_msg["role"] == "user"
              and sum(1 for p in vi_msg["content"] if p.get("type") == "image_url") == 4
              and "Showing first 4 of 6" in vi_msg["content"][-1]["text"]
              and "Next step note" in vi_msg["content"][-1]["text"])
        tok_est = estimate_ctx_tokens([vi_msg]) if vi_msg else 0
        check("estimate_ctx_tokens uses ~1024 tok/image, NOT base64 chars",
              4096 <= tok_est < 5000, f" (got {tok_est})")

        # 6. ToolLoop integration (non-streaming mocked POST)
        _lsm_mod.manager = _MockManager(True)
        try:
            seen_payloads: list[dict] = []

            def _handler(request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8"))
                seen_payloads.append(body)
                if len(seen_payloads) == 1:
                    return httpx.Response(200, json={
                        "choices": [{
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [{
                                    "id": "call_vi_1",
                                    "type": "function",
                                    "function": {
                                        "name": "view_image",
                                        "arguments": json.dumps({"path": "ui.png"}),
                                    },
                                }],
                            },
                            "finish_reason": "tool_calls",
                        }],
                    })
                return httpx.Response(200, json={
                    "choices": [{
                        "message": {"role": "assistant", "content": "Image inspected."},
                        "finish_reason": "stop",
                    }],
                })

            tools_on = _get_inline_tools(mode="direct_python", include_view_image=True)
            async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as hc:
                loop = ToolLoop(
                    config=ToolLoopConfig(
                        stream=False, max_rounds=3, model="m:vl", num_ctx=16384,
                        tools=tools_on, tool_mode="direct_python",
                        allow_view_image=True, require_tool_call=False,
                    ),
                    http_client=hc,
                    port=9999,
                    workspace=str(ws2),
                )
                evs = [ev async for ev in loop.run([{"role": "user", "content": "Inspect ui.png"}])]
            check("ToolLoop completes round 2 after injecting viewed image",
                  len(seen_payloads) == 2 and any(e.get("type") == "token" for e in evs))
            r2_msgs = seen_payloads[1]["messages"]
            check("ToolLoop round 2 carries tool result then user image_url message",
                  len(r2_msgs) >= 4
                  and r2_msgs[-2]["role"] == "tool"
                  and r2_msgs[-1]["role"] == "user"
                  and isinstance(r2_msgs[-1]["content"], list)
                  and any(p.get("type") == "image_url" for p in r2_msgs[-1]["content"]))
        finally:
            _lsm_mod.manager = orig_mgr2

    # 7. browser hint + SSE make_tool_call_event
    ev = make_tool_call_event("view_image", {"path": "shots/ui.png"})
    check("make_tool_call_event formats view_image chip",
          ev["label"] == "shots/ui.png" and ev["detail"] == "" and ev["extra"] == {})

    orig_cvt = TSHARED.coder_vision_truth
    try:
        TSHARED.coder_vision_truth = lambda: True
        check("browser module imports and sees patched coder_vision_truth=True",
              hasattr(TB, "browser_tool") and TSHARED.coder_vision_truth() is True)
    finally:
        TSHARED.coder_vision_truth = orig_cvt


asyncio.run(_test_handler())
sys.exit(h.finish("view_image_tool"))

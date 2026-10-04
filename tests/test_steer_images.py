# -*- coding: utf-8 -*-
"""Steering with images (2026-10-04, user decision: 'Das hört sich sehr
gut an'). Covers the pure builder, the queue roundtrip incl. legacy
normalisation, and the endpoint validation rules."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.duo_helpers import build_steer_user_message  # noqa: E402
from infra.run_control import queue_steer, drain_steer_messages  # noqa: E402


def test_builder_text_only_is_plain_string():
    m = build_steer_user_message("focus on the header", None)
    assert m == {"role": "user", "content": "[USER STEER] focus on the header"}


def test_builder_with_images_parts_first():
    m = build_steer_user_message("look at this", ["QUJD", "data:image/png;base64,WFla"])
    c = m["content"]
    assert isinstance(c, list) and len(c) == 3
    assert c[0]["type"] == "image_url" and c[0]["image_url"]["url"].startswith("data:image/jpeg;base64,QUJD")
    assert c[1]["image_url"]["url"].startswith("data:image/png;base64,")  # data-URL kept
    assert c[2]["type"] == "text" and "[USER STEER] look at this" in c[2]["text"]


def test_builder_empty_images_falls_back_to_string():
    m = build_steer_user_message("text only", ["", "  "])
    assert isinstance(m["content"], str)


def test_queue_roundtrip_mixed_entries():
    from infra import run_control as rcx
    rcx._steer_queue.clear()
    rcx._run_abort_registry["steer-rt"] = object()
    ok1 = queue_steer("steer-rt", "plain text")
    ok2 = queue_steer("steer-rt", "with pic", ["QUJD"])
    assert ok1 and ok2
    items = drain_steer_messages("steer-rt")
    assert items[0] == {"text": "plain text", "images": []}
    assert items[1] == {"text": "with pic", "images": ["QUJD"]}
    # queue drained
    assert drain_steer_messages("steer-rt") == []


def test_queue_rejects_empty_and_inactive():
    from infra import run_control as rcx
    rcx._steer_queue.clear()
    assert queue_steer("no-such-run", "hi") is False  # not registered
    rcx._run_abort_registry["steer-empty"] = object()
    assert queue_steer("steer-empty", "") is False       # no text, no images
    assert queue_steer("steer-empty", "", ["QUJD"]) is True  # image-only is valid


def test_endpoint_validates_images_field():
    from routers.core import steer_run

    class _Req:
        def __init__(self, body):
            self._b = body

        async def json(self):
            return self._b

    # images not a list -> 400
    out = asyncio.run(steer_run("ep-1", _Req({"text": "hi", "images": "nope"})))
    assert out.status_code == 400
    # too many -> 400
    out = asyncio.run(steer_run("ep-1", _Req({"text": "hi", "images": ["x"] * 5})))
    assert out.status_code == 400
    # image too small (not base64) -> 400
    out = asyncio.run(steer_run("ep-1", _Req({"text": "hi", "images": ["ab"]})))
    assert out.status_code == 400
    # registered run accepts text+images
    from infra import run_control as rcx
    rcx._steer_queue.clear()
    rcx._run_abort_registry["ep-1"] = object()
    out = asyncio.run(steer_run("ep-1", _Req(
        {"text": "screenshot", "images": ["QUJD" * 100]})))
    assert out["status"] == "queued" and out["images"] == 1
    items = drain_steer_messages("ep-1")
    assert items[0]["images"] == ["QUJD" * 100]
    # image-only steer (no text) is accepted
    out = asyncio.run(steer_run("ep-1", _Req({"text": "", "images": ["QUJD" * 100]})))
    assert out["status"] == "queued"
    items = drain_steer_messages("ep-1")
    assert items[0]["text"] == "" and items[0]["images"]


def test_wire_shape_matches_llama_payload():
    # the parts shape must be what llama-server expects (image_url objects
    # first, text last) - same convention as attach_images_to_last_user
    m = build_steer_user_message("check", ["QQ=="])
    assert [p["type"] for p in m["content"]] == ["image_url", "text"]

# -*- coding: utf-8 -*-
"""Fake-backend integration test: the truncation→salvage→append flow (2026-09-30).

The unit suite pins validate_tool_calls in isolation; what no real run has
ever exercised is the CHAIN: SSE accumulation in AgenticToolLoop.post_with_retry
(finish_reason=length, fragmented tool-call deltas) → validate_tool_calls
(repair/salvage/note) → next round carrying the recovery append. This runs the
real post_with_retry against a local fake llama-server that answers two
scripted SSE responses — no ctx mocks, no model, seconds instead of minutes.

Run:  .venv/Scripts/python.exe tests/test_truncation_flow_fake.py
"""
import asyncio
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from core.agentic_duo_state import DuoRoundState  # noqa: E402
from core.agentic_tool_loop import AgenticToolLoop  # noqa: E402
from core.tool_loop import ToolLoopConfig  # noqa: E402
from utils.tool import validate_tool_calls  # noqa: E402

CONTENT = "<html>\n" + "".join(
    f"  <p>filler line {i:03d} of the file body</p>\n" for i in range(300))
APPEND = "  <p>recovered tail after the seam</p>\n</html>\n"


def _sse(events: list[dict]) -> bytes:
    out = [f"data: {json.dumps(e)}\n\n".encode("utf-8") for e in events]
    out.append(b"data: [DONE]\n\n")
    return b"".join(out)


class _FakeLlama:
    """Two scripted responses: truncated write (length), then append (stop)."""

    def __init__(self, extra_response: bytes | None = None):
        self.requests = 0
        self.responses: list[bytes] = []
        traw = json.dumps({"path": "index.html", "content": CONTENT})[:-2]
        frag1, frag2, frag3 = traw[:400], traw[400:800], traw[800:]
        self.responses.append(_sse([
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1",
              "type": "function", "function": {"name": "write_file", "arguments": frag1}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0,
              "function": {"arguments": frag2}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0,
              "function": {"arguments": frag3}}]}}]},
            {"choices": [{"delta": {"reasoning_content": "thinking about the board layout"}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
            {"usage": {"prompt_tokens": 6100, "completion_tokens": 3000}},
        ]))
        araw = json.dumps({"path": "index.html", "content": APPEND})
        self.responses.append(_sse([
            {"choices": [{"delta": {"role": "assistant"}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c2",
              "type": "function", "function": {"name": "write_file_append",
                                               "arguments": araw[:200]}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0,
              "function": {"arguments": araw[200:]}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"usage": {"prompt_tokens": 9500, "completion_tokens": 60}},
        ]))
        if extra_response is not None:
            # FIRST response: single-request tests (the drift scenario) must
            # get it — responses[] is indexed by request count
            self.responses.insert(0, extra_response)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                body = fake.responses[fake.requests]
                fake.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        return H

    def close(self):
        self.server.shutdown()


class _FakeCtx:
    """Minimal run-context stub: not aborted, no chat, silent emit."""

    chat_id = None

    def aborted(self):
        return False

    def is_aborted_chat(self, chat_id):
        return False

    async def emit(self, event):
        return None


class TruncationFlowFakeBackend(unittest.TestCase):
    def tearDown(self):
        # validate_tool_calls records salvage events into the REAL fixture
        # dump (logs/write_truncations.jsonl) — in the dev clone that file
        # must hold real model output only (the committed 14:36 incident
        # lives in tests/fixtures/). Remove test noise after every run.
        import os
        from pathlib import Path as _P
        _dump = _P(__file__).resolve().parents[1] / "logs" / "write_truncations.jsonl"
        try:
            _dump.unlink()
        except OSError:
            pass

    def test_length_truncated_write_flows_through_salvage_and_append(self):
        fake = _FakeLlama()
        try:
            async def run():
                cfg = ToolLoopConfig(model="fake", stream=True, max_post_attempts=1)
                rs = DuoRoundState(current_port=fake.port, exec_model="fake")
                loop = AgenticToolLoop(cfg, httpx.AsyncClient(timeout=10.0), round_state=rs)
                ctx = _FakeCtx()
                payload = {"model": "fake", "stream": True, "max_tokens": 3000,
                           "messages": [{"role": "user", "content": "build tetris"}]}
                r1 = await loop.post_with_retry(dict(payload), dtool_msgs=[dict(m) for m in payload["messages"]],
                                                ctx=ctx, _parts=[])
                tcs = r1["dr_msg"].get("tool_calls", [])
                v1 = validate_tool_calls(tcs, finish_reason=r1.get("dr_finish_reason"),
                                         model="fake", token_budget=8000, chars_per_token=2.5)
                # round 2: the model answers the salvage note with an append
                msgs2 = payload["messages"] + [{"role": "assistant", "tool_calls": v1["tool_calls"]}]
                if v1["salvage_notes"]:
                    msgs2.append({"role": "user", "content": "\n".join(v1["salvage_notes"])})
                r2 = await loop.post_with_retry({**payload, "messages": msgs2}, dtool_msgs=msgs2,
                                                ctx=ctx, _parts=[])
                v2 = validate_tool_calls(r2["dr_msg"].get("tool_calls", []),
                                         finish_reason=r2.get("dr_finish_reason"),
                                         model="fake", token_budget=8000, chars_per_token=2.5)
                return r1, v1, r2, v2

            r1, v1, r2, v2 = asyncio.run(run())
        finally:
            fake.close()

        # round 1: assembled truncated call + length + reasoning parts
        self.assertEqual(r1.get("dr_finish_reason"), "length")
        self.assertEqual(len(r1["dr_msg"]["tool_calls"]), 1)
        self.assertEqual(r1["dr_msg"]["tool_calls"][0]["function"]["name"], "write_file")
        # round 1 validation: salvaged, not dropped — note carries the tail contract
        self.assertTrue(v1["tool_calls"], "truncated write must be salvaged, not dropped")
        saved = json.loads(v1["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(saved["path"], "index.html")
        self.assertTrue(saved["content"].endswith("\n"))
        self.assertIn("[WRITE-SALVAGE]", v1["salvage_notes"][0])
        self.assertIn("write_file_append", v1["salvage_notes"][0])
        self.assertTrue(v1["meta"]["has_tool_call"])
        # round 2: the append arrives complete and passes validation untouched
        self.assertEqual(r2.get("dr_finish_reason"), "tool_calls")
        self.assertEqual(len(v2["tool_calls"]), 1)
        appended = json.loads(v2["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(appended["content"], APPEND)
        self.assertFalse(v2["drop_notices"] or v2["salvage_notes"])
        # seam sanity: salvaged prefix + append reassemble without loss/duplication
        self.assertIn(CONTENT.strip()[-40:], saved["content"])


class RepetitionDriftLiveFixture(unittest.TestCase):
    """The REAL 30k dump (2026-09-30 14:36, minicpm5:2b-sharp): 293x the same
    line inside a content-first write_file call, finish=length at 8000 tokens.
    Streamed through the REAL post_with_retry — the drift detector must cut
    the stream early instead of consuming the whole loop."""

    def _load_raw(self) -> str:
        fx = Path(__file__).parent / "fixtures" / "write_truncations_live.jsonl"
        for line in fx.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if e["raw_len"] > 20000:
                return e["raw"]
        raise AssertionError("live loop fixture missing")

    def test_drift_cuts_generation_early_on_real_loop(self):
        raw = self._load_raw()
        chunks = [raw[i:i + 512] for i in range(0, len(raw), 512)]
        events = [{"choices": [{"delta": {"role": "assistant"}}]},
                  {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c9",
                    "type": "function", "function": {"name": "write_file",
                                                     "arguments": chunks[0]}}]}}]}]
        for c in chunks[1:]:
            events.append({"choices": [{"delta": {"tool_calls": [{"index": 0,
                          "function": {"arguments": c}}]}}]})
        events.append({"choices": [{"delta": {}, "finish_reason": "length"}]})
        fake = _FakeLlama(extra_response=_sse(events))
        try:
            async def run():
                cfg = ToolLoopConfig(model="fake", stream=True, max_post_attempts=1)
                rs = DuoRoundState(current_port=fake.port, exec_model="fake")
                loop = AgenticToolLoop(cfg, httpx.AsyncClient(timeout=10.0), round_state=rs)
                return await loop.post_with_retry(
                    {"model": "fake", "stream": True, "max_tokens": 8000,
                     "messages": [{"role": "user", "content": "x"}]},
                    dtool_msgs=[{"role": "user", "content": "x"}],
                    ctx=_FakeCtx(), _parts=[])

            r = asyncio.run(run())
        finally:
            fake.close()

        self.assertEqual(r.get("dr_finish_reason"), "drift")
        tcs = r["dr_msg"].get("tool_calls") or []
        # cut EARLY: the accumulated arguments must be far shorter than the
        # full 30k loop (the detector fires after ~10 repeated lines)
        acc = str((tcs[0].get("function") or {}).get("arguments")) if tcs else ""
        self.assertLess(len(acc), 8000, f"drift cut too late: {len(acc)} chars")
        self.assertGreater(len(acc), 1000, "cut too early —valid prefix lost")


if __name__ == "__main__":
    unittest.main(verbosity=2)

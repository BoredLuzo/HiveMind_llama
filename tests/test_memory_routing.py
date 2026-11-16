"""Memory keyword routing must not hijack long task texts.

Regression: a Tetris task spec containing "Allow the player to store one
piece" was routed to the memory early-return (`early return: memory_request`)
instead of the coder/planner pipeline, silently swallowing the whole task.
"""
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, ".")

from hive_functions.pipeline import Pipeline  # noqa: E402

TETRIS_SPEC = """Goal:
Develop a fully functional, classic Tetris game that runs in a web browser. The implementation must be split into multiple separate files for better maintainability and modularity.

Technical Requirements:

    Languages: Use pure HTML5, CSS3, and vanilla JavaScript (no external libraries, no jQuery, no framework).

    File Structure: Organize the code into at least the following files:

        index.html - the main HTML file that loads all other resources.

    Hold Piece (optional but nice): Allow the player to store one piece and swap it with the current piece.

    Additional Notes (optional but appreciated): Add a ghost piece.

    Game Over: When a piece cannot be placed at the spawn position.
"""


class TestMemoryRouting(unittest.TestCase):
    def setUp(self):
        self.p = Pipeline(memory=MagicMock(), agent_overrides={})

    def test_long_task_spec_is_not_memory(self):
        self.assertFalse(self.p._is_memory_request(TETRIS_SPEC))

    def test_long_task_spec_is_not_forget(self):
        self.assertFalse(self.p._is_forget_request(TETRIS_SPEC))

    def test_short_memory_commands_still_match(self):
        for t in (
            "Remember that my name is Alex",
            "note: I prefer dark themes",
            "Please keep in mind that I use Python 3.14",
            "save this: my project is HiveMind",
        ):
            self.assertTrue(self.p._is_memory_request(t), t)

    def test_short_forget_commands_still_match(self):
        for t in ("delete my name", "forget everything about my project", "remove herkunft"):
            self.assertTrue(self.p._is_forget_request(t), t)

    def test_task_word_in_short_spec_beyond_window_is_ignored(self):
        # Trigger only appears deep inside a longer message -> no memory route.
        t = ("Build a small browser game with a shop where the player can store one item "
             "between rounds and retrieve it later from the inventory screen.")
        self.assertGreater(len(t), self.p._MEMORY_CMD_MAX_CHARS)
        self.assertFalse(self.p._is_memory_request(t))

    def test_memory_extractor_reuses_warm_slot_and_caps_ctx(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        self.p.set_agent_model("direct", "lfm3:1.7b-qad")
        self.p.set_agent_model("judge", "minicpm5:2b-sharp")
        calls = []

        async def _fake_chat(model, messages, temperature=0.1, max_tokens=100, **kw):
            calls.append((model, kw.get("ctx")))
            return '{"key": "dessert", "value": "Vanillepudding"}'

        self.p.ollama.chat = AsyncMock(side_effect=_fake_chat)
        fake_mgr = SimpleNamespace(_slots=[
            SimpleNamespace(model="lfm3:1.7b-qad", is_running=True, _loading=False, _num_ctx=16384),
        ])
        with patch("backend.llama_server_manager.manager", fake_mgr):
            res = asyncio.run(self.p._handle_memory_request("Merk dir ich mag Vanillepudding"))
        self.assertEqual(res, "✅ Saved: Vanillepudding")
        self.assertEqual(calls, [("lfm3:1.7b-qad", 4096)])

        # Smaller warm slot ctx (2048) must NOT trigger a 4096 ctx-upgrade reload.
        calls.clear()
        fake_mgr._slots[0]._num_ctx = 2048
        with patch("backend.llama_server_manager.manager", fake_mgr):
            asyncio.run(self.p._handle_memory_request("Merk dir das"))
        self.assertEqual(calls, [("lfm3:1.7b-qad", 2048)])

        # Cold engine: prefer direct over judge and cap even a 131k judge profile.
        calls.clear()
        fake_mgr._slots = []
        with patch("backend.llama_server_manager.manager", fake_mgr):
            asyncio.run(self.p._handle_memory_request("Merk dir das"))
        self.assertEqual(calls, [("lfm3:1.7b-qad", 4096)])

        calls.clear()
        self.p.set_agent_model("direct", "")
        with patch("backend.llama_server_manager.manager", fake_mgr):
            asyncio.run(self.p._handle_memory_request("Merk dir das"))
        self.assertEqual(calls, [("minicpm5:2b-sharp", 4096)])

    def test_memory_side_call_suppresses_thinking_and_falls_back(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from context.chat_util import _extract_memory

        self.assertEqual(
            _extract_memory("my favorite food is pizza remember that"),
            ("favorite_food", "pizza"),
        )

        self.p.set_agent_model("direct", "lfm3:1.7b-qad")
        seen = []

        async def _side_skip(model, messages, temperature=0.1, max_tokens=100, **kw):
            seen.append((max_tokens, kw.get("think"), kw.get("thinking_budget")))
            return ""  # simulates [SIDE-SKIP] empty content

        self.p.ollama.chat = AsyncMock(side_effect=_side_skip)
        fake_mgr = SimpleNamespace(_slots=[])

        msg = "Its a joke about asking the other people smt about joe and then saying joe mama remember that"
        with patch("backend.llama_server_manager.manager", fake_mgr):
            res = asyncio.run(self.p._handle_memory_request(msg))
        self.assertTrue(res.startswith("✅ Saved:"), res)
        self.assertEqual(seen[0], (400, False, 0))
        self.p.memory.remember.assert_called()
        _k, _v = self.p.memory.remember.call_args[0]
        self.assertTrue(_k.startswith("note_"), _k)
        self.assertNotIn("remember that", _v.lower())

        # Bare filler without extractable substance still refuses cleanly.
        self.p.memory.remember.reset_mock()
        with patch("backend.llama_server_manager.manager", fake_mgr):
            res_empty = asyncio.run(self.p._handle_memory_request("Merk dir das"))
        self.assertIn("Could not extract", res_empty)
        self.p.memory.remember.assert_not_called()

    def test_chat_memory_shortcuts_emit_agent_envelope(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        import core.state as _st
        from core.chat_shortcuts import _ChatShortcutState, _chat_memory_shortcuts

        events = []

        async def _emit(ev):
            events.append(ev)
            return ev

        def _done(toks, stop_reason):
            return {"type": "done", "tokens": toks, "stop_reason": stop_reason}

        class _Mem:
            def __init__(self):
                self.items = {}
                self.sess = []

            def remember(self, k, v):
                self.items[k] = v

            def forget(self, k):
                return self.items.pop(k, None) is not None

            def get_all(self):
                return dict(self.items)

            def list_memories(self):
                for k, v in self.items.items():
                    yield k, v, "2026-11-16"

            def add_to_session(self, r, c):
                self.sess.append((r, c))

        fake_pipe = Pipeline(memory=_Mem(), agent_overrides={"direct": "lfm3:1.7b-qad"})
        fake_pipe._handle_memory_request = AsyncMock(return_value="⚠️ Could not extract a fact.")

        async def _collect(text):
            events.clear()
            ps = _ChatShortcutState(
                user_input=text,
                run_id="r1",
                carry={"emit": _emit, "_done_event": _done},
            )
            async for _ in _chat_memory_shortcuts(ps):
                pass
            return list(events), ps.handled

        with patch.object(_st, "pipeline", fake_pipe), \
             patch.object(_st, "memory", fake_pipe.memory):
            evs_fail, handled_fail = asyncio.run(_collect("remember that nothing"))
            self.assertTrue(handled_fail)
            self.assertEqual([e["type"] for e in evs_fail], ["agent", "token", "agent_done", "done"])

            evs_ok, handled_ok = asyncio.run(_collect("remember my name is Alex"))
            self.assertTrue(handled_ok)
            self.assertEqual(
                [e["type"] for e in evs_ok],
                ["agent", "token", "agent_done", "memory_saved", "done"],
            )
            self.assertIn("Stored:", evs_ok[1]["content"])

            evs_list, _ = asyncio.run(_collect("show memory"))
            self.assertEqual([e["type"] for e in evs_list], ["agent", "token", "agent_done", "done"])
            self.assertIn("Alex", evs_list[1]["content"])


if __name__ == "__main__":
    unittest.main()

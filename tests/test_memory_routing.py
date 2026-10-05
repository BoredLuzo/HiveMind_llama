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


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""Success-path + salvage tests for the write tools (2026-09-30).

Born from the e14f64f incident: write_file_append reported FAILED (dangling
_split_discard_note NameError) AFTER the disk write had succeeded — the model
retried and duplicated content. Every write tool needs a success-path test so
that class of bug can never land silently again.

Run from the repo root:  .venv/Scripts/python.exe tests/test_write_tools.py
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.handlers.file_ops import (  # noqa: E402
    _inline_tool_write_file,
    _inline_tool_write_file_append,
)
from utils.tool import (  # noqa: E402
    resolve_write_char_limits,
    salvage_truncated_write_args,
    validate_tool_calls,
)


def _run(coro):
    return asyncio.run(coro)


class WriteFileSuccessPath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_reports_success_and_writes_disk(self):
        res = _run(_inline_tool_write_file(
            {"path": "new_mod.py", "content": "def a():\n    return 1\n"}, self.ws, None))
        self.assertNotIn("[TOOL_ERROR", res)
        self.assertIn("[write_file: created", res)
        self.assertEqual((self.ws / "new_mod.py").read_text(encoding="utf-8"),
                         "def a():\n    return 1\n")

    def test_append_reports_success_and_writes_disk(self):
        (self.ws / "log.txt").write_text("line1\n", encoding="utf-8")
        res = _run(_inline_tool_write_file_append(
            {"path": "log.txt", "content": "line2\n"}, self.ws, None))
        # the e14f64f regression: FAILED text after a successful disk write
        self.assertNotIn("[TOOL_ERROR", res)
        self.assertIn("[Appended:", res)
        self.assertEqual((self.ws / "log.txt").read_text(encoding="utf-8"),
                         "line1\nline2\n")

    def test_append_inserts_newline_boundary(self):
        (self.ws / "no_nl.txt").write_text("line1", encoding="utf-8")
        res = _run(_inline_tool_write_file_append(
            {"path": "no_nl.txt", "content": "line2\n"}, self.ws, None))
        self.assertNotIn("[TOOL_ERROR", res)
        self.assertIn("newline boundary inserted", res)
        self.assertEqual((self.ws / "no_nl.txt").read_text(encoding="utf-8"),
                         "line1\nline2\n")

    def test_append_requires_existing_file(self):
        res = _run(_inline_tool_write_file_append(
            {"path": "missing.txt", "content": "x\n"}, self.ws, None))
        self.assertIn("[TOOL_ERROR", res)


class SalvageFixtures(unittest.TestCase):
    @staticmethod
    def _truncated(content: str) -> str:
        # drop the closing quote + brace -> unclosed JSON string, the exact
        # shape a finish_reason=length cut leaves behind
        return json.dumps({"path": "src/main.py", "content": content},
                          ensure_ascii=False)[:-2]

    def test_cut_inside_unicode_escape(self):
        content = 'def a():\n    return "hä"\n\ndef b():\n    return 2\n'
        raw = json.dumps({"path": "a.py", "content": content})
        m = raw.find("\\u00e4")            # the escaped ä inside the JSON body
        self.assertGreater(m, 0)
        cut = raw[:m + 4]                  # keep "\u0 — incomplete escape
        r = salvage_truncated_write_args(cut, "write_file")
        self.assertTrue(r and r["_salvage"])
        # the escape fragment AND the partial line are cut at the line boundary
        self.assertEqual(r["args"]["content"], "def a():\n")
        self.assertNotIn("\\u00", r["args"]["content"])

    def test_cut_mid_line_drops_partial_line(self):
        cut = self._truncated("def a():\n    return 1\n\ndef b():\n    return 2\n")
        cut = cut[:cut.rfind("return 2") + 4]  # cut inside the last line
        r = salvage_truncated_write_args(cut, "write_file")
        self.assertTrue(r)
        self.assertTrue(r["args"]["content"].endswith("\n"))
        self.assertNotIn("retur", r["args"]["content"].splitlines()[-1])

    def test_closed_string_keeps_trailing_backslash(self):
        # a CLOSED string is complete content: a legitimate trailing
        # backslash must survive verbatim (no escape stripping)
        content = 'x = "c:\\path\\\\"\n'
        ok = json.dumps({"path": "a.py", "content": content}, ensure_ascii=False)
        r = salvage_truncated_write_args(ok[:-1], "write_file")  # only brace missing
        self.assertTrue(r)
        self.assertEqual(r["args"]["content"], content)

    def test_quick_repair_keeps_complete_content(self):
        full = json.dumps({"path": "a.py", "content": "one\ntwo\n"}, ensure_ascii=False)
        r = salvage_truncated_write_args(full[:-1], "write_file")
        self.assertTrue(r)
        self.assertEqual(r["args"]["content"], "one\ntwo\n")

    def test_repetition_loop_tail_is_trimmed(self):
        loop_line = "    return VALUE_XY\n"
        content = "def a():\n    pass\n" + loop_line * 6
        r = salvage_truncated_write_args(self._truncated(content), "write_file")
        self.assertTrue(r)
        self.assertEqual(r["args"]["content"].count(loop_line.rstrip("\n")), 1)
        self.assertEqual(r["_salvage_trimmed"], 5)

    def test_html_cell_skeleton_short_runs_are_kept(self):
        # 4-5 identical consecutive lines are legitimate generated HTML /
        # boilerplate ("<td></td>" skeletons) — the trim threshold is 6
        for line, reps in (("<td></td>", 4), ("<div class=\"cell\"></div>", 5)):
            content = "<table>\n" + (line + "\n") * reps
            r = salvage_truncated_write_args(self._truncated(content), "write_file")
            self.assertTrue(r, line)
            self.assertEqual(r["_salvage_trimmed"], 0, line)
            self.assertEqual(r["args"]["content"].count(line), reps)

    def test_html_cell_skeleton_degenerate_run_is_trimmed(self):
        line = "<td></td>"
        content = "<table>\n" + (line + "\n") * 9
        r = salvage_truncated_write_args(self._truncated(content), "write_file")
        self.assertTrue(r)
        self.assertEqual(r["args"]["content"].count(line), 1)
        self.assertEqual(r["_salvage_trimmed"], 8)

    def test_separator_noise_is_never_trimmed(self):
        content = "title\n" + ("----------\n" * 5) + "body\n"
        r = salvage_truncated_write_args(self._truncated(content), "write_file")
        self.assertTrue(r)
        self.assertEqual(r["_salvage_trimmed"], 0)
        self.assertEqual(r["args"]["content"].count("----------"), 5)

    def test_benign_distinct_lines_are_untouched(self):
        content = "alpha_one\nbeta_two\ngamma_three\n"
        r = salvage_truncated_write_args(self._truncated(content), "write_file")
        self.assertTrue(r)
        self.assertEqual(r["args"]["content"], content)
        self.assertEqual(r["_salvage_trimmed"], 0)


class ToolCallValidation(unittest.TestCase):
    """validate_tool_calls — the pure extract of the duo tool-round block.

    Pins the VERBATIM-move contract: what comes out for valid, nameless,
    repairable, salvageable and hopeless tool calls, plus the meta fields the
    [WRITE-TRUNCATION] log line consumes.
    """

    @staticmethod
    def _tc(name, args):
        return {"function": {"name": name, "arguments": args}}

    def tearDown(self):
        # validate_tool_calls records salvage events into the REAL fixture
        # dump (logs/write_truncations.jsonl). In the dev clone that file can
        # only contain test noise — real model dumps accrue in the live copy —
        # so remove it to keep the replay corpus honest.
        import os
        from pathlib import Path as _P
        _dump = _P(__file__).resolve().parents[1] / "logs" / "write_truncations.jsonl"
        try:
            _dump.unlink()
        except OSError:
            pass

    def test_valid_passthrough_untouched(self):
        args = json.dumps({"path": "a.py", "content": "x = 1\n"})
        r = validate_tool_calls([self._tc("write_file", args)])
        self.assertEqual(len(r["tool_calls"]), 1)
        self.assertEqual(r["tool_calls"][0]["function"]["arguments"], args)
        self.assertFalse(r["drop_notices"] or r["salvage_notes"] or r["dropped_names"])
        self.assertTrue(r["meta"]["has_tool_call"])

    def test_nameless_call_dropped(self):
        r = validate_tool_calls([self._tc("", "{}")])
        self.assertEqual(r["tool_calls"], [])
        self.assertIn("no name", r["drop_notices"][0])

    def test_backslash_repair_keeps_call(self):
        args = '{"path": "C:\\Users\\x\\a.py", "content": "x = 1\\n"}'
        with self.assertRaises(json.JSONDecodeError):
            json.loads(args)  # precondition: unescaped backslashes are invalid
        r = validate_tool_calls([self._tc("write_file", args)])
        self.assertEqual(len(r["tool_calls"]), 1)
        json.loads(r["tool_calls"][0]["function"]["arguments"])  # repaired: valid
        self.assertFalse(r["drop_notices"])

    def test_malformed_nonwrite_dropped_with_length_suffix(self):
        r = validate_tool_calls([self._tc("run_bash", '{"cmd": "echo')],
                                finish_reason="length")
        self.assertEqual(r["tool_calls"], [])
        self.assertIn("finish_reason=length", r["drop_notices"][0])
        self.assertEqual(r["dropped_names"], ["run_bash"])
        self.assertFalse(r["meta"]["has_tool_call"])

    def test_truncated_write_salvaged_with_note_and_budget_hint(self):
        raw = json.dumps({"path": "t.py", "content": "a\n" * 400})[:-2]
        r = validate_tool_calls([self._tc("write_file", raw)], finish_reason="length",
                                model="sharp", token_budget=8000, chars_per_token=2.5)
        self.assertEqual(len(r["tool_calls"]), 1)
        note = r["salvage_notes"][0]
        self.assertIn("[WRITE-SALVAGE]", note)
        self.assertIn("write_file_append", note)
        self.assertIn("~5000 chars", note)  # 8000 * 2.5 * 0.25 append hint
        saved = json.loads(r["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(saved["path"], "t.py")

    def test_budgetless_fallback_matches_old_behaviour(self):
        raw = json.dumps({"path": "t.py", "content": "a\n" * 400})[:-2]
        r = validate_tool_calls([self._tc("write_file", raw)], token_budget=None,
                                chars_per_token=None)
        self.assertIn("~500 chars", r["salvage_notes"][0])  # resolver floor

    def test_integration_wiring_contract(self):
        """The single loop-level integration test: an assembled dr_msg with a
        truncated write call flows through validation into the message shapes
        duo_runner builds (user notice / salvage note flushing)."""
        dr_msg = {"role": "assistant", "content": "",
                  "tool_calls": [self._tc(
                      "write_file",
                      json.dumps({"path": "index.html",
                                  "content": "<html>\n" + "  <p>x</p>\n" * 300})[:-2])]}
        res = validate_tool_calls(dr_msg["tool_calls"], finish_reason="length",
                                  model="minicpm5:2b-sharp",
                                  token_budget=8000, chars_per_token=2.5)
        dtool_msgs = []
        if res["drop_notices"]:
            dtool_msgs.append({"role": "user", "content": "\n".join(res["drop_notices"])})
        if res["salvage_notes"]:
            dtool_msgs.append({"role": "user", "content": "\n".join(res["salvage_notes"])})
        self.assertEqual(res["meta"]["has_tool_call"], True)
        self.assertEqual(len(dtool_msgs), 1)               # salvage, not drop
        self.assertIn("index.html", dtool_msgs[0]["content"])
        self.assertTrue(res["tool_calls"][0]["_salvage"]["_salvaged_lines"] > 1)


class ResolverHints(unittest.TestCase):
    def test_budget_math_and_floor(self):
        write_hint, append_hint = resolve_write_char_limits("m", 8000, 2.5, 0)
        self.assertEqual((write_hint, append_hint), (8000, 5000))
        self.assertEqual(resolve_write_char_limits("m", 8000, 2.5, 3000), (5000, 3125))
        self.assertEqual(resolve_write_char_limits("m", 100, 2.5, 0), (500, 500))


class TruncationFixtureDump(unittest.TestCase):
    def test_cap_keeps_last_incidents(self):
        import json as _json
        from utils.tool import record_truncation_fixture
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
            path = tf.name
        try:
            for i in range(5):
                raw = json.dumps({"path": f"t{i}.py", "content": "x"})[:-2]
                self.assertTrue(record_truncation_fixture(
                    "write_file", raw, model="m", finish_reason="length",
                    cap=3, path=path))
            lines = [l for l in Path(path).read_text(encoding="utf-8").splitlines() if l]
            self.assertEqual(len(lines), 3)
            names = [_json.loads(l)["raw"] for l in lines]
            self.assertIn("t4", names[-1])   # newest survives
            self.assertIn("t2", names[0])    # oldest (t0, t1) dropped
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

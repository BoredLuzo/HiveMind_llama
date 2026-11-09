# -*- coding: utf-8 -*-
"""Behavior test: the TOOL-CALL-MISS warning (2026-10-30).

Live Groot run: 7/7 coder rounds emitted tool calls as plain text and
nothing parsed; the request state of the failing round is the only evidence.
The logging helper must emit exactly that state once, at WARNING, and must
never break the round when formatting fails.

2026-11-08: converted from a source pin inside run_code_duo to a behavior
test of the extracted `_log_tool_call_miss` helper.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.duo_policies import (  # noqa: E402
    _log_tool_call_miss, _log_write_truncation,
    _plan_dropped_tool_retry, _plan_text_only_round,
)
from core.duo_helpers import _log_msgsig_drift  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

_REQUIRED = (
    "thinking=", "ctk=", "tools=", "schema_chars=", "hash=",
    "tool_choice=", "slot_ctx=", "req_ctx=", "prompt_tokens=", "cached=",
    "finish=", "completion=", "max_tokens=",
    "temp=", "presence=", "top_p=", "top_k=", "min_p=", "seed=",
    "content[0:500]=", "reasoning[0:500]=", "raw[0:500]=",
)


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


_logger = logging.getLogger("hivemind.duo")
_cap = _Capture()
_old_level = _logger.level
_logger.addHandler(_cap)
_logger.setLevel(logging.DEBUG)

try:
    _log_tool_call_miss(
        round_no=3,
        coder_mdl="coder:4b",
        thinking=True,
        tool_payload={
            "tools": [{"name": "read_file"}],
            "tool_choice": "auto",
            "max_tokens": 900,
            "temperature": 0.2,
            "presence_penalty": 1.5,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "seed": 7,
            "chat_template_kwargs": {"enable_thinking": True},
        },
        dtool_opts={"num_ctx": 8192},
        coder_ctx_eff=8000,
        result={
            "dr_usage_final": {
                "prompt_tokens": 120, "cached_tokens": 30,
                "completion_tokens": 40,
            },
            "dr_finish_reason": "stop",
            "dr_thinking_parts": ["reasoning text"],
            "dr_content_parts": ["raw text"],
        },
        dr_msg={"content": "hello world"},
    )
    _msg = next((r.getMessage() for r in _cap.records
                 if "[TOOL-CALL-MISS]" in r.getMessage()), "")
    check("warning emitted at WARNING",
          any(r.levelno == logging.WARNING and "[TOOL-CALL-MISS]" in r.getMessage()
              for r in _cap.records))
    _missing = [f for f in _REQUIRED if f not in _msg]
    check("every incident field is logged", not _missing, f"missing={_missing}")
    check("content and reasoning are sliced to 500 chars",
          "content[0:500]='hello world'" in _msg
          and "reasoning[0:500]='reasoning text'" in _msg)
    check("tools are identified by a stable hash",
          "hash=" in _msg and len(_msg.split("hash=")[1].split()[0]) == 12)

    _cap.records.clear()
    _log_write_truncation(
        coder_mdl="coder:4b", round_no=7, finish_reason="length",
        had_tool_calls=True, has_tool_call=False, content_len=123,
        usage={"completion_tokens_details": {"reasoning_tokens": 9}},
        thinking_parts=["abc", "de"],
    )
    _wt = next(r.getMessage() for r in _cap.records
               if "[WRITE-TRUNCATION]" in r.getMessage())
    check("write truncation logs had/has and reasoning tokens",
          "had_tool_call=True" in _wt and "has_tool_call=False" in _wt
          and "reasoning_tokens=9" in _wt and "think_chars=5" in _wt)
    check("write truncation content length is logged",
          "content_len=123" in _wt and "round=7" in _wt)

    # Formatting failure must not escape into the run.
    class _Bad:
        def get(self, *a, **kw):
            raise TypeError("boom")

    _cap.records.clear()
    try:
        _log_tool_call_miss(
            round_no=1, coder_mdl="m", thinking=False, tool_payload=_Bad(),
            dtool_opts={}, coder_ctx_eff=0, result={}, dr_msg={},
        )
        _raised = False
    except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        _raised = True
    check("formatting failure cannot break the round", not _raised)
    check("formatting failure is debug-logged",
          any("[TOOL-CALL-MISS] log failed" in r.getMessage()
              for r in _cap.records))
finally:
    _logger.removeHandler(_cap)
    _logger.setLevel(_old_level)

# The helper is called only for real tool rounds.
_src = (_ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8",
                                                    errors="replace")
_call = _src.find("_log_tool_call_miss(\n")
_seg = _src[_src.rfind("if not _dr_tcs:", 0, _call):_call] if _call > 0 else ""
check("helper is called inside the no-tool-calls branch",
      bool(_seg) and "if _is_tool_round:" in _seg)

# ── dropped/truncated tool-call policy ───────────────────────────────────
def _plan(**over):
    _kw = dict(
        result={"dr_finish_reason": "length"},
        drop_names=["edit_file"], drop_len="", dr=0, max_tool_rounds=10,
        max_tokens_round=800, messages=[], think_only_retries=2,
        dropped_tool_retries=0, force_compress_next=False,
        loop_detected=False,
        should_compress=lambda _f, _n: False,
        build_retry=lambda names: "retry " + ",".join(names),
        ld_setter=lambda _line: None,
    )
    _kw.update(over)
    return _plan_dropped_tool_retry(**_kw)


_msgs: list = []
_r = _plan(messages=_msgs)
check("dropped call: retry continues with counters reset",
      _r.action == "continue" and _r.dropped_tool_retries == 1
      and _r.think_only_retries == 0 and len(_r.events) == 1)
check("dropped call: targeted repair message is appended",
      len(_msgs) == 1 and "retry edit_file" in _msgs[0].get("content", ""))

_r2 = _plan(dropped_tool_retries=2)
check("third dropped call aborts the loop",
      _r2.action == "break" and _r2.loop_detected is True
      and len(_r2.events) == 1)

_msgs3: list = []
_r3 = _plan(messages=_msgs3, should_compress=lambda _f, _n: True)
check("truncated edit forces compression instead of retrying",
      _r3.action == "continue" and _r3.force_compress_next is True
      and not _r3.events and len(_msgs3) == 1
      and "OUTPUT LIMIT" in _msgs3[0].get("content", ""))

# ── text-only / empty-answer policy ──────────────────────────────────────
def _txt(**over):
    _kw = dict(
        final="short text", result={"dr_finish_reason": "stop"}, dr_msg={},
        thinking_parts=[], file_changes={}, ws_str="", seen_explore_paths=set(),
        dr=0, max_tool_rounds=10, messages=[], parts=[],
        think_only_retries=0, soft_check_done=False,
        zero_activity_nudged=False, coder_no_think=False,
        loop_detected=False, coder_mdl="m",
        build_soft_check=lambda _f, _w: "soft-check", ld_setter=lambda _l: None,
    )
    _kw.update(over)
    return _plan_text_only_round(**_kw)


_msgs_s: list = []
_ts = _txt(file_changes={"a.py": 1}, messages=_msgs_s)
check("text round injects the soft check once",
      _ts.action == "continue" and _ts.soft_check_done is True
      and _ts.think_only_retries == 0 and len(_msgs_s) == 1)

_parts_a: list = []
_ta = _txt(final="x" * 200, file_changes={"a.py": 1},
           think_only_retries=2, soft_check_done=True, parts=_parts_a)
check("substantial summary with activity is accepted",
      _ta.action == "break" and not _ta.loop_detected
      and _parts_a == ["x" * 200] and len(_ta.events) == 1)

_tz = _txt(think_only_retries=2)
check("zero-activity summary gets one start nudge",
      _tz.action == "continue" and _tz.zero_activity_nudged is True
      and _tz.think_only_retries == 0 and len(_tz.events) == 0)

_tl = _txt(think_only_retries=2, soft_check_done=True,
           file_changes={"a.py": 1})
check("text-only loop aborts after the summary ladder",
      _tl.action == "break" and _tl.loop_detected is True
      and len(_tl.events) == 1)

_me: list = []
_te = _txt(final="", messages=_me)
check("empty answer retries with a nudge",
      _te.action == "continue" and len(_me) == 1
      and "empty response" in _me[0].get("content", "")
      and len(_te.events) == 1)

_parts_e: list = []
_tf = _txt(final="", dr=9, max_tool_rounds=10, parts=_parts_e)
check("empty answer on the last round records the fallback text",
      _tf.action == "break" and len(_parts_e) == 1
      and "empty answer" in _parts_e[0])

# ── DEBUG message-signature diagnostics ─────────────────────────────────
_sig_log = logging.getLogger("hivemind.duo")
_sig_cap = _Capture()
_sig_old = _sig_log.level
_sig_log.addHandler(_sig_cap)
_sig_log.setLevel(logging.DEBUG)
try:
    _prev = [None]
    _log_msgsig_drift([{"role": "user", "content": "a"}], _prev,
                      round_no=2, chunk_no=1)
    check("role dump is emitted at DEBUG",
          any("[ROLE-DUMP]" in r.getMessage() for r in _sig_cap.records)
          and _prev[0] == [("user", 1)])
    _sig_cap.records.clear()
    _log_msgsig_drift([{"role": "user", "content": "a" * 20}], _prev,
                      round_no=3, chunk_no=1)
    _warn = [r.getMessage() for r in _sig_cap.records
             if "[MSGSIG-CHANGE]" in r.getMessage()]
    check("a shape change is logged once with round + size",
          len(_warn) == 1 and "round=3" in _warn[0] and "(msgs=1)" in _warn[0])
finally:
    _sig_log.removeHandler(_sig_cap)
    _sig_log.setLevel(_sig_old)

sys.exit(h.finish("tool_call_miss_log"))

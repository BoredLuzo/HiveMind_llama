# -*- coding: utf-8 -*-
"""Tool utility functions (extracted from server.py)."""
from __future__ import annotations
import json
import re

_RE_UNESCAPED_BACKSLASH = re.compile(r'\\([^\\/"bfnrtu])')


def _repair_json_backslashes(raw: str) -> str:
    """Escapes unescaped backslashes in a JSON text (Windows paths)."""
    return _RE_UNESCAPED_BACKSLASH.sub(r'\\\\\1', raw)


def parse_tool_args(raw) -> dict:


    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            try:
                return json.loads(_repair_json_backslashes(raw))
            except Exception:
                return {}
    return raw if isinstance(raw, dict) else {}


# ── WRITE-LIMIT-BUDGET (2026-08-22) ────────────────────────────────────────
# at the output token limit (duo_coder.max_tokens=8000) mid JSON/XML argument
# truncated -> finish_reason=length -> DROPPED -> 3x retry -> loop stop.
# calibration: [WRITE-CALIBRATION] logs (agentic_tool_loop) provide real
# ── WRITE-SIZE HINT (2026-09-29) ──────────────────────────────────────────────
# No per-size tiers anymore. Core insight: a fully-arrived call (finish_reason
# == stop, valid JSON) can never exceed max_tokens — max_tokens IS the hard
# limit, so a complete write is always accepted, whatever its size. The only
# failure mode is TRUNCATION (finish_reason == length), and the salvage path
# recovers that at the last complete line. The number below is therefore just
# a soft hint (chars per write_file / write_file_append call) that keeps
# models clear of the truncation/salvage path:
#   usable_tokens = max_tokens - reasoning_tokens   [thinking models burn it]
#   write_hint    = usable_tokens * chars_per_token * 0.4
#   append_hint   = usable_tokens * chars_per_token * 0.25
# The 0.4 cold-start factor reserves room for reasoning/thinking output and
# the tool-call wrapper. No name matching — new models need no entry here.
_WRITE_HINT_WRITE_FACTOR = 0.4
_WRITE_HINT_APPEND_FACTOR = 0.25
_WRITE_HINT_MIN_CHARS = 500


def resolve_write_char_limits(model: str = "", token_budget: int | None = None,
                              chars_per_token: float | None = None,
                              reasoning_tokens: int = 0) -> tuple[int, int]:


    _cpt = float(chars_per_token or 2.5)
    if _cpt <= 0:
        _cpt = 2.5
    _tok = max(0, int(token_budget or 0) - max(0, int(reasoning_tokens or 0)))
    _write = max(_WRITE_HINT_MIN_CHARS, int(_tok * _cpt * _WRITE_HINT_WRITE_FACTOR))
    _append = max(_WRITE_HINT_MIN_CHARS, int(_tok * _cpt * _WRITE_HINT_APPEND_FACTOR))
    return _write, _append


# ── TRUNCATION-SALVAGE (2026-08-22) ────────────────────────────────────────
_RE_SURROGATE = re.compile(r"[\ud800-\udfff]")
# incomplete escape fragment at the very end of a truncated string
_RE_TRAILING_PARTIAL_ESCAPE = re.compile(r"\\(?:u[0-9a-fA-F]{0,3}|\\)?$")


def _decode_salvaged_string(body: str) -> str:


    out = []
    i, n = 0, len(body)
    while i < n:
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            break  # dangling backslash: truncation -> discard the tail
        nxt = body[i + 1]
        if nxt == "u":
            _hex = body[i + 2:i + 6]
            if len(_hex) < 4 or not all(c in "0123456789abcdefABCDEF" for c in _hex):
                break  # incomplete \\uXXXX: truncation -> discard the tail
            out.append(chr(int(_hex, 16)))
            i += 6
            continue
        _esc_map = {"\\": "\\", '"': '"', "/": "/", "b": "\b",
                    "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
        if nxt in _esc_map:
            out.append(_esc_map[nxt])
            i += 2
            continue
        out.append("\\")
        out.append(nxt)
        i += 2
    return "".join(out)


def _scan_json_string(s: str, q: int) -> tuple[str, bool]:


    i, n = q + 1, len(s)
    while i < n:
        ch = s[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return s[q + 1:i], True
        i += 1
    return s[q + 1:n], False


def _extract_write_key(raw: str, key: str, start: int) -> tuple[str, bool] | None:


    _m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"', raw[start:])
    if not _m:
        return None
    _q = start + _m.end() - 1
    return _scan_json_string(raw, _q)


def _salvaged_line_count(content: str) -> int:


    return content.count("\n") if content.endswith("\n") else content.count("\n") + 1


def _trim_degenerate_tail(content: str) -> tuple[str, int]:
    """Collapse a trailing run of identical non-trivial lines to one (2026-09-30).

    A small model stuck in a repetition loop runs to the token limit; the
    salvaged prefix then ends with the same line repeated many times — and the
    recovery message asks the model to build ON TOP of that loop. >= 6
    consecutive identical lines that are not whitespace/separator noise are
    treated as a loop, trimmed to one occurrence, and flagged via
    _salvage_trimmed. Threshold is 6, not 4: short runs of identical lines are
    legitimate in generated HTML/boilerplate ("<td></td>" cell skeletons,
    "<div class=...></div>" grids) and must survive verbatim — degenerate
    loops observed live run far longer. Multi-line loop patterns (A,B,A,B,...)
    are NOT caught here — that needs the stream-level n-gram detector.
    """
    if not content.endswith("\n"):
        return content, 0
    lines = content.split("\n")
    if len(lines) < 7:
        return content, 0
    tail = lines[-2]  # lines[-1] is "" from the trailing newline
    stripped = tail.strip()
    if len(stripped) < 8 or len(set(stripped)) < 3:
        return content, 0  # separator/brace noise ("----", "}}}") — never trim
    run = 0
    i = len(lines) - 2
    while i >= 0 and lines[i] == tail:
        run += 1
        i -= 1
    if run < 6:
        return content, 0
    return "\n".join(lines[:i + 1] + [tail, ""]), run - 1


def record_truncation_fixture(tool: str, raw: str, *, model: str = "",
                              finish_reason: str | None = None,
                              cap: int = 20, path=None) -> bool:
    """Persist malformed/truncated write-call args as REAL replay fixtures.

    The first two-version replay ran on synthetic fixtures because raw args
    are persisted nowhere (sessions keep only summaries, logs only counts).
    This appends every malformed write-call argument string to
    logs/write_truncations.jsonl (last `cap` incidents, first field wins in
    later analysis — finish_reason separates true max_tokens cuts from other
    JSON breakage). Best effort: never raises into the run loop.
    """
    import time as _time
    try:
        from pathlib import Path as _P
        _path = _P(path) if path else (
            _P(__file__).resolve().parents[1] / "logs" / "write_truncations.jsonl")
        entry = {"ts": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "tool": str(tool), "model": str(model or ""),
                 "finish_reason": finish_reason,
                 "raw_len": len(raw or ""), "raw": str(raw or "")[:64000]}
        old: list = []
        if _path.exists():
            import json as _json_tr
            for _ln in _path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    old.append(_json_tr.loads(_ln))
                except Exception:
                    pass
        old.append(entry)
        _path.parent.mkdir(parents=True, exist_ok=True)
        _path.write_text("\n".join(json.dumps(e, ensure_ascii=True)
                                   for e in old[-cap:]) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


def arguments_repetition_drift(buf: str, *, min_chars: int = 1024,
                               min_repeat: int = 30) -> bool:
    """Detect a degenerate identical-line loop in RAW tool-call arguments (2026-09-30).

    Live incident: a small model repeated one line 293x inside a write_file
    call and burned the full 8000-token budget (~11 min at 11.5 t/s) before
    finish_reason=length fired. This check runs on the ESCAPED arguments
    buffer while it streams in — a literal backslash-n separates lines, so no
    JSON decoding is needed. min_repeat is 30 (not 10): a VALID write may
    legitimately contain runs of identical lines (data rows, grid literals),
    and a false cut costs a full regeneration — observed loops run into the
    hundreds, so 30 still fires within ~3k chars of loop start.
    """
    if not buf or len(buf) < min_chars:
        return False
    NL = chr(92) + "n"  # literal backslash-n in the JSON-escaped stream
    lines = buf[-16384:].split(NL)
    if len(lines) < min_repeat + 1:
        return False
    # the LAST element is always a PARTIAL line (the buffer cut is
    # arbitrary mid-stream) — using it as the pattern never matches its
    # complete predecessor. The last COMPLETE line (index -2) is the pattern.
    ref = lines[-2]
    if len(ref.strip()) < 8 or len(set(ref.strip())) < 3:
        return False  # separator/brace noise ("----", "}}}")
    run = 0
    j = len(lines) - 2
    while j >= 0 and lines[j] == ref:
        run += 1
        j -= 1
    return run >= min_repeat


# ── TOOL-CALL VALIDATION (extracted from duo_runner 2026-09-30) ──────────────
# PURE function: tool_calls + budget in, validated tool_calls / drop notices /
# salvage notes / dropped names / meta out. No ctx, no emit, no loop state —
# a verbatim move of the duo tool-round validation block so fix_agent,
# subagent_lite and the chat loop can reuse the exact same repair+salvage
# behavior. The write-tools test suite pins it; wiring errors of the
# NameError class are caught by the F821 pre-commit hook.
def validate_tool_calls(tool_calls, *, finish_reason: str | None = None,
                        model: str = "", token_budget: int | None = None,
                        chars_per_token: float | None = None) -> dict:

    import logging as _logging
    _log = _logging.getLogger("hivemind.tools")
    validated: list = []
    drop_notices: list = []
    drop_names: list = []
    salvage_notes: list = []
    _drop_len = (" — finish_reason=length (output token limit reached)"
                 if finish_reason == "length" else "")
    for _vtc in (tool_calls or []):
        if not isinstance(_vtc, dict):
            continue
        _vname = (str(((_vtc.get("function") or {}).get("name")) or "")).strip()
        _vargs = (_vtc.get("function") or {}).get("arguments", "")
        if not _vname:
            drop_notices.append(
                "[DROPPED: tool call with no name — stream interrupted]")
            continue
        try:
            json.loads(_vargs) if _vargs else {}
        except (json.JSONDecodeError, TypeError):
            try:
                _repaired_args = _repair_json_backslashes(_vargs)
                json.loads(_repaired_args)
                _vtc = dict(_vtc)
                _vtc["function"] = dict(_vtc.get("function") or {})
                _vtc["function"]["arguments"] = _repaired_args
            except (json.JSONDecodeError, TypeError):
                # truncation at the output limit — salvageable prefix.
                # REAL-FIXTURE-DUMP: keep the raw args (last 20) so the next
                # replay corpus is real model output, not synthetic fixtures
                record_truncation_fixture(_vname, _vargs, model=model,
                                          finish_reason=finish_reason)
                _salv = None
                if _vname in ("write_file", "write_file_append"):
                    try:
                        _salv = salvage_truncated_write_args(_vargs, _vname)
                    except Exception:
                        _salv = None
                if _salv:
                    _vtc = dict(_vtc)
                    _vtc["function"] = dict(_vtc.get("function") or {})
                    _vtc["function"]["arguments"] = json.dumps(
                        _salv["args"], ensure_ascii=False)
                    _vtc["_salvage"] = _salv
                    _salv_path = _salv["args"].get("path", "?")
                    _append_hint = 4000
                    try:
                        _append_hint = int(
                            (resolve_write_char_limits(_vname, token_budget,
                                                       chars_per_token) or (0, 4000))[1]
                        ) or 4000
                    except Exception:
                        _append_hint = 4000
                    _salv_tail = str(_salv.get("_salvaged_tail", "") or "")
                    _salv_trimmed = int(_salv.get("_salvage_trimmed", 0) or 0)
                    _trim_note = (
                        f" A repetition loop was detected at the cut — "
                        f"{_salv_trimmed} duplicated trailing line(s) were "
                        f"removed; re-check that region before continuing."
                        if _salv_trimmed else "")
                    salvage_notes.append(
                        f"[WRITE-SALVAGE] {_vname} for '{_salv_path}' was cut off at the "
                        f"output limit — wrote {_salv['_salvaged_chars']} "
                        f"chars ({_salv['_salvaged_lines']} lines), cut at the "
                        f"last complete line.{_trim_note} The file is now INCOMPLETE. Do NOT rewrite "
                        f"the whole file (that duplicates content). Continue exactly "
                        f"after the LAST line below with write_file_append "
                        f"(chunks of max ~{_append_hint} chars):\n"
                        f"{_salv_tail}"
                    )
                    _log.warning(
                        "[WRITE-SALVAGE] %s '%s' salvaged: %d chars, %d lines%s",
                        _vname, _salv_path,
                        _salv["_salvaged_chars"], _salv["_salvaged_lines"],
                        f", repetition-trimmed {_salv_trimmed} line(s)" if _salv_trimmed else "")
                else:
                    drop_names.append(_vname)
                    drop_notices.append(
                        f"[DROPPED: tool call '{_vname}' had malformed JSON args "
                        f"— stream interrupted{_drop_len}]"
                    )
                    continue
        validated.append(_vtc)
    # had_tool_calls counts calls BEFORE validation, has_tool_call AFTER —
    # a truncation whose only call was dropped by salvage rules must not read
    # as "reasoning-overrun without any call" in the diagnostics line
    _had = [t for t in (tool_calls or [])
            if isinstance(t, dict)
            and str(((t.get("function") or {}).get("name")) or "").strip()]
    return {"tool_calls": validated,
            "drop_notices": drop_notices,
            "salvage_notes": salvage_notes,
            "dropped_names": drop_names,
            "meta": {"finish_reason": finish_reason,
                     "had_tool_calls": bool(_had),
                     "has_tool_call": bool(validated)}}


def salvage_truncated_write_args(raw: str, tool_name: str = "write_file") -> dict | None:


    if not isinstance(raw, str) or not raw:
        return None
    if tool_name not in ("write_file", "write_file_append"):
        return None
    _start = raw.find("{")
    if _start == -1:
        return None
    _cand = raw[_start:]

    try:
        _parsed = json.loads(_cand + "}")
        if isinstance(_parsed, dict) and _parsed.get("path") and _parsed.get("content") is not None:
            _c = str(_parsed["content"])
            if _c:
                return {"args": {"path": str(_parsed["path"]), "content": _c},
                        "_salvage": True,
                        "_salvaged_chars": len(_c),
                        "_salvaged_lines": _salvaged_line_count(_c)}
    except (json.JSONDecodeError, TypeError):
        pass

    _path_body, _path_closed = _extract_write_key(_cand, "path", 0) or (None, False)
    if not _path_body or not _path_closed:
        return None
    _path = _decode_salvaged_string(_path_body)
    if not _path:
        return None

    _body, _closed = _extract_write_key(_cand, "content", 0) or (None, False)
    if _body is None:
        return None
    _content = _decode_salvaged_string(_body)
    _content = _RE_SURROGATE.sub("\ufffd", _content)
    if not _content:
        return None
    # TRUNCATION-EDGE (2026-09-29): a cut inside an escape sequence leaves a
    # literal fragment at the end of the decoded content ("\u00e", a lone
    # backslash). Only strip it when the JSON string was NOT closed — a real
    # trailing backslash in a closed string is legitimate content.
    if not _closed:
        _content = _RE_TRAILING_PARTIAL_ESCAPE.sub("", _content)
    if not _content:
        return None
    if not _closed and "\n" in _content:
        _head, _, _tail = _content.rpartition("\n")
        if _tail:
            _content = _head + "\n"
    _trimmed = 0
    if not _closed:
        # repetition-loop cut BEFORE the tail is sampled for the recovery
        # message — the model must not continue on top of a degenerate loop
        _content, _trimmed = _trim_degenerate_tail(_content)
        if not _content:
            return None
    _tail_lines = _content.splitlines()[-3:]
    return {"args": {"path": _path, "content": _content},
            "_salvage": True,
            "_salvaged_chars": len(_content),
            "_salvaged_lines": _salvaged_line_count(_content),
            "_salvage_trimmed": _trimmed,
            "_salvaged_tail": "\n".join(_tail_lines)}


def run_bash_failed(result: str) -> bool:
    from tools.errors import parse_tool_error as _parse_tool_error
    txt = str(result or "")
    _terr = _parse_tool_error(txt)
    if _terr and str(_terr.get("tool", "") or "") == "run_bash":
        return True
    low = txt.lower()
    if txt.startswith("[run_bash error:"):
        return True
    if "[run_bash: timeout" in low:
        return True
    # TEST-RESULT-FAIL (2026-09-13): run_tests results carry no [exit code:]
    # marker — a failing suite (server-generated ❌) must count as a failure,
    # otherwise the verify gates treat mutations as verified despite red tests.
    if "[test-result] ❌" in low:
        return True
    if "[exit code:" in txt:
        # LAST match wins: the real marker is appended at the very end of the
        # output; an earlier literal "[exit code: 0]" (echoed by the model)
        # must not mask a genuine non-zero exit.
        matches = __import__("re").findall(r"\[exit code:\s*(\d+)", txt)
        if matches and int(matches[-1]) != 0:
            return True
    return False

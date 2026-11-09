# -*- coding: utf-8 -*-
"""Discard write_file calls cut at the output limit (updated 2026-11-09).

Never write partial content or a syntax-breaking truncation marker to disk.
When finish_reason=="length", any write_file/write_file_append call in
``validated_tcs`` is dropped in-place and reported via ``[WRITE-TRUNCATED]``
in ``drop_notices`` so the model re-sends in smaller chunks.
"""
from __future__ import annotations

import json


def salvage_truncated_writes(validated_tcs: list, drop_notices: list) -> list[str]:
    """Drop cut write calls in-place and emit a [WRITE-TRUNCATED] notice.

    Returns the list of dropped target paths (for calls whose path was
    parseable). Non-write tool calls in ``validated_tcs`` are left untouched.
    """
    dropped_paths: list[str] = []
    had_write_call = False
    for _wi in reversed(range(len(validated_tcs))):
        _wtc = validated_tcs[_wi]
        if str(((_wtc.get("function") or {}).get("name")) or "") not in (
                "write_file", "write_file_append"):
            continue
        had_write_call = True
        validated_tcs.pop(_wi)
        try:
            _wa = _wtc["function"].get("arguments", "")
            _ad = json.loads(_wa) if isinstance(_wa, str) else dict(_wa or {})
            _p = str(_ad.get("path", "") or "").strip()
            if _p:
                dropped_paths.append(_p)
        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
            pass
    if dropped_paths:
        drop_notices.append(
            "[WRITE-TRUNCATED] write_file hit the output token limit and was "
            "discarded — nothing was written to " + ", ".join(dropped_paths) + ". "
            "Write in smaller chunks: call write_file with the first part, then "
            "write_file_append for the remaining parts.")
    elif had_write_call or not validated_tcs:
        drop_notices.append(
            "[WRITE-TRUNCATED] write_file was cut at the output token limit and "
            "discarded — nothing was written. Re-send in smaller pieces: "
            "write_file the first half, then write_file_append the rest.")
    return dropped_paths

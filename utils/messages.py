"""Message-list helpers shared by the senders."""

from __future__ import annotations


def normalize_system_messages(msgs: list[dict]) -> list[dict]:
    """Merge every system message into one at index 0 (strict-template guard).

    MiniCPM-V 4.7's chat template raises "System message must be at the beginning"
    when a system message appears after the first user turn, and HiveMind appends
    "no-think" / nudge / retry notes as system messages mid-history. Merging keeps
    the content, fixes the order, and is a no-op for a well-formed history.
    """
    if not isinstance(msgs, list) or not msgs:
        return msgs
    _systems = [m for m in msgs if isinstance(m, dict) and m.get("role") == "system"]
    if len(_systems) <= 1 and msgs[0].get("role") == "system":
        return msgs
    if not _systems:
        return msgs
    _rest = [m for m in msgs if not (isinstance(m, dict) and m.get("role") == "system")]
    _merged = "\n\n".join(
        str(m.get("content") or "") for m in _systems
        if str(m.get("content") or "").strip()
    )
    return [{"role": "system", "content": _merged}] + _rest

# -*- coding: utf-8 -*-
"""Static repo-map pinning into the system message (2026-09-06).

The system message is the only part of the prompt that survives compression
byte-identical (plus tool definitions). Moving the deterministic
"## Static Repo-Map" block there extends the stable cache prefix beyond the
~7.1k base instead of letting the map live inside the big first user message
that full compression rewrites.

Deltas policy: repo-map deltas (files created/deleted during the run) are
appended AFTER the pinned block, never inside it, and only at a compression
(see append_repo_map_delta): compression rewrites the prefix anyway, so the
addition costs no extra prefill. Between compressions the block stays
byte-stable - that is the whole point of the pin.
The block is capped (MAX_DELTA_LINES) so it cannot grow without bound.
"""
from __future__ import annotations

from pathlib import Path

_PIN_MARKER = "[REPO-MAP — pinned, byte-stable]"
_MAP_HEADING = "## Static Repo-Map"
_DELTA_HEAD = "[REPO-MAP-DELTA — changed since the pin]"
MAX_DELTA_LINES = 12        # paths per appended delta line
MAX_DELTA_BLOCKS = 4        # older delta blocks are merged away


def _extract_map_block(content: str):
    """Remove ONLY the '## Static Repo-Map' block (heading .. next '## ' / EOF).

    Everything before/after the block stays in the message - unlike
    ctx_utils.split_static_map_section (which assumes map+contracts only).
    Returns (block_or_None, rest).
    """
    i = content.find(_MAP_HEADING)
    if i < 0:
        return None, content
    j = content.find("\n## ", i + len(_MAP_HEADING))
    end = j if j > 0 else len(content)
    block = content[i:end].strip("\n")
    rest = (content[:i] + content[end:]).strip("\n")
    return block, rest


def _already_pinned(system_content: str) -> bool:
    return (_MAP_HEADING in system_content) or (_PIN_MARKER in system_content)


def pin_static_map(messages: list) -> bool:
    """Move the '## Static Repo-Map' block from user messages into system[0].

    - System content keeps everything else.
    - User content that contained ONLY the map block is dropped.
    - Returns True when a move happened (list mutated in place).
    """
    if not messages:
        return False
    sysm = messages[0]
    if not isinstance(sysm, dict) or sysm.get("role") != "system":
        return False
    sys_content = str(sysm.get("content") or "")
    if _already_pinned(sys_content):
        return False

    map_blocks: list = []
    seen: set = set()
    new_msgs: list = [sysm]
    moved = False
    for m in messages[1:]:
        if not isinstance(m, dict):
            new_msgs.append(m)
            continue
        c = m.get("content")
        if not isinstance(c, str) or _MAP_HEADING not in c:
            new_msgs.append(m)
            continue
        static, rest = _extract_map_block(c)
        if not static:
            new_msgs.append(m)
            continue
        moved = True
        if static not in seen:
            seen.add(static)
            map_blocks.append(static)
        if rest and rest.strip():
            new_msgs.append({**m, "content": rest.strip()})
        # else: the message contained only the map -> drop it.

    if not moved:
        return False

    if map_blocks:
        sysm["content"] = (
            sys_content
            + "\n\n"
            + _PIN_MARKER
            + "\n"
            + "\n\n".join(map_blocks)
        ).strip()
    messages[:] = new_msgs
    return True


def append_repo_map_delta(messages: list, changed_paths, *,
                          removed_paths=None) -> bool:
    """Append one delta line to the pinned block (called at compression only).

    The repo map is frozen for the run so the prefix cache holds; files the
    agent created or deleted are therefore invisible to it. Compression is the
    one moment where the prefix is rewritten anyway, so the delta rides along
    for free. Returns True when something was appended.
    """
    if not messages:
        return False
    sysm = messages[0]
    if not isinstance(sysm, dict) or sysm.get("role") != "system":
        return False
    content = str(sysm.get("content") or "")
    if _PIN_MARKER not in content:
        return False                      # nothing pinned - nothing to extend
    added = [str(p) for p in (changed_paths or []) if str(p).strip()]
    removed = [str(p) for p in (removed_paths or []) if str(p).strip()]
    if not added and not removed:
        return False

    # keep only what the pinned block does not already mention
    added = [p for p in dict.fromkeys(added) if Path(p).name not in content]
    if not added and not removed:
        return False

    parts = []
    if added:
        parts.append("+ " + ", ".join(p.replace("\\", "/") for p in added[:MAX_DELTA_LINES]))
    if removed:
        parts.append("- " + ", ".join(p.replace("\\", "/") for p in removed[:MAX_DELTA_LINES]))
    line = _DELTA_HEAD + "\n" + "\n".join(parts)

    blocks = [b for b in content.split(_DELTA_HEAD) if b.strip()]
    head = content.split(_DELTA_HEAD)[0]
    prev = [(_DELTA_HEAD + b).strip() for b in blocks[1:]]
    prev.append(line)
    prev = prev[-MAX_DELTA_BLOCKS:]
    sysm["content"] = (head.rstrip() + "\n\n" + "\n\n".join(prev)).strip()
    return True

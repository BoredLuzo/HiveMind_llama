# -*- coding: utf-8 -*-
"""Static repo-map pinning into the system message (2026-09-06).

The system message is the only part of the prompt that survives compression
byte-identical (plus tool definitions). Moving the deterministic
"## Static Repo-Map" block there extends the stable cache prefix beyond the
~7.1k base instead of letting the map live inside the big first user message
that full compression rewrites.

Deltas policy (future): repo-map deltas (new/deleted files) must be appended
after the pinned block, never rewrite it.
TODO (consolidation): when delta generation lands, rebuild + flush the pinned
map after N deltas / X minutes - otherwise the pinned block grows unbounded
(same problem #2 solves for the rest of the context, relocated into the one
region that compression protects).
"""
from __future__ import annotations

from typing import Optional

_PIN_MARKER = "[REPO-MAP — pinned, byte-stable]"
_MAP_HEADING = "## Static Repo-Map"


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




from __future__ import annotations


import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Awaitable, Any

from settings import load_settings as _load_settings
settings = _load_settings()  # Runtime settings dict

import httpx

from core.duo_helpers import RE_THINK_CLEANUP as _RE_THINK_CLEANUP
from core.duo_helpers import _apply_thinking_fields
from hive_functions.ctx_utils import compute_char_caps, extract_known_files

logger = logging.getLogger("hivemind.planner")

# ═══════════════════════════════════════════════════════════════════════════════
#  MODEL SIZE ESTIMATION
# ═══════════════════════════════════════════════════════════════════════════════

def estimate_model_size(model_name: str) -> str:
    """Estimates the model size from its name — returns '4b', '9b', '14b+' etc."""
    model_name = str(model_name or "").lower()
    for tag in ["2b", "4b", "9b", "14b", "70b"]:
        if tag in model_name:
            return tag
    return "9b"


# ═══════════════════════════════════════════════════════════════════════════════
#  SYSTEM PROMPT GENERATORS
# ═══════════════════════════════════════════════════════════════════════════════

_STRUCT_OUTPUT = """

=== OUTPUT FORMAT ===

Structure your answer ALWAYS like this:

## Plan: [Short summary, max 1 sentence]

### Open Tasks
1. [Concrete, actionable task]
2. [Next task]
...

### Completed Tasks
- [x] [What gets done in this run]

### Summary
[Max 3 sentences: what the coder should do, order, key constraints]

Do NOT omit any of these sections.
"""


def make_thinking_planner_sys(max_steps: int, model_name: str = "") -> str:
    """Size-adaptive planner prompt for thinking models.

    4-9B: slim thinking directive (focused on top-2 risks)
    10-14B+: full analysis (top-3 risks + edge cases)
    """
    model_size = estimate_model_size(model_name)
    is_small_model = model_size in ("2b", "4b")

    if is_small_model:
        return (
        f"Produce a concrete, ordered codebase-aware implementation plan.\n"
        f"Codebase analysis is provided if available (CONTRACT OVERVIEW with complexity scores).\n\n"
        f"THINKING PHASE (1-2 min only - strategic, not a checklist):\n"
        f"- What is the MINIMAL CHANGE SET? (what must NOT be changed to keep the system stable?)\n"
        f"- What is the single blocking architectural decision that determines everything else?\n"
        f"Only then produce your implementation plan.\n\n"
        f"Rules:\n"
        f"- Use exactly as many steps as the task genuinely requires - one step per file/component change.\n"
        f"- There is NO fixed step count. A simple fix may need 2 steps, a complex feature may need 10+. Judge by scope.\n"
        f"- NO generic boilerplate.\n"
        f"- Each step touches exactly ONE file - never bundle files.\n"
        f"- Base STRICTLY on CONTRACT OVERVIEW files/classes.\n"
        f"- Order by dependency (interfaces first).\n"
        f"- Every listed task: file | touch | decision | risk\n\n"
        f"Place your numbered steps under ### Open Tasks.\n"
        f"List already-completed work under ### Completed Tasks.\n"
        + _STRUCT_OUTPUT
        )
    else:
        return (
        f"Produce a concrete, ordered codebase-aware implementation plan.\n"
        f"Codebase analysis is provided if available (CONTRACT OVERVIEW with complexity scores).\n\n"
        f"THINKING PHASE (strategic reasoning - NOT a risk checklist):\n"
        f"- MINIMAL CHANGE SET: What must stay untouched? Which boundaries must not move?\n"
        f"- DEPENDENCY ORDER: What is the strict A->B->C chain? What blocks everything else?\n"
        f"- SHARED CONTRACTS: Which interfaces/schemas must be stable before any implementation starts?\n"
        f"- BLOCKING DECISION: Name the ONE architectural decision with the largest downstream impact.\n"
        f"- VARIABLE/IMPORT MAP: Identify shared variables, types, imports that cross file boundaries. Map which\n"
        f"  symbols are defined where and consumed where - this prevents redundant creation across steps.\n"
        f"Do NOT enumerate generic risks. Think about structure, not symptoms.\n"
        f"Only then produce your implementation plan.\n\n"
        f"Rules:\n"
        f"- Use exactly as many steps as the task genuinely requires - one step per file/component change.\n"
        f"- There is NO fixed step count. A simple fix may need 2 steps, a complex feature may need 10+. Judge by scope.\n"
        f"- NO generic boilerplate (e.g. 'Setup project overview', 'Initialize dependencies', 'Test the implementation').\n"
        f"- Each step must touch exactly ONE file or ONE interface boundary - never bundle multiple files into one step.\n"
        f"- Base STRICTLY on existing files/classes shown in CONTRACT OVERVIEW.\n"
        f"- Order by dependency (interfaces before implementations, shared modules first).\n"
        f"- Every step must include concrete file, exact touchpoint, one decision, and one risk.\n"
        f"- When steps share types/imports: note the shared symbol and its origin file to prevent duplication.\n\n"
        f"Place your numbered steps under ### Open Tasks.\n"
        f"List already-completed work under ### Completed Tasks.\n"
        + _STRUCT_OUTPUT
        )


def make_planner_analysis_sys(model_name: str = "") -> str:


    model_size = estimate_model_size(model_name)
    is_small_model = model_size in ("2b", "4b")

    if is_small_model:
        return (
            "Produce a concise implementation briefing for the coding agent.\n"
            "Describe: key components, implementation order, shared interfaces.\n"
            "Free-form text — paragraphs, bullets, whatever is clearest.\n"
            + _STRUCT_OUTPUT
        )
    else:
        return (
            "You are an architecture analyst. Produce an IMPLEMENTATION BRIEFING.\n"
            "The coder will use this as context to implement the task directly.\n\n"
            "Describe:\n"
            "- COMPONENTS: Key parts of the system and how they connect\n"
            "- ORDER: What to build first and why (foundation before surface)\n"
            "- CONTRACTS: Interfaces, types, schemas that cross boundaries\n"
            "- DECISIONS: Key architectural choices and their rationale\n\n"
            "Format as free-form text under the required section headers below.\n"
            + _STRUCT_OUTPUT
        )


def make_planner_sys(max_steps: int) -> str:
    """System prompt for Non-Thinking planner."""
    return (
        f"Split this coding task into specific implementation steps.\n"
        f"Use as many steps as the task genuinely requires - there is NO fixed step count.\n"
        f"A simple fix may need 2 steps, a complex feature may need 10+.\n\n"
        f"Rules:\n"
        f"- Base STRICTLY on the provided codebase context. No generic boilerplate.\n"
        f"- One step per file/component - never bundle multiple files into one step.\n"
        f"- Order by dependency: interfaces/shared modules before consumers.\n"
        f"- Do NOT output separate prose reasoning; encode reasoning in decision/risk fields.\n"
        f"- Every step must include concrete file, exact touchpoint, one decision, and one risk.\n"
        f"- When steps share types/imports: note the shared symbol and its origin file to prevent duplication.\n\n"
        f"Place your numbered steps under ### Open Tasks.\n"
        f"List already-completed work under ### Completed Tasks.\n"
        + _STRUCT_OUTPUT
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  OUTPUT PARSING
# ═══════════════════════════════════════════════════════════════════════════════

def parse_arch_chunks(raw: str) -> list[dict]:


    chunks = []
    parts = re.split(r"(?im)^CHUNK\s+\d+\s*:\s*", raw)
    for part in parts[1:]:
        lines = part.strip().splitlines()
        if not lines:
            continue
        title = lines[0].strip()
        chunk: dict = {"title": title, "why": "", "goal": "", "files": [], "depends": "none", "risk": ""}
        current_key = None
        for line in lines[1:]:
            stripped = line.strip()
            if not stripped:
                continue
            upper = stripped.upper()
            if upper.startswith("WHY:"):
                chunk["why"] = stripped[4:].strip()
                current_key = "why"
            elif upper.startswith("GOAL:"):
                chunk["goal"] = stripped[5:].strip()
                current_key = "goal"
            elif upper.startswith("FILES:"):
                current_key = "files"
            elif upper.startswith("DEPENDS:"):
                chunk["depends"] = stripped[8:].strip()
                current_key = "depends"
            elif upper.startswith("RISK:"):
                chunk["risk"] = stripped[5:].strip()
                current_key = "risk"
            elif current_key == "files" and stripped.startswith("-"):
                file_line = stripped[1:].strip()
                m = re.match(r"^([^\s\u2014:]+(?:\.[\w]+)?)\s*(?:\u2014|-|:)\s*(.*)", file_line)
                if m:
                    chunk["files"].append({"path": m.group(1).strip(), "note": m.group(2).strip()})
                else:
                    chunk["files"].append({"path": file_line, "note": ""})
        if chunk["title"] and chunk["files"]:
            chunks.append(chunk)
    return chunks


def arch_chunks_to_subtasks(chunks: list[dict]) -> list[str]:
    """Converts arch chunks into subtask strings for the existing coder pipeline."""
    subtasks = []
    for i, c in enumerate(chunks, 1):
        files_str = ", ".join(f["path"] for f in c["files"])
        goal = c.get("goal") or c.get("title", "")
        why = c.get("why", "")
        risk = c.get("risk", "")
        task = f"[CHUNK {i}: {c['title']}]"
        if why:
            task += f" - {why}"
        task += f"\nGoal: {goal}"
        task += f"\nFiles: {files_str}"
        if risk:
            task += f"\nRisk: {risk}"
        subtasks.append(task)
    return subtasks


# ═══════════════════════════════════════════════════════════════════════════════
#  CONTEXT COMPACTION
# ═══════════════════════════════════════════════════════════════════════════════

_PLANNER_NOISE_LINE_RE = re.compile(
    r'^\s*(?:'
    r'time="[^"]+"\s+level=\w+'
    r'|#\d+\s+(?:\[[^\]]+\]|\d+\.\d+)'
    r'|npm\s+error\b'
    r'|Dockerfile:\d+'
    r'|------'
    r'|\[\+\]\s+up\s+\d+/\d+'
    r'|Starting\s+.*services'
    r'|Waiting\s+for\s+services\s+to\s+start'
    r')',
    re.IGNORECASE,
)


def compact_planner_text(raw: str, *, max_chars: int, max_lines: int) -> str:
    """Compress text for planner prompts: filter noise, limit length."""
    txt = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    if not txt.strip():
        return ""

    kept: list[str] = []
    dropped = 0
    for line in txt.split("\n"):
        if _PLANNER_NOISE_LINE_RE.search(line or ""):
            dropped += 1
            continue
        kept.append(line.rstrip())

    if max_lines > 0 and len(kept) > max_lines:
        head = max_lines // 2
        tail = max_lines - head
        kept = kept[:head] + ["... [planner context trimmed] ..."] + kept[-tail:]

    txt = "\n".join(kept)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()

    if len(txt) > max_chars:
        head = int(max_chars * 0.7)
        tail = max(120, max_chars - head - 40)
        txt = txt[:head].rstrip() + "\n... [planner context truncated] ...\n" + txt[-tail:].lstrip()

    if dropped:
        txt += f"\n\n[planner omitted {dropped} runtime log lines]"
    return txt


def slice_toml_ctx_inline(explore_ctx: str, max_chars: int) -> str:
    """
    TOML prioritization before planner context.
    - touched_by_task=true/likely -> preferred (full)
    - touched_by_task=false/unlikely -> stub
    - unknown -> keep
    """
    if not explore_ctx or len(explore_ctx) <= max_chars:
        return explore_ctx

    _block_re   = re.compile(r"```toml([\s\S]*?)```", re.IGNORECASE)
    _touched_re = re.compile(r"touched_by_task\s*=\s*(true|likely|\"likely\"|\"true\")", re.IGNORECASE)
    _unlikely_re = re.compile(r"touched_by_task\s*=\s*(false|unlikely|\"unlikely\"|\"false\")", re.IGNORECASE)
    _file_re    = re.compile(r'file\s*=\s*"?([^"\n]+)"?')

    likely_blocks:   list[str] = []
    unlikely_stubs:  list[str] = []
    non_toml_parts:  list[str] = []
    last_end = 0

    for m in _block_re.finditer(explore_ctx):
        pre = explore_ctx[last_end:m.start()].strip()
        if pre:
            non_toml_parts.append(pre)
        last_end = m.end()
        content = (m.group(1) or "").strip()
        if _touched_re.search(content):
            likely_blocks.append(m.group(0))
        elif _unlikely_re.search(content):
            fm = _file_re.search(content)
            if fm:
                unlikely_stubs.append(f"# unlikely: {fm.group(1).strip()}")
        else:
            likely_blocks.append(m.group(0))  # keep unknown

    tail = explore_ctx[last_end:].strip()
    if tail:
        non_toml_parts.append(tail)

    assembled = "\n\n".join(non_toml_parts[:1] + likely_blocks)
    if len(assembled) < max_chars - 60 and unlikely_stubs:
        stubs = "\n".join(unlikely_stubs)[: max_chars - len(assembled) - 60]
        assembled += f"\n\n[Unlikely partitions - not touched by task]:\n{stubs}"
    return assembled[:max_chars]


# ═══════════════════════════════════════════════════════════════════════════════
#  PLANNER USER PROMPT BUILDER
# ═══════════════════════════════════════════════════════════════════════════════

def build_planner_user_prompt(
    task: str,
    explore_ctx: str = "",
    *,
    max_task_chars: int = 2600,
    max_ctx_chars: int = 80000,
    websearch_available: bool = False,
    caps: Any | None = None,
) -> str:


    if caps is not None:
        max_task_chars = caps.task
    task_part = compact_planner_text(task, max_chars=max_task_chars, max_lines=150)
    explore_ctx_prioritized = slice_toml_ctx_inline(explore_ctx, max_chars=max_ctx_chars)
    ctx_part = compact_planner_text(explore_ctx_prioritized, max_chars=max_ctx_chars, max_lines=400)
    msg = f"Task:\n{task_part or '-'}"
    if ctx_part:
        msg += (
            f"\n\n[Codebase analysis summary]\n"
            f"Use the following insights from the Pre-Explore phase to formulate your plan. "
            f"DO NOT invent generic boilerplate steps. Map your plan DIRECTLY to these existing files and structures:\n"
            f"{ctx_part}"
        )
    if caps is not None and explore_ctx:
        _known = extract_known_files(explore_ctx, cap_chars=caps.known_files)
        if _known:
            _trunc = "" if len(_known) < 80 else f"\n(... more files - full list see repo-map)"
            msg += (
                f"\n\n[Known files - reference only these paths in your steps]\n"
                + "\n".join(f"  - {f}" for f in _known)
                + _trunc
            )
    if websearch_available:
        msg += (
            "\n\n[Web Search Available]\n"
            "You have access to web search via searchXng. PREFER LOOKUP OVER GUESSING: if a step "
            "involves an external API, library, framework, or anything you are not 100% certain about, "
            "mark it for web search. A wrong API assumption breaks the build; a search takes 2 seconds.\n"
            "Mark steps with: | websearch: [what to search for]\n"
            "Example: 3. file: src/api/client.py | touch: APIClient.connect | decision: use OAuth2 flow | websearch: latest OAuth2 RFC for device authorization | risk: API change breaking auth\n"
            "When in doubt: search."
        )
    return msg


# ═══════════════════════════════════════════════════════════════════════════════
#  OUTPUT NORMALIZATION & VALIDATION
# ═══════════════════════════════════════════════════════════════════════════════

def trim_query(raw: str, max_chars: int) -> str:
    """Trim a query string to max_chars on the last word boundary."""
    if len(raw) <= max_chars:
        return raw.strip()
    cut = raw[:max_chars]
    last_space = cut.rfind(" ")
    if last_space > max_chars // 2:
        cut = cut[:last_space]
    return cut.strip()


def normalize_planner_steps(raw_text: str, *, max_steps: int = 20, max_chars: int = 3000) -> str:
    """Normalisiert rohen Planner-Output in nummeriertes Step-Format."""
    raw = _RE_THINK_CLEANUP.sub("", str(raw_text or "")).strip()
    if not raw:
        return ""

    raw = re.sub(r"```[\s\S]*?```", " ", raw)
    bad_line_re = re.compile(
        r"(apply one or more search/replace|edit_file\(|write_file_append|always call read_file first|<<<<<<<|=======|>>>>>>>)",
        re.IGNORECASE,
    )

    items: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        s = line.strip()
        if not s or bad_line_re.search(s):
            continue
        s = re.sub(r"^\s*(?:\d+[.)]\s*|[-*]\s+)", "", s).strip()
        if len(s) < 8:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(s[:420])
        if len(items) >= max_steps:
            break

    if not items:
        return trim_query(re.sub(r"\s+", " ", raw), min(max_chars, 320))

    out = "\n".join(f"{i+1}. {s}" for i, s in enumerate(items))
    if len(out) > max_chars:
        out = trim_query(out, max_chars)
    return out


def fallback_planner_steps(goal: str, *, max_steps: int = 4) -> str:
    """Generiert generische 4-Schritt-Fallback-Plan wenn LLM-Planner fehlschlaegt."""
    short_goal = trim_query(re.sub(r"\s+", " ", str(goal or "").strip()), 180) or "the requested task"
    base = [
        f"Clarify scope and acceptance criteria for: {short_goal}",
        "Identify exact files/components to create or modify and in which order",
        "Implement smallest runnable increment first, then integrate remaining changes",
        "Run verification/build/tests and fix any reported errors before final response",
    ]
    steps = base[: max(2, min(max_steps, len(base)))]
    return "\n".join(f"{i+1}. {s}" for i, s in enumerate(steps))


def planner_field(step: str, key: str) -> str:
    m = re.search(rf"\b{re.escape(key)}\s*:\s*([^|\n]+)", str(step or ""), re.IGNORECASE)
    return m.group(1).strip() if m else ""


def dedupe_chunk_plan_steps(steps: list[str]) -> tuple[list[str], int]:
    """Dedupliziert Planner-Step-Strings, verwirft Eintraege <8 Zeichen."""
    cleaned: list[str] = []
    seen_text: set[str] = set()
    dropped_short = 0
    for raw in steps or []:
        s = re.sub(r"\s+", " ", str(raw or "").strip())
        if len(s) < 8:
            dropped_short += 1
            continue
        key = s.lower()
        if key in seen_text:
            continue
        seen_text.add(key)
        cleaned.append(s)
    return cleaned, dropped_short


def structured_chunk_plan_count(steps: list[str]) -> int:
    return sum(1 for s in steps if planner_field(s, "file") and planner_field(s, "touch"))


def chunk_step_target(step: str) -> tuple[str, str, str, str]:
    """Extrahiert (file, touch, decision, risk) aus einem Step."""
    return (
        planner_field(step, "file"),
        planner_field(step, "touch"),
        planner_field(step, "decision").lower(),
        planner_field(step, "risk").lower(),
    )


def chunk_step_has_forward_dependency(step: str, next_idx: int) -> bool:
    """Detects circular forward/self dependencies."""
    _deps = re.findall(r"depends\s+on\s+step\s+(\d+)", step, re.IGNORECASE)
    return any(int(d) >= next_idx for d in _deps)


def chunk_plan_is_repetitive(steps: list[str]) -> bool:
    """Detects whether >50% of the steps share the same prefix."""
    if len(steps) < 3:
        return False
    heads = [re.sub(r"\s+", " ", x.lower())[:42] for x in steps]
    return len(set(heads)) <= max(1, len(steps) // 2)


def validate_chunk_plan(steps: list[str], *, max_steps: int) -> tuple[list[str], dict]:
    """Sanitize planner chunk lists and reject repetitive/contradictory plans."""
    cleaned, dropped_short = dedupe_chunk_plan_steps(steps)
    _structured_count = structured_chunk_plan_count(cleaned)
    enforce_structure = (
        len(cleaned) > 0
        and _structured_count / len(cleaned) >= 0.8
    )

    out: list[str] = []
    seen_targets: dict[str, tuple[str, str]] = {}
    dropped_invalid = 0
    dropped_conflict = 0

    for s in cleaned:
        if enforce_structure:
            _file, _touch, _decision, _risk = chunk_step_target(s)
            if not _file or not _touch:
                dropped_invalid += 1
                continue
            target = f"{_file.lower()}|{_touch.lower()}"

            if target in seen_targets:
                if seen_targets[target] != (_decision, _risk):
                    dropped_conflict += 1
                    continue
                dropped_invalid += 1
                continue
            seen_targets[target] = (_decision, _risk)

            _next_idx = len(out) + 1
            if chunk_step_has_forward_dependency(s, _next_idx):
                dropped_conflict += 1
                continue

        out.append(s)
        if max_steps and max_steps > 0 and len(out) >= max_steps:
            break

    repetitive_reject = chunk_plan_is_repetitive(out)
    if repetitive_reject:
        out = []

    meta = {
        "enforce_structure": enforce_structure,
        "dropped_short": dropped_short,
        "dropped_invalid": dropped_invalid,
        "dropped_conflict": dropped_conflict,
        "repetitive_reject": repetitive_reject,
    }
    if not out:
        meta["rejected"] = True
        meta["reason"] = "repetitive_or_invalid_plan"
    else:
        meta["rejected"] = False
        meta["reason"] = ""
    return out, meta


# ═══════════════════════════════════════════════════════════════════════════════
#  FULL OUTPUT PARSING PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def parse_planner_output(raw: str, *, step_cap: int = 20) -> tuple[list[str], str, dict]:
    """
    Parse raw planner output into a subtask list.

    Tries in order:
      1. Arch-chunk parser (CHUNK N: header)
      2. Numbered-List-Parser
      3. STEP N Block-Parser
      4. JSON Array-Parser

    Returns: (subtasks, parse_mode, quality_meta)
    """
    _pr = _RE_THINK_CLEANUP.sub("", str(raw or "")).strip()
    if not _pr:
        return [], "empty", {"rejected": True, "reason": "empty_input"}

    _cap = step_cap if (step_cap and step_cap > 0) else None

    subtasks: list[str] = []
    parse_mode = "none"
    parsed: list[str] = []

    # 1. Arch-Chunk-Parser
    _arch_chunks = parse_arch_chunks(_pr)
    if _arch_chunks:
        subtasks = arch_chunks_to_subtasks(_arch_chunks)
        if _cap and len(subtasks) > _cap:
            subtasks = subtasks[:_cap]
        parse_mode = "arch_chunks"
        parsed = subtasks
    else:
        # 2. Numbered-List-Parser
        _num_items = re.findall(r"^\s*(?:\d+[.)]\s*|\*\*\d+[.)]\*\*\s*|[-*]\s+)(.+)", _pr, re.MULTILINE)
        _num_items = [s.strip() for s in _num_items if len(s.strip()) >= 8]
        if _num_items:
            parsed = _num_items
            parse_mode = "numbered"
        else:
            parsed = []
            _step_blocks = re.split(r"(?im)^\s*STEP\s+\d+\s*$", _pr)
            if len(_step_blocks) > 1:
                for _blk in _step_blocks[1:]:
                    _file = _touch = _decision = _risk = ""
                    for _ln in str(_blk or "").splitlines():
                        _ls = _ln.strip()
                        if not _ls:
                            continue
                        _ll = _ls.lower()
                        if _ll.startswith("file:") and not _file:
                            _file = _ls.split(":", 1)[1].strip()
                        elif _ll.startswith("touch:") and not _touch:
                            _touch = _ls.split(":", 1)[1].strip()
                        elif _ll.startswith("decision:") and not _decision:
                            _decision = _ls.split(":", 1)[1].strip()
                        elif _ll.startswith("risk:") and not _risk:
                            _risk = _ls.split(":", 1)[1].strip()
                    _parts = []
                    if _file:   _parts.append(f"file: {_file}")
                    if _touch:  _parts.append(f"touch: {_touch}")
                    if _decision: _parts.append(f"decision: {_decision}")
                    if _risk:   _parts.append(f"risk: {_risk}")
                    if _parts:
                        parsed.append(" | ".join(_parts))
                if parsed:
                    parse_mode = "step_blocks"
            if not parsed:
                # 4. JSON Array-Parser
                _pm = re.search(r"\[[\s\S]*?\]", _pr)
                if _pm:
                    try:
                        parsed = json.loads(_pm.group(0))
                        if parsed:
                            parse_mode = "json"
                    except json.JSONDecodeError:
                        _raw_items = re.findall(r'"([^"]+)"|([^\[\],\n]+)', _pm.group(0))
                        parsed = [a or b for a, b in _raw_items if (a or b).strip()]
                        if parsed:
                            parse_mode = "json_loose"

    # Filter empty/too-short subtasks
    subtasks = [str(s).strip() for s in parsed if len(str(s).strip()) >= 8]
    if _cap and len(subtasks) > _cap:
        subtasks = subtasks[:_cap]

    # Validate
    quality_meta: dict = {}
    if subtasks:
        subtasks, quality_meta = validate_chunk_plan(subtasks, max_steps=step_cap)

    return subtasks, parse_mode, quality_meta


# ═══════════════════════════════════════════════════════════════════════════════
#  RESULT DATACLASS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class PlannerResult:
    """Strukturiertes Ergebnis des Planner-Laufs."""
    subtasks:          list[str]     = field(default_factory=list)
    thinking:          str           = ""
    planner_model:     str           = ""
    used_thinking:     bool          = False
    used_fallback:     bool          = False
    fallback_reason:   str           = ""
    parse_mode:        str           = "none"
    context_trimmed:   bool          = False
    step_limit:        int           = 20
    plan_guard:        dict          = field(default_factory=dict)
    planner_id:        str           = ""
    plan_content:      str           = ""


# ═══════════════════════════════════════════════════════════════════════════════
#  LLM CALL HELPER (Streaming)
# ═══════════════════════════════════════════════════════════════════════════════

# THINKING-BUDGET-GUARD (2026-08-31): llama.cpp does not reliably enforce
# thinking_budget on hermes3.6/MTP (observed live: 600s thinking loop without
# plan content, then wall timeout). Here the streamed thinking is bounded
# itself (chars/3 estimate, consistent with frontend/server) and aborted early,
# so run_planner falls back to the NT planner.
_THINK_EST_HEADROOM = 1.4    # tolerance against chars/3 estimation error
_NT_THINK_CAP_EST   = 2500   # NT mode: no thinking expected — abort early on loop

async def _llm_stream(
    model: str,
    port: int,
    messages: list[dict],
    *,
    temp: float = 0.2,
    max_tokens: int = 2048,
    read_timeout: float = 600.0,
    emit_fn=None,
    aborted_fn=None,
    wall_timeout: float = 600.0,
    heartbeat_fn=None,
    thinking_budget: int = 0,
    use_thinking: bool = False,
    ctx_limit: int = 0,
) -> tuple[str, str, bool]:


    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": temp,
        "max_tokens": max_tokens,
    }
    _apply_thinking_fields(payload, use_thinking, thinking_budget)

    # DEBUG-THINKING: Log actual payload sent to llama.cpp
    logger.info("[PLANNER-LLM] POST port=%d model=%s thinking_budget=%d payload.thinking=%s payload.thinking_budget=%s max_tokens=%d",
                port, model, thinking_budget,
                payload.get("thinking"), payload.get("thinking_budget"), payload.get("max_tokens"))

    thinking_chunks: list[str] = []
    content_chunks: list[str] = []
    in_think = False
    hit_timeout = False

    # message chars (~chars/3), completion chunks accumulated live, emit
    _pm_chars = sum(len(str(m.get("content") or "")) for m in messages if isinstance(m, dict))
    _gen_chars = 0
    _think_chars = 0
    _last_ctx_emit = 0.0
    _ctx_limit_eff = int(ctx_limit or 0)

    # Thinking-Budget-Guard: statische Schwelle pro Aufruf.
    if use_thinking and thinking_budget > 0:
        _think_cap_est = max(64, int(thinking_budget * _THINK_EST_HEADROOM))
    elif not use_thinking:
        _think_cap_est = _NT_THINK_CAP_EST
    else:
        _think_cap_est = 0

    async def _emit_ctx_meter_if_due() -> None:
        nonlocal _last_ctx_emit
        if not emit_fn or _ctx_limit_eff <= 0:
            return
        now = time.monotonic()
        if now - _last_ctx_emit < 1.0:
            return
        _last_ctx_emit = now
        est = int(_pm_chars / 3 + _gen_chars / 3)
        try:
            await emit_fn({"type": "ctx_meter", "est_tokens": est,
                           "ctx_limit": _ctx_limit_eff, "compressing": False})
        except Exception:
            pass

    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        timeout=httpx.Timeout(connect=10.0, read=read_timeout, write=10.0, pool=5.0),
    ) as client:
        line_q: asyncio.Queue = asyncio.Queue()

        async def _feed_lines(resp):
            try:
                async for line in resp.aiter_lines():
                    await line_q.put(line)
            finally:
                await line_q.put(None)  # EOF sentinel

        _p_gen_t0 = time.monotonic()
        _p_usage_final = None
        async with client.stream(
            "POST",
            f"http://127.0.0.1:{port}/v1/chat/completions",
            json=payload,
        ) as resp:
            if resp.status_code != 200:
                logger.warning("Planner LLM HTTP %d port=%d", resp.status_code, port)
                return "", "", False

            feed_task = asyncio.create_task(_feed_lines(resp))
            wall_start = time.time()
            try:
                while True:
                    try:
                        line = await asyncio.wait_for(line_q.get(), timeout=0.1)
                    except asyncio.TimeoutError:
                        # No line yet — drain heartbeat
                        if heartbeat_fn:
                            try:
                                await heartbeat_fn()
                            except Exception:
                                pass
                        if aborted_fn and aborted_fn():
                            break
                        if time.time() - wall_start > wall_timeout:
                            logger.warning("Planner LLM wall-timeout (%.0fs)", wall_timeout)
                            hit_timeout = True
                            break
                        continue

                    # Drain heartbeat after each line
                    if heartbeat_fn:
                        try:
                            await heartbeat_fn()
                        except Exception:
                            pass

                    if line is None:
                        break
                    if aborted_fn and aborted_fn():
                        break
                    if time.time() - wall_start > wall_timeout:
                        logger.warning("Planner LLM wall-timeout (%.0fs)", wall_timeout)
                        hit_timeout = True
                        break
                    if not line.startswith("data:"):
                        continue
                    raw_data = line[5:].strip()
                    if raw_data == "[DONE]":
                        break
                    try:
                        chunk_data = json.loads(raw_data)
                    except Exception:
                        continue
                    _p_usage = chunk_data.get("usage")
                    if _p_usage and _p_usage.get("completion_tokens"):
                        try:
                            _p_cached = int((_p_usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
                        except Exception:
                            _p_cached = 0
                        # TOKEN-DISPLAY-FIX (2026-08-31): llama.cpp reports the pure
                        # thinking tokens under completion_tokens_details.reasoning_tokens.
                        # Passed to the frontend via usage_meta so the thinking block
                        # shows the REAL number instead of the chars/4 estimate.
                        try:
                            _p_reasoning = int(((_p_usage.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)
                        except Exception:
                            _p_reasoning = 0
                        _p_usage_final = {
                            "completion_tokens": int(_p_usage["completion_tokens"]),
                            "prompt_tokens": int(_p_usage.get("prompt_tokens") or 0),
                            "cached_tokens": _p_cached,
                            "reasoning_tokens": _p_reasoning,
                            "gen_ms": int((time.monotonic() - _p_gen_t0) * 1000),
                        }
                    delta = (chunk_data.get("choices") or [{}])[0].get("delta", {})

                    # Field normalization (llama.cpp vs Ollama vs vLLM)
                    think_tok = (
                        delta.get("thinking")
                        or delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or ""
                    )
                    cont_tok = delta.get("content") or ""

                    if not think_tok and not cont_tok:
                        continue

                    # PERF-LIVE-FIX: accumulate chunk chars + throttled ctx_meter
                    _gen_chars += len(think_tok) + len(cont_tok)
                    await _emit_ctx_meter_if_due()

                    if think_tok:
                        thinking_chunks.append(think_tok)
                        _think_chars += len(think_tok)
                        if emit_fn:
                            try:
                                await emit_fn({"type": "planner_thinking_token", "content": think_tok})
                            except Exception:
                                pass

                    if cont_tok:
                        # Fallback: <think/>-tag detection
                        if not in_think:
                            if "<think" in cont_tok:
                                in_think = True
                            elif cont_tok.rstrip().endswith("<") or cont_tok.rstrip().endswith("<t") or cont_tok.rstrip().endswith("<th"):
                                thinking_chunks.append(cont_tok)
                                _think_chars += len(cont_tok)
                                continue
                        if in_think:
                            thinking_chunks.append(cont_tok)
                            _think_chars += len(cont_tok)
                            if emit_fn:
                                try:
                                    await emit_fn({"type": "planner_thinking_token", "content": cont_tok})
                                except Exception:
                                    pass
                            if "</think" in cont_tok:
                                in_think = False
                        else:
                            content_chunks.append(cont_tok)
                            if emit_fn:
                                try:
                                    await emit_fn({"type": "planner_plan_token", "content": cont_tok})
                                except Exception:
                                    pass

                    # THINKING-BUDGET-GUARD (2026-08-31): while NO plan content has
                    # started yet and the streamed thinking clearly exceeds the budget,
                    # abort → run_planner falls back to the NT planner. Prevents the
                    # observed "600s thinking loop without plan".
                    if (
                        _think_cap_est > 0
                        and not content_chunks
                        and (_think_chars // 3) > _think_cap_est
                    ):
                        logger.warning(
                            "[Planner] Thinking-Budget-Guard: est=%d tokens (chars=%d) > cap=%d, no content — fallback to NT planner",
                            _think_chars // 3, _think_chars, _think_cap_est,
                        )
                        hit_timeout = True
                        break
            finally:
                try:
                    feed_task.cancel()
                    await feed_task
                except asyncio.CancelledError:
                    pass

            if _p_usage_final and emit_fn:
                try:
                    await emit_fn({"type": "usage_meta", "phase": "planner", **_p_usage_final})
                except Exception:
                    pass

    thinking_text = "".join(thinking_chunks).strip()
    thinking_text = re.sub(r"</?thinking?>", "", thinking_text, flags=re.IGNORECASE).strip()
    content_text = re.sub(r"<thinking?>[\s\S]*?(?:</thinking?>|$)", "", "".join(content_chunks), flags=re.DOTALL | re.IGNORECASE).strip()

    return thinking_text, content_text, hit_timeout


# ═══════════════════════════════════════════════════════════════════════════════
#  CONNECT-RECOVERY (2026-08-31): heal phantom/stale ports yourself
# ═══════════════════════════════════════════════════════════════════════════════
# Observed live: planner POST to planner_port raised httpx.ConnectError even
# though the slot manager reported the model as "loaded" (port was dead). The
# agentic tool loop had evict+ensure_loaded+retry for this; the planner had
# NOTHING — a single dead port killed the whole planner phase. Here we add the
# same resilience: ConnectError -> evict + reload (fresh port) -> retry.

async def _planner_refresh_port(model: str, port: int, num_ctx: int) -> int:
    """Evict + reload the planner model after a dead port. Returns a fresh port."""
    try:
        from backend.llama_server_manager import manager as _lsm_p
        try:
            await _lsm_p.evict(model)
        except Exception:
            pass
        _new_port = await _lsm_p.ensure_loaded(model, num_ctx=num_ctx or 0, n_parallel=1)
        return int(_new_port or port)
    except Exception:
        return port


async def _llm_stream_retry(
    model: str,
    port: int,
    messages: list[dict],
    *,
    max_retries: int = 2,
    num_ctx: int = 0,
    aborted_fn=None,
    **kwargs,
) -> tuple[str, str, bool]:
    """_llm_stream with ConnectError recovery.

    Only ConnectError is retried (server unreachable) — no mid-stream retry
    (ReadError/ReadTimeout), to avoid token duplicates. On each retry the port
    is refreshed via evict+ensure_loaded.
    """
    for _attempt in range(max_retries + 1):
        if aborted_fn and aborted_fn():
            return "", "", False
        try:
            return await _llm_stream(
                model, port, messages, aborted_fn=aborted_fn, **kwargs
            )
        except httpx.ConnectError:
            if _attempt >= max_retries:
                logger.warning(
                    "[Planner] ConnectError after %d retries (%s) — port %d stays dead.",
                    _attempt, model, port,
                )
                raise
            logger.warning(
                "[Planner] ConnectError on port %d — evict+reload (%s), retry %d/%d",
                port, model, _attempt + 1, max_retries,
            )
            port = await _planner_refresh_port(model, port, num_ctx)
    return "", "", False


# ═══════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_planner_max_tokens(settings: dict | None, planner_ctx: int) -> int:


    _s = settings or {}
    try:
        _cfg = int(_s.get("duo_planner_max_tokens", 0) or 0)
    except (TypeError, ValueError):
        _cfg = 0
    _ctx = max(1, int(planner_ctx or 0))
    if _cfg > 0:
        return min(_cfg, _ctx)
    return _ctx


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY: run_planner()
# ═══════════════════════════════════════════════════════════════════════════════

async def run_planner(
    *,
    task:                 str,
    explore_ctx:          str,
    planner_model:        str,
    planner_port:         int,
    step_cap:             int = 20,
    use_thinking:         bool = True,
    chunking:             bool = True,
    thinking_budget:      int = 0,
    max_output_tokens:    int = 2048,
    planner_ctx:          int = 8192,
    websearch_available:  bool = False,
    settings:             dict | None = None,
    aborted_fn:           Callable[[], bool] | None = None,
    emit_fn:              Callable[[dict], Awaitable[None]] | None = None,
    heartbeat_fn:         Callable[[], Awaitable[None]] | None = None,
) -> PlannerResult:


    settings = settings or {}
    result = PlannerResult(
        planner_model=planner_model,
        used_thinking=use_thinking,
        step_limit=step_cap,
        planner_id=str(uuid.uuid4()),
    )

    _planner_max_tokens = _resolve_planner_max_tokens(settings, planner_ctx)

    if aborted_fn and aborted_fn():
        return result

    # ── 1. Prompt-Generierung ────────────────────────────────────────────────
    if not chunking:
        _planner_sys = make_planner_analysis_sys(model_name=planner_model)
    elif use_thinking:
        _planner_sys = make_thinking_planner_sys(step_cap, model_name=planner_model)
    else:
        _planner_sys = make_planner_sys(step_cap)

    _planner_user = build_planner_user_prompt(
        task,
        explore_ctx,
        max_task_chars=2600,
        max_ctx_chars=80000,
        websearch_available=websearch_available,
        caps=compute_char_caps(planner_ctx, overrides=(settings or {}).get("duo_caps")),
    )
    result.context_trimmed = (
        "[planner context truncated]" in _planner_user
        or "[planner context trimmed]" in _planner_user
        or "[planner omitted" in _planner_user
    )

    # ── 3. LLM-Aufruf ────────────────────────────────────────────────────────
    _plan_thinking = ""
    _plan_content = ""
    _used_fallback = False
    _fallback_reason = ""

    # chat_template_kwargs.enable_thinking im Payload (_apply_thinking_fields unten).
    _sys_prompt = _planner_sys
    if use_thinking:
        if not planner_model.startswith("qwen3.6"):
            if "/think" not in _sys_prompt:
                _sys_prompt = "/think\n" + _sys_prompt
    else:
        if not planner_model.startswith("qwen3.6"):
            if "/no_think" not in _sys_prompt:
                _sys_prompt = _sys_prompt.rstrip() + "\n/no_think"

    _messages = [
        {"role": "system", "content": _sys_prompt},
        {"role": "user",   "content": _planner_user},
    ]

    _wall_timeout = float(settings.get("duo_planner_thinking_timeout_s", 600.0))

    if use_thinking:
        # ── Thinking Planner (Streaming) ──────────────────────────────────────
        try:
            try:
                _budget_setting = int(settings.get("duo_planner_thinking_budget", 0) or 0)
            except (TypeError, ValueError):
                _budget_setting = 0
            _eff_think_budget = thinking_budget if thinking_budget > 0 else max(0, _budget_setting)
            _plan_thinking, _plan_content, _hit_timeout = await _llm_stream_retry(
                planner_model,
                planner_port,
                _messages,
                temp=0.2,
                # OUTPUT-LIMIT-POLICY (0.99.2): max_tokens = min(output+budget,
                # min(setting, planner_ctx). Wall-Timeout schuetzt zeitlich.
                max_tokens=min(max_output_tokens + _eff_think_budget, _planner_max_tokens),
                read_timeout=_wall_timeout,
                emit_fn=emit_fn,
                aborted_fn=aborted_fn,
                wall_timeout=_wall_timeout,
                heartbeat_fn=heartbeat_fn,
                thinking_budget=_eff_think_budget,
                use_thinking=True,
                ctx_limit=planner_ctx,
                num_ctx=planner_ctx,
            )
            if _hit_timeout:
                _used_fallback = True
                _fallback_reason = "thinking_planner_wall_timeout"
                if emit_fn:
                    try:
                        await emit_fn({"type": "status",
                            "content": "Thinking planner aborted (timeout/budget limit) - falling back to NT planner"})
                    except Exception:
                        pass
        except Exception as e:
            logger.error("[Planner] Thinking LLM error: %s", e, exc_info=True)
            _used_fallback = True
            _fallback_reason = f"thinking_llm_error: {type(e).__name__}"

        # Empty output -> retry with Non-Thinking prompt.
        if not _plan_content:
            if emit_fn:
                try:
                    await emit_fn({"type": "status",
                        "content": "Thinking planner: empty output - retry with non-thinking prompt"})
                except Exception:
                    pass
            _nt_sys = make_planner_sys(step_cap)
            if "/no_think" not in _nt_sys:
                _nt_sys = _nt_sys.rstrip() + "\n/no_think"
            _nt_messages = [
                {"role": "system", "content": _nt_sys},
                {"role": "user",   "content": _planner_user},
            ]
            _nt_timeout = float(settings.get("duo_soft_planner_wall_timeout_s", 180))
            try:
                _plan_thinking2, _plan_content2, _ = await _llm_stream_retry(
                    planner_model,
                    planner_port,
                    _nt_messages,
                    temp=0.1,
                    # OUTPUT-LIMIT-POLICY (0.99.2): max_tokens = _planner_max_tokens
                    # (enthaelt min(setting, planner_ctx)).
                    max_tokens=_planner_max_tokens,
                    read_timeout=180.0,
                    emit_fn=emit_fn,
                    aborted_fn=aborted_fn,
                    wall_timeout=_nt_timeout,
                    ctx_limit=planner_ctx,
                    num_ctx=planner_ctx,
                )
                if _plan_content2:
                    _plan_content = _plan_content2
                else:
                    _used_fallback = True
                    _fallback_reason = "nt_retry_empty"
            except Exception as e:
                logger.warning("[Planner] NT Retry error: %s (%s)", e or "(no message)", type(e).__name__, exc_info=True)
                _used_fallback = True
                _fallback_reason = f"nt_retry_error: {type(e).__name__}"

        # Last resort: use thinking as plan content
        if not _plan_content and _plan_thinking:
            _plan_thinking_plan = _plan_thinking
            if len(_plan_thinking_plan) > 6000:
                _plan_thinking_plan = _plan_thinking_plan[:6000] + "\n... [plan truncated from thinking content]"
            _plan_content = _plan_thinking_plan
            logger.warning(
                "[Planner] Empty content - using think content as plan (%d chars, fallback=%s)",
                len(_plan_content), _fallback_reason or "keiner",
            )

    else:
        # ── Non-Thinking Planner (Streaming) ──────────────────────────────────
        _nt_timeout = float(settings.get("duo_soft_planner_wall_timeout_s", 300))
        try:
            _plan_thinking, _plan_content, _ = await _llm_stream_retry(
                planner_model,
                planner_port,
                _messages,
                temp=0.1,
                # OUTPUT-LIMIT-POLICY (0.99.2): max_tokens = _planner_max_tokens
                # (enthaelt min(setting, planner_ctx)).
                max_tokens=_planner_max_tokens,
                read_timeout=_nt_timeout,
                emit_fn=emit_fn,
                aborted_fn=aborted_fn,
                wall_timeout=_nt_timeout,
                heartbeat_fn=heartbeat_fn,
                ctx_limit=planner_ctx,
                num_ctx=planner_ctx,
            )
        except Exception as e:
            logger.error("[Planner] NT LLM error: %s", e, exc_info=True)
            _used_fallback = True
            _fallback_reason = f"nt_llm_error: {type(e).__name__}"

    # ── 4. Output-Parsing & Validation ───────────────────────────────────────
    _raw_output = _plan_content or ""
    if not _raw_output:
        _used_fallback = True
        _fallback_reason = _fallback_reason or "empty_plan_output"

    if _raw_output:
        subtasks, parse_mode, quality_meta = parse_planner_output(_raw_output, step_cap=step_cap)
        result.subtasks = subtasks
        result.parse_mode = parse_mode
        result.plan_guard = quality_meta

        if quality_meta.get("repetitive_reject"):
            if emit_fn:
                try:
                    await emit_fn({"type": "status",
                        "content": "Planner plan rejected (repetitive chunks). Use a normal coder run."})
                except Exception:
                    pass
        elif (quality_meta.get("dropped_invalid", 0) + quality_meta.get("dropped_conflict", 0)) > 0:
            if emit_fn:
                try:
                    await emit_fn({"type": "status",
                        "content": "Planner plan cleaned: conflicting/invalid chunks removed."})
                except Exception:
                    pass

        if not subtasks and not _used_fallback:
            _used_fallback = True
            _fallback_reason = str(quality_meta.get("reason") or "empty_or_unstructured_plan")

    result.thinking = _plan_thinking
    result.used_fallback = _used_fallback
    result.fallback_reason = _fallback_reason
    result.plan_content = _raw_output

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  IN-LOOP PLANNER: run_inloop_planner()
# ═══════════════════════════════════════════════════════════════════════════════

async def run_inloop_planner(
    *,
    task:                 str,
    explore_ctx:          str,
    planner_model:        str,
    planner_port:         int,
    step_cap:             int = 20,
    use_thinking:         bool = True,
    thinking_budget:      int = 0,
    max_output_tokens:    int = 2048,
    planner_ctx:          int = 8192,
    websearch_available:  bool = False,
    settings:             dict | None = None,
    aborted_fn:           Callable[[], bool] | None = None,
    emit_fn:              Callable[[dict], Awaitable[None]] | None = None,
) -> tuple[str, str, bool]:
    """
    In-loop planner for the agentic tool-call cycle.

    Non-streaming LLM call with polling heartbeats.

    Returns: (plan_text, plan_thinking, used_fallback)
    """
    _inloop_max_tokens = _resolve_planner_max_tokens(settings, planner_ctx)
    settings = settings or {}

    _plan_user_msg = build_planner_user_prompt(
        task,
        explore_ctx,
        max_task_chars=2200,
        max_ctx_chars=80000,
        websearch_available=websearch_available,
        caps=compute_char_caps(planner_ctx, overrides=settings.get("duo_caps")),
    )

    _inloop_plan_sys = make_thinking_planner_sys(step_cap, model_name=planner_model)
    if use_thinking:
        if not planner_model.startswith("qwen3.6"):
            if "/think" not in _inloop_plan_sys:
                _inloop_plan_sys = "/think\n" + _inloop_plan_sys
    else:
        if not planner_model.startswith("qwen3.6"):
            if "/no_think" not in _inloop_plan_sys:
                _inloop_plan_sys = _inloop_plan_sys.rstrip() + "\n/no_think"

    try:
        _inloop_budget_setting = int(settings.get("duo_planner_thinking_budget", 0) or 0)
    except (TypeError, ValueError):
        _inloop_budget_setting = 0
    _inloop_eff_budget = thinking_budget if thinking_budget > 0 else max(0, _inloop_budget_setting)
    _plan_payload = {
        "model":    planner_model,
        "messages": [
            {"role": "system", "content": _inloop_plan_sys},
            {"role": "user",   "content": _plan_user_msg},
        ],
        "stream":          False,
        "temperature":     0.3,
        "max_tokens":      min(max_output_tokens + _inloop_eff_budget, _inloop_max_tokens),
    }
    _apply_thinking_fields(_plan_payload, use_thinking, _inloop_eff_budget)

    _inloop_read_timeout = 600.0
    _plan_resp = None
    _plan_err_last = None

    for _plan_attempt in range(2):
        try:
            async with httpx.AsyncClient(
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
                timeout=httpx.Timeout(connect=10.0, read=_inloop_read_timeout, write=10.0, pool=5.0),
            ) as _plan_client:
                _inloop_post_task = asyncio.ensure_future(
                    _plan_client.post(
                        f"http://127.0.0.1:{planner_port}/v1/chat/completions",
                        json=_plan_payload,
                        timeout=httpx.Timeout(connect=10.0, read=_inloop_read_timeout, write=10.0, pool=5.0),
                    )
                )
                _inloop_hb_elapsed = 0
                while not _inloop_post_task.done():
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(_inloop_post_task), timeout=1.0
                        )
                    except asyncio.TimeoutError:
                        _inloop_hb_elapsed += 1
                        if emit_fn:
                            try:
                                await emit_fn({
                                    "type": "status",
                                    "content": f"Planning in progress... {_inloop_hb_elapsed}s",
                                })
                            except Exception:
                                pass
                _plan_resp = _inloop_post_task.result()
            _plan_err_last = None
            break
        except Exception as _plan_post_err:
            _plan_err_last = _plan_post_err
            if _plan_attempt == 0:
                if emit_fn:
                    try:
                        _pe_name = type(_plan_post_err).__name__
                        _pe_msg = (str(_plan_post_err).strip() or repr(_plan_post_err))[:120]
                        await emit_fn({
                            "type": "status",
                            "content": f"Thinking-plan retry after {_pe_name}" + (f": {_pe_msg}" if _pe_msg else ""),
                        })
                    except Exception:
                        pass
                await asyncio.sleep(1.5)
                continue
            raise

    if _plan_resp is None and _plan_err_last is not None:
        raise _plan_err_last

    if _plan_resp.status_code == 200:
        _plan_data = _plan_resp.json()
        _plan_msg  = (_plan_data.get("choices") or [{}])[0].get("message", {})
        _plan_raw = _plan_msg.get("content") or ""
        _think_m = re.search(r"<think\>([\s\S]*?)\</think\>", _plan_raw, re.IGNORECASE)
        _plan_thinking = _think_m.group(1).strip() if _think_m else ""
        _plan_text = re.sub(r"<think\>[\s\S]*?\</think\>", "", _plan_raw, flags=re.IGNORECASE).strip()

        # Distilled <think/> Fallback
        if not _plan_text and _plan_thinking:
            _plan_text = _plan_thinking
            logger.info("Distilled <think/>-Fallback activated | %d chars from thinking block", len(_plan_thinking))

        if not _plan_text:
            _plan_text = fallback_planner_steps(task, max_steps=max(3, min(6, step_cap)))
            logger.warning("Planner exhausted - using LLM-generated fallback steps")
            return _plan_text, _plan_thinking, True

        return _plan_text, _plan_thinking, False
    else:
        logger.warning("Thinking-Planning-Step HTTP %d", _plan_resp.status_code)
        _fb = fallback_planner_steps(task, max_steps=max(3, min(6, step_cap)))
        return _fb, "", True

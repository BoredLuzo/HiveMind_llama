"""
ctx_utils.py — Budget-Adaptive Context Enrichment
===================================================
Distills explore TOML + plan + code snippets into stage-appropriate
context. Scales from 4K to 70K+ context automatically.

Stages:
  explore_to_planner_ctx() — TOML → structured architecture for planner
  planner_to_coder_ctx()   — plan + contracts + snippets → coder briefing
  extract_code_snippets()  — file content → reusable signatures/patterns

Legacy:
  slice_toml_ctx() — still works, now uses priority-ordered distillation
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Budget ──────────────────────────────────────────────────────────────────

@dataclass
class ContextBudget:
    """Char budgets per section. Scales with available context."""
    plan: int = 2000
    contracts: int = 1500
    snippets: int = 3000
    index: int = 800
    static_map: int = 6000
    max_snippet_files: int = 4

    @property
    def total(self) -> int:
        return self.plan + self.contracts + self.snippets + self.index + self.static_map

    def with_static_map_override(self, override_chars: Optional[int]) -> "ContextBudget":

        if override_chars and int(override_chars) > 0:
            self.static_map = int(override_chars)
        return self

    @staticmethod
    def from_content_tokens(content_tokens: int) -> ContextBudget:
        """
        Derive char budgets from available content tokens.
        content_tokens = model_ctx - system_prompt - tool_defs - history_reserve
        Approx: 1 token ≈ 3.5 chars.
        """
        chars = int(content_tokens * 3.5)

        if chars < 4000:        # tight: ~1K tokens (4B, 8K ctx, heavy quant)
            return ContextBudget(
                plan=600, contracts=400, snippets=0,
                index=300, static_map=800, max_snippet_files=0,
            )
        if chars < 14000:
            return ContextBudget(
                plan=1500, contracts=1200, snippets=2000,
                index=600, static_map=3000, max_snippet_files=3,
            )
        if chars < 35000:       # comfortable: ~10K tokens
            return ContextBudget(
                plan=3000, contracts=2500, snippets=5000,
                index=1000, static_map=6000, max_snippet_files=6,
            )
        # rich: 10K+ tokens (70K ctx models)
        return ContextBudget(
            plan=5000, contracts=20000, snippets=40000,
            index=1500, static_map=8000, max_snippet_files=10,
        )


# ═══════════════════════════════════════════════════════════════════════════
#  CharCaps — dynamische Injektions-Budgets
# ═══════════════════════════════════════════════════════════════════════════
#

@dataclass(frozen=True)
class CharCaps:
    """Char-Budgets pro Injektions-Section, abgeleitet aus ctx_tokens."""
    task: int = 2600
    plan: int = 3000
    session_user: int = 600
    session_assistant: int = 1200
    session_budget: int = 8000
    explore_inject: int = 12000
    known_files: int = 2000
    goal_pin: int = 800


_CHARS_PER_TOKEN = 3.5
_CTX_UTIL_RATIO = 0.7

# (section, prozent, floor, ceiling)
_CAP_DEFS: tuple[tuple[str, float, int, int], ...] = (
    ("task",             0.030,  800, 8000),
    ("plan",             0.040, 1200, 8000),
    ("session_user",     0.015,  600, 4000),
    ("session_assistant",0.030, 1200, 12000),
    ("session_budget",   0.150, 4000, 32000),
    ("explore_inject",   0.140, 8000, 24000),
    ("known_files",      0.020, 1200, 4000),
    ("goal_pin",         0.015,  800, 2400),
)


def compute_char_caps(
    ctx_tokens: int,
    overrides: Optional[dict] = None,
) -> CharCaps:


    chars = int(max(1024, int(ctx_tokens or 0)) * _CHARS_PER_TOKEN * _CTX_UTIL_RATIO)
    vals: dict[str, int] = {}
    for section, frac, floor, ceiling in _CAP_DEFS:
        vals[section] = max(floor, min(ceiling, int(chars * frac)))
    for key, val in (overrides or {}).items():
        if key in vals:
            try:
                vals[key] = max(256, int(val))
            except (TypeError, ValueError):
                pass
    return CharCaps(**vals)


# ── Session budget: greedy, newest messages first ─────────────────────────

def budget_session_msgs(
    msgs: list[dict],
    budget_chars: int,
    user_cap: int = 2000,
    assistant_cap: int = 8000,
    max_msgs: int = 10,
) -> list[dict]:


    out: list[dict] = []
    used = 0
    for m in reversed(msgs or []):
        role = m.get("role") if m.get("role") in ("user", "assistant") else "user"
        cap = assistant_cap if role == "assistant" else user_cap
        content = str(m.get("content") or "")[:cap]
        cost = len(content)
        if cost > budget_chars - used:
            break
        out.append({"role": role, "content": content})
        used += cost
        if len(out) >= max_msgs:
            break
    out.reverse()
    return out


_KNOWN_FILE_LINE_RE = re.compile(
    r"(?m)^\s*([A-Za-z0-9_.\-]+(?:[\\/][A-Za-z0-9_.\-]+)*(?:\.[A-Za-z0-9]+)?)\s*$"
)
_KNOWN_FILE_QUOTED_RE = re.compile(r'file\s*=\s*"?([^"\n]+)"?')


def extract_known_files(
    explore_ctx: str,
    cap_chars: int = 2000,
    subtask: str = "",
    max_files: int = 80,
) -> list[str]:


    if not explore_ctx:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def _add(p: str):
        p = p.strip().strip('"')
        if not p or p in seen or p.startswith("..."):
            return
        if "." not in p and "/" not in p and "\\" not in p:
            return
        seen.add(p)
        found.append(p)

    for m in _KNOWN_FILE_LINE_RE.finditer(explore_ctx):
        _add(m.group(1))
    for m in _KNOWN_FILE_QUOTED_RE.finditer(explore_ctx):
        _add(m.group(1))

    if not found:
        return []

    subtask_low = (subtask or "").lower()
    subtask_files = [f for f in found if f.lower() in subtask_low]
    rest = [f for f in found if f not in subtask_files]

    out: list[str] = []
    used = 0
    for f in subtask_files + rest:
        cost = len(f) + 2
        if used + cost > cap_chars:
            break
        out.append(f)
        used += cost
        if len(out) >= max_files:
            break
    return out


# ── Partition Parsing ───────────────────────────────────────────────────────

@dataclass
class PartitionInfo:
    name: str
    role: str = ""
    exports: list[str] = field(default_factory=list)
    imports_internal: list[str] = field(default_factory=list)
    imports_external: list[str] = field(default_factory=list)
    data_flow: str = ""
    touched_by_task: str = "unknown"   # "likely" | "unknown" | "unlikely"
    key_files: list[str] = field(default_factory=list)
    complexity_score: float = 0.5
    hint: str = ""
    _fallback: bool = False


def parse_toml_partitions(toml_str: str) -> list[PartitionInfo]:
    """Parse [partition.X] and [contract] blocks from explore TOML."""
    partitions: list[PartitionInfo] = []

    blocks = re.split(r"(?=\[partition\.)", toml_str)
    blocks = [b.strip() for b in blocks if b.strip() and "[partition" in b]

    if not blocks:
        blocks = re.split(r"(?=\[contract\])", toml_str)
        blocks = [b.strip() for b in blocks if b.strip() and "[contract" in b]

    for block in blocks:
        m = re.search(r'\[partition\.(\w+)\]', block)
        name = m.group(1) if m else ""
        if not name:
            m = re.search(r'partition\s*=\s*"([^"]+)"', block)
            name = m.group(1) if m else "unknown"

        def _field(key: str, default: str = "") -> str:
            match = re.search(rf'{key}\s*=\s*"([^"]*)"', block)
            if match:
                return match.group(1)
            match = re.search(rf'{key}\s*=\s*(\S+)', block)
            return match.group(1).strip('"').strip("'") if match else default

        def _list_field(key: str) -> list[str]:
            match = re.search(rf'{key}\s*=\s*\[([^\]]*)\]', block)
            if not match:
                return []
            return [
                s.strip().strip('"').strip("'")
                for s in match.group(1).split(",")
                if s.strip()
            ]

        touched = _field("touched_by_task", "unknown").lower()
        if touched in ("true", "yes", "1"):
            touched = "likely"
        elif touched in ("false", "no", "0"):
            touched = "unlikely"

        try:
            complexity = float(_field("complexity_score", "0.5"))
        except ValueError:
            complexity = 0.5

        partitions.append(PartitionInfo(
            name=name,
            role=_field("role"),
            exports=_list_field("exports"),
            imports_internal=_list_field("imports_internal"),
            imports_external=_list_field("imports_external"),
            data_flow=_field("data_flow"),
            touched_by_task=touched,
            key_files=_list_field("key_files") or _list_field("files_read"),
            complexity_score=complexity,
            hint=_field("hint"),
            _fallback=_field("_fallback").lower() in ("true", "1", "yes"),
        ))

    return partitions


# ── Code Snippet Extraction ─────────────────────────────────────────────────

@dataclass
class CodeSnippet:
    file: str
    kind: str
    content: str
    line_range: str  # "L1-23"


def extract_code_snippets(file_content: str, file_path: str) -> list[CodeSnippet]:
    """Extract structurally important sections from a file.
    Returns empty list for tiny files (<5 lines)."""
    lines = file_content.splitlines()
    if len(lines) < 5:
        return []

    snippets: list[CodeSnippet] = []

    import_lines: list[str] = []
    import_end = 0
    for i, line in enumerate(lines[:60]):
        stripped = line.strip()
        if stripped.startswith(("import ", "from ", "#")):
            import_lines.append(line)
            import_end = i
        elif stripped == "" and import_lines and i < import_end + 3:
            import_lines.append(line)
            import_end = i
        elif import_lines:
            break
    if import_lines:
        while import_lines and not import_lines[-1].strip():
            import_lines.pop()
        snippets.append(CodeSnippet(
            file=file_path, kind="import_block",
            content="\n".join(import_lines),
            line_range=f"L1-{len(import_lines)}",
        ))

    # 2. Class / function / type signatures
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        is_class = bool(re.match(
            r'^(class |export\s+(class|interface|type|enum)\s)', stripped
        ))
        is_func = bool(re.match(
            r'^(def |async def |function |export function |pub fn |const \w+\s*=\s*(?:async\s+)?\()',
            stripped,
        ))
        is_model = bool(re.match(
            r'^(type |interface |enum |struct |Schema|class.*(?:Model|Schema|Config|Type))',
            stripped,
        ))

        if is_class or is_func or is_model:
            kind = "data_model" if is_model else "signature"
            sig_lines, end = _extract_signature_block(lines, i)
            if sig_lines:
                snippets.append(CodeSnippet(
                    file=file_path, kind=kind,
                    content="\n".join(sig_lines),
                    line_range=f"L{i+1}-{end+1}",
                ))
            i = max(end + 1, i + 1)
        else:
            i += 1

    return snippets


def _extract_signature_block(
    lines: list[str], start: int, max_body: int = 12,
) -> tuple[list[str], int]:
    """Extract: definition + docstring + first body lines."""
    result = [lines[start]]
    end = start
    base_indent = len(lines[start]) - len(lines[start].lstrip())
    j = start + 1

    # Docstring
    if j < len(lines):
        next_s = lines[j].strip()
        if next_s.startswith(('"""', "'''")):
            quote = next_s[:3]
            result.append(lines[j])
            end = j
            # BUG FIX: One-liner docstrings (e.g. """Short doc.""") already
            # contain the closing quote on the same line. The old code ignored
            # this and kept scanning into the function body looking for a
            # closing quote — potentially consuming body lines or stopping at
            # an unrelated string literal that happens to contain """.
            if next_s[3:].find(quote) >= 0:
                # Closing quote found on opening line → already done
                pass
            else:
                for k in range(j + 1, min(j + 15, len(lines))):
                    result.append(lines[k])
                    end = k
                    if quote in lines[k] and k > j:
                        break
            j = end + 1
        elif next_s.startswith(("/**", "//")):
            for k in range(j, min(j + 10, len(lines))):
                result.append(lines[k])
                end = k
                if "*/" in lines[k]:
                    break
            j = end + 1

    # Body lines
    body_count = 0
    for k in range(j, min(j + max_body, len(lines))):
        line = lines[k]
        stripped = line.strip()
        if not stripped:
            result.append(line)
            end = k
            continue
        current_indent = len(line) - len(line.lstrip())
        if current_indent <= base_indent and body_count > 0:
            break
        result.append(line)
        end = k
        body_count += 1
        if body_count >= 6:
            break

    return result, end


def collect_snippets_from_files(
    file_paths: list[str], workspace: str = "",
) -> list[CodeSnippet]:
    """Re-read files and extract snippets. Called once after explore."""
    ws = Path(workspace) if workspace else Path(".")
    snippets: list[CodeSnippet] = []
    for raw_path in file_paths:
        p = Path(raw_path)
        if not p.is_absolute():
            p = ws / raw_path
        try:
            if not p.exists() or not p.is_file():
                continue
            content = p.read_text(encoding="utf-8", errors="replace")
            snippets.extend(extract_code_snippets(content, str(p)))
        except Exception:
            continue
    return snippets


# ── Static repo-map / explore-context separation (2026-08-17) ───────────────────
# and LLM contracts — used by the planner (explore_to_planner_ctx), the coder

_STATIC_MAP_HEADING = "## Static Repo-Map"
_CONTRACTS_HEADING = "## Codebase Architecture Map"


def split_static_map_section(explore_ctx: str) -> tuple[str, str]:


    if not explore_ctx or _STATIC_MAP_HEADING not in explore_ctx:
        return "", explore_ctx
    if _CONTRACTS_HEADING in explore_ctx:
        _parts = explore_ctx.split(_CONTRACTS_HEADING, 1)
        return _parts[0].strip(), (_CONTRACTS_HEADING + _parts[1]).strip()
    _sm_end = explore_ctx.find("\n## ", explore_ctx.index(_STATIC_MAP_HEADING) + 20)
    if _sm_end > 0:
        return explore_ctx[:_sm_end].strip(), explore_ctx[_sm_end:].strip()
    return explore_ctx.strip(), ""


def budget_explore_window(explore_ctx: str, window_chars: int, map_chars: int) -> str:


    if not explore_ctx:
        return ""
    window_chars = max(0, int(window_chars or 0))
    if window_chars == 0 or len(explore_ctx) <= window_chars:
        return explore_ctx

    map_sec, contracts_sec = split_static_map_section(explore_ctx)
    map_budget = max(0, int(map_chars or 0))
    map_cap = map_budget if map_budget > 0 else window_chars
    map_part = map_sec[: min(map_cap, window_chars)]
    if map_sec and len(map_part) < len(map_sec):
        map_part = map_part.rstrip() + "\n[.. static map truncated for token budget ..]"
    remaining = max(0, window_chars - len(map_part))
    contracts_part = contracts_sec[:remaining]
    if contracts_sec and len(contracts_part) < len(contracts_sec):
        contracts_part = contracts_part.rstrip() + "\n[... explore_ctx truncated for token budget ...]"

    if map_part and contracts_part:
        return map_part + "\n\n" + contracts_part
    return map_part or contracts_part


# ── Stage 1: Explore → Planner ──────────────────────────────────────────────

def explore_to_planner_ctx(
    explore_ctx: str,
    task: str = "",
    budget: ContextBudget | None = None,
) -> str:
    """Structure TOML contracts for the planner. Full architecture picture."""
    if budget is None:
        budget = ContextBudget.from_content_tokens(8000)

    static_section, _contracts_body = split_static_map_section(explore_ctx)

    if static_section and len(static_section) > budget.static_map:
        static_section = static_section[:budget.static_map] + "\n[truncated]"

    partitions = parse_toml_partitions(_contracts_body)
    if not partitions:
        _fallback = _contracts_body[:budget.contracts]
        if static_section:
            return static_section + "\n\n" + _fallback
        return _fallback

    likely = [p for p in partitions if p.touched_by_task == "likely"]
    unknown = [p for p in partitions if p.touched_by_task == "unknown"]
    unlikely = [p for p in partitions if p.touched_by_task == "unlikely"]

    sections: list[str] = []

    # Dependency graph
    dep = _build_dependency_graph(partitions)
    if dep:
        sections.append("DEPENDENCY GRAPH:\n" + dep)

    # Likely — full detail
    if likely:
        sections.append("PARTITIONS TO MODIFY:")
        for p in likely:
            sections.append(_fmt_partition(p, "full"))

    # Unknown — moderate
    if unknown:
        sections.append("PARTITIONS (possibly touched):")
        for p in unknown:
            sections.append(_fmt_partition(p, "moderate"))

    # Unlikely — exports only
    if unlikely:
        ro = [f"  [{p.name}] exports: [{', '.join(p.exports[:6])}]" for p in unlikely]
        sections.append("READ-ONLY PARTITIONS:\n" + "\n".join(ro))

    result = "\n\n".join(sections)
    _contracts_cap = max(budget.contracts, budget.contracts * 4)
    if len(result) > _contracts_cap:
        result = result[:_contracts_cap] + "\n[truncated]"
    if static_section:
        result = static_section + "\n\n" + result
    return result


def _build_dependency_graph(partitions: list[PartitionInfo]) -> str:
    edges: list[str] = []
    for p in partitions:
        for imp in p.imports_internal:
            target = imp.strip("/").replace("\\", "/")
            target = target.split("/")[-1].rsplit(".", 1)[0] or target
            edges.append(f"  {p.name} → {target}")
    return "\n".join(edges) if edges else ""


def _fmt_partition(p: PartitionInfo, detail: str = "full") -> str:
    _fb = " \u26a0\ufe0f PROSE-FALLBACK " if p._fallback else ""
    if detail == "full":
        return (
            f"[{p.name}]{_fb}\n"
            f"  role: {p.role}\n"
            f"  exports: [{', '.join(p.exports[:10])}]\n"
            f"  imports_internal: [{', '.join(p.imports_internal[:8])}]\n"
            f"  imports_external: [{', '.join(p.imports_external[:6])}]\n"
            f"  data_flow: {p.data_flow}\n"
            f"  key_files: [{', '.join(p.key_files[:8])}]\n"
            f"  complexity: {p.complexity_score}\n"
            f"  hint: {p.hint}"
        )
    if detail == "moderate":
        return (
            f"[{p.name}]{_fb}\n"
            f"  role: {p.role}\n"
            f"  exports: [{', '.join(p.exports[:6])}]\n"
            f"  imports: [{', '.join(p.imports_internal[:4])}]\n"
            f"  files: [{', '.join(p.key_files[:6])}]"
        )
    return f"[{p.name}]{_fb} {p.role}"


# ── Stage 2: Planner → Coder ────────────────────────────────────────────────

def planner_to_coder_ctx(
    plan_text: str,
    explore_ctx: str,
    snippets: list[CodeSnippet] | None = None,
    task: str = "",
    budget: ContextBudget | None = None,
) -> str:
    """
    Build coder context: plan + contracts + snippets + file index.
    Budget-adaptive: drops snippets when context is tight.
    """
    if budget is None:
        budget = ContextBudget.from_content_tokens(8000)

    _static_section_coder, _contracts_body_coder = split_static_map_section(explore_ctx)

    if _static_section_coder and len(_static_section_coder) > budget.static_map:
        _static_section_coder = _static_section_coder[:budget.static_map] + "\n[truncated]"

    partitions = parse_toml_partitions(_contracts_body_coder)
    sections: list[str] = []

    # Task
    if task:
        sections.append(f"TASK:\n{task[:500]}")

    # Plan
    plan_section = plan_text[:budget.plan]
    if len(plan_text) > budget.plan:
        plan_section += "\n[plan continues in conversation]"
    sections.append(f"PLAN:\n{plan_section}")

    # Contracts for touched partitions
    touched = [p for p in partitions if p.touched_by_task in ("likely", "unknown")]
    readonly = [p for p in partitions if p.touched_by_task == "unlikely"]

    if touched:
        c_lines: list[str] = []
        for p in touched:
            detail = "full" if p.touched_by_task == "likely" else "moderate"
            c_lines.append(_fmt_partition(p, detail))
        contracts_content = "\n\n".join(c_lines)
        prefix = "PARTITIONS TO MODIFY:\n"
        max_content = max(0, budget.contracts - len(prefix))
        sections.append(prefix + contracts_content[:max_content])

    # Read-only — exports only
    if readonly and budget.contracts > 800:
        ro = [f"  [{p.name}] exports: [{', '.join(p.exports[:6])}]" for p in readonly]
        sections.append("READ-ONLY (available imports):\n" + "\n".join(ro))

    # Snippets — only if budget allows
    if snippets and budget.snippets > 0:
        snippet_text = _format_snippets(snippets, budget)
        if snippet_text:
            sections.append("EXISTING CODE IN TARGET FILES:\n" + snippet_text)

    # File index
    idx = _build_file_index(partitions)
    if idx:
        sections.append("FILE INDEX:\n" + idx[:budget.index])

    # Tool hint
    if budget.snippets == 0:
        sections.append(
            "RULE: You have NO code snippets. Use read_file before editing ANY file."
        )
    else:
        sections.append(
            "RULE: Snippets show signatures/patterns. For full detail, use read_file."
        )

    result = "\n\n".join(sections)
    if _static_section_coder:
        result = _static_section_coder + "\n\n" + result
    return result


def _format_snippets(snippets: list[CodeSnippet], budget: ContextBudget) -> str:
    by_file: dict[str, list[CodeSnippet]] = {}
    for s in snippets:
        by_file.setdefault(s.file, []).append(s)

    files = list(by_file.keys())[:budget.max_snippet_files]
    parts: list[str] = []
    total = 0

    for filepath in files:
        file_header = f"── {filepath} ──\n"
        file_text = file_header
        for s in by_file[filepath]:
            block = f"  [{s.kind} | {s.line_range}]\n"
            for line in s.content.splitlines():
                block += f"    {line}\n"
            if total + len(block) > budget.snippets:
                # BUG FIX: only append if we actually added snippet content
                # beyond the header. Old code always appended file_text here,
                # producing a dangling "── filepath ──" header with no code
                # when the budget was already exhausted on the first snippet
                # of a new file.
                if file_text != file_header:
                    parts.append(file_text.rstrip())
                return "\n\n".join(parts)
            file_text += block
            total += len(block)
        parts.append(file_text.rstrip())

    return "\n\n".join(parts)


def _build_file_index(partitions: list[PartitionInfo]) -> str:
    lines: list[str] = []
    for p in partitions:
        if p.key_files:
            marker = "✎" if p.touched_by_task == "likely" else "○"
            lines.append(f"  {marker} {p.name}: {', '.join(p.key_files[:8])}")
    return "\n".join(lines)


# ── Budget Computation ───────────────────────────────────────────────────────

def compute_content_budget(
    model_ctx: int | None,
    system_prompt_tokens: int = 800,
    tool_def_tokens: int = 1200,
    history_reserve_tokens: int = 1500,
    profile: str = "balanced",
) -> int:
    """
    Available content tokens for task context.
    model_ctx: model's num_ctx. None → 16384.
    profile: "fast" | "balanced" | "critical" — scales history reserve.
    """
    total = model_ctx or 16384
    history_scale = {"fast": 0.6, "balanced": 1.0, "critical": 1.8}
    history = int(history_reserve_tokens * history_scale.get(profile, 1.0))
    safety = int(total * 0.12)
    content = total - system_prompt_tokens - tool_def_tokens - history - safety
    return max(2048, content)


def derive_static_map_budget(
    coder_ctx: int | None,
    planner_ctx_target: int = 0,
    duo_static_map_chars: int = 0,
) -> int:


    _coder = int(coder_ctx or 8192)
    _planner = int(planner_ctx_target or 0)
    _eff = min(_coder, _planner) if _planner > 0 else _coder
    return ContextBudget.from_content_tokens(
        compute_content_budget(_eff)
    ).with_static_map_override(duo_static_map_chars).static_map


# ── Convenience: Full Pipeline ──────────────────────────────────────────────

def run_context_pipeline(
    explore_toml: str,
    plan_text: str,
    user_task: str,
    workspace: str = "",
    model_ctx: int | None = None,
    profile: str = "balanced",
) -> dict:
    """
    Run the full context pipeline. Called once after explore + planner complete.

    Returns dict:
      planner_ctx: context for planner stage
      coder_ctx:   context for coder stage
      snippets:    extracted CodeSnippet list
      budget:      the ContextBudget used
      touched_files: file paths from touched partitions
      content_tokens: available content tokens
    """
    content_tokens = compute_content_budget(model_ctx, profile=profile)
    budget = ContextBudget.from_content_tokens(content_tokens)
    partitions = parse_toml_partitions(explore_toml)

    # Collect touched files
    touched_files: list[str] = []
    seen: set[str] = set()
    for p in partitions:
        if p.touched_by_task in ("likely", "unknown"):
            for f in p.key_files:
                if f not in seen:
                    seen.add(f)
                    touched_files.append(f)

    # Extract snippets (only if budget allows)
    snippets: list[CodeSnippet] = []
    if budget.snippets > 0 and touched_files:
        snippets = collect_snippets_from_files(touched_files, workspace)

    # Build contexts
    planner_ctx = explore_to_planner_ctx(explore_toml, task=user_task, budget=budget)
    coder_ctx = planner_to_coder_ctx(
        plan_text, explore_toml,
        snippets=snippets, task=user_task, budget=budget,
    )

    return {
        "planner_ctx": planner_ctx,
        "coder_ctx": coder_ctx,
        "snippets": snippets,
        "budget": budget,
        "touched_files": touched_files,
        "content_tokens": content_tokens,
    }


# ── Legacy API ───────────────────────────────────────────────────────────────

def slice_toml_ctx(
    toml_str: str,
    ctx_target: int,
    ratio: float = 0.35,
) -> str:
    """Legacy entry point — now uses priority-ordered distillation."""
    budget = ContextBudget.from_content_tokens(int(ctx_target * ratio))
    return explore_to_planner_ctx(toml_str, budget=budget)
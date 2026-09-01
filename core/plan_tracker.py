
"""
PlanTracker with deviation detection for HiveMind Code-Duo.

Implements Plan Object + Trigger-Logic spec:
- Plan dataclass with intent, criteria, allowed/disallowed paths, steps
- DeviationState tracking with streak counting
- Hard rules (DEVIATES), soft rules (MAYBE), optional classifier
- Graduated reminder: NONE→normal, MAYBE→warning, DEVIATES→warning, streak>=3→replan signal, hard DEVIATES streak>=2→replan signal (siehe needs_replan)

Backward-compatible with existing API:
  .should_advance(tool_name, tool_result) -> bool
  .advance(tool_name)
  .tick()
  .reminder() -> str | None
"""
from __future__ import annotations


import datetime
import fnmatch
import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger("hivemind.plan_tracker")

# ═══════════════════════════════════════════════════════════════════════════
# Data Model
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class PlanStep:
    id: str
    intent: str
    keywords: list[str] = field(default_factory=list)
    expected_ops: list[str] = field(default_factory=list)
    expected_paths: list[str] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)
    done_when: list[str] = field(default_factory=list)


@dataclass
class DeviationState:
    status: str = "NONE"             # NONE | MAYBE | DEVIATES
    reasons: list[str] = field(default_factory=list)
    streak: int = 0


@dataclass
class LastToolState:
    name: str = ""
    path: str = ""
    summary: str = ""
    ok: bool = True
    timestamp: str = ""


@dataclass
class Plan:
    id: str = ""
    intent: str = ""
    success_criteria: list[str] = field(default_factory=list)
    allowed_paths: list[str] = field(default_factory=list)
    disallowed_paths: list[str] = field(default_factory=list)
    steps: list[PlanStep] = field(default_factory=list)
    current_step_id: str = ""
    completed_step_ids: list[str] = field(default_factory=list)
    last_tool: LastToolState = field(default_factory=LastToolState)
    deviation: DeviationState = field(default_factory=DeviationState)


# ═══════════════════════════════════════════════════════════════════════════
# Tool-name → Operation-type mapping
# ═══════════════════════════════════════════════════════════════════════════

_TOOL_OP_MAP: dict[str, str] = {
    "read_file":       "read",
    "get_signatures":  "read",
    "edit_file":       "edit",
    "replace_lines":   "edit",
    "write_file_append": "edit",
    "write_file":      "edit",
    "patch_file":      "edit",
    "edit_ast":        "edit",
    "undo_last":       "edit",
    "run_bash":        "run",
    "run_python":      "run",
    "search_code":     "search",
    "find_files":      "search",
    "list_dir":        "list",
    "ask_user":        "ask",
    "git_status":      "run",
    "git_commit":      "run",
}


def _normalize_path(p: str) -> str:
    """Normalize a path to forward-slash, strip leading dots/slashes."""
    p = p.replace("\\", "/")
    p = re.sub(r"^\.?/", "", p)
    return p.rstrip("/")


def _path_matches(pattern: str, path: str, *, is_prefix: bool = False) -> bool:
    """Check if *path* matches *pattern* (glob or prefix).

    - If *pattern* contains glob metacharacters (``*``, ``?``, ``[``), ``fnmatch`` is used.
    - If *is_prefix*, *pattern* is a directory prefix: ``path.startswith(pattern)``.
    - Otherwise exact file match.
    """
    p_pat = _normalize_path(pattern)
    p_path = _normalize_path(path)
    if is_prefix:
        return p_path.startswith(p_pat + "/") or p_path == p_pat
    if any(c in p_pat for c in "*?["):
        return fnmatch.fnmatch(p_path, p_pat)
    return p_path == p_pat


def _extract_keywords(text: str, max_tokens: int = 6) -> list[str]:
    """Tokenize text into lowercased alphanumeric tokens (>=3 chars)."""
    tokens = re.findall(r"[a-zA-Z0-9_]{3,}", text.lower())
    seen: set[str] = set()
    result: list[str] = []
    for t in tokens:
        if t not in seen and t not in ("the", "and", "for", "with", "that", "this", "from", "file", "path", "step"):
            seen.add(t)
            result.append(t)
            if len(result) >= max_tokens:
                break
    return result


def _infer_expected_ops(action_text: str, touch_text: str = "") -> list[str]:
    """Infer expected operations from action/touch text keywords."""
    combined = (action_text + " " + touch_text).lower()
    ops: list[str] = []
    if any(w in combined for w in ("create", "add", "write", "edit", "modify", "update", "implement", "patch", "change")):
        ops.append("edit")
    if any(w in combined for w in ("read", "analyze", "inspect", "examine", "review", "understand")):
        ops.append("read")
    if any(w in combined for w in ("run", "test", "execute", "verify", "check", "validate", "build")):
        ops.append("run")
    if any(w in combined for w in ("search", "find", "locate", "grep")):
        ops.append("search")
    if not ops:
        ops.append("read")
        ops.append("edit")
    return ops


# Regex for planner step lines:
#   1. file: path/to/file.py | touch: ClassName.method | decision: add hook | risk: signature break
_STEP_LINE_RE = re.compile(
    r"^\s*(\d+)\.\s*"
    r"file:\s*([^\|]+?)\s*\|\s*"
    r"touch:\s*([^\|]+?)\s*\|\s*"
    r"decision:\s*([^\|]+?)(?:\s*\|\s*risk:\s*(.+?))?\s*$",
    re.MULTILINE,
)

# More relaxed fallback regex:
#   1. file: path/to/file.py  (minimal)
_STEP_FALLBACK_RE = re.compile(
    r"^\s*(\d+)\.\s*file:\s*([^\|]+?)(?:\s*\||$)",
    re.MULTILINE,
)


def _parse_planner_steps(plan_text: str) -> list[PlanStep]:
    """Parse planner output text into PlanStep list."""
    steps: list[PlanStep] = []
    seen_ids: set[str] = set()

    matches = list(_STEP_LINE_RE.finditer(plan_text))
    if not matches:
        matches = list(_STEP_FALLBACK_RE.finditer(plan_text))

    for m in matches:
        step_id = m.group(1)
        if step_id in seen_ids:
            continue
        seen_ids.add(step_id)

        groups = m.groups()
        file_path = _normalize_path(groups[1].strip()) if len(groups) > 1 else ""
        touch = groups[2].strip() if len(groups) > 2 else ""
        decision = groups[3].strip() if len(groups) > 3 else ""
        # group(4) is risk if present

        intent = decision or touch or f"Implement changes to {file_path}" or plan_text[:80]
        keywords = _extract_keywords(touch + " " + decision)
        expected_ops = _infer_expected_ops(decision, touch)

        _raw_files = groups[1].strip() if len(groups) > 1 else ""
        _split_paths = [_normalize_path(p.strip()) for p in _raw_files.split(",") if p.strip()]

        steps.append(PlanStep(
            id=step_id,
            intent=intent,
            keywords=keywords or _extract_keywords(plan_text),
            expected_ops=expected_ops,
            expected_paths=_split_paths,
            produces=[f"diff in {file_path}"] if file_path else [],
            done_when=[f"edit to {file_path} complete"] if file_path else [],
        ))

    return steps


def _contracts_to_allowed_disallowed(contracts: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """Extract allowed_paths, disallowed_paths, and all_files from parsed contracts.

    - allowed_paths: all files_read from partitions with touched_by_task='yes'
    - disallowed_paths: all files_read from partitions with touched_by_task='unlikely'
    """
    allowed: list[str] = []
    disallowed: list[str] = []
    all_files: list[str] = []

    for c in contracts:
        files = c.get("files_read", [])
        all_files.extend(files)
        touched = c.get("touched_by_task", "unknown")
        if touched in ("yes", True):
            allowed.extend(files)
        elif touched in ("unlikely", False):
            disallowed.extend(files)
    return allowed, disallowed, all_files


def _summary_normalize(text: str, max_chars: int = 200) -> str:
    """Normalize tool result for keyword matching."""
    if not text:
        return ""
    s = text.strip()[:max_chars].lower()
    s = re.sub(r"\s+", " ", s)
    return s


# ═══════════════════════════════════════════════════════════════════════════
# PlanTracker
# ═══════════════════════════════════════════════════════════════════════════


class PlanTracker:
    def __init__(self, steps: list[dict] | None = None, *, plan: Plan | None = None,
                 workspace: str = ""):
        if plan is not None:
            self._plan = plan
            self._legacy_steps: list[dict] = [
                {
                    "step": s.id,
                    "file": ", ".join(s.expected_paths) if s.expected_paths else "?",
                    "action": s.intent,
                }
                for s in plan.steps
            ]
        elif steps:
            self._plan = _legacy_steps_to_plan(steps)
            self._legacy_steps = steps
        else:
            self._plan = Plan()
            self._legacy_steps = []

        self._ticks: int = 0
        self._stall_ticks: int = 0  # increments when step doesn't advance
        self._last_file_changes_count: int = 0  # progress tracking across replan caps
        self._workspace: str = workspace or ""  # for file-existence checks after rebuild
        self._classifier: str = "heuristic"
        self._initial_read_phase: bool = True
        self._initial_reads: int = 0

    @classmethod
    def configure(cls, settings: dict | None = None):
        """Set global config from settings dict (call at init time)."""
        cls._classifier = (settings or {}).get("plan_tracker_classifier", "heuristic")

    # ── Backward-compat properties ─────────────────────────────────────────

    @property
    def total(self) -> int:
        return len(self._plan.steps)

    @property
    def is_finished(self) -> bool:
        return len(self._plan.completed_step_ids) >= len(self._plan.steps) and self.total > 0

    # ── Backward-compat: should_advance / advance ───────────────────────────

    def should_advance(self, tool_name: str, tool_result: str) -> bool:
        """Check if the current step's done_when criteria are satisfied."""
        step = self._current_step()
        if step is None:
            return
        if self.is_finished:
            return
        if step.id not in self._plan.completed_step_ids:
            self._plan.completed_step_ids.append(step.id)
        self._plan.current_step_id = self._next_step_id()
        self._stall_ticks = 0
        self._plan.deviation.streak = 0
        self._plan.deviation.status = "NONE"
        self._plan.deviation.reasons.clear()
        self._initial_read_phase = True  # fresh read window per step
        self._initial_reads = 0

    def _current_step(self) -> PlanStep | None:
        if not self._plan.steps:
            return None
        if not self._plan.current_step_id and self._plan.steps:
            self._plan.current_step_id = self._plan.steps[0].id
        for s in self._plan.steps:
            if s.id == self._plan.current_step_id:
                return s
        return None

    def _next_step_id(self) -> str:
        step_ids = [s.id for s in self._plan.steps]
        try:
            idx = step_ids.index(self._plan.current_step_id)
        except ValueError:
            idx = -1
        next_idx = idx + 1
        if next_idx < len(step_ids):
            return step_ids[next_idx]
        return ""

    # ── Tick (now runs deviation check) ────────────────────────────────────

    def tick(self, tool_name: str = "", tool_result: str = "",
             touched_paths: list[str] | None = None,
             file_changes_count: int = 0):
        """Advance stall counter and run deviation check.

        Backward-compatible: called with no args, does nothing harmful.
        """
        self._ticks += 1

        if file_changes_count > self._last_file_changes_count:
            self._stall_ticks = 0  # progress detected → reset
            self._last_file_changes_count = file_changes_count
        else:
            self._stall_ticks += 1

        if not tool_name:
            return  # old call signature — no observations to check

        self._plan.last_tool = LastToolState(
            name=tool_name,
            path=touched_paths[0] if touched_paths else "",
            summary=_summary_normalize(tool_result),
            ok=True,
            timestamp=datetime.datetime.now().isoformat(),
        )

        self.check_deviation(
            tool_name=tool_name,
            tool_path=self._plan.last_tool.path,
            tool_summary=self._plan.last_tool.summary,
            touched_paths=touched_paths or [],
        )

    # ── Deviation Check (7-step trigger logic) ─────────────────────────────

    def check_deviation(self, *, tool_name: str, tool_path: str,
                        tool_summary: str, touched_paths: list[str]):
        """Run full deviation detection pipeline.

        1. Collect observations
        2. Hard rules → DEVIATES
        3. Soft rules → MAYBE
        4. Classifier (if MAYBE)
        5. Update state
        6. Replan gate (caller checks needs_replan())

        Args:
            tool_name: Normalized tool name (e.g. 'edit_file')
            tool_path: Primary path the tool operated on
            tool_summary: Normalized tool result (first 200 chars)
            touched_paths: All paths touched by this tool call
        """
        step = self._current_step()
        if step is None:
            return
        if self.is_finished:
            return

        if self._initial_read_phase:
            op = _TOOL_OP_MAP.get(tool_name, "unknown")
            if op in ("read", "search", "list"):
                self._initial_reads += 1
                if self._initial_reads <= 8:
                    return
                # budget exhausted — fall through to enforcement
            self._initial_read_phase = False  # first edit/run or read budget exhausted — start enforcing

        reasons: list[str] = []
        final_status = "NONE"
        allowed = self._plan.allowed_paths
        disallowed = self._plan.disallowed_paths
        step_allowed = step.expected_paths if step.expected_paths else allowed

        # ── Step 3: Hard Rules ─────────────────────────────────────────────
        # Rule: any touched_path ∈ disallowed_paths
        _tool_op = _TOOL_OP_MAP.get(tool_name, "unknown")
        _is_read_op = _tool_op in ("read", "search", "list")
        for tp in touched_paths:
            for dp in disallowed:
                if _path_matches(dp, tp):
                    if _is_read_op:
                        reasons.append(f"read on disallowed path '{tp}' (context gathering, soft)")
                        if final_status == "NONE":
                            final_status = "MAYBE"
                    else:
                        reasons.append(f"touched disallowed path '{tp}' (matches '{dp}')")
                        final_status = "DEVIATES"
                    break
            if final_status == "DEVIATES":
                break

        # Rule: tool is edit but all touched paths are outside allowed_paths
        if final_status == "NONE" and _TOOL_OP_MAP.get(tool_name) == "edit" and touched_paths and allowed:
            all_outside = True
            for tp in touched_paths:
                if any(_path_matches(ap, tp) for ap in allowed):
                    all_outside = False
                    break
            if all_outside:
                reasons.append(f"edit to paths {touched_paths} — none in allowed_paths")
                final_status = "DEVIATES"

        # Rule: tool_name not in step.expected_ops
        if final_status == "NONE" and step.expected_ops:
            op = _TOOL_OP_MAP.get(tool_name, "read")
            if op not in step.expected_ops:
                # Allow 'read' as fallback — reading before editing is normal
                if not (op == "read" and "edit" in step.expected_ops):
                    reasons.append(f"tool '{tool_name}' (op={op}) not in step expected_ops {step.expected_ops}")
                    final_status = "DEVIATES"

        # ── Step 4: Soft Rules ─────────────────────────────────────────────
        if final_status == "NONE" and touched_paths:
            any_in_step = False
            for tp in touched_paths:
                if any(_path_matches(sp, tp) for sp in step_allowed):
                    any_in_step = True
                    break
            if not any_in_step:
                # Check if still inside plan allowed_paths (soft, not hard)
                any_in_plan = False
                for tp in touched_paths:
                    if any(_path_matches(ap, tp) for ap in allowed):
                        any_in_plan = True
                        break
                if any_in_plan or not allowed:
                    reasons.append(f"touched paths {touched_paths} outside step scope {step.expected_paths}")
                    final_status = "MAYBE"

        # Rule: keyword overlap
        if final_status != "DEVIATES" and step.keywords and tool_summary:
            overlap = sum(1 for kw in step.keywords if kw in tool_summary)
            if overlap == 0 and tool_name != "ask_user":
                reasons.append(f"tool summary lacks all step keywords {step.keywords}")
                if final_status == "NONE":
                    final_status = "MAYBE"

        # Rule: step jump (current_step_id changed without should_advance/advance)
        # We detect this implicitly: if stall_ticks == 1 and we're still on same step,
        # that's normal. If there's a mismatch in completion tracking, it surfaces
        # via the keyword/path checks above.

        # Rule: edit affects file not in any contract
        if final_status != "DEVIATES" and _TOOL_OP_MAP.get(tool_name) == "edit" and touched_paths and allowed:
            for tp in touched_paths:
                if not any(_path_matches(ap, tp) for ap in allowed):
                    reasons.append(f"edit to '{tp}' — file not in any contract")
                    if final_status == "NONE":
                        final_status = "MAYBE"
                    break

        # ── Step 5: Classifier (only if MAYBE) ─────────────────────────────
        if final_status == "MAYBE" and self._classifier == "heuristic":
            resolved = self._heuristic_classify(step, tool_summary, touched_paths)
            if resolved == "DEVIATES":
                final_status = "DEVIATES"
            # ALIGNED: keep MAYBE — soft warning surfaces via reminder()
            # Do NOT clear reasons — they inform the model via (Note: possible drift)
            # Only clear reasons if there are zero reasons (defensive)
            if not reasons:
                final_status = "NONE"

        # ── Step 6: Update deviation state ─────────────────────────────────
        if final_status == "DEVIATES":
            self._plan.deviation.status = "DEVIATES"
            self._plan.deviation.streak += 1
            self._plan.deviation.reasons = reasons
        elif final_status == "MAYBE":
            self._plan.deviation.status = "MAYBE"
            self._plan.deviation.reasons = reasons
            self._plan.deviation.streak = max(0, self._plan.deviation.streak - 1)
        else:
            self._plan.deviation.status = "NONE"
            self._plan.deviation.streak = 0
            self._plan.deviation.reasons.clear()

    def _heuristic_classify(self, step: PlanStep, tool_summary: str,
                            touched_paths: list[str]) -> str:
        """Heuristic classifier for MAYBE cases.

        Returns 'DEVIATES' if the evidence strongly suggests deviation,
        'ALIGNED' (NONE) otherwise.
        """
        # Count total keyword overlap across all touched paths and summary
        combined = tool_summary + " " + " ".join(touched_paths)
        if step.keywords:
            overlap = sum(1 for kw in step.keywords if kw in combined)
            ratio = overlap / len(step.keywords)
            if ratio < 0.1 and tool_summary:
                return "DEVIATES"
        # If there's at least some path overlap, consider aligned
        if touched_paths and step.expected_paths:
            any_match = any(
                any(_path_matches(sp, tp) for sp in step.expected_paths)
                for tp in touched_paths
            )
            if any_match:
                return "ALIGNED"
        return "ALIGNED"  # Default: not deviating

    # ── Replan Gate ────────────────────────────────────────────────────────

    def needs_replan(self) -> bool:
        """True when deviation.streak >= 3 or hard DEVIATES with streak >= 2."""
        d = self._plan.deviation
        if d.streak >= 3:
            return True
        if d.status == "DEVIATES" and d.streak >= 2:
            return True
        return False

    def replan_prompt(
        self,
        written_files: set | None = None,
        file_changes: dict | None = None,
    ) -> str:
        """Return the system injection message for replanning with file context."""
        written = sorted(written_files or [])
        _w_str = (
            ", ".join(written[:15]) + ("..." if len(written) > 15 else "")
            if written else "none"
        )

        step = self._current_step()
        step_label = "unknown"
        if step:
            intent = getattr(step, "intent", "") or ""
            paths = getattr(step, "expected_paths", []) or []
            step_label = f"step {step.id}: {intent}"
            if paths:
                step_label += f" → target files: {', '.join(str(p) for p in paths[:5])}"

        return (
            "[REPLAN REQUIRED] Deviation detected from planned path.\n"
            f"Already written: {_w_str}\n"
            f"Current step: {step_label}\n"
            "Your next action MUST be one of:\n"
            "  (a) A tool call that directly fixes the blocking issue.\n"
            "  (b) task_complete(status='blocked', reason='...').\n"
            "Do NOT re-read files already in your context. Do NOT output a plan."
        )

    # ── Reminder (deviation-aware, backward-compat) ────────────────────────

    def reminder(self) -> str | None:
        """Return a progress reminder string, or None to signal replan.

        Backward-compatible: returns a string for injection into messages.
        Returns None when needs_replan() is True (caller must handle).
        """
        if self.total == 0:
            return None
        if self.is_finished:
            return None

        step = self._current_step()
        if step is None:
            return None

        current_idx = self._current_step_index() + 1  # 1-based for display
        total = self.total

        d = self._plan.deviation

        # Replan signal
        if self.needs_replan():
            return None

        base = f"[Plan Step {current_idx}/{total}: {step.intent}]"

        if d.status == "DEVIATES":
            reasons_txt = "; ".join(d.reasons) if d.reasons else "unexpected action"
            return (
                f"⚠️ {base}\n"
                f"Off-plan (streak {d.streak}): {reasons_txt}.\n"
                f"Reconcile: return to step {current_idx} or create a short revised plan."
            )
        elif d.status == "MAYBE":
            reasons_txt = "; ".join(d.reasons) if d.reasons else "unexpected scope"
            return f"{base}\n(Note: possible drift — {reasons_txt})"

        return base

    def _current_step_index(self) -> int:
        if not self._plan.current_step_id:
            return 0
        for i, s in enumerate(self._plan.steps):
            if s.id == self._plan.current_step_id:
                return i
        return 0

    # ── Rebuild from replan result ─────────────────────────────────────────

    def _step_files_exist(self, step, workspace: str) -> bool:
        import os as _os_fs
        paths = getattr(step, "expected_paths", None) or []
        if not paths:
            return False
        return all(
            _os_fs.path.exists(
                p if _os_fs.path.isabs(p) else _os_fs.path.join(workspace, str(p))
            )
            for p in paths
        )

    def rebuild_from_plan(self, plan_text: str):
        """Parse replanned steps and reset tracker state."""
        new_steps = _parse_planner_steps(plan_text)
        if not new_steps:
            # Fallback: create single catch-all step
            new_steps = [PlanStep(
                id="1",
                intent=plan_text[:80] if plan_text else "Complete the remaining task",
                keywords=_extract_keywords(plan_text) if plan_text else [],
                expected_ops=["read", "edit", "run", "search"],
            )]

        # Recalculate allowed/disallowed paths from new plan steps
        self._plan.steps = new_steps
        self._plan.current_step_id = new_steps[0].id if new_steps else ""
        _new_allowed = list(self._plan.allowed_paths)
        for step in new_steps:
            for sp in step.expected_paths:
                if sp not in _new_allowed:
                    _new_allowed.append(sp)
        self._plan.allowed_paths = _new_allowed
        self._plan.disallowed_paths = [
            dp for dp in self._plan.disallowed_paths
            if not any(_path_matches(sp, dp) for sp in _new_allowed)
        ]
        # Preserve steps whose files still exist on disk
        _ws = getattr(self, "_workspace", "") or ""
        _keep_completed: set = set()
        for _old_step_id in list(self._plan.completed_step_ids):
            _matching_step = next(
                (s for s in (new_steps or []) if s.id == _old_step_id),
                None,
            )
            if _matching_step and self._step_files_exist(_matching_step, _ws):
                _keep_completed.add(_old_step_id)
        self._plan.completed_step_ids = _keep_completed
        self._plan.deviation = DeviationState()
        self._stall_ticks = 0
        self._ticks = 0
        self._initial_read_phase = True  # fresh read window after replan
        self._initial_reads = 0
        self._legacy_steps = [
            {"step": s.id, "file": ", ".join(s.expected_paths) if s.expected_paths else "?",
             "action": s.intent}
            for s in new_steps
        ]


# ═══════════════════════════════════════════════════════════════════════════
# Legacy step conversion
# ═══════════════════════════════════════════════════════════════════════════


def _legacy_steps_to_plan(steps: list[dict]) -> Plan:
    """Convert old-style [{step, file, action}] dicts to Plan object."""
    plan_steps: list[PlanStep] = []
    for i, s in enumerate(steps):
        step_id = str(s.get("step", i + 1))
        file_path = s.get("file", "?").replace("\\", "/").strip()
        action = s.get("action", "") or ""
        intent = action or f"Process {file_path}"
        keywords = _extract_keywords(action + " " + file_path)
        expected_paths = [file_path] if file_path and file_path != "?" else []
        expected_ops = _infer_expected_ops(action)
        plan_steps.append(PlanStep(
            id=step_id,
            intent=intent,
            keywords=keywords,
            expected_ops=expected_ops,
            expected_paths=expected_paths,
        ))

    # All files from legacy steps become allowed_paths
    allowed_paths = [s.expected_paths[0] for s in plan_steps if s.expected_paths]

    return Plan(
        id=f"legacy-{id(steps)}",
        intent=steps[0].get("action", "") if steps else "",
        allowed_paths=allowed_paths,
        steps=plan_steps,
        current_step_id=plan_steps[0].id if plan_steps else "",
    )


# ═══════════════════════════════════════════════════════════════════════════
# Factory functions
# ═══════════════════════════════════════════════════════════════════════════


def build_tracker_from_contracts(contracts: list[dict], workspace: str = "") -> PlanTracker | None:
    """Build a PlanTracker from parsed contract summaries.

    Extracts allowed_paths from touched-by-task partitions,
    disallowed_paths from unlikely partitions,
    and steps from embedded plan_steps.
    """
    if not contracts:
        return None

    allowed, disallowed, _all = _contracts_to_allowed_disallowed(contracts)

    # Extract plan steps from contracts
    raw_steps: list[dict] = []
    for c in contracts:
        for ps in c.get("plan_steps", []):
            if isinstance(ps, dict):
                raw_steps.append({
                    "step": ps.get("step", len(raw_steps) + 1),
                    "file": ps.get("file", "?"),
                    "action": ps.get("action", ""),
                })

    if workspace:
        import os as _os_exist
        _missing = set()
        for _rs in raw_steps:
            _f = _rs.get("file", "")
            if _f and _f != "?" and not _os_exist.path.exists(_os_exist.path.join(workspace, _f)):
                _missing.add(_f)
        if _missing:
            logger.warning("[PLANNER] Steps reference %d nonexistent file(s): %s",
                           len(_missing), ", ".join(sorted(_missing)[:10]))

    if not raw_steps:
        # Create a single step covering all touched files
        file_list = ", ".join(allowed) if allowed else "?"
        raw_steps = [{"step": 1, "file": file_list, "action": "Execute task on touched partitions"}]

    plan = _legacy_steps_to_plan(raw_steps)
    plan.allowed_paths = allowed
    plan.disallowed_paths = disallowed

    return PlanTracker(plan=plan, workspace=workspace)


def build_tracker_from_planner(planner_result, contracts: list[dict] | None = None,
                               workspace: str = "") -> PlanTracker | None:
    """Build a PlanTracker from PlannerResult + contracts.

    Uses planner subtasks/thinking for step intents and keywords,
    contracts for allowed/disallowed path policies.
    """
    if planner_result is None:
        return None

    # Collect planner text from subtasks + thinking
    plan_text = ""
    if hasattr(planner_result, "subtasks") and planner_result.subtasks:
        plan_text = "\n".join(str(s) for s in planner_result.subtasks)
    if not plan_text and hasattr(planner_result, "thinking") and planner_result.thinking:
        plan_text = planner_result.thinking

    if not plan_text:
        return None

    new_steps = _parse_planner_steps(plan_text)
    if not new_steps:
        return None

    allowed, disallowed, _all = (_contracts_to_allowed_disallowed(contracts)
                                 if contracts else ([], [], []))

    plan = Plan(
        id=f"planner-{id(planner_result)}",
        intent=plan_text[:200],
        allowed_paths=allowed,
        disallowed_paths=disallowed,
        steps=new_steps,
        current_step_id=new_steps[0].id,
    )

    return PlanTracker(plan=plan, workspace=workspace)

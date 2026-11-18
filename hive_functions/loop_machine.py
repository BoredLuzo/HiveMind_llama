from __future__ import annotations
import re
import logging
from enum import Enum

_log = logging.getLogger("hivemind.loop_machine")


class AgentState(Enum):
    INIT = "init"
    PLANNING = "planning"
    EXPLORE = "explore"
    CODING = "coding"
    VERIFY = "verify"
    CRITIC_REVIEW = "critic_review"
    ROLLBACK = "rollback"
    HALTED = "halted"


class StopReason(Enum):
    SUCCESS = "success"
    MAX_ITERATIONS = "max_iterations"
    MAX_TOOL_ROUNDS = "max_tool_rounds"
    STUCK_IN_LOOP = "stuck_in_loop"
    USER_ABORTED = "user_aborted"
    CRITICAL_ERROR = "critical_error"


def _normalize_for_jaccard(text: str) -> str:
    return text.replace("\\", "/").lower()


_RE_STACKTRACE_LINE = re.compile(r'(?:File ".+?", line \d+|at line \d+|line \d+, in )')


def _normalize_stacktrace_lines(text: str) -> str:
    return _RE_STACKTRACE_LINE.sub(
        lambda m: re.sub(r'\d+', 'X', m.group(0)),
        text
    )


class ExecutionController:
    def __init__(self, max_iterations: int, max_repeats_stuck: int = 5):
        self.state = AgentState.INIT
        self.iteration = 0
        self.tool_rounds = 0
        self.max_iterations = max_iterations
        self.stop_reason: StopReason | None = None
        
        # Stuck detection
        self.max_repeats = max_repeats_stuck
        self._last_outputs: list[str] = []
        self._last_semantic: list[set[str]] = []
        self.verify_fail_streak: int = 0
        self.verify_fail_sig: str = ""
        self.history: list[dict] = []

    def transition(self, next_state: AgentState):
        self.history.append({
            "from": self.state.value, 
            "to": next_state.value, 
            "iteration": self.iteration,
            "tool_rounds": self.tool_rounds
        })
        self.state = next_state
        if next_state == AgentState.HALTED and not self.stop_reason:
            self.stop_reason = StopReason.SUCCESS

    def abort(self, reason: StopReason):
        self.stop_reason = reason
        self.transition(AgentState.HALTED)

    def record_output(self, test_output: str, tool_name: str = "run_bash"):


        if not test_output or not test_output.strip():
            return

        norm = self._normalize(f"[{tool_name}] {test_output}")
        sem = self._semantic_signature(norm)
        self._last_outputs.append(norm)
        self._last_semantic.append(sem)
        if len(self._last_outputs) > self.max_repeats + 1:
            self._last_outputs.pop(0)
        if len(self._last_semantic) > self.max_repeats + 1:
            self._last_semantic.pop(0)

    def is_stuck(self) -> bool:
        if len(self._last_outputs) < self.max_repeats:
            return False
        last = self._last_outputs[-self.max_repeats:]
        if len(set(last)) == 1:
            return True

        # Semantic guard: catches tiny wording changes that keep repeating the same outcome.
        sem_last = self._last_semantic[-self.max_repeats:]
        if len(sem_last) < self.max_repeats:
            return False
        anchor = sem_last[0]
        if not anchor:
            return False
        return all(self._jaccard(anchor, cur) >= 0.92 for cur in sem_last[1:])

    def reset_stuck_detection(self):


        self._last_outputs.clear()
        self._last_semantic.clear()

    def note_verify_result(self, failed: bool, output: str = "") -> int:
        """Track consecutive failing verification runs across interleaved edits.

        Unlike ``reset_stuck_detection()`` (which clears identical-output history
        after each file edit), ``verify_fail_streak`` only resets when a
        verification run succeeds.
        """
        if not failed:
            self.verify_fail_streak = 0
            self.verify_fail_sig = ""
            return 0
        cleaned = re.sub(r"\n\[cmd\]\s*[^\n]*\s*$", "", (output or "").strip())
        sig = self._normalize(cleaned)[:300]
        if not sig:
            return self.verify_fail_streak
        same = (
            sig == self.verify_fail_sig
            or (
                bool(self.verify_fail_sig)
                and self._jaccard(
                    self._semantic_signature(self.verify_fail_sig),
                    self._semantic_signature(sig),
                ) >= 0.88
            )
        )
        if same:
            self.verify_fail_streak += 1
        else:
            self.verify_fail_streak = 1
            self.verify_fail_sig = sig
        return self.verify_fail_streak

    def _normalize(self, text: str) -> str:
        s = text.strip().lower()
        if len(s) > 1000:
            s = (s[:500] + s[-500:])
        s = re.sub(r'\d+\.\d+s', 'Xs', s)
        s = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', 'TS', s)
        s = _normalize_stacktrace_lines(s)   # stack-trace line numbers only — preserves tool args
        s = re.sub(r'0x[0-9a-fA-F]+', 'PTR', s) # Objektadressen
        s = re.sub(r'\s+', ' ', s) # Whitespace normieren
        return s

    def _semantic_signature(self, text: str) -> set[str]:
        if not text:
            return set()
        normalized = _normalize_for_jaccard(text)
        tokens = set(t for t in re.findall(r"[a-z0-9_./:-]+", normalized) if len(t) >= 3)
        return tokens

    def _jaccard(self, a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        inter = len(a & b)
        union = len(a | b)
        if union == 0:
            return 0.0
        return inter / union

    def sync_tool_rounds(self, total: int):


        self.tool_rounds = total

    def get_summary(self) -> str:
        reason = self.stop_reason.value if self.stop_reason else "unknown"
        return f"[Agentic Loop Halted: {reason.upper()} (Iterations: {self.iteration}/{self.max_iterations})]"
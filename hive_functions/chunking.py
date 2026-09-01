"""
hive_functions.chunking - Chunking pipeline module
===================================================

Centralises ALL chunking logic that was previously scattered across server.py.

Architecture
------------
ChunkState
    Mutable state object tracking progress through a chunked run:
    done tasks, written files, retry counters, fix overrides, etc.
    Replaces the loose local variables (_done_tasks, _written_files,
    _chunk_test_retries, …) that lived in the main while-loop.

Resume helpers
    Pure functions for serialising / deserialising chunking state
    so interrupted runs can be resumed:
    normalize_resume_chunk, normalize_resume_chunks, resume_block_is_valid,
    chunk_hint_sources, extract_chunk_file_hints, normalize_resume_candidate_path,
    collect_resume_file_snapshot.

Context builder
    build_chunk_context() — assembles the coder input prompt for a specific chunk.

Auto-test runner
    run_chunk_auto_test() — runs the test suite after a chunk completes,
    emits events, returns (TestResult, is_clean).

Self-fix logic
    ChunkState.handle_auto_test_result() — the re-awaken / retry / completion-note
    pattern that was duplicated in server.py (agentic + non-agentic paths).

Combined output
    ChunkState.combine_outputs() — merges per-chunk coder output + completion notes
    into the combined string that Synthesis receives.
"""
from __future__ import annotations


import logging
import os
import re
from utils.patterns import _RE_PATH_KEY, _RE_REL_PATH_HINT
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Awaitable, Any

from hive_functions.ctx_utils import budget_explore_window

from utils.file import quick_file_sha1

if TYPE_CHECKING:
    from hive_functions.test_runner import TestResult as _TestResult

logger = logging.getLogger("hivemind.chunking")


# ═══════════════════════════════════════════════════════════════════════════════
#  Lazy imports — avoid circular / heavy module load at import time
# ═══════════════════════════════════════════════════════════════════════════════

async def _get_test_runner():
    """Lazy-load test_runner to avoid circular imports."""
    from hive_functions.test_runner import run_tests, TestResult
    return run_tests, TestResult


# ═══════════════════════════════════════════════════════════════════════════════
#  File signature helpers  (for resume validation)
# ═══════════════════════════════════════════════════════════════════════════════

def build_file_signature(path_like: str | Path) -> dict | None:
    """Build a lightweight file signature (mtime, size, sha1) for change detection."""
    p = Path(path_like)
    try:
        st = p.stat()
    except OSError:
        return None
    if not p.is_file():
        return None
    sig: dict[str, Any] = {
        "mtime": float(st.st_mtime),
        "size": int(st.st_size),
    }
    try:
        sig["sha1"] = quick_file_sha1(p)
    except Exception:
        pass
    return sig


def _normalize_cached_signature(cached) -> dict | None:
    if isinstance(cached, (int, float)):
        return {"mtime": float(cached)}
    if not isinstance(cached, dict):
        return None
    out: dict[str, Any] = {}
    if "mtime" in cached:
        try:
            out["mtime"] = float(cached["mtime"])
        except Exception:
            pass
    if "size" in cached:
        try:
            out["size"] = int(cached["size"])
        except Exception:
            pass
    _sha = cached.get("sha1")
    if isinstance(_sha, str) and _sha.strip():
        out["sha1"] = _sha.strip().lower()
    return out or None


def file_signature_matches(path_like: str | Path, cached, *, allow_missing: bool = True) -> bool:
    """Compatibility matcher for old mtime-only and new signature dict formats."""
    sig = _normalize_cached_signature(cached)
    if not sig:
        return True
    p = Path(path_like)
    try:
        st = p.stat()
    except OSError:
        return bool(allow_missing)

    cached_mtime = sig.get("mtime")
    cached_size = sig.get("size")

    if cached_mtime is not None and abs(float(st.st_mtime) - float(cached_mtime)) <= 0.1:
        if cached_size is None or int(st.st_size) == int(cached_size):
            return True

    if cached_size is not None and int(st.st_size) != int(cached_size):
        return False

    cached_sha = sig.get("sha1")
    if cached_sha:
        try:
            return quick_file_sha1(p).lower() == cached_sha
        except Exception:
            return False

    if cached_mtime is not None:
        return abs(float(st.st_mtime) - float(cached_mtime)) <= 0.1

    # No matching criteria - assume match
    return True


def signature_changed(before: dict | None, after: dict | None) -> bool:


    if before is None or after is None:
        return True
    _b_sha = before.get("sha1")
    _a_sha = after.get("sha1")
    if _b_sha and _a_sha:
        return _b_sha != _a_sha
    _b_m = before.get("mtime")
    _a_m = after.get("mtime")
    _b_s = before.get("size")
    _a_s = after.get("size")
    if _b_m is not None and _a_m is not None and _b_s is not None and _a_s is not None:
        return not (abs(_a_m - _b_m) <= 0.1 and _a_s == _b_s)
    return True


def resolve_explore_reset(round_tool_names: set, bash_changed, current_value: int) -> int:


    if round_tool_names == {"run_bash"} and bash_changed is not None:
        return 0 if bash_changed else current_value
    return 0


def compute_bash_changed(snap_before: dict | None, snap_after: dict | None) -> bool | None:


    if not snap_before or snap_after is None:
        return None
    return any(signature_changed(b, snap_after.get(p)) for p, b in snap_before.items())


# ═══════════════════════════════════════════════════════════════════════════════
#  Resume helpers — pure functions for chunk serialisation / deserialisation
# ═══════════════════════════════════════════════════════════════════════════════

def _push_unique_text(target: list[str], seen: set[str], raw: str):
    s = str(raw or "").strip().strip('"').strip("'")
    if not s or s in seen:
        return
    seen.add(s)
    target.append(s)


def _extract_path_hints_from_text(text: str) -> list[str]:
    found: list[str] = []
    for _m in re.finditer(r"\bfile\s*:\s*([^|\n]+)", text or "", re.IGNORECASE):
        found.append(_m.group(1))
    found.extend(_m.group(0) for _m in _RE_PATH_KEY.finditer(text or ""))
    found.extend(_m.group(0) for _m in _RE_REL_PATH_HINT.finditer(text or ""))
    return found


def chunk_hint_sources(chunk) -> tuple[list[str], str]:
    """Extract files_hint list and text blob from a chunk dict or string."""
    if isinstance(chunk, dict):
        raw_hints: list[str] = []
        _fh = chunk.get("files_hint")
        if isinstance(_fh, list):
            raw_hints = [str(x) for x in _fh]
        elif isinstance(_fh, str):
            raw_hints = [x for x in re.split(r"[,;\n]+", _fh) if str(x).strip()]
        blob = " | ".join(str(chunk.get(k, "")) for k in ("title", "prompt"))
        return raw_hints, blob
    return [], str(chunk)


def extract_chunk_file_hints(chunks_remaining: list[dict]) -> list[str]:
    """Collect all unique file hints from remaining chunks."""
    hints: list[str] = []
    seen: set[str] = set()

    for _chunk in chunks_remaining or []:
        _raw_hints, _blob = chunk_hint_sources(_chunk)
        for _it in _raw_hints:
            _push_unique_text(hints, seen, _it)
        for _it in _extract_path_hints_from_text(_blob):
            _push_unique_text(hints, seen, _it)

    return hints


def normalize_resume_candidate_path(raw: str, workspace_root: Path | None) -> str | None:
    """Normalise and validate a file path from a resume block."""
    _s = str(raw or "").strip().strip('"').strip("'")
    if not _s:
        return None
    p = Path(_s)
    if not p.is_absolute() and workspace_root:
        p = (workspace_root / p).resolve()
    else:
        p = p.resolve()
    if workspace_root:
        try:
            p.relative_to(workspace_root)
        except Exception:
            return None
    return str(p)


def collect_resume_file_snapshot(workspace: str, written_files: list[str], chunks_remaining: list[dict]) -> dict:
    """Build a file-signature snapshot for resume validation."""
    ws = Path(workspace).resolve() if workspace else None
    out: dict[str, Any] = {}
    candidates: set[str] = set()

    for _wf in written_files or []:
        _p = normalize_resume_candidate_path(_wf, ws)
        if _p:
            candidates.add(_p)
    for _hint in extract_chunk_file_hints(chunks_remaining or []):
        _p = normalize_resume_candidate_path(_hint, ws)
        if _p:
            candidates.add(_p)

    for _p in candidates:
        _sig = build_file_signature(_p)
        if _sig:
            out[_p] = _sig
    return out


def normalize_resume_chunk(chunk) -> dict | None:
    """Normalise a single chunk from a resume block into a standard dict."""
    if isinstance(chunk, dict):
        title = str(chunk.get("title") or "").strip()
        prompt = str(chunk.get("prompt") or "").strip()
        if not title:
            title = prompt[:180]
        if not title:
            return None
        return {
            "title": title,
            "prompt": prompt,
            "files_hint": chunk.get("files_hint", []),
        }
    title = str(chunk or "").strip()
    if not title:
        return None
    return {"title": title, "prompt": "", "files_hint": []}


def normalize_resume_chunks(raw_chunks: list) -> list[dict]:
    """Normalise a list of chunks from a resume block."""
    norm: list[dict] = []
    for _chunk in raw_chunks or []:
        _item = normalize_resume_chunk(_chunk)
        if _item:
            norm.append(_item)
    return norm


def resume_block_is_valid(resume_block: dict) -> bool:
    """Check if a resume block has the minimum required structure."""
    if not isinstance(resume_block, dict):
        return False
    try:
        chunks_total = int(resume_block.get("chunks_total", 0) or 0)
    except Exception:
        return False
    if chunks_total <= 0 or chunks_total > 300:
        return False
    chunks_done = resume_block.get("chunks_done", [])
    chunks_remaining = resume_block.get("chunks_remaining", [])
    if not isinstance(chunks_done, list) or not isinstance(chunks_remaining, list):
        return False
    if not chunks_remaining:
        return False
    if chunks_total < (len(chunks_done) + len(chunks_remaining)):
        return False
    return True


def validate_resume_files(ctx_files: dict, workspace: str) -> bool:
    """Check that all files in a resume snapshot still match on disk.

    Returns False if any file has been modified since the snapshot was taken,
    which would make the resume unsafe.
    """
    for _p, _cached in ctx_files.items():
        if not file_signature_matches(_p, _cached, allow_missing=False):
            logger.warning("[resume] File snapshot mismatch: %s — resume discarded", _p)
            return False
    return True


def load_resume_data(chat_context: dict) -> dict | None:
    """Load and validate resume data from a chat context dict.

    This is a pure-data function — server.py's _load_chat_context() must
    fetch the raw context first, then pass it here for validation.

    Returns None if resume is not possible or invalid.
    """
    r = chat_context.get("resume")
    if not r:
        return None
    if not resume_block_is_valid(r):
        logger.warning("[resume] Invalid resume block shape — discarded")
        return None
    ws = chat_context.get("workspace", "")
    if ws and not Path(ws).is_dir():
        logger.warning("[resume] Workspace gone: %s — resume discarded", ws)
        return None

    _ctx_files = chat_context.get("files", {}) if isinstance(chat_context.get("files", {}), dict) else {}
    if not validate_resume_files(_ctx_files, ws):
        return None

    _norm_chunks = normalize_resume_chunks(r.get("chunks_remaining", []))
    if not _norm_chunks:
        return None
    r = dict(r)
    r["chunks_remaining"] = _norm_chunks
    return {"resume": r, "workspace": ws, "explore_ctx": chat_context.get("explore_ctx", "")}


def build_resume_block(
    workspace: str,
    chunks_total: int,
    chunks_done: list[str],
    chunks_remaining: list[dict],
    written_files: list[str],
    last_summary: str,
    plan_msgs: list,
    explore_ctx: str,
) -> dict:
    """Build a resume block dict suitable for writing to chat context.

    Returns the full dict to be stored under the "resume" key in the
    chat context. The caller (server.py) is responsible for writing
    it via _mutate_chat_context().
    """
    _resume_files = collect_resume_file_snapshot(workspace, written_files, chunks_remaining)
    _resume_chunks = normalize_resume_chunks(chunks_remaining)
    return {
        "workspace": workspace,
        "explore_ctx": explore_ctx,
        "ts": time.time(),
        "files": _resume_files,
        "resume": {
            "chunks_total": chunks_total,
            "chunks_done": chunks_done,
            "chunks_remaining": _resume_chunks,
            "written_files": written_files,
            "last_summary": last_summary,
            "plan_msgs": plan_msgs,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  build_chunk_context
# ═══════════════════════════════════════════════════════════════════════════════

def build_chunk_context(
    user_input: str,
    explore_ctx: str,
    done_tasks: list[str],
    written_files: list[str],
    subtask: str,
    di: int,
    n_items: int,
    critic_issues: list[str],
    all_subtasks: list[str] | None = None,
    pass_explore_files: str = "none",
    explore_cap: int = 8000,
    static_map_chars: int = 0,
    known_files: list[str] | None = None,
) -> str | None:


    if not subtask or not subtask.strip():
        logger.warning("[CHUNKING] Chunk %s/%s has empty subtask — skipping context build", di + 1, n_items)
        return None

    _ctx = [
        f"SUBTASK {di+1} OF {n_items} — IMPLEMENT ONLY THIS SUBTASK"
    ]

    # Full plan overview with status badges — local models need this for orientation
    if all_subtasks and len(all_subtasks) > 1:
        _plan_lines = []
        for i, st in enumerate(all_subtasks):
            if i < di:
                _plan_lines.append(f"  {i+1}. \u2713 {st}")
            elif i == di:
                _plan_lines.append(f"  {i+1}. \u2192 {st}  \u25c0 YOU ARE HERE")
            else:
                _plan_lines.append(f"  {i+1}. \u25cb {st}")
        _ctx.append("FULL PLAN:\n" + "\n".join(_plan_lines))

    _ctx.append(f"OVERALL GOAL: {user_input}")

    if explore_ctx:
        if pass_explore_files != "none":
            _explore_rule = (
                "PRE-EXPLORED: File contents below are already confirmed from workspace.\n"
                "If a file's full content is shown above, edit it directly — do NOT re-read it.\n"
                "Only call read_file for files NOT included in the pre-exploration results above.\n\n"
            )
        else:
            _explore_rule = (
                "CRITICAL RULE: The codebase analysis below contains SUMMARIES only — NOT full source code. "
                "You MUST call read_file on ANY file you plan to edit BEFORE using edit_file or patch_file. "
                "NEVER edit a file based on the summary alone — you will produce wrong code.\n\n"
            )
        _EXPLORE_CTX_CAP = explore_cap  # CTX-AWARE (2026-08-12)
        if len(explore_ctx) > _EXPLORE_CTX_CAP:
            explore_ctx = budget_explore_window(explore_ctx, _EXPLORE_CTX_CAP, static_map_chars)
        _ctx.append(f"[Codebase Analysis]:\n{_explore_rule}{explore_ctx}")
    if known_files:
        _ctx.append(
            "[Known files — preferred paths for this chunk]\n"
            + "\n".join(f"  - {f}" for f in known_files)
        )
    if done_tasks:
        _ctx.append("Already completed:\n" + "\n".join(f"  \u2713 {t}" for t in done_tasks))
    if written_files:
        _ctx.append("Files already on disk: " + ", ".join(written_files))
    if critic_issues and di > 0:
        _ctx.append("Issues to fix:\n" + "\n".join(f"  - {i}" for i in critic_issues))
    _ctx.append(f"YOUR SUBTASK ({di+1}/{n_items}):\n{subtask}")
    return "\n\n".join(_ctx)


# ═══════════════════════════════════════════════════════════════════════════════
#  run_chunk_auto_test
# ═══════════════════════════════════════════════════════════════════════════════

async def run_chunk_auto_test(
    workspace: str,
    di: int,
    n_items: int,
    emit_fn,
    test_timeout: int = 90,
    chat_id: str = "",
) -> tuple["_TestResult", bool]:
    """Run the auto-test suite after a chunk completes.

    Emits status and test_result events via *emit_fn*.

    Parameters
    ----------
    workspace : str
        Absolute path to the workspace directory.
    di : int
        0-based chunk index.
    n_items : int
        Total number of chunks.
    emit_fn : coroutine
        The emit function for streaming events to the client.
    test_timeout : int
        Timeout in seconds for the test suite run.

    Returns
    -------
    tuple[TestResult, bool]
        (test_result, is_clean) — *is_clean* is ``True`` when all tests pass.
    """
    _run_tests, _TestResult = await _get_test_runner()

    await emit_fn({"type": "status",
        "content": f"\U0001f9ea Verifying chunk {di+1}/{n_items}..."})
    try:
        _test_result = await _run_tests(
            workspace=workspace,
            timeout=test_timeout,
            chat_id=chat_id,
        )
    except Exception as _cte:
        _test_result = _TestResult(
            success=False, language="unknown", command="",
            failure_count=0, error_lines=[str(_cte)],
            raw_output="",
            inject_msg=f"\u26a0\ufe0f Test runner error: {_cte}"
        )
    await emit_fn({
        "type": "test_result",
        "chunk": di + 1,
        "total_chunks": n_items,
        "passed": _test_result.is_clean(),
        "language": _test_result.language,
        "failures": _test_result.failure_count,
        "command": _test_result.command,
    })
    return _test_result, _test_result.is_clean()


# ═══════════════════════════════════════════════════════════════════════════════
#  ChunkState — mutable state for a chunked run
# ═══════════════════════════════════════════════════════════════════════════════

# Action returned by handle_auto_test_result()
class ChunkAction:
    """Enum-like class for chunk loop actions."""
    CONTINUE = "continue"       # Re-process same chunk (re-awaken fix)
    INCREMENT = "increment"     # Move to next chunk
    BREAK = "break"             # Last chunk done, exit loop


@dataclass
class ChunkState:
    """Mutable state tracking progress through a chunked pipeline run.

    Replaces the loose local variables that were scattered through
    server.py's main while-loop.

    Usage
    -----
    1. Create at the start of the chunking run.
    2. Call ``prepare_coder_input()`` for each chunk iteration.
    3. After coder runs, call ``record_written_file()`` / ``record_chunk_output()``.
    4. Call ``mark_chunk_done()`` when the chunk is approved.
    5. Call ``handle_auto_test_result()`` after auto-test — returns an action.
    6. At the end, call ``combine_outputs()`` for Synthesis.
    """

    # Tracked state
    done_tasks: list[str] = field(default_factory=list)
    written_files: list[str] = field(default_factory=list)
    coder_outputs: list[str] = field(default_factory=list)
    completion_notes: list[str] = field(default_factory=list)

    # Self-fix / re-awaken state
    test_retries: int = 0
    max_test_retries: int = 2   # 2 retries = 3 total attempts (increased from 1)
    fix_override: str | None = None   # Set by auto-test retry or critic rejection
    rerun_without_fix: bool = False   # Set when flaky test should be re-run without LLM call

    # Cross-chunk cleanup: critic_issues should NOT carry over between chunks.
    # Each chunk starts with a clean slate.
    critic_issues: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    #  Coder input preparation
    # ------------------------------------------------------------------ #

    def prepare_coder_input(
        self,
        user_input: str,
        explore_ctx: str,
        subtask: str | None,
        di: int,
        n_items: int,
        all_subtasks: list[str] | None = None,
        pass_explore_files: str = "none",
        explore_cap: int = 8000,
        static_map_chars: int = 0,
        known_files: list[str] | None = None,
    ) -> str:
        """Build the coder input for the current chunk.

        If ``fix_override`` is set (from a previous auto-test failure
        or critic rejection), it takes precedence over the normal
        ``build_chunk_context()`` output.

        Parameters
        ----------
        user_input : str
            The overall task from the user.
        explore_ctx : str
            Pre-exploration context.
        subtask : str | None
            The current subtask description, or None for non-chunking mode.
        di : int
            0-based chunk index.
        n_items : int
            Total number of chunks.
        all_subtasks : list[str] | None
            The full subtask list for the complete plan overview.

        Returns
        -------
        str
            The coder input prompt.
        """
        if self.fix_override:
            _override = self.fix_override
            self.fix_override = None
            return _override

        if not subtask:
            # Non-chunking mode — caller builds its own input
            return ""

        return build_chunk_context(
            user_input=user_input,
            explore_ctx=explore_ctx,
            done_tasks=self.done_tasks,
            written_files=self.written_files,
            subtask=subtask,
            di=di,
            n_items=n_items,
            critic_issues=self.critic_issues,
            all_subtasks=all_subtasks,
            pass_explore_files=pass_explore_files,
            explore_cap=explore_cap,
            static_map_chars=static_map_chars,
            known_files=known_files,
        )

    # ------------------------------------------------------------------ #
    #  File / output tracking
    # ------------------------------------------------------------------ #

    def record_written_file(self, filepath: str):
        """Track a file that was written to disk."""
        if filepath and filepath not in self.written_files:
            self.written_files.append(filepath)

    def record_chunk_output(self, output: str):
        """Store the coder output for a chunk (for Synthesis)."""
        self.coder_outputs.append(output)

    # ------------------------------------------------------------------ #
    #  Chunk lifecycle
    # ------------------------------------------------------------------ #

    def mark_chunk_done(self, subtask: str):
        """Mark a subtask as completed and clear cross-chunk state."""
        if subtask and subtask not in self.done_tasks:
            self.done_tasks.append(subtask)
        # CROSS-CHUNK-CLEANUP: critic_issues of the completed chunk
        # must NOT leak into the next chunk's context.
        self.critic_issues = []

    # ------------------------------------------------------------------ #
    #  Auto-test self-fix logic
    # ------------------------------------------------------------------ #

    async def run_auto_test(
        self,
        workspace: str,
        di: int,
        n_items: int,
        emit_fn,
        test_timeout: int = 90,
        chat_id: str = "",
    ) -> tuple["_TestResult", bool]:
        """Run auto-test for the current chunk. Delegates to run_chunk_auto_test()."""
        return await run_chunk_auto_test(
            workspace=workspace,
            di=di,
            n_items=n_items,
            emit_fn=emit_fn,
            test_timeout=test_timeout,
            chat_id=chat_id,
        )

    def handle_auto_test_result(
        self,
        test_result: "_TestResult",
        subtask: str | None,
        di: int,
        n_items: int,
        emit_fn,
        chat_id: str = "",
    ) -> str:
        """Process auto-test result and determine the next loop action.

        This encapsulates the re-awaken / retry / completion-note pattern
        that was previously duplicated in server.py (agentic + non-agentic).

        Parameters
        ----------
        test_result : TestResult
            The test result from run_auto_test().
        subtask : str | None
            The current subtask description.
        di : int
            0-based chunk index.
        n_items : int
            Total number of chunks.
        emit_fn : coroutine
            The emit function for streaming events.

        Returns
        -------
        str
            One of ChunkAction.CONTINUE / INCREMENT / BREAK.

        Side effects
        -------------
        - On test pass: clears ``test_retries``, no override set.
        - On test fail with retries left: increments ``test_retries``,
          removes subtask from ``done_tasks``, sets ``fix_override``.
        - On test fail with no retries: appends to ``completion_notes``,
          clears ``test_retries``.
        """
        import asyncio  # local import to avoid top-level overhead

        _is_last = (di == n_items - 1)

        if test_result.is_clean():
            # Tests passed — chunk is truly done
            # Emit is done by caller (server.py) since emit is async
            self.test_retries = 0
            if _is_last:
                return ChunkAction.BREAK
            return ChunkAction.INCREMENT

        # Tests failed
        from hive_functions.test_runner import classify_test_failure
        from context.chat import _load_chat_context
        _class = classify_test_failure(
            test_result.raw_output,
            test_result.language,
            chat_history=_load_chat_context(chat_id) if chat_id else None,
        )

        if _class["type"] == "dependency" and _class["confidence"] > 0.8:
            # Dependency error — tell coder to install, then rerun test (no retry count)
            from hive_functions.test_runner import _extract_missing_package
            _pkg = _extract_missing_package(test_result.raw_output, test_result.language)
            if subtask and subtask in self.done_tasks:
                self.done_tasks.remove(subtask)
            self.fix_override = (
                f"[AUTO-INSTALL] Missing dependency: {_pkg or 'unknown'}\n"
                f"Run the install command for this package first, "
                f"then rerun the tests.\n\n"
                f"Raw error:\n{test_result.inject_msg}"
            )
            return ChunkAction.CONTINUE
        elif _class["type"] == "compile" and _class["confidence"] > 0.8:
            # Compilation error — inject file:line + error, request fix
            _files = ", ".join(_class["affected_files"][:3]) or "unknown"
            if subtask and subtask in self.done_tasks:
                self.done_tasks.remove(subtask)
            self.fix_override = (
                f"[COMPILE ERROR] in {_files}:\n"
                f"{test_result.inject_msg}\n\n"
                f"Fix the compilation error above, then rerun tests."
            )
            return ChunkAction.CONTINUE
        elif _class["type"] == "flaky" and self.test_retries < 2:
            # Flaky test — rerun without LLM call, don't increment retry counter
            self.rerun_without_fix = True
            self.test_retries += 1  # track for limit but count separately
            return ChunkAction.CONTINUE

        # logic / flaky / timeout / unknown — use retry counter
        if self.test_retries < self.max_test_retries:
            self.test_retries += 1
            if subtask and subtask in self.done_tasks:
                self.done_tasks.remove(subtask)
            self.fix_override = (
                f"Tests FAILED for chunk '{subtask}':\n"
                f"{test_result.inject_msg}\n\n"
                f"Fix the test failures above. After fixing, rerun tests."
            )
            return ChunkAction.CONTINUE
        else:
            # Max retries reached — mark as completion note
            self.completion_notes.append(
                f"Chunk {di+1}: {test_result.failure_count} test(s) still failing ({test_result.language})"
            )
            self.test_retries = 0
            if _is_last:
                return ChunkAction.BREAK
            return ChunkAction.INCREMENT

    # ------------------------------------------------------------------ #
    #  Critic rejection fix override
    # ------------------------------------------------------------------ #

    def set_critic_fix_override(self, subtask: str, issues: list[str]):
        """Create a fix override from critic rejection issues.

        This is used when a chunk is rejected by the critic (non-agentic
        mode) and needs a targeted fix round instead of cross-chunk
        issue injection.
        """
        if issues:
            _fix_block = "\n".join(f"{i+1}. {iss}" for i, iss in enumerate(issues))
            self.fix_override = (
                f"Fix the following issues in the code you just wrote for subtask '{subtask}':\n"
                f"{_fix_block}\n\nApply all fixes, then confirm each is resolved."
            )
        self.critic_issues = []

    def set_verification_fix_override(self, subtask: str):
        """Create a fix override for missing verification after file writes."""
        self.fix_override = (
            f"Before marking subtask '{subtask}' done, run verification now. "
            "Execute run_bash with the project test/build command and ensure exit code 0. "
            "Then report what command passed."
        )

    # ------------------------------------------------------------------ #
    #  Combined output for Synthesis
    # ------------------------------------------------------------------ #

    def combine_outputs(self, fallback_coder_out: str = "") -> str:
        """Combine all chunk outputs + completion notes for Synthesis.

        Parameters
        ----------
        fallback_coder_out : str
            If no chunk outputs were recorded, use this as fallback.

        Returns
        -------
        str
            The combined output string for the Synthesis phase.
        """
        combined = (
            "\n\n".join(
                f"--- Step {i+1}: {self.done_tasks[i] if i < len(self.done_tasks) else '?'} ---\n{o}"
                for i, o in enumerate(self.coder_outputs)
            ) if self.coder_outputs else fallback_coder_out
        )
        if self.completion_notes:
            combined += "\n\n--- Open Issues ---\n" + "\n".join(f"\u26a0 {n}" for n in self.completion_notes)
        return combined

    # ------------------------------------------------------------------ #
    #  Resume state extraction
    # ------------------------------------------------------------------ #

    def load_from_resume(self, resume_data: dict):
        """Restore state from a loaded resume block.

        Parameters
        ----------
        resume_data : dict
            The dict returned by ``load_resume_data()``.
        """
        _rb = resume_data["resume"]
        self.done_tasks = list(_rb.get("chunks_done", []))
        self.written_files = list(_rb.get("written_files", []))

    def get_remaining_items(self, loop_items: list, current_di: int) -> list[dict]:
        """Build the chunks_remaining list for a resume save.

        Parameters
        ----------
        loop_items : list
            The full _loop_items list (subtasks or [None]*rounds).
        current_di : int
            Current 0-based index — items from here onward are remaining.

        Returns
        -------
        list[dict]
            Remaining items as [{"title": ...}] dicts.
        """
        return [{"title": str(t)} for t in loop_items[current_di:]]

    # ------------------------------------------------------------------ #
    #  Reset helpers
    # ------------------------------------------------------------------ #

    def reset_test_retries(self):
        """Reset the test retry counter (typically between chunks)."""
        self.test_retries = 0
        self.rerun_without_fix = False

    @property
    def has_written_files(self) -> bool:
        """True if at least one file has been written to disk."""
        return bool(self.written_files)

    @property
    def is_chunking(self) -> bool:
        """True if we're in chunking mode (subtasks were set)."""
        return bool(self.done_tasks) or bool(self.coder_outputs)

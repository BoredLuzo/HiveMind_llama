"""Chat-run history seed (2026-11-06 split 3.3).

The session/history bootstrap of run_stream: the per-chat json/sidecar
seed (re-seeded every run), long-term memory context, the token-budget
session window and the session compression trigger. Verbatim code
motion — no logic changes. Outputs flow back through `ps`.
"""
from __future__ import annotations

import dataclasses
import logging

from context.compression import SESSION_COMPRESS_THRESHOLD, _compress_chat_session
from core import state as _state
from core.state import settings
from utils.token import window_by_budget

logger = logging.getLogger("hivemind.chat_run")
_logger = logger


@dataclasses.dataclass
class _ChatHistorySeedState:
    """History-seed in/out (split 3.3)."""
    chat_id: str | None = None
    mode: str = ""
    duo_config: object = None
    _pipeline_mem_ctx: str = ""
    _pipeline_sess_msgs: list = dataclasses.field(default_factory=list)
    _sess_compress_threshold: int = 0


async def _chat_history_seed(ps):
    from core.chat_run import _history_budget_tokens
    chat_id = ps.chat_id
    mode = ps.mode
    duo_config = ps.duo_config
    # HISTORY SOURCE (2026-10-03): the chat json transcript is the ONE
    # continuation source - UI and model now share the same story. The
    # sidecar "session" key demotes to a fallback for chats without a json.
    # RE-SEED EVERY RUN (2026-10-04): the chat-switch tag
    # alone only fixed A->B. Same-chat EDIT/REGENERATE kept the discarded
    # history in memory (same chat_id -> no clear -> seed skips a filled
    # memory). The json is ALWAYS fresher than the memory (the frontend PUTs
    # every turn), so whenever a chat json exists it is re-seeded; the memory
    # stays a fallback for chats without json, tagged per chat, and a new
    # chat without id never inherits anything.
    # LABEL-LIE FIX (2026-10-05, TG-PROBE experiment): history_seed() falls
    # back to the sidecar INTERNALLY while this probe labelled the result
    # 'source=json' - a live run on an empty-main-json chat logged
    # 'source=json seeded 2' that was actually sidecar history. The
    # provenance probe decides the label; history_seed only normalises.
    _json_seed: list = []
    _seed_source = "none"
    if chat_id:
        try:
            from context.chat import history_seed_provenance as _seed_prov
            _raw_seed, _raw_src = _seed_prov(chat_id)
        except (OSError, ValueError):
            _raw_seed, _raw_src = [], "none"
        # BRANCH ON ORIGIN, NOT ON SEED EMPTINESS (2026-10-05, edge
        # case): a main json holding ONLY the edited trailing user message
        # normalizes to an empty seed - falling back on emptiness would
        # resurrect the discarded sidecar/memory history. json/sidecar own
        # the history: clear ALWAYS, seed whatever normalized (even empty).
        if _raw_src in ("json", "sidecar"):
            _json_seed = _raw_seed
            _seed_source = _raw_src
    _mem_cid = getattr(_state.memory, "_session_chat_id", None)
    if _seed_source in ("json", "sidecar"):
        # ALWAYS-RESEED: the chat files are fresher than the memory - every run.
        _state.memory.clear_session()
        if _json_seed:
            _state.memory.seed_session(_json_seed)
        try:
            _state.memory._session_chat_id = chat_id or ""
        except AttributeError:
            pass
        _logger.info("[HISTORY-SEED] chat=%s source=%s seeded %d message(s)",
                     chat_id, _seed_source, len(_json_seed))
    else:
        # no turns anywhere (new chat, or only a trailing user that IS the q):
        # never inherit another chat's history - clear unless the memory is
        # already tagged for THIS chat.
        if _mem_cid != (chat_id or ""):
            _state.memory.clear_session()
            try:
                _state.memory._session_chat_id = chat_id or ""
            except AttributeError:
                pass
        elif not chat_id and _state.memory.get_session_messages():
            _state.memory.clear_session()
            try:
                _state.memory._session_chat_id = ""
            except AttributeError:
                pass

    _pipeline_mem_ctx  = _state.pipeline.memory.as_context_string()
    # Caps raised to sidecar parity (8000 chars): 600/1200 used to cut code
    # answers mid-line. The window is message-count-capped via seed (40) and
    # compression above the threshold handles older turns.
    _pipeline_sess_msgs = _state.memory.get_session_messages(
        limit=40, user_cap=8000, assistant_cap=8000,
    )
    _sess_before_window = len(_pipeline_sess_msgs)
    # TOKEN BUDGET (2026-10-03): 40x8000 chars can dwarf a small ctx -
    # the window keeps the newest turns and drops older ones whole.
    _pipeline_sess_msgs = window_by_budget(
        _pipeline_sess_msgs, _history_budget_tokens(settings),
    )  # SESSION-CACHE: 8 DB-Calls - 1 pro Run
    # WINDOW TRACE (2026-10-04): the seed count alone does not prove
    # the budget reached the window - log before/after so run C's expected N
    # (window_by_budget offline) can be checked against the log.
    _logger.info("[SESSION-WINDOW] before=%d after=%d budget=%d",
                 _sess_before_window, len(_pipeline_sess_msgs),
                 _history_budget_tokens(settings))
    # Threshold: SESSION_COMPRESS_THRESHOLD messages (configurable via settings).
    _sess_compress_threshold = int(settings.get("session_compress_threshold", SESSION_COMPRESS_THRESHOLD))
    _skip_compress = (mode == "code_duo" or duo_config.agentic_mode)
    if not _skip_compress and len(_pipeline_sess_msgs) > _sess_compress_threshold:
        _pipeline_sess_msgs = await _compress_chat_session(_pipeline_sess_msgs)


    # Natural agent routing is controlled by the Intent-Agent toggle.

    # ── sync back: everything the runner consumes after this block ──
    ps._pipeline_mem_ctx = _pipeline_mem_ctx
    ps._pipeline_sess_msgs = _pipeline_sess_msgs
    ps._sess_compress_threshold = _sess_compress_threshold

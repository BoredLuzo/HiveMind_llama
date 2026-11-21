"""Chat-Context-Persistenz (aus server.py extrahiert)."""
from __future__ import annotations
from html.parser import HTMLParser
import json, logging, os, re, threading, time
from pathlib import Path
from typing import Callable
from utils.file import write_json_atomic as _write_json_atomic

logger = logging.getLogger("hivemind.chat_context")
_chat_ctx_locks: dict[str, threading.Lock] = {}
_chat_ctx_locks_guard = threading.Lock()
_cache_lock: threading.Lock = threading.Lock()
_chats_cache: dict = {}
_SESSIONS_DIR: Path | None = None
# Injected by init_chat_context(); None until then (same pattern as
# _SESSIONS_DIR above).
_file_signature_matches: Callable[..., bool] | None = None
_build_file_signature: Callable[..., object] | None = None

def init_chat_context(sessions_dir, chats_cache=None, cache_lock=None,
                      file_signature_matches=None, build_file_signature=None):
    global _SESSIONS_DIR, _chats_cache, _cache_lock
    global _file_signature_matches, _build_file_signature
    _SESSIONS_DIR = Path(sessions_dir)
    if chats_cache is not None: _chats_cache = chats_cache
    if cache_lock is not None: _cache_lock = cache_lock
    if file_signature_matches: _file_signature_matches = file_signature_matches
    if build_file_signature: _build_file_signature = build_file_signature


def _read_chat_json(chat_id: str) -> dict | None:
    try:
        if not _SESSIONS_DIR:
            return None
        matches = sorted(
            m for m in _SESSIONS_DIR.glob(f"*_{chat_id}.json")
            # SIDECAR EXCLUSION (2026-10-05, transcript experiment): the
            # sidecar `<...>.context.json` ALSO matches the glob (it ends in
            # `_<id>.json`) and sorts LAST alphabetically - so matches[-1]
            # read the weak sidecar instead of the main chat json whenever
            # both existed (every UI-PUT chat). The transcript (history
            # seed) must come from the MAIN json only.
            if not m.name.endswith(".context.json")
        )
        if not matches:
            return None
        return json.loads(matches[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _normalize_seed(seed: list) -> list:
    """Drop ALL trailing user messages (the newest is the new prompt itself,
    an aborted run's unanswered attempt before it would break alternation)
    and merge consecutive same-role messages (strict gemma/mistral
    templates reject two user roles in a row)."""
    while seed and seed[-1].get("role") == "user":
        seed = seed[:-1]
    merged: list = []
    for m in seed:
        if merged and merged[-1].get("role") == m.get("role"):
            merged[-1] = dict(merged[-1])
            merged[-1]["content"] = (merged[-1].get("content", "") + "\n" + m.get("content", "")).strip()
        else:
            merged.append(m)
    return merged


def history_seed_provenance(chat_id: str, limit: int = 40) -> tuple:
    """(seed, source) with HONEST provenance (2026-10-05, label-lie fix).

    The previous history_seed() fell back to the sidecar INTERNALLY while
    chat_run labelled whatever it returned as source=json - the label lied
    whenever the main json transcript was empty (live TG-PROBE experiment:
    'source=json seeded 2' was sidecar history). Callers that need the
    source use this probe; history_seed() stays for compatibility.
    source is 'json' ONLY when the MAIN chat json carried the transcript,
    'sidecar' when the fallback fired, 'none' when neither had turns.
    The seed is NORMALIZED identically to history_seed (trailing-user drop
    + merge), so the logged N is the post-normalization count."""
    seed = load_chat_transcript(chat_id, limit=limit)  # MAIN json only
    if seed:
        return _normalize_seed(seed), "json"
    try:
        side = (_load_chat_context(chat_id) or {}).get("session") or []
    except (OSError, ValueError):
        side = []
    if side:
        return _normalize_seed(side[-limit:]), "sidecar"
    return [], "none"


def history_seed(chat_id: str, limit: int = 40) -> list:
    """Seed for a NEW run (2026-10-03): the saved conversation, minus a
    TRAILING user message - that message is the new prompt itself and
    arrives via the run, so seeding it would duplicate it. Falls back to
    the sidecar "session" for chats without a json.
    NOTE (2026-10-05): the fallback is INVISIBLE here - callers that report
    a source must use history_seed_provenance()."""
    seed = load_chat_transcript(chat_id, limit=limit)
    if not seed:
        try:
            seed = (_load_chat_context(chat_id) or {}).get("session") or []
        except (OSError, ValueError):
            seed = []
    return _normalize_seed(seed)


def get_chat_workspace(chat_id: str) -> str:
    """Top-level workspace stored in the chat json ("" when absent)."""
    data = _read_chat_json(chat_id)
    return str((data or {}).get("workspace") or "")


_BLOB_MAGIC = b"HIVEBLOB1\n"
_BLOB_ID_RE = re.compile(r"^[0-9a-f]{16}$")

_HTML_CHROME_CLASSES = frozenset({
    "tool-call-chip", "tool-call-row", "tool-result-block", "tool-result-pre",
    "tool-act", "tc-extra", "tc-extra-pre", "tc-pruned-notice", "tg-card",
    "think-block", "think-body", "planner-think-block", "pre-call-reasoning",
    "amsg-foot", "msg-speak", "duo-verdict", "critic-box", "img-desc-box",
    "budget-bar", "ctx-cut", "files-summary", "status-txt", "pload",
    "steer-note", "fc-preview", "repair-badge",
})
_HTML_SKIP_TAGS = frozenset({"details", "script", "style"})
_HTML_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})
_HTML_BLOCK_TAGS = frozenset({
    "p", "div", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "table", "section", "article",
})
_LANG_CLASS_RE = re.compile(r"\blang-([a-zA-Z0-9_+#.-]+)\b")
_DOM_TOOL_CHIP_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF]"
    r"(?:web_search|web_fetch|read_file|write_file|write_file_append|"
    r"edit_file|patch_file|replace_lines|edit_ast|run_bash|run_tests|"
    r"run_python|install_package|find_files|find_references|search_code|"
    r"get_signatures|list_dir|git_commit|git_status|task_complete|"
    r"start_background|get_background_output|stop_background|"
    r"mcp_[a-zA-Z0-9_]+)\s*\u00b7\s*"
)
_UNTRUSTED_BLOCK_RE = re.compile(
    r"\[UNTRUSTED CONTENT - source:[^\]]*\][\s\S]*?\[END UNTRUSTED CONTENT\]"
)


class _AnswerHtmlExtractor(HTMLParser):
    """Extract clean assistant answer text from saved `.abody` HTML, skipping UI chrome."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_pre = False
        self._pre_lang = ""
        self._pre_buf: list[str] = []
        self._in_tr = False
        self._in_cell = False
        self._tr_cells: list[str] = []
        self._cell_buf: list[str] = []
        self._out: list[str] = []

    def _push_nl(self) -> None:
        if self._out and not self._out[-1].endswith("\n"):
            self._out.append("\n")

    def handle_starttag(self, tag: str, attrs) -> None:
        t = tag.lower()
        if t in _HTML_VOID_TAGS:
            if self._skip_depth == 0:
                if self._in_pre:
                    if t == "br":
                        self._pre_buf.append("\n")
                elif self._in_cell:
                    if t == "br":
                        self._cell_buf.append(" ")
                elif t == "br":
                    self._out.append("\n")
                elif t == "hr":
                    self._push_nl()
                    self._out.append("---\n")
            return
        if self._skip_depth > 0:
            self._skip_depth += 1
            return
        attr_map = dict(attrs)
        cls_raw = str(attr_map.get("class") or "")
        cls_set = set(cls_raw.split()) if cls_raw else set()
        if t in _HTML_SKIP_TAGS or (cls_set & _HTML_CHROME_CLASSES):
            self._skip_depth = 1
            return
        if t == "pre":
            self._in_pre = True
            m = _LANG_CLASS_RE.search(cls_raw)
            self._pre_lang = m.group(1) if m else ""
            self._pre_buf = []
            return
        if self._in_pre:
            return
        if t == "tr":
            self._in_tr = True
            self._tr_cells = []
            return
        if self._in_tr and t in ("td", "th"):
            self._in_cell = True
            self._cell_buf = []
            return
        if t in _HTML_BLOCK_TAGS:
            self._push_nl()

    def handle_startendtag(self, tag: str, attrs) -> None:
        t = tag.lower()
        if self._skip_depth > 0:
            return
        if t == "br":
            if self._in_pre:
                self._pre_buf.append("\n")
            elif self._in_cell:
                self._cell_buf.append(" ")
            else:
                self._out.append("\n")
        elif t == "hr" and not self._in_pre and not self._in_cell:
            self._push_nl()
            self._out.append("---\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in _HTML_VOID_TAGS:
            return
        if self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if t == "pre" and self._in_pre:
            code = "".join(self._pre_buf).replace("\r\n", "\n").rstrip("\n")
            self._push_nl()
            self._out.append(f"```{self._pre_lang}\n{code}\n```\n")
            self._in_pre = False
            self._pre_lang = ""
            self._pre_buf = []
            return
        if self._in_pre:
            return
        if t in ("td", "th") and self._in_cell:
            cell_txt = re.sub(r"\s+", " ", "".join(self._cell_buf)).strip()
            self._tr_cells.append(cell_txt)
            self._in_cell = False
            self._cell_buf = []
            return
        if t == "tr" and self._in_tr:
            if self._tr_cells:
                self._push_nl()
                self._out.append(" | ".join(self._tr_cells) + "\n")
            self._in_tr = False
            self._tr_cells = []
            return
        if t in _HTML_BLOCK_TAGS:
            self._push_nl()

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0 or not data:
            return
        if self._in_pre:
            self._pre_buf.append(data)
        elif self._in_cell:
            self._cell_buf.append(data)
        else:
            self._out.append(data)

    def get_text(self) -> str:
        raw = "".join(self._out).replace("\r\n", "\n")
        raw = re.sub(r"[ \t]+\n", "\n", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _extract_answer_from_html(html_str: str) -> str:
    parser = _AnswerHtmlExtractor()
    parser.feed(html_str)
    parser.close()
    return parser.get_text()


def _read_html_blob(chat_id: str, blob_ref: object) -> str | None:
    if not _SESSIONS_DIR or not isinstance(blob_ref, str) or not _BLOB_ID_RE.fullmatch(blob_ref):
        return None
    try:
        sessions_root = _SESSIONS_DIR.resolve()
        bdir = (_SESSIONS_DIR / f"{chat_id}.blobs").resolve()
        if len(bdir.relative_to(sessions_root).parts) != 1:
            return None
        target = (bdir / blob_ref).resolve()
        if not target.relative_to(bdir).parts or target == bdir:
            return None
        raw = target.read_bytes()
    except (OSError, ValueError, RuntimeError):
        return None
    if raw.startswith(_BLOB_MAGIC):
        return raw[len(_BLOB_MAGIC):].decode("utf-8", errors="replace")
    if os.name == "nt":
        return raw.replace(b"\r\n", b"\n").decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _looks_like_dom_scraped_tool_chrome(text: str) -> bool:
    """Detect legacy assistant `content` scraped from raw `.abody.textContent`."""
    if not text:
        return False
    if "[UNTRUSTED CONTENT - source:" in text and "[END UNTRUSTED CONTENT]" in text:
        return True
    if _DOM_TOOL_CHIP_RE.search(text):
        return True
    if "\U0001F9E0 Thinking" in text and "\u25B2" in text:
        return True
    if "\U0001F4AD\u25B8Reasoning" in text:
        return True
    return False


def _sanitize_assistant_transcript(chat_id: str, msg: dict, content: str) -> str:
    """Strip DOM-scraped tool/think chrome from legacy saved assistant turns.

    Older frontend builds saved `abody.textContent` directly into `content`,
    mixing tool chips (`web_search · ...`), `[UNTRUSTED CONTENT]` dumps and
    inline reasoning ahead of the real answer. That poisoned follow-up turns
    into imitating DOM chrome as plain text and pushed the real answer past
    `assistant_cap`. When chrome markers are present, recover the clean prose
    + code from saved `html` (inline or blob); fall back to stripping raw
    untrusted blocks when no HTML is available.
    """
    if not _looks_like_dom_scraped_tool_chrome(content):
        return content
    raw_html = msg.get("html")
    html_str: str | None = None
    if isinstance(raw_html, str) and raw_html.strip():
        html_str = raw_html
    elif isinstance(raw_html, dict) and "_ref" in raw_html:
        html_str = _read_html_blob(chat_id, raw_html.get("_ref"))
    if html_str:
        return _extract_answer_from_html(html_str)
    cleaned = _UNTRUSTED_BLOCK_RE.sub("", content)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def load_chat_transcript(chat_id: str, limit: int = 40) -> list:
    """USER-VISIBLE transcript as THE model history source (2026-10-03).

    Reads the newest `limit` user/assistant text messages straight from the
    chat json in sessions/. Legacy assistant turns whose `content` was
    DOM-scraped from `.abody.textContent` are cleaned via their saved HTML
    (inline or blob) so tool chips and `[UNTRUSTED CONTENT]` dumps never
    poison follow-up turns. The sidecar "session" key demotes to a fallback
    for chats without a json (or run-state-only sidecars)."""
    data = _read_chat_json(chat_id)
    if not data:
        return []
    out = []
    for m in data.get("messages", []):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        # part=true marks UI-only parts (planner bubble, thinking block,
        # checklist) - they persist for the RELOAD but stay out of the
        # model history so raw thinking does not crowd the token budget.
        if m.get("part"):
            continue
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            if role == "assistant":
                content = _sanitize_assistant_transcript(chat_id, m, content)
                if not content.strip():
                    continue
            out.append({"role": role, "content": content})
    return out[-limit:]


def _get_chat_ctx_lock(chat_id: str) -> threading.Lock:
    """Per-chat lock to serialize context file access."""
    _cid = str(chat_id or "")
    with _chat_ctx_locks_guard:
        _lk = _chat_ctx_locks.get(_cid)
        if _lk is None:
            _lk = threading.Lock()
            _chat_ctx_locks[_cid] = _lk
        return _lk


def _load_chat_context_locked(chat_id: str) -> dict:
    """Internal context loader. Caller must hold per-chat lock."""
    p = _ctx_path_for_chat(chat_id)
    if not p or not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_chat_context_locked(chat_id: str, ctx: dict):
    """Internal context saver. Caller must hold per-chat lock."""
    p = _ctx_path_for_chat(chat_id)
    if not p:
        return
    try:
        _write_json_atomic(p, ctx)
    except Exception as _e:
        logger.warning("Context save failed for %s: %s", p, _e)


def _mutate_chat_context(chat_id: str, mutator):
    """Atomically load-mutate-save context for one chat_id."""
    if not chat_id:
        return
    _lk = _get_chat_ctx_lock(chat_id)
    with _lk:
        _ctx = _load_chat_context_locked(chat_id)
        mutator(_ctx)
        _save_chat_context_locked(chat_id, _ctx)


def _ctx_path_for_chat(chat_id: str) -> Path | None:
    if not chat_id:
        return None
    try:
        with _cache_lock:
            chat = _chats_cache.get(chat_id)
        if chat and chat.get("_file"):
            p = Path(chat["_file"])
            return p.with_suffix(".context.json")
        if _SESSIONS_DIR is None:
            # Not initialized yet (same guard as _read_chat_json).
            return None
        for f in _SESSIONS_DIR.glob(f"*_{chat_id}.json"):
            return f.with_suffix(".context.json")
        # STANDALONE FALLBACK (2026-10-14): API-driven runs have no session
        # file — the path resolved to None and resume-block writes silently
        # no-opped while the log claimed "Block saved" (found in the
        # chunk-resume verify run: hard kill -> resume started from scratch
        # despite the checkpoint). Give every chat_id a durable home; the
        # name deliberately does NOT match the *_{chat_id}.json glob above.
        return _SESSIONS_DIR / f"standalone_{chat_id}.context.json"
    except Exception as _e:
        logger.warning("_ctx_path_for_chat(%s) failed: %s", chat_id, _e)
    return None


def _load_chat_context(chat_id: str) -> dict:
    if not chat_id:
        return {}
    _lk = _get_chat_ctx_lock(chat_id)
    with _lk:
        return _load_chat_context_locked(chat_id)


def _save_chat_context(chat_id: str, ctx: dict):
    if not chat_id:
        return
    _lk = _get_chat_ctx_lock(chat_id)
    with _lk:
        _save_chat_context_locked(chat_id, ctx)


def _chat_context_valid(ctx: dict, explore_ttl: int = 3600) -> bool:
    if not ctx or not ctx.get("workspace") or not ctx.get("explore_ctx"):
        return False
    if not Path(ctx["workspace"]).is_dir():
        return False
    if time.time() - ctx.get("ts", 0) > explore_ttl:
        return False
    _sig_matches = _file_signature_matches
    if _sig_matches is None:
        # Signature checks unavailable -> treat context as unverified.
        return False
    for path_str, cached_mtime in ctx.get("files", {}).items():
        if not _sig_matches(path_str, cached_mtime):
            return False
    return True


def update_test_history(chat_id: str, results: dict[str, str]) -> None:
    """Persist per-test outcomes for flaky detection.

    results = {test_id: "pass"|"fail"}
    Keeps last 8 outcomes per test_id.
    """
    if not chat_id:
        return
    _lk = _get_chat_ctx_lock(chat_id)
    with _lk:
        ctx = _load_chat_context_locked(chat_id) or {}
        hist = ctx.get("test_history", {})
        for tid, outcome in results.items():
            entry = hist.setdefault(tid, {"outcomes": []})
            entry["outcomes"] = (entry["outcomes"] + [outcome])[-8:]
        ctx["test_history"] = hist
        _save_chat_context_locked(chat_id, ctx)


# ── IMAGE CONTEXT (2026-11-04) ────────────────────────────────────────────
# Telegram/UI images used to live only inside one run. The chat transcript is
# text, so the next text turn could not see the photo it referred to. Keep the
# last few images per chat on disk and re-attach them to follow-up text turns.
_IMAGE_CONTEXT_MAX = 4


def _image_ctx_dir(chat_id: str) -> Path | None:
    if not _SESSIONS_DIR or not chat_id:
        return None
    safe = "".join(c for c in str(chat_id) if c.isalnum() or c in "-_")
    if not safe:
        return None
    return Path(_SESSIONS_DIR) / ".images" / safe


def _image_payload(raw: str) -> tuple[bytes, str] | None:
    import base64
    s = str(raw or "").strip()
    if not s:
        return None
    if "," in s[:32]:
        s = s.split(",", 1)[1]
    try:
        data = base64.b64decode(s)
    except (ValueError, TypeError):
        return None
    if not data:
        return None
    ext = "png"
    if data[:2] == b"\xff\xd8":
        ext = "jpg"
    elif data[:4] == b"\x89PNG":
        ext = "png"
    elif data[:4] == b"GIF8":
        ext = "gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = "webp"
    return data, ext


def save_chat_images(chat_id: str, images: list) -> None:
    """Persist the newest image set for a chat (no-op on any error)."""
    folder = _image_ctx_dir(chat_id)
    if folder is None or not images:
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        keep = []
        for idx, raw in enumerate(list(images)[-_IMAGE_CONTEXT_MAX:], 1):
            payload = _image_payload(raw)
            if payload is None:
                continue
            data, ext = payload
            fp = folder / f"ctx_{idx}.{ext}"
            fp.write_bytes(data)
            keep.append(fp.name)
        if not keep:
            return
        for old in folder.glob("ctx_*"):
            if old.name not in keep:
                try:
                    old.unlink()
                except OSError:
                    pass
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("[IMAGE-CONTEXT] save failed (non-fatal): %s", exc)


def load_chat_images(chat_id: str) -> list[str]:
    """Return the persisted image set as data URLs (newest chat context)."""
    import base64
    folder = _image_ctx_dir(chat_id)
    if folder is None or not folder.is_dir():
        return []
    out: list[str] = []
    for fp in sorted(folder.glob("ctx_*")):
        try:
            data = fp.read_bytes()
        except OSError:
            continue
        if not data:
            continue
        mime = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif",
            ".webp": "image/webp",
        }.get(fp.suffix.lower(), "image/png")
        out.append(f"data:{mime};base64," + base64.b64encode(data).decode("ascii"))
    return out


def clear_chat_images(chat_id: str) -> None:
    """Drop the chat's persisted image context (chat deletion / privacy)."""
    import shutil
    folder = _image_ctx_dir(chat_id)
    if folder is not None:
        shutil.rmtree(folder, ignore_errors=True)


def clear_all_image_context(sessions_dir=None) -> None:
    """Remove every chat's ephemeral image context (startup/shutdown).

    `sessions_dir` is needed on the startup path: context.chat is initialized
    later in the server boot, so its module global is still None there.
    """
    import shutil
    base = Path(sessions_dir) if sessions_dir else _SESSIONS_DIR
    if not base:
        return
    shutil.rmtree(Path(base) / ".images", ignore_errors=True)

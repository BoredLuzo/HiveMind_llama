


from __future__ import annotations

import json
import time
import re
import math
import threading
import hashlib
import logging
from pathlib import Path

from utils.file import write_json_atomic

logger = logging.getLogger("hivemind.memory")

# PERF-4 FIX: Pre-compile regex — re.findall() recompiles the pattern on every
# call which is expensive when called hundreds of times per distillation cycle.
_TOKENIZE_RE = re.compile(r"[a-zA-Z0-9_./:-]+")


def _now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _norm_path(p: str) -> str:
    if not p:
        return ''
    return str(p).replace('\\', '/').strip().lower()


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return _TOKENIZE_RE.findall(text.lower())


_EMBED_VERSION = 2


def _hash_embedding(text: str, dims: int = 96) -> list[float]:
    """
    Lightweight fallback embedding without external dependencies.
    Uses hashed token bins + L2 normalization for cosine similarity.

    PERF-2 FIX: Uses Python's built-in hash() instead of MD5 for non-crypto
    purposes. hash() is ~5-10× faster than hashlib.md5 and sufficient for
    bin assignment. The result is still deterministic within a process.
    """
    vec = [0.0] * dims
    toks = _tokenize(text)
    if not toks:
        return vec
    for tok in toks:
        h = int(hashlib.md5(tok.encode("utf-8", errors="ignore")).hexdigest(), 16)
        idx = h % dims
        sign = -1.0 if (h & 1) else 1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return float(sum(x * y for x, y in zip(a, b)))


class ToolContextLRU:
    """
    Tracks tool-output relevance for semantic context eviction.

    Design:
    - conversational turns remain untouched
    - only tool-output entries are TTL-decayed and evicted first
    """

    def __init__(self, default_ttl: int = 3):
        self.default_ttl = max(1, int(default_ttl or 3))
        self._entries: list[dict] = []
        self._turn = 0

    def register(self, message_index: int, path: str = '', size_chars: int = 0, kind: str = 'tool_output', ttl_override: int | None = None):
        self._entries.append({
            'idx': int(message_index),
            'path': _norm_path(path),
            'ttl': ttl_override if ttl_override is not None else self.default_ttl,
            'size_chars': max(0, int(size_chars or 0)),
            'kind': str(kind or 'tool_output'),
            'evicted': False,
            'turn': self._turn,
        })

    def decay(self, focus_path: str = ''):
        self._turn += 1
        f = _norm_path(focus_path)
        for e in self._entries:
            if e.get('evicted'):
                continue
            ep = e.get('path', '')
            if not ep:
                continue
            if f and ep == f:
                e['ttl'] = self.default_ttl
                e['turn'] = self._turn
            else:
                e['ttl'] = max(0, int(e.get('ttl', self.default_ttl)) - 1)

    def mark_evicted(self, message_index: int):
        for e in self._entries:
            if e.get('idx') == int(message_index):
                e['evicted'] = True

    def candidates(self) -> list[dict]:
        """Lowest TTL first, then oldest, then largest payload."""
        alive = [e for e in self._entries if not e.get('evicted')]
        alive.sort(key=lambda e: (int(e.get('ttl', 0)), int(e.get('turn', 0)), -int(e.get('size_chars', 0))))
        return alive

    def reset(self):
        self._entries = []
        self._turn = 0


class Memory:


    
    def __init__(self, persist_path=None):


        self._store: dict = {}
        self._session: list = []
        self._path = None
        self._insight_path: Path | None = None
        self._insights: list[dict] = []
        self._insight_lock = threading.RLock()
        # PERF-3 FIX: Debounced persistence — avoid writing the full JSON file on
        # every single insight add. Multiple insights per loop (up to 8) would
        # trigger 8× ~100KB writes under the lock. Instead, mark dirty and let
        # the next distillation cycle (run_soul_cycle) or explicit flush write it.
        self._insights_dirty: bool = False
        self._insight_persist_interval: float = 5.0  # seconds
        self._insight_last_persist: float = 0.0
        if persist_path:
            self._path = persist_path if hasattr(persist_path, 'read_text') else Path(persist_path)
            if self._path.exists():
                try:
                    data = json.loads(self._path.read_text(encoding='utf-8'))
                    raw  = data.get('memories', {})
                    for k, v in raw.items():
                        if isinstance(v, dict):
                            self._store[k] = v
                        else:
                            self._store[k] = {'value': str(v), 'saved_at': ''}
                except Exception:
                    pass  # Bei Lese Fehlern leere Store verwenden
            try:
                self._insight_path = self._path.parent / 'learning_logs' / 'memories_db.json'
                if self._insight_path.exists():
                    raw_db = json.loads(self._insight_path.read_text(encoding='utf-8'))
                    if isinstance(raw_db, list):
                        self._insights = [x for x in raw_db if isinstance(x, dict)]
            except Exception:
                self._insights = []
        if self._insights:
            self._refresh_insight_embeddings()

    def _refresh_insight_embeddings(self):
        changed = False
        for rec in self._insights:
            if not isinstance(rec, dict):
                continue
            if rec.get("embedding_v") != _EMBED_VERSION:
                txt = str(rec.get("insight", "")).strip()
                if not txt:
                    continue
                rec["embedding"] = _hash_embedding(txt)
                rec["embedding_v"] = _EMBED_VERSION
                changed = True
        if changed:
            self._insights_dirty = True
            self._do_persist_insights()

    def remember_repo_insight(self, insight: str, trigger_path: str = '', source: str = 'critic', relevance_score: float = 1.0) -> bool:
        """
        Saves a compact repository insight in a lightweight vector store.
        relevance_score: Initial score (0.0-1.0). Insight Extractor seeds this
        with its confidence; other callers default to 1.0.
        Thread-safe: protected by _insight_lock.
        """
        txt = (insight or '').strip()
        if not txt:
            return False
        rec = {
            'insight': txt[:400],
            'trigger_path': _norm_path(trigger_path),
            'source': str(source or 'critic')[:40],
            'saved_at': _now_iso(),
            'embedding': _hash_embedding(txt),
            'embedding_v': _EMBED_VERSION,
            'relevance_score': max(0.0, min(1.0, float(relevance_score))),
            'merge_count': 1,
        }
        with self._insight_lock:
            self._insights.append(rec)
            # NOTE: No blind FIFO trim here — SkillDistiller._evict() handles
            # compaction by relevance_score. After distillation the list is sorted
            # by relevance (highest first), so a FIFO [-N:] would kill the BEST
            # entries. Instead, keep the top-500 by relevance_score.
            if len(self._insights) > 500:
                self._insights.sort(key=lambda x: float(x.get('relevance_score', 0)), reverse=True)
                self._insights = self._insights[:500]
            # PERF-3 FIX: Debounced persist — mark dirty instead of writing every time.
            # Actual write happens in flush_insights_if_dirty() or _persist_insights()
            # when the interval has elapsed.
            self._insights_dirty = True
            self._maybe_persist_insights()
        return True

    def query_repo_insights(self, query: str, trigger_path: str = '', top_k: int = 2, min_score: float = 0.12) -> list[dict]:
        """
        Returns top-k relevant insights by cosine similarity.
        Prefer same-path hints when available.
        Thread-safe: reads _insights under lock.
        """
        with self._insight_lock:
            insights_snapshot = list(self._insights)
        if not insights_snapshot:
            return []
        q = (query or '').strip()
        if not q:
            return []
        q_emb = _hash_embedding(q)
        tp = _norm_path(trigger_path)
        scored: list[dict] = []
        for rec in insights_snapshot:
            emb = rec.get('embedding') or []
            if not isinstance(emb, list):
                continue
            score = _cosine(q_emb, emb)
            rp = _norm_path(rec.get('trigger_path', ''))
            if tp and rp and (tp.startswith(rp) or rp.startswith(tp)):
                score += 0.08
            if score >= float(min_score):
                scored.append({'score': score, **rec})
        scored.sort(key=lambda r: r.get('score', 0.0), reverse=True)
        return scored[:max(1, int(top_k or 2))]

    def _maybe_persist_insights(self):
        """PERF-3: Debounced persist — only writes if enough time has elapsed."""
        if not self._insight_path or not self._insights_dirty:
            return
        now = time.time()
        if now - self._insight_last_persist < self._insight_persist_interval:
            return
        self._do_persist_insights()

    def flush_insights_if_dirty(self):
        """Force-persist if dirty. Called by run_soul_cycle after distillation."""
        if not self._insights_dirty:
            return
        with self._insight_lock:
            self._do_persist_insights()

    def _do_persist_insights(self):
        """Actual write — must be called under _insight_lock."""
        if not self._insight_path:
            return
        try:
            self._insight_path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(self._insight_path, self._insights)
            self._insights_dirty = False
            self._insight_last_persist = time.time()
        except Exception as _e:
            logger.warning(
                "[Memory] Failed to persist insights to %s: %s",
                self._insight_path, _e
            )

    def _persist_insights(self):
        """Legacy entry point — now forces immediate write (used by distiller)."""
        if not self._insight_path:
            return
        with self._insight_lock:
            self._do_persist_insights()

    def remember(self, key: str, value: str):


        self._store[key] = {'value': str(value), 'saved_at': time.strftime('%Y-%m-%d %H:%M')}
        self._persist()

    def forget(self, key: str) -> bool:


        if key in self._store:
            del self._store[key]
            self._persist()
            return True
        return False

    def list_memories(self):


        for key, data in self._store.items():
            yield key, data.get('value', ''), data.get('saved_at', '')

    def get_all(self) -> dict:
        return {k: v.get('value', '') for k, v in self._store.items()}

    def as_context_string(self) -> str:
        if not self._store:
            return ''
        lines = ['[Gespeicherte Informationen]']
        for key, data in self._store.items():
            lines.append('  ' + key + ': ' + data.get('value', ''))
        return '\n'.join(lines)[:2000]

    def add_to_session(self, role: str, content: str):
        self._session.append({'role': role, 'content': content})
        if len(self._session) > 40:
            self._session = self._session[-40:]

    def get_session_context(self) -> str:
        if not self._session:
            return ''
        lines = ['[Conversation history]']
        for m in self._session[-10:]:
            prefix = 'User' if m['role'] == 'user' else 'Hivemind'
            lines.append(f'  {prefix}: {m["content"][:200]}')
        return '\n'.join(lines)

    def get_session_messages(self, limit: int = 10, user_cap: int = 600, assistant_cap: int = 1200) -> list:
        if not self._session:
            return []
        msgs = []
        for m in self._session[-limit:]:
            # Assistant-Messages (Code, lange Outputs) brauchen mehr Platz als User-Messages
            cap = assistant_cap if m['role'] == 'assistant' else user_cap
            msgs.append({
                'role':    m['role'] if m['role'] in ('user', 'assistant') else 'user',
                'content': m['content'][:cap],
            })
        return msgs

    def seed_session(self, msgs: list):


        if not self._session and msgs:
            self._session = [
                {"role": m.get("role") if m.get("role") in ("user", "assistant") else "user",
                 "content": str(m.get("content") or "")}
                for m in msgs[-40:]
            ]

    def clear_session(self):
        self._session = []

    def _persist(self):
        if not self._path:
            return
        try:
            write_json_atomic(self._path, {'memories': self._store})
        except Exception as _e:
            logger.warning(
                "[Memory] Failed to persist memories to %s: %s",
                self._path, _e
            )

"""Routing-Decider (optional, CPU) — Schaltplan fuer Route und Planner.

Warum: der LLM-Judge kostet einen Modell-Load in den VRAM (60 s + Verdraengung)
und ist als Text-Parser fehleranfaellig. Ein Encoder-Klassifikator (GLiNER2.5)
entscheidet dieselbe Frage in ~0.5-1.2 s auf der CPU, ohne VRAM. Beide sollen
einzeln schaltbar bleiben - dieses Modul kapselt NUR die Entscheidungslogik und
die optionale Encoder-Anbindung; der Judge bleibt, wo er ist.

Schalter (settings):
    routing_decider   off | shadow | on     Encoder entscheidet die Route
    routing_judge     off | shadow | on     LLM-Judge entscheidet die Route
    routing_planner   auto | decider | judge | always | never   (auto = decider wenn an)
    routing_decider_threshold   float >= 0.5   unter dieser Konfidenz eskalieren
    routing_decider_model       HF-Checkpoint (Default: English GLiNER2.5-Decide)

Praeedenz im Resolver:
    expliziter Modus / force_complexity  >  Decider  >  Judge  >  Regeln
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger("hivemind.routing")

# ── Label-Taxonomie (eine Quelle fuer Prompt, UI und Tests) ────────────────
# Deutsch/Englisch gemischt im Text ist okay - die Beschreibungen sind absichtlich
# englisch, weil das verwendete Checkpoint englisch trainiert ist.
ROUTE_LABELS: dict[str, str] = {
    "direct": (
        "a question, an explanation, a summary, small talk or a one-off text: nothing in the "
        "user's own project has to be created, edited or debugged. Asking about code or a "
        "language feature is direct as long as no file is touched."
    ),
    "agentic": (
        "the user wants a file, script, program or project created, modified, fixed or debugged: "
        "mentions a file name or extension (.rs, .py, .js, .css, .md, .toml), a path, a line "
        "number, 'my game', 'my script', a repository, or reports a bug in something that exists."
    ),
}
PLANNER_LABELS: dict[str, str] = {
    "yes": (
        "several files, a project from scratch, an unclear cause that has to be diagnosed, or a "
        "feature that needs a structural decision - a plan pays off before the first edit"
    ),
    "no": (
        "one concrete edit that is already fully described: rename a symbol, change a colour or "
        "value, fix a typo, adjust one line"
    ),
}

_MODES = ("off", "shadow", "on")
_PLANNER_SOURCES = ("auto", "decider", "judge", "always", "never")


def _default(key: str, fallback):
    """Default aus settings.DEFAULT_SETTINGS (Bestands-Settings haben die Keys nicht)."""
    try:
        from settings import DEFAULT_SETTINGS
    except (ImportError, OSError):
        return fallback
    return DEFAULT_SETTINGS.get(key, fallback)


def _mode_from(settings: dict, key: str) -> str:
    raw = (settings or {}).get(key)
    if raw is None or str(raw).strip() == "":
        raw = _default(key, "off")
    return normalise_mode(raw)


def _planner_from(settings: dict) -> str:
    raw = (settings or {}).get("routing_planner")
    if raw is None or str(raw).strip() == "":
        raw = _default("routing_planner", "auto")
    return normalise_planner_source(raw)


def _threshold_from(settings: dict) -> float:
    raw = (settings or {}).get("routing_decider_threshold")
    if raw is None or str(raw).strip() == "":
        raw = _default("routing_decider_threshold", 0.85)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.85


def _model_from(settings: dict) -> str:
    raw = (settings or {}).get("routing_decider_model")
    if raw is None or str(raw).strip() == "":
        raw = _default("routing_decider_model", "fastino/GLiNER2.5-Decide")
    return str(raw)



@dataclass
class Decision:
    """Ergebnis einer Routing-Entscheidung."""
    route: str = "direct"                 # direct | agentic
    planner: bool = False
    source: str = "rule"                  # rule | decider | judge
    confidence: float = 0.0
    reason: str = ""
    escalated: bool = False
    latency_ms: float = 0.0
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "route": self.route, "planner": bool(self.planner), "source": self.source,
            "confidence": round(self.confidence, 3), "reason": self.reason,
            "escalated": bool(self.escalated), "latency_ms": round(self.latency_ms, 1),
            "details": self.details,
        }


def normalise_mode(value) -> str:
    """off | shadow | on - alles andere gilt als 'off' (fail closed)."""
    text = str(value or "").strip().lower()
    return text if text in _MODES else "off"


def normalise_planner_source(value) -> str:
    text = str(value or "").strip().lower()
    return text if text in _PLANNER_SOURCES else "auto"


def extract_features(text: str) -> dict:
    """Lokale, deterministische Signale - kostenlos und erklaerbar.

    Sie dienen (a) dem Regel-Fallback, wenn weder Decider noch Judge laufen, und
    (b) als Diagnose im Log, wenn eine Entscheidung ueberrascht.
    """
    body = str(text or "")
    lower = body.lower()
    words = len(body.split())
    return {
        "words": words,
        "has_file_ext": bool(re.search(r"\.(rs|py|js|ts|tsx|jsx|css|html|md|toml|json|yml|yaml|sh|go|c|cpp|h|java|cs|lua|sql)\b", lower)),
        "has_path": bool(re.search(r"([a-z]:[\\/]|\.{0,2}/[\w.-]+/|[\w-]+/[\w.-]+)", lower)),
        "has_line_ref": bool(re.search(r"\b(line|zeile|ln)\s*\d+|\bline\s+\d+", lower)),
        "code_fence": "```" in body,
        "bug_words": bool(re.search(r"\b(bug|crash|error|exception|stack ?trace|fails?|broken|stutter|glitch|traceback|keyerror|np\.?exception)\b", lower)),
        "build_words": bool(re.search(r"\b(create|build|make|add|implement|write|refactor|rename|fix|change|patch|generate|erstelle|baue|implementiere|schreib|ändere|aendere|repariere)\b", lower)),
        "big_words": bool(re.search(r"\b(complete|full|whole|from scratch|multi[- ]?file|architecture|rewrite|komplett|vollständig|von grund auf)\b", lower)),
    }


def rule_decision(text: str) -> Decision:
    """Fallback ohne Modell: konservativ, aber nicht still 'simple'.

    Code-Signal (Dateiendung/Pfad/Zeile/Fence/Bugwort + Bauwort) -> agentic,
    sonst direct. Planner nur bei 'big' Signalen oder mehreren Signalen.
    """
    f = extract_features(text)
    code = (f["has_file_ext"] or f["has_path"] or f["has_line_ref"] or f["code_fence"]) and (
        f["build_words"] or f["bug_words"]
    )
    route = "agentic" if (code or (f["bug_words"] and f["build_words"])) else "direct"
    planner = route == "agentic" and (f["big_words"] or (f["has_file_ext"] and f["words"] > 60))
    return Decision(
        route=route, planner=bool(planner), source="rule", confidence=0.0,
        reason="local features", details=f,
    )


class DeciderUnavailable(RuntimeError):
    """Encoder-Backend ist nicht installiert oder liess sich nicht laden."""


class EncoderBackend:
    """GLiNER2.5 als Route-/Planner-Klassifikator (CPU, optional).

    Das Modell liefert seine Konfidenz selbst; wir nehmen den besten Label-Wert
    als Confidence und eskalieren unterhalb der Schwelle an den Judge.
    """

    def __init__(self, model_name: str = "fastino/GLiNER2.5-Decide") -> None:
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        try:
            import gliner2  # noqa: F401
        except (ImportError, OSError) as exc:
            logger.debug("[DECIDER] gliner2 not importable: %s", exc)
            return False
        return True

    def _ensure(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                from gliner2 import AutoExtractor
            except (ImportError, OSError) as exc:  # fehlendes Paket oder fehlende libs
                raise DeciderUnavailable(f"gliner2 not installed: {exc}") from exc
            logger.info("[DECIDER] loading %s (CPU, once per process)", self.model_name)
            t0 = time.perf_counter()
            self._model = AutoExtractor.from_pretrained(self.model_name)
            logger.info("[DECIDER] %s ready in %.1fs", self.model_name, time.perf_counter() - t0)
            return self._model

    def decide(self, text: str, want_planner: bool = True) -> tuple[str, float, dict]:
        model = self._ensure()
        route_task = {"labels": dict(ROUTE_LABELS)}
        out = model.classify_text(text, {"route": route_task}, include_confidence=True)
        route_obj = out.get("route") or {}
        route = str(route_obj.get("label") or "direct")
        try:
            conf = float(route_obj.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        details = {"route": route_obj}
        if want_planner and route == "agentic":
            out2 = model.classify_text(text, {"planner": {"labels": dict(PLANNER_LABELS)}},
                                      include_confidence=True)
            details["planner"] = out2.get("planner") or {}
        return route, conf, details


class DecisionCache:
    """Kleine LRU: identische Prompts kosten keine Inferenz (Testlaeufe!)."""

    def __init__(self, size: int = 512) -> None:
        self.size = size
        self._data: dict[str, Decision] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    @staticmethod
    def key(text: str, model: str) -> str:
        raw = f"{model}\x00{re.sub(r'\\s+', ' ', str(text or '')).strip().lower()}"
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()

    def get(self, key: str) -> Decision | None:
        with self._lock:
            hit = self._data.get(key)
            if hit is not None:
                self._order.remove(key)
                self._order.append(key)
            return hit

    def put(self, key: str, value: Decision) -> None:
        with self._lock:
            if key not in self._data:
                self._order.append(key)
            self._data[key] = value
            while len(self._order) > self.size:
                self._data.pop(self._order.pop(0), None)


# ── Resolver: die einzige Stelle, die aus Schaltern eine Route macht ──────
def resolve(settings: dict, text: str, *, forced_complexity: str | None = None,
            encoder: EncoderBackend | None = None,
            judge_call=None, cache: DecisionCache | None = None) -> Decision:
    """Route + Planner bestimmen - gemaess der drei Schalter.

    Reihenfolge: expliziter Modus/force  >  Decider  >  Judge  >  Regeln.
    `judge_call` ist ein Callable(text) -> (level, confidence) und darf None
    sein (dann laeuft der Judge nicht). Nichts hier blockiert den Event-Loop
    von selbst - der Aufrufer entscheidet, in welchem Thread das laeuft.
    """
    decider_mode = _mode_from(settings, "routing_decider")
    judge_mode = _mode_from(settings, "routing_judge")
    planner_src = _planner_from(settings)
    threshold = _threshold_from(settings)
    if planner_src == "auto":
        # Planner dort entscheiden lassen, wo er nichts extra kostet:
        # Decider an -> Decider; sonst der Judge.
        planner_src = "decider" if decider_mode != "off" else "judge"

    # 1. harte Overrides
    if str(forced_complexity or "") == "simple":
        return Decision(route="direct", planner=False, source="force", reason="forced simple")
    if str(forced_complexity or "") == "code_duo":
        return Decision(route="agentic", planner=(planner_src != "never"), source="force",
                        reason="forced code_duo")

    decision: Decision | None = None
    escalated = False

    # 2. Decider (Encoder) - 'shadow' rechnet mit, entscheidet aber nicht
    if decider_mode != "off" and encoder is not None:
        key = DecisionCache.key(text, encoder.model_name) if cache is not None else ""
        cached = cache.get(key) if (cache is not None and key) else None
        if cached is not None:
            decision = Decision(**{**cached.to_dict()})
            decision.source = "decider"
            logger.info("[DECIDER] cache hit route=%s planner=%s", decision.route, decision.planner)
        else:
            t0 = time.perf_counter()
            try:
                route, conf, details = encoder.decide(text, want_planner=(planner_src == "decider"))
                decision = Decision(
                    route=route, planner=bool((details.get("planner") or {}).get("label") == "yes"),
                    source="decider", confidence=conf,
                    latency_ms=(time.perf_counter() - t0) * 1000.0, details=details,
                )
                if cache is not None and key:
                    cache.put(key, decision)
            except DeciderUnavailable as exc:
                logger.warning("[DECIDER] unavailable, falling back: %s", exc)
                decision = None
        if decision is not None and decider_mode == "shadow":
            # Shadow: nur protokollieren, die Entscheidung faellt weiter unten
            logger.info("[DECIDER][SHADOW] would route=%s planner=%s conf=%.2f",
                        decision.route, decision.planner, decision.confidence)
            decision = None
        elif decision is not None and decision.confidence < threshold:
            escalated = True
            logger.info("[DECIDER] low confidence %.2f < %.2f -> escalate", decision.confidence, threshold)

    # 3. Judge - nur wenn der Decider nichts (sicheres) geliefert hat
    need_judge = decision is None or escalated
    if need_judge and judge_mode != "off" and judge_call is not None:
        try:
            level, jconf = judge_call(text)
        except (RuntimeError, ValueError, TypeError, KeyError, LookupError,
                TimeoutError, OSError) as exc:
            # ein kaputter Judge darf den Run nicht sprengen - aber nie stumm
            logger.warning("[JUDGE] failed, using rules: %s", exc)
            level, jconf = "", 0.0
        level = str(level or "")
        if level in ("trivial", "simple"):
            route = "direct"
        elif level in ("complex", "code_duo"):
            route = "agentic"
        else:
            route = None
        if route is not None:
            decision = Decision(
                route=route, planner=bool(level in ("complex", "code_duo")),
                source="judge", confidence=float(jconf or 0.0), escalated=escalated,
                reason=f"judge level={level}",
            )

    # 4. Regeln
    if decision is None:
        decision = rule_decision(text)
        decision.escalated = escalated

    # Planner-Quelle anwenden
    if planner_src == "never":
        decision.planner = False
    elif planner_src == "always":
        decision.planner = decision.route == "agentic"
    elif planner_src == "judge" and decision.source != "judge" and judge_mode != "off" and judge_call is not None:
        # Judge entscheidet den Planner, auch wenn die Route vom Decider kam
        try:
            level, _ = judge_call(text)
            decision.planner = str(level or "") in ("complex", "code_duo")
        except (RuntimeError, ValueError, TypeError, KeyError, LookupError,
                TimeoutError, OSError) as exc:
            logger.debug("[DECIDER] planner judge failed, keeping %s: %s", decision.planner, exc)

    return decision


async def decide_async(settings: dict, text: str, *, forced_complexity: str | None = None,
                       judge_call_async=None) -> Decision:
    """Async-Variante: Encoder im Worker-Thread, Judge im Event-Loop.

    Der Judge bleibt damit genau da, wo er heute laeuft (im Loop, sequenziell);
    nur der CPU-Encoder (0.5-1.2 s) wird ausgelagert. Ohne `judge_call_async`
    verhaelt sich die Funktion wie `resolve(..., judge_call=None)`.
    """
    import asyncio

    decider_mode = _mode_from(settings, "routing_decider")
    enc = None
    if decider_mode != "off":
        enc = backend(_model_from(settings))

    # 1. Encoder (Thread) - Ergebnis auch im Shadow-Modus, dort nur zum Loggen
    enc_decision: Decision | None = None
    if enc is not None and str(forced_complexity or "") not in ("simple", "code_duo"):
        key = DecisionCache.key(text, enc.model_name)
        cached = cache().get(key)
        if cached is not None:
            enc_decision = cached
        else:
            def _run_encoder():
                return enc.decide(text, want_planner=(_planner_from(settings) in ("decider", "auto")))

            t0 = time.perf_counter()
            try:
                route, conf, details = await asyncio.wait_for(
                    asyncio.to_thread(_run_encoder), timeout=20.0)
                enc_decision = Decision(
                    route=route,
                    planner=bool((details.get("planner") or {}).get("label") == "yes"),
                    source="decider", confidence=conf,
                    latency_ms=(time.perf_counter() - t0) * 1000.0, details=details,
                )
                cache().put(key, enc_decision)
            except (DeciderUnavailable, TimeoutError, RuntimeError, OSError, ValueError) as exc:
                logger.warning("[DECIDER] unavailable/failed (%s: %s) - falling back",
                               type(exc).__name__, exc)
                enc_decision = None

    if enc_decision is not None and decider_mode == "shadow":
        logger.info("[DECIDER][SHADOW] would route=%s planner=%s conf=%.2f",
                    enc_decision.route, enc_decision.planner, enc_decision.confidence)
        enc_decision = None

    # 2. Rest (Judge/Regeln/Overrides) mit der reinen Sync-Logik entscheiden
    threshold = _threshold_from(settings)
    if enc_decision is not None and enc_decision.confidence >= threshold:
        decision = enc_decision
    else:
        escalated = enc_decision is not None
        if escalated:
            logger.info("[DECIDER] low confidence %.2f < %.2f -> escalate", enc_decision.confidence, threshold)
        judge_mode = _mode_from(settings, "routing_judge")
        decision = None
        if judge_mode != "off" and judge_call_async is not None:
            try:
                level, jconf = await judge_call_async(text)
            except (RuntimeError, ValueError, TypeError, KeyError, LookupError,
                    TimeoutError, OSError) as exc:
                logger.warning("[JUDGE] failed, using rules: %s", exc)
                level, jconf = "", 0.0
            if str(level or "") in ("trivial", "simple"):
                decision = Decision(route="direct", planner=False, source="judge",
                                    confidence=float(jconf or 0.0), escalated=escalated,
                                    reason=f"judge level={level}")
            elif str(level or "") in ("complex", "code_duo"):
                decision = Decision(route="agentic", planner=True, source="judge",
                                    confidence=float(jconf or 0.0), escalated=escalated,
                                    reason=f"judge level={level}")
        if decision is None:
            decision = rule_decision(text)
            decision.escalated = escalated
            if enc_decision is not None:
                # Encoder war da, Judge aus: seine Route als bestes Signal nehmen
                decision = enc_decision
                decision.escalated = True

    # 3. Planner-Quelle anwenden
    planner_src = _planner_from(settings)
    if planner_src == "auto":
        planner_src = "decider" if decider_mode != "off" else "judge"
    if planner_src == "never":
        decision.planner = False
    elif planner_src == "always":
        decision.planner = decision.route == "agentic"
    elif planner_src == "decider" and enc_decision is None:
        decision.planner = False
    return decision


def complexity_for(decision: Decision) -> str:
    """Resolver-Ergebnis -> engine-interner `complexity`-Wert (ohne pipeline)."""
    return "code_duo" if decision.route == "agentic" else "simple"


_CACHE = DecisionCache()
_BACKEND: EncoderBackend | None = None
_BACKEND_LOCK = threading.Lock()


def backend(model_name: str = "fastino/GLiNER2.5-Decide") -> EncoderBackend:
    """Prozessweit ein Backend (Modell nur einmal laden)."""
    global _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is None or _BACKEND.model_name != model_name:
            _BACKEND = EncoderBackend(model_name)
        return _BACKEND


def cache() -> DecisionCache:
    return _CACHE

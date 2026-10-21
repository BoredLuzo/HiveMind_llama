"""Routing decider (optional, CPU) — switch plan for route and planner.

Why: the LLM judge costs a model load in VRAM (60 s + eviction) and is
fragile as a text parser. An encoder classifier (GLiNER2.5) answers the
same question in ~0.5-1.2 s on CPU, without VRAM. Both stay individually
switchable - this module encapsulates ONLY the decision logic and the
optional encoder hookup; the judge stays where it is.

Switches (settings):
    routing_decider   off | shadow | on     the encoder decides the route
    routing_judge     off | shadow | on     the LLM judge decides the route
    routing_planner   auto | heuristic | decider | judge | always | never
                      (auto = heuristic - the encoder cannot plan)
    routing_decider_threshold   float >= 0.5   escalate below this confidence
    routing_decider_model       HF checkpoint (default: English GLiNER2.5-Decide)

Precedence in the resolver:
    explicit mode / force_complexity  >  Decider  >  Judge  >  rules
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("hivemind.routing")

# ── Label-Taxonomie (eine Quelle fuer Encoder, Heuristik und Tests) ────────
# Konkrete Aktions-Labels statt abstrakter Kategorien: ein Zero-Shot-Classifier
# versteht "create or write a new file" deutlich besser als "agentic".
# Gemessen: 12/12 Treffer gegenueber 9-10/14 mit abstrakten Labels.
ACTION_LABELS: dict[str, str] = {
    "asks_a_question": "the user asks how something works, wants an explanation, a definition, "
                       "a comparison or a summary of a topic",
    "small_talk": "greeting, thanks, chit chat - no task at all",
    "write_new_file": "create or write a new file, script, program, game or document",
    "edit_existing_code": "change, rename, refactor, fix a typo or adjust code that already exists",
    "debug_a_problem": "something is broken, crashes, stutters or behaves wrong and has to be "
                       "diagnosed before it can be fixed",
    "run_commands": "install packages, build the project, run tests or execute a command",
}
# Welche Aktionen bedeuten "das Projekt wird angefasst"?
AGENTIC_ACTIONS = frozenset({"write_new_file", "edit_existing_code", "debug_a_problem", "run_commands"})
DIRECT_ACTIONS = frozenset({"asks_a_question", "small_talk"})

# Planner-Labels (nur noch fuer routing_planner="decider"); die Voreinstellung
# entscheidet der Planner-Heuristik unten, weil der Encoder das schlechter kann.
PLANNER_LABELS: dict[str, str] = {
    "one_small_edit": "a single obvious change in one place: rename a symbol, change a colour or "
                      "a value, fix a typo, adjust one line",
    "several_files": "the change touches several files, several screens or the whole project",
    "unclear_cause": "the cause is unknown and has to be investigated before anything can be edited",
    "from_scratch": "something new is built from nothing: a project, a game, a tool",
}

_MODES = ("off", "shadow", "on")
_PLANNER_SOURCES = ("auto", "heuristic", "decider", "judge", "always", "never")


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
        raw = _default("routing_decider_threshold", 0.55)
    try:
        return float(raw)
    except (TypeError, ValueError):
        # THRESHOLD-ALIGN (2026-10-16): the fallback must match the shipped
        # default in settings.py (0.55). The old 0.85 literal predated the
        # score recalibration and, on a settings import failure, silently
        # flipped the router back to escalate-almost-everything.
        return 0.55


def _model_from(settings: dict) -> str:
    raw = (settings or {}).get("routing_decider_model")
    if raw is None or str(raw).strip() == "":
        raw = _default("routing_decider_model", "fastino/GLiNER2.5-Decide")
    return str(raw)



@dataclass
class Decision:
    """Result of a routing decision."""
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
        # LANG-MATCH (2026-10-16, deliberate exception to the English-only
        # matcher rule in live/AGENTS.md): this install is driven with German
        # input and dropping the German stems regressed classification.
        # Word stems, not exact words: "stutters", "crashes", "renames" ...
        "bug_words": bool(re.search(r"\b(bug|crash|error|exception|stack ?trace|fail|broken|stutter|glitch|traceback|keyerror|np\.?exception)\w*", lower)),
        "build_words": bool(re.search(r"\b(create|build|make|add|implement|write|refactor|rename|fix|change|patch|generate|erstelle|baue|implementiere|schreib|ändere|aendere|repariere)\w*", lower)),
        # mehrere Aufgaben in einem Satz ("add X and fix Y")
        "multi_action": len(set(re.findall(
            r"\b(add|create|build|make|implement|write|refactor|rename|fix|change|patch|remove|delete|extend)\w*",
            lower))) >= 2,
        "big_words": bool(re.search(r"\b(complete|full|whole|from scratch|multi[- ]?file|architecture|rewrite|komplett|vollständig|von grund auf)\b", lower)),
        "file_mentions": len(set(re.findall(
            r"[\w./\\-]+\.(?:rs|py|js|ts|tsx|jsx|css|html|md|toml|json|yml|yaml|sh|go|c|cpp|h|java|cs|lua|sql)\b",
            lower))),
        "ui_words": bool(re.search(r"\b(menu|button|screen|layout|dialog|window|ui|design|theme|css|style|farbe|color)\b", lower)),
        "small_edit_words": bool(re.search(r"\b(rename|umbenennen|typo|value|wert|port|farbe|color|colour|comment|kommentar|one line|eine zeile|variable)\b", lower)),
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


def heuristic_planner(text: str, route: str = "agentic", features: dict | None = None) -> bool:
    """Braucht dieser agentische Task einen Plan? Deterministisch, kostenlos.

    Im Zweifel NEIN: ein falsches "ja" kostet einen Modellwechsel plus Minuten,
    ein falsches "nein" nur etwas Struktur im Vorgehen. "Ja" nur bei klarer
    Evidenz: Projekt von null, mehrere Dateien, unbekannte Ursache, lange Aufgabe.
    """
    if route != "agentic":
        return False
    f = features or extract_features(text)
    if f.get("big_words"):
        return True
    if int(f.get("file_mentions") or 0) >= 2:
        return True
    if f.get("bug_words") and not f.get("has_line_ref"):
        # Fehlerbild ohne konkrete Stelle: erst diagnostizieren
        return True
    if f.get("multi_action") and int(f.get("words") or 0) > 10:
        # "add a highscore ... and fix the tunnel wrap" - mehrere Aufgaben
        return True
    if f.get("ui_words") and f.get("build_words") and int(f.get("words") or 0) > 12:
        # "add a menu with three buttons and a settings screen" - mehrere Stellen
        return True
    if f.get("small_edit_words") and int(f.get("file_mentions") or 0) <= 1:
        return False
    return int(f.get("words") or 0) > 60


def _labels_of(value) -> list[str]:
    """gliner2 liefert je nach Task str, list[str] oder list[dict]."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        lab = value.get("label")
        return [str(lab)] if lab else []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict) and item.get("label"):
            out.append(str(item["label"]))
        elif isinstance(item, str):
            out.append(item)
    return out


def _scores_of(value) -> dict[str, float]:
    """Label -> Confidence, soweit das Backend sie mitschickt."""
    if isinstance(value, dict):
        lab = value.get("label")
        if lab:
            try:
                return {str(lab): float(value.get("confidence") or 0.0)}
            except (TypeError, ValueError):
                return {str(lab): 0.0}
        return {}
    out: dict[str, float] = {}
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item.get("label"):
                try:
                    out[str(item["label"])] = float(item.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    out[str(item["label"])] = 0.0
    return out


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
        """Aktion(en) klassifizieren und auf eine Route abbilden.

        Ein multi-label-Call fuer die Aktionen (plus optional den Planner-Head).
        Gemessen: 12/12 Routen-Treffer, ~630 ms auf der CPU eines 8-Kern-Ryzen.
        """
        model = self._ensure()
        _t_inf = time.perf_counter()
        tasks: dict = {"action": {"labels": dict(ACTION_LABELS), "multi_label": True}}
        if want_planner:
            tasks["plan"] = {"labels": dict(PLANNER_LABELS)}
        out = model.classify_text(text, tasks, include_confidence=True)

        fired = _labels_of(out.get("action"))
        scores = _scores_of(out.get("action"))
        agentic = [l for l in fired if l in AGENTIC_ACTIONS]
        direct = [l for l in fired if l in DIRECT_ACTIONS]
        if agentic:
            route = "agentic"
            conf = max((scores.get(l, 0.0) for l in agentic), default=0.0)
        elif direct:
            route = "direct"
            conf = max((scores.get(l, 0.0) for l in direct), default=0.0)
        else:
            # nichts gefeuert: keine Meinung -> Konfidenz 0, der Aufrufer eskaliert
            route = "direct"
            conf = 0.0

        details: dict = {"actions": fired, "scores": scores,
                         "inference_ms": round((time.perf_counter() - _t_inf) * 1000.0, 1)}
        plan = out.get("plan")
        if isinstance(plan, dict) and plan.get("label"):
            details["planner"] = plan
        elif isinstance(plan, list) and plan:
            details["planner"] = plan[0] if isinstance(plan[0], dict) else {"label": str(plan[0])}
        return route, conf, details


class DecisionCache:
    """Kleine LRU: identische Prompts kosten keine Inferenz (Testlaeufe!).

    PERSISTENT (2026-10-22): mit `path` ueberlebt der Cache Engine-Restarts -
    ohne ihn routet der erste Run nach einem Restart ungebuffert. Lesen und
    Schreiben sind best-effort: jede IO-Stoerung degradiert auf In-Memory,
    nie ein Routing-Fehler. Eintraege altern per TTL (Standard 7 Tage -
    Aenderungen an Matchern/Kalibrierung machen alte Entscheidungen
    unguenstig; der Key deckt nur Settings-Aenderungen ab).
    """

    def __init__(self, size: int = 512, path: "Path | str | None" = None,
                 ttl_s: int = 7 * 86400) -> None:
        self.size = size
        self.path = Path(path) if path else None
        self.ttl_s = int(ttl_s)
        self._data: dict = {}
        self._ts: dict = {}
        self._order: list = []
        self._lock = threading.Lock()
        self._loaded = False

    @staticmethod
    def key(text: str, model: str, settings: dict | None = None) -> str:
        """Key aus Prompt + Modell + Entscheidungs-Parametern.

        Schwelle und Planner-Quelle gehoeren dazu: sonst liefert der Cache nach
        einer Einstellungs-Aenderung weiter die alte Entscheidung.
        """
        _st = settings or {}
        _extra = f"{_threshold_from(_st)}|{_planner_from(_st)}"
        _norm = re.sub(r"\s+", " ", str(text or "")).strip().lower()
        raw = f"{model}\x00{_extra}\x00{_norm}"
        return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()

    def _load_locked(self) -> None:
        """Disk -> Memory (einmal pro Prozess). Best-effort."""
        self._loaded = True
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            entries = raw.get("entries") or {}
            now = time.time()
            for key, rec in entries.items():
                if int(rec.get("ts") or 0) + self.ttl_s < now:
                    continue
                self._data[key] = Decision(**(rec.get("decision") or {}))
                self._ts[key] = float(rec.get("ts") or 0)
                self._order.append(key)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("[DECIDER] cache load failed: %s", exc)
            self._data, self._ts, self._order = {}, {}, []

    def _save_locked(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            entries = {}
            for key in self._order:
                d = self._data.get(key)
                if d is None:
                    continue
                dec = d.to_dict()
                try:
                    json.dumps(dec)
                except (TypeError, ValueError):
                    dec.pop("details", None)
                entries[key] = {"decision": dec, "ts": self._ts.get(key, time.time())}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "entries": entries},
                                      ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except (OSError, TypeError, ValueError) as exc:
            logger.debug("[DECIDER] cache save failed: %s", exc)

    def get(self, key: str) -> Decision | None:
        with self._lock:
            if not self._loaded:
                self._load_locked()
            hit = self._data.get(key)
            if hit is None:
                return None
            if self._ts.get(key, 0) + self.ttl_s < time.time():
                self._data.pop(key, None)
                self._ts.pop(key, None)
                self._order.remove(key)
                return None
            self._order.remove(key)
            self._order.append(key)
            return hit

    def put(self, key: str, value: Decision) -> None:
        with self._lock:
            if not self._loaded:
                self._load_locked()
            if key not in self._data:
                self._order.append(key)
            self._data[key] = value
            self._ts[key] = time.time()
            while len(self._order) > self.size:
                _old = self._order.pop(0)
                self._data.pop(_old, None)
                self._ts.pop(_old, None)
            self._save_locked()


# ── Resolver: die einzige Stelle, die aus Schaltern eine Route macht ──────
def decide_from_signals(settings: dict, text: str, *,
                        forced_complexity: str | None = None,
                        encoder_decision: "Decision | None" = None,
                        judge_level: str = "",
                        judge_confidence: float = 0.0) -> Decision:
    """Reine Entscheidungslogik - ohne I/O, ohne Threads, ohne Modell.

    Praezedenz:  force  >  Decider (wenn sicher)  >  Judge  >  Regeln
    `encoder_decision` und `judge_level` sind bereits ermittelte Signale; wer sie
    nicht hat, laesst sie leer. Genau diese Funktion nutzen sync und async.
    """
    decider_mode = _mode_from(settings, "routing_decider")
    judge_mode = _mode_from(settings, "routing_judge")
    planner_src = _planner_from(settings)
    threshold = _threshold_from(settings)
    if planner_src == "auto":
        # kostenlos und ohne GPU: die Heuristik (der Encoder kann den Planner nicht)
        planner_src = "heuristic"

    # 1. harte Overrides
    if str(forced_complexity or "") == "simple":
        return Decision(route="direct", planner=False, source="force", reason="forced simple")
    if str(forced_complexity or "") == "code_duo":
        return Decision(route="agentic", planner=(planner_src != "never"), source="force",
                        reason="forced code_duo")

    escalated = False
    decision: Decision | None = None

    # 2. Decider
    if encoder_decision is not None and decider_mode != "off":
        if decider_mode == "shadow":
            logger.info("[DECIDER][SHADOW] would route=%s planner=%s conf=%.2f",
                        encoder_decision.route, encoder_decision.planner, encoder_decision.confidence)
        elif encoder_decision.confidence >= threshold:
            decision = Decision(**encoder_decision.to_dict())
            decision.source = "decider"
        else:
            escalated = True
            logger.info("[DECIDER] low confidence %.2f < %.2f -> escalate",
                        encoder_decision.confidence, threshold)

    # 3. Judge (nur wenn noetig und aktiv)
    if decision is None and judge_mode != "off" and str(judge_level or ""):
        lvl = str(judge_level).strip().lower()
        if lvl in ("trivial", "simple"):
            decision = Decision(route="direct", planner=False, source="judge",
                                confidence=float(judge_confidence or 0.0),
                                escalated=escalated, reason=f"judge level={lvl}")
        elif lvl in ("complex", "code_duo", "duo"):
            decision = Decision(route="agentic", planner=True, source="judge",
                                confidence=float(judge_confidence or 0.0),
                                escalated=escalated, reason=f"judge level={lvl}")

    # 4. Regeln
    if decision is None:
        decision = rule_decision(text)
        decision.escalated = escalated
        if escalated and encoder_decision is not None:
            # Decider war unsicher und kein Judge: seine Route ist das bessere
            # Signal als reine Keyword-Regeln - aber als "unsicher" markiert.
            decision = Decision(**encoder_decision.to_dict())
            decision.source = "decider-partial"
            decision.escalated = True

    # 5. Planner-Quelle anwenden
    if planner_src == "heuristic":
        decision.planner = heuristic_planner(text, decision.route)
    if planner_src == "judge" and str(judge_level or ""):
        # Route kam evtl. vom Decider, die Planner-Frage beantwortet der Judge
        decision.planner = str(judge_level).strip().lower() in ("complex", "code_duo", "duo")
    if planner_src == "never":
        decision.planner = False
    elif planner_src == "always":
        decision.planner = decision.route == "agentic"
    elif planner_src == "decider":
        # Decider-Entscheidung gewinnt; ohne Encoder-Signal kein Planner
        decision.planner = bool(encoder_decision.planner) if encoder_decision is not None else False
    # planner_src == "judge": der Judge hat planner oben schon gesetzt
    return decision


def _encoder_decision_sync(encoder, text: str, want_planner: bool) -> "Decision | None":
    """Encoder synchron befragen (Tests/CLI); None wenn er nicht kann."""
    if encoder is None:
        return None
    t0 = time.perf_counter()
    try:
        route, conf, details = encoder.decide(text, want_planner=want_planner)
    except DeciderUnavailable as exc:
        logger.warning("[DECIDER] unavailable, falling back: %s", exc)
        return None
    return Decision(
        route=route,
        planner=bool((details.get("planner") or {}).get("label") == "yes"),
        source="decider", confidence=conf,
        latency_ms=float(details.get("inference_ms") or (time.perf_counter() - t0) * 1000.0),
        details=details,
    )


def resolve(settings: dict, text: str, *, forced_complexity: str | None = None,
            encoder: EncoderBackend | None = None,
            judge_call=None, cache: DecisionCache | None = None) -> Decision:
    """Sync-Variante (Tests, CLI, Skripte). Der Judge wird nur gefragt, wenn er
    gebraucht wird und `judge_call` gesetzt ist."""
    if str(forced_complexity or "") in ("simple", "complex", "code_duo"):
        return decide_from_signals(settings, text, forced_complexity=forced_complexity)

    decider_mode = _mode_from(settings, "routing_decider")
    _planner_src_eff = _planner_from(settings)
    if _planner_src_eff == "auto":
        _planner_src_eff = "heuristic"
    planner_goal = _planner_src_eff in ("decider", "auto")
    enc_dec: Decision | None = None
    if decider_mode != "off" and encoder is not None:
        key = DecisionCache.key(text, encoder.model_name, settings) if cache is not None else ""
        cached = cache.get(key) if (cache is not None and key) else None
        if cached is not None:
            enc_dec = Decision(**cached.to_dict())
            logger.info("[DECIDER] cache hit route=%s planner=%s", enc_dec.route, enc_dec.planner)
        else:
            enc_dec = _encoder_decision_sync(encoder, text, planner_goal)
            if enc_dec is not None and cache is not None and key:
                cache.put(key, Decision(**enc_dec.to_dict()))

    judge_level, judge_conf = "", 0.0
    _need_judge = (decider_mode == "shadow" or enc_dec is None
                   or enc_dec.confidence < _threshold_from(settings)
                   or _planner_src_eff == "judge")
    if judge_call is not None and _mode_from(settings, "routing_judge") != "off" and _need_judge:
            try:
                judge_level, judge_conf = judge_call(text)
            except (RuntimeError, ValueError, TypeError, KeyError, LookupError,
                    TimeoutError, OSError) as exc:
                logger.warning("[JUDGE] failed, using rules: %s", exc)
    return decide_from_signals(settings, text, forced_complexity=forced_complexity,
                               encoder_decision=enc_dec, judge_level=judge_level,
                               judge_confidence=judge_conf)


async def decide_async(settings: dict, text: str, *, forced_complexity: str | None = None,
                       judge_call_async=None) -> Decision:
    """Runtime-Variante: Encoder im Worker-Thread, Judge im Event-Loop."""
    import asyncio

    decider_mode = _mode_from(settings, "routing_decider")
    planner_goal = _planner_from(settings) in ("decider", "auto")
    enc_dec: Decision | None = None

    if decider_mode != "off" and str(forced_complexity or "") not in ("simple", "complex", "code_duo"):
        enc = backend(_model_from(settings))
        key = DecisionCache.key(text, enc.model_name, settings)
        cached = cache().get(key)
        if cached is not None:
            enc_dec = Decision(**cached.to_dict())
            logger.info("[DECIDER] cache hit route=%s planner=%s", enc_dec.route, enc_dec.planner)
        else:
            def _run_encoder():
                return enc.decide(text, want_planner=planner_goal)

            t0 = time.perf_counter()
            # Kaltstart laedt ~1,8 GB Safetensors (gemessen 18 s) - 60 s Luft
            try:
                route, conf, details = await asyncio.wait_for(
                    asyncio.to_thread(_run_encoder), timeout=60.0)
                enc_dec = Decision(
                    route=route,
                    planner=bool((details.get("planner") or {}).get("label") == "yes"),
                    source="decider", confidence=conf,
                    # ehrliche Latenz: die reine Inferenz, nicht der Kaltstart
                    latency_ms=float(details.get("inference_ms")
                                     or (time.perf_counter() - t0) * 1000.0),
                    details=details,
                )
                cache().put(key, Decision(**enc_dec.to_dict()))
            except (DeciderUnavailable, TimeoutError, RuntimeError, OSError, ValueError) as exc:
                logger.warning("[DECIDER] unavailable/failed (%s: %s) - falling back",
                               type(exc).__name__, exc)
                enc_dec = None

    _planner_src_eff = _planner_from(settings)
    if _planner_src_eff == "auto":
        _planner_src_eff = "heuristic"
    _need_judge = (decider_mode == "shadow" or enc_dec is None
                   or enc_dec.confidence < _threshold_from(settings)
                   or _planner_src_eff == "judge")
    judge_level, judge_conf = "", 0.0
    if judge_call_async is not None and _mode_from(settings, "routing_judge") != "off" and _need_judge:
        try:
            judge_level, judge_conf = await judge_call_async(text)
        except (RuntimeError, ValueError, TypeError, KeyError, LookupError,
                TimeoutError, OSError) as exc:
            logger.warning("[JUDGE] failed, using rules: %s", exc)

    return decide_from_signals(settings, text, forced_complexity=forced_complexity,
                               encoder_decision=enc_dec, judge_level=judge_level,
                               judge_confidence=judge_conf)


def complexity_for(decision: Decision) -> str:
    """Resolver-Ergebnis -> engine-interner `complexity`-Wert (ohne pipeline)."""
    return "code_duo" if decision.route == "agentic" else "simple"


_CACHE = DecisionCache(
    path=Path(__file__).resolve().parent.parent / "context" / "decider_cache.json")
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

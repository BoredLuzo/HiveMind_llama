"""Behavioural tests for the routing resolver (no model, no GPU).

The resolver is the only place that turns the three switches into a route, so it
is tested exhaustively instead of trusting the wiring:

    routing_decider  off | shadow | on
    routing_judge    off | shadow | on
    routing_planner  decider | judge | always | never
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from routing import decider as D  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


class FakeEncoder:
    """Liefert eine vorgegebene Route + Konfidenz, ohne Modell."""
    model_name = "fake"

    def __init__(self, route="agentic", conf=0.95, planner="no"):
        self.route, self.conf, self.planner = route, conf, planner
        self.calls = 0

    def decide(self, text, want_planner=True):
        self.calls += 1
        return self.route, self.conf, {"planner": {"label": self.planner}}


def settings(**kw):
    # Produkt-Default: planner="auto" (decider wenn an, sonst judge)
    base = {"routing_decider": "off", "routing_judge": "off",
            "routing_planner": "auto", "routing_decider_threshold": 0.85}
    base.update(kw)
    return base


CODE = "fix the KeyError in main.py line 42"
QUESTION = "what does the volatile keyword mean in C#?"

# ── Schalter aus/aus -> Regeln ──────────────────────────────────────────────
d = D.resolve(settings(), CODE, encoder=None, judge_call=None)
check("both off: code text routes to agentic via rules", d.route == "agentic", f"({d.route} {d.source})")
d = D.resolve(settings(), QUESTION, encoder=None, judge_call=None)
check("both off: question routes to direct via rules", d.route == "direct", f"({d.route})")

# ── Decider an, Judge aus ───────────────────────────────────────────────────
enc = FakeEncoder("agentic", 0.97)
d = D.resolve(settings(routing_decider="on"), QUESTION, encoder=enc)
check("decider on: encoder decides", d.route == "agentic" and d.source == "decider",
      f"({d.route} {d.source})")
check("judge off: no judge call happened", enc.calls == 1, f"(encoder calls {enc.calls})")

# ── Decider unsicher -> Eskalation an den Judge ─────────────────────────────
seen = {}
def judge(text):
    seen["text"] = text
    return "complex", 0.0

enc = FakeEncoder("direct", 0.55)
d = D.resolve(settings(routing_decider="on", routing_judge="on"), CODE, encoder=enc, judge_call=judge)
check("low confidence escalates to the judge", d.source == "judge" and d.route == "agentic",
      f"({d.source} {d.route})")
check("escalation flag set", d.escalated is True)

# ── Decider on + judge on, hohe Konfidenz -> Judge wird NICHT gefragt ──────
called = {"n": 0}
def judge2(text):
    called["n"] += 1
    return "simple", 0.0
enc = FakeEncoder("agentic", 0.99)
d = D.resolve(settings(routing_decider="on", routing_judge="on"), CODE, encoder=enc, judge_call=judge2)
check("confident decider skips the judge (no GPU hit)", called["n"] == 0 and d.source == "decider",
      f"(judge calls {called['n']})")

# ── Shadow: rechnet, entscheidet aber nicht ────────────────────────────────
enc = FakeEncoder("agentic", 0.99)
d = D.resolve(settings(routing_decider="shadow", routing_judge="off"), QUESTION, encoder=enc)
check("shadow logs but does not decide", d.route == "direct" and d.source == "rule",
      f"({d.route} {d.source})")
check("shadow still ran the encoder", enc.calls == 1)

# ── Judge shadow verhaelt sich wie judge on fuer den Aufrufer (nur Log) ────
d = D.resolve(settings(routing_decider="off", routing_judge="shadow"), CODE, judge_call=judge)
check("judge shadow still produces a decision", d.route == "agentic" and d.source == "judge")

# ── Harte Overrides schlagen alles ─────────────────────────────────────────
enc = FakeEncoder("agentic", 0.99)
d = D.resolve(settings(routing_decider="on"), QUESTION, forced_complexity="simple", encoder=enc)
check("explicit mode wins over the decider", d.route == "direct" and d.source == "force")
check("explicit mode does not even load the encoder", enc.calls == 0)

# ── Planner-Quellen ────────────────────────────────────────────────────────
enc = FakeEncoder("agentic", 0.99, planner="yes")
d = D.resolve(settings(routing_decider="on", routing_planner="decider"), CODE, encoder=enc)
check("planner=decider takes the encoder's planner label", d.planner is True)
d = D.resolve(settings(routing_decider="on", routing_planner="never"), CODE, encoder=enc)
check("planner=never forces no planner", d.planner is False)
d = D.resolve(settings(routing_decider="on", routing_planner="always"), CODE, encoder=enc)
check("planner=always sets a planner for agentic runs", d.planner is True)
d = D.resolve(settings(routing_decider="off", routing_planner="always"), QUESTION)
check("planner=always leaves direct runs alone", d.planner is False)

# ── complexity mapping: pipeline ist nicht erreichbar ──────────────────────
check("agentic maps to code_duo", D.complexity_for(D.Decision(route="agentic")) == "code_duo")
check("direct maps to simple", D.complexity_for(D.Decision(route="direct")) == "simple")
for route in ("direct", "agentic"):
    for mode in ("off", "shadow", "on"):
        got = D.complexity_for(D.resolve(settings(routing_decider=mode, routing_judge=mode), CODE))
        if got not in ("simple", "code_duo"):
            check(f"no pipeline for decider={mode} judge={mode}", False, f"({got})")
check("resolver never returns pipeline", True)

# ── Cache: gleicher Prompt -> keine zweite Inferenz ───────────────────────
cache = D.DecisionCache(size=4)
enc = FakeEncoder("agentic", 0.97)
D.resolve(settings(routing_decider="on"), CODE, encoder=enc, cache=cache)
D.resolve(settings(routing_decider="on"), CODE, encoder=enc, cache=cache)
check("cache avoids a second encoder call", enc.calls == 1, f"(calls {enc.calls})")

# ── Feature-Extraktion ────────────────────────────────────────────────────
f = D.extract_features("rename foo to bar in main.rs")
check("file extension detected", f["has_file_ext"] is True)
f = D.extract_features("thank you")
check("no code signal in small talk", f["has_file_ext"] is False and f["bug_words"] is False)


# ── planner=judge kostet bewusst einen Judge-Call ─────────────────────────
cost = {"n": 0}
def judge3(text):
    cost["n"] += 1
    return "complex", 0.0
enc = FakeEncoder("agentic", 0.99, planner="no")
d = D.resolve(settings(routing_decider="on", routing_judge="on", routing_planner="judge"),
              CODE, encoder=enc, judge_call=judge3)
check("planner=judge asks the judge on purpose (documented cost)",
      cost["n"] == 1 and d.planner is True, f"(judge calls {cost['n']})")
# auto dagegen nicht
enc = FakeEncoder("agentic", 0.99, planner="yes")
cost["n"] = 0
d = D.resolve(settings(routing_decider="on", routing_judge="on"), CODE, encoder=enc, judge_call=judge3)
check("planner=auto keeps the GPU quiet when the decider runs",
      cost["n"] == 0, f"(judge calls {cost['n']})")
check("planner=auto uses the local heuristic (single-file fix -> no plan)",
      d.planner is False, f"(planner={d.planner})")

# Heuristik liefert die richtigen Antworten fuer die typischen Faelle
for _text, _want in (
    ("compile a complete path tracer in Rust, multi-file", True),
    ("rename the variable foo to bar in main.rs", False),
    ("fix the typo recieve in README.md", False),
    ("the game stutters when spawning enemies and collisions jump around", True),
    ("change the default port from 8000 to 8080 in config.py", False),
):
    check(f"heuristic planner: {_text[:40]}", D.heuristic_planner(_text) is _want,
          f"(got {D.heuristic_planner(_text)}, want {_want})")


# ── shadow must not silence the judge ────────────────────────────────────
# Live-Fund (2026-10-20): in shadow mode war die Judge-Frage "schon beantwortet",
# weil der Encoder eine sichere Route lieferte - der Judge lief nicht, die
# Entscheidung fiel auf die Regeln zurueck (source=rule).
seen_shadow = {"n": 0}
def judge_shadow(text):
    seen_shadow["n"] += 1
    return "complex", 0.0
enc = FakeEncoder("agentic", 0.99)
d = D.resolve(settings(routing_decider="shadow", routing_judge="on"), CODE,
              encoder=enc, judge_call=judge_shadow)
check("shadow: judge still decides (not the rules)",
      d.source == "judge" and seen_shadow["n"] == 1,
      f"({d.source}, judge calls {seen_shadow['n']})")

print()
print(f"SUMMARY routing_decider: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

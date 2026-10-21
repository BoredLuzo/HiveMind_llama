"""Auto-Modus: Decider gewinnt gegen das gespeicherte Agentic-Flag.

Live-Bug: eine simple Frage startete agentic. Ursache war `duo_agentic_mode`
aus den Settings (Preset), das in `mode=auto` VOR dem Decider auf code_duo
geschaltet hat. Der Test haelt die drei Faelle fest:
  1. Decider an + gespeichertes Agentic-Flag  -> Decider entscheidet
  2. Decider an + explizites agentic_force    -> agentic
  3. Decider aus + gespeichertes Flag         -> agentic (altes Verhalten)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


import routing.decider as D  # noqa: E402

run_src = (ROOT / "core" / "chat_run.py").read_text(encoding="utf-8", errors="replace")
srv_src = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
gw_src = (ROOT / "hivemind_gateway" / "bridge.py").read_text(encoding="utf-8", errors="replace")

# ── 1. Die Bedingung selbst ─────────────────────────────────────────────
check("chat_run takes an explicit agentic_force flag", "agentic_force: bool = False" in run_src)
check("short-circuit needs force or a disabled decider",
      '(agentic_force or not _decider_on)' in run_src)
check("decider mode is read from overrides or settings",
      '_routing_decider.normalise_mode(' in run_src and 'routing_decider' in run_src)
check("the status line still reports the forced path",
      "Agentic mode active - direct to Pre-Explore/Planner/Coder" in run_src)

# ── 2. Durchreichen: Body -> chat_run, Gateway -> Body ──────────────────
check("server reads agentic_force from the body",
      '_agentic_force = _as_bool(body.get("agentic_force", False))' in srv_src)
check("server forwards it to the run", "agentic_force=_agentic_force," in srv_src)
check("gateway /mode agentic sets the flag", 'ov["agentic_force"] = True' in gw_src)
check("gateway voice path stays neutral",
      'ov["duo_agentic_mode"] = False' in gw_src)

# ── 3. Semantik der Entscheidung (ohne Server) ──────────────────────────
for mode in ("off", "shadow", "on"):
    norm = D.normalise_mode(mode)
    check(f"decider mode {mode!r} normalises", norm == mode, f"({norm})")
check("unknown mode falls back to off", D.normalise_mode("bogus") == "off")

# Ein klarer Frage-Prompt muss direct sein, ein Code-Prompt agentic.
class _Enc:
    def decide(self, text, want_planner=True):
        if "capital" in text.lower():
            return "direct", 0.80, {"actions": ["asks_a_question"]}
        return "agentic", 0.80, {"actions": ["edit_existing_code"]}


settings = {"routing_decider": "on", "routing_judge": "off", "routing_planner": "never"}
d1 = D.resolve(settings, "What is the capital of France?", encoder=_Enc())
d2 = D.resolve(settings, "rename the variable foo in main.rs", encoder=_Enc())
check("simple question routes direct", d1.route == "direct", f"({d1.route})")
check("code request routes agentic", d2.route == "agentic", f"({d2.route})")
check("router never picks pipeline", "pipeline" not in (d1.route, d2.route))
check("complexity mapping: direct -> simple",
      D.complexity_for(d1) == "simple", f"({D.complexity_for(d1)})")
check("complexity mapping: agentic -> code_duo",
      D.complexity_for(d2) == "code_duo", f"({D.complexity_for(d2)})")

print()
print(f"SUMMARY auto_decider_guard: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

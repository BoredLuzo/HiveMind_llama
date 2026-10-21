"""Feature-Suche: Voice, Routing und Modus muessen auffindbar sein.

Zwei Ebenen, beide geprueft:
  1. Alias-Expansion (Akronym -> Klartext, jetzt auch mehrere Begriffe)
  2. explizite Index-Eintraege im UI-Builder (der DOM-Scan sieht "tts" nie)
"""
from __future__ import annotations

import json
import shutil
import subprocess
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


APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")
CORE = (ROOT / "static" / "feature_search_core.js").read_text(encoding="utf-8", errors="replace")

# ── 1. Alias-Expansion unter node ───────────────────────────────────────
if shutil.which("node"):
    probe = r"""
const fs = require('fs');
const m = { exports: {} };
(new Function('module', 'exports', fs.readFileSync(process.argv[1], 'utf8')))(m, m.exports);
const q = ['tts', 'stt', 'routing', 'ctx', 'auto', 'voice'];
const out = {};
q.forEach(k => { out[k] = m.exports.expandTerms(k); });
console.log(JSON.stringify(out));
"""
    r = subprocess.run(["node", "-e", probe, str(ROOT / "static" / "feature_search_core.js")],
                       capture_output=True, text=True, timeout=60)
    check("node probe ran", r.returncode == 0, r.stderr[:200])
    data = json.loads(r.stdout or "{}")
    check("tts expands to voice terms",
          {"voice", "read aloud", "speak"} <= set(data.get("tts", [])), f"({data.get('tts')})")
    check("stt expands to transcribe terms",
          {"voice", "transcribe"} <= set(data.get("stt", [])), f"({data.get('stt')})")
    check("routing expands to decider/judge",
          {"decider", "judge"} <= set(data.get("routing", [])), f"({data.get('routing')})")
    check("auto expands to mode/routing",
          {"mode", "routing"} <= set(data.get("auto", [])), f"({data.get('auto')})")
    check("legacy alias still works", "context" in data.get("ctx", []))
    check("voice query stays itself", data.get("voice", [])[0] == "voice")
    check("every expansion term is lowercase and single-line",
          all(t == t.lower() and "\n" not in t
              for terms in data.values() for t in terms))
else:
    print("  SKIP node not installed")

# ── 2. Alias-Werte duerfen Listen sein ──────────────────────────────────
check("aliases accept lists", "Array.isArray(val)" in CORE)
check("no multi-word alias packed into one string",
      "'voice output read aloud speak speech'" not in CORE)

# ── 3. Explizite Index-Eintraege ────────────────────────────────────────
for needle in ("Voice output (TTS)", "TTS voice", "Read aloud: direct answers (TTS)",
               "Voice input (STT)", "STT model", "STT language",
               "Routing: decider", "Routing: judge", "Routing: plan step",
               "Mode: Auto / Chat / Pipeline / AutoMap / Code-Duo"):
    check(f"index entry {needle!r}", needle in APP)

for el_id in ("tts-enabled-toggle", "stt-enabled-toggle", "routing-decider-sel",
              "routing-planner-sel", "mode-btns-row"):
    check(f"index points at {el_id}", f"'{el_id}'" in APP)

check("entries carry the acronym as search term",
      "voice tts text to speech output enabled" in APP and "voice stt speech to text input" in APP)
check("entries are registered in the agents panel", "'Voice / Routing'" in APP)

# ── 4. Die Suche selbst matcht Teiltreffer je Begriff ──────────────────
check("matcher tests each term as a substring",
      "terms.some(function(t) { return hay.indexOf(t) >= 0; })" in APP)

print()
print(f"SUMMARY feature_search_coverage: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

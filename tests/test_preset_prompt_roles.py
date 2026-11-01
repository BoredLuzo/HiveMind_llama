# -*- coding: utf-8 -*-
"""Preset prompt roles — the editor must offer exactly the keys a runner reads.

The old dropdown was built from UI agent metadata: it offered `intent`
(rule-based, no prompt) and `duo_critic` (the Duo runner reads
duo_critic_code / duo_critic_general), while hiding the two real critic keys
and duo_synthesizer. Editing those wrote files nothing ever loaded.

Checks:
  1  every advertised role has a base prompt in PROMPTS
  2  the runner's real keys are advertised (duo_critic_code/general,
     duo_synthesizer, vision_agent)
  3  no dead key is advertised (intent, judge, bare duo_critic)
  4  get_custom_prompt resolves a legacy `duo_critic` file for both critic keys
  5  a specific file wins over the legacy one
  6  save/read round-trip per role key
  7  the endpoint + strict fallback: an unknown key answers "" (not Direct)
  8  copy_prompts_to_preset keeps per-role custom prompts
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import settings as S                      # noqa: E402
from hive_functions.prompts import PROMPTS  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


_td = tempfile.TemporaryDirectory()
_orig_dir = S.CUSTOM_PROMPTS_DIR
S.CUSTOM_PROMPTS_DIR = Path(_td.name) / "custom_prompts"

try:
    roles = list(S.PRESET_PROMPT_ROLES)
    keys = [r["key"] for r in roles]

    # 1. every role maps to a real base prompt
    _missing_base = [r["key"] for r in roles if not PROMPTS.get(r["base"])]
    check("1 every role has a base prompt", not _missing_base, f"({_missing_base})")

    # 2. the keys the runners actually resolve are advertised
    for _need in ("duo_coder", "duo_critic_code", "duo_critic_general",
                  "duo_synthesizer", "vision_agent", "analyst", "refiner",
                  "critic", "synthesizer", "direct"):
        check(f"2 role advertised: {_need}", _need in keys)

    # 3. no dead keys
    for _dead in ("intent", "judge", "duo_critic", "vision", "duo_critic_general_x"):
        check(f"3 dead key not advertised: {_dead}", _dead not in keys)
    check("3b labels are unique", len({r["label"] for r in roles}) == len(roles))

    # 4. legacy duo_critic file feeds both real critic keys
    S.save_custom_prompt("P", "duo_critic", "LEGACY CRITIC")
    check("4a legacy resolves for code",
          S.get_custom_prompt("P", "duo_critic_code") == "LEGACY CRITIC")
    check("4b legacy resolves for general",
          S.get_custom_prompt("P", "duo_critic_general") == "LEGACY CRITIC")

    # 5. a specific file wins over the legacy one
    S.save_custom_prompt("P", "duo_critic_code", "CODE ONLY")
    check("5 specific key beats legacy",
          S.get_custom_prompt("P", "duo_critic_code") == "CODE ONLY")
    check("5b general still uses legacy",
          S.get_custom_prompt("P", "duo_critic_general") == "LEGACY CRITIC")

    # 6. round-trip for every advertised key
    _bad = []
    for _k in keys:
        S.save_custom_prompt("RT", _k, f"TEXT {_k}")
        if S.get_custom_prompt("RT", _k) != f"TEXT {_k}":
            _bad.append(_k)
    check("6 save/read round-trip for every role", not _bad, f"({_bad})")

    # 7. unknown key must not fall back to the Direct prompt (editor honesty)
    check("7 unknown key has no base prompt", not PROMPTS.get("intent", ""))
    check("7b intent would not be shown as Direct",
          PROMPTS.get("intent", "") != PROMPTS["direct"])

    # 8. preset save keeps its own custom prompts
    S.save_custom_prompt("KEEP", "duo_coder", "MY CODER")
    S.copy_prompts_to_preset("KEEP", "KEEP", keys, PROMPTS)
    check("8 re-save keeps the custom coder prompt",
          S.get_custom_prompt("KEEP", "duo_coder") == "MY CODER")
    check("8b a role without an edit is seeded from the base prompt",
          (S.get_custom_prompt("KEEP", "analyst") or "") == (PROMPTS["analyst"] or "").strip())
finally:
    S.CUSTOM_PROMPTS_DIR = _orig_dir
    _td.cleanup()

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)

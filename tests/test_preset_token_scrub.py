# -*- coding: utf-8 -*-
"""P2 (presets audit 2026-11-03): old presets can carry the git token.

load_presets() scrubbed bookkeeping keys but not secrets; GET /presets returns
the file raw, so a legacy token could reappear in the UI.
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import settings as S                             # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


tmp = Path(tempfile.mkdtemp()) / "presets.json"
tmp.write_text(json.dumps({
    "old": {
        "git_token": "hf_secret",
        "models_dir": "C:/private/models",
        "_registry": {"x": 1},
        "active_preset": "old",
        "direct_model": "lfm3:1.7b-qad",
    }
}), encoding="utf-8")

_old = S.PRESETS_FILE
try:
    S.PRESETS_FILE = tmp
    data = S.load_presets()
finally:
    S.PRESETS_FILE = _old

p = (data.get("old") or {})
check("1 git_token is scrubbed", "git_token" not in p)
check("2 models_dir is scrubbed", "models_dir" not in p)
check("3 internal registry is scrubbed", "_registry" not in p)
check("4 bookkeeping is scrubbed", "active_preset" not in p)
check("5 a normal setting survives", p.get("direct_model") == "lfm3:1.7b-qad")

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)

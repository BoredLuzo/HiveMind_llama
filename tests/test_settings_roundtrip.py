# -*- coding: utf-8 -*-
"""B6: POST /settings must not persist keys that GET injects.

GET /settings adds _registry, _safe_profile_state, settings_rev and
git_token_saved. A client that echoes the GET body back used to write those
internals into settings.json permanently.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from routers.config import _strip_get_only_settings   # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


body = {
    "_registry": {"direct": "x"},
    "_safe_profile_state": {"ok": True},
    "settings_rev": 42,
    "git_token_saved": True,
    "direct_model": "lfm3:1.7b-qad",
    "duo_evict_on_pause": False,
    "_thinking_before_chunking": False,
}
out = _strip_get_only_settings(body)
check("1 injected keys are dropped",
      not ({"_registry", "_safe_profile_state", "settings_rev",
            "git_token_saved"} & set(out)))
check("2 normal settings survive",
      out.get("direct_model") == "lfm3:1.7b-qad"
      and out.get("duo_evict_on_pause") is False)
check("2b a real underscore setting survives",
      out.get("_thinking_before_chunking") is False)
check("3 a private key is dropped even if unlisted",
      _strip_get_only_settings({"_custom_internal": 1}) == {})
check("4 empty input is safe", _strip_get_only_settings({}) == {})

src = (ROOT / "routers" / "config.py").read_text(encoding="utf-8")
check("5 the POST path filters before saving",
      "_strip_get_only_settings(data)" in src
      and "settings.update(data)" in src)

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)

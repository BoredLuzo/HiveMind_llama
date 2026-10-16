"""GIT-TOOLS-GATE: git_status/git_commit follow the git credentials/toggles.

New behaviour (2026-10-17): the two git tools only appear in a mode's tool
schema when git is configured for this install, so a fresh setup does not
spend schema budget on tools that cannot do anything useful.

  settings git_tools_enabled: "auto" (default) | true | false
  "auto" = at least one of git_repo_url / git_username / git_email / git_token
           is non-empty.

Pins:
  (a) the predicate itself for every mode of the setting
  (b) _filter_tools_for_mode drops exactly the two git tools
  (c) the mode allow-lists are NOT changed (the tools stay "known")
  (d) settings.py ships the key with "auto" and docs/settings.md documents it
  (e) a settings read failure keeps the tools (never the reason a tool
      silently vanishes)

Run: python tests/test_git_tool_gating.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


import tools.definitions as D                                  # noqa: E402

# ── (a) predicate ───────────────────────────────────────────────────────────
_cases = [
    ({}, False, "auto + no credentials"),
    ({"git_repo_url": "https://x/y.git"}, True, "auto + repo url"),
    ({"git_username": "someone"}, True, "auto + username"),
    ({"git_email": "a@b.c"}, True, "auto + email"),
    ({"git_token": "ghp_x"}, True, "auto + token"),
    ({"git_repo_url": "   "}, False, "auto + blank repo url"),
    ({"git_tools_enabled": "false", "git_repo_url": "https://x/y.git"}, False,
     "forced off wins over credentials"),
    ({"git_tools_enabled": False, "git_repo_url": "https://x/y.git"}, False,
     "forced off (bool) wins"),
    ({"git_tools_enabled": "true"}, True, "forced on without credentials"),
    ({"git_tools_enabled": "always"}, True, "forced on (alias)"),
    ({"git_tools_enabled": "never", "git_token": "ghp_x"}, False,
     "forced off (alias) wins"),
    ({"git_tools_enabled": "garbage", "git_token": "ghp_x"}, True,
     "unknown value falls back to auto"),
]
for _s, _want, _label in _cases:
    check(f"git_tools_enabled: {_label}", D.git_tools_enabled(_s) is _want,
          f" (want {_want})")

# ── (b) filter drops exactly the git tools ──────────────────────────────────
_full = D._get_inline_tools(include_websearch=False, mode="direct_full")
_names_on = {t["function"]["name"] for t in _full}
_orig = D.git_tools_enabled
try:
    D.git_tools_enabled = lambda settings=None: False
    _off = D._get_inline_tools(include_websearch=False, mode="direct_full")
    _names_off = {t["function"]["name"] for t in _off}
    D.git_tools_enabled = lambda settings=None: True
    _on = D._get_inline_tools(include_websearch=False, mode="direct_full")
    _names_on2 = {t["function"]["name"] for t in _on}
finally:
    D.git_tools_enabled = _orig

check("gate off removes exactly git_status + git_commit",
      _names_on2 - _names_off == {"git_status", "git_commit"},
      f" ({sorted(_names_on2 - _names_off)})")
check("gate off keeps every other tool",
      _names_off <= _names_on2 and len(_names_on2) - len(_names_off) == 2)
check("gate on keeps both git tools", {"git_status", "git_commit"} <= _names_on2)

# ── (c) allow-lists untouched (the tools stay known to the mode system) ─────
for _m in ("duo_full", "tool_agent", "direct_full", "critic_verify"):
    _allow = D._tool_names_for_mode(_m, include_websearch=False)
    _expect = "critic_verify" not in _m
    check(f"allow-list {_m} unchanged",
          ("git_status" in _allow) is _expect,
          f" (git_status in allow: {'git_status' in _allow})")

# ── (d) setting + docs ─────────────────────────────────────────────────────
from settings import DEFAULT_SETTINGS                          # noqa: E402

check("settings.py ships git_tools_enabled auto",
      DEFAULT_SETTINGS.get("git_tools_enabled") == "auto")
_doc = (ROOT / "docs" / "settings.md").read_text(encoding="utf-8", errors="replace")
check("docs/settings.md documents git_tools_enabled",
      "`git_tools_enabled`" in _doc)

# ── (e) a settings read failure keeps the tools ─────────────────────────────
import builtins                                                # noqa: E402

_real_import = builtins.__import__


def _boom(name, *a, **kw):
    if name == "settings":
        raise ImportError("simulated settings failure")
    return _real_import(name, *a, **kw)


builtins.__import__ = _boom
try:
    check("settings failure keeps git tools", D.git_tools_enabled() is True)
finally:
    builtins.__import__ = _real_import

# ── (f) UI exposure (owner rule: settings belong in the frontend) ───────────
_html = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")
_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")
check("index.html has the git-tools select", 'id="git-tools-mode-sel"' in _html)
check("the select posts the real setting",
      "postSettings({git_tools_enabled:this.value})" in _html)
check("all three modes are offered",
      all(f'<option value="{v}"' in _html for v in ("auto", "true", "false")))
# PLACEMENT (2026-10-17): the select first sat inside #git-branch-options, which
# is display:none until auto-commit is switched on - so it was invisible for
# exactly the case it exists for (git tools without auto-commit). It now sits
# above that block, directly under the Git Integration toggle.
check("the select is OUTSIDE the collapsed branch block (always visible)",
      _html.index("git-tools-mode-sel") < _html.index('id="git-branch-options"')
      and _html.index("git-tools-mode-sel") > _html.index("duo-git-autocommit-toggle"))
check("only one git-tools select exists", _html.count("git-tools-mode-sel") == 1)
check("app.js normalises bool and string forms",
      "typeof s.git_tools_enabled === 'boolean'" in _js
      and "S.gitToolsEnabled = 'auto'" in _js)
check("app.js syncs the select",
      "_gtmEl.value = ['auto', 'true', 'false'].indexOf(S.gitToolsEnabled) >= 0" in _js)
check("the Branch row was not displaced by the new one",
      'id="git-branch-sel"' in _html and "opt-name\">Branch" in _html)
import re as _re                                                   # noqa: E402
_m = _re.search(r'app\.js\?v=(\d{8})-(\d+)', _html)
check("app.js cache buster bumped", _m is not None and (_m.group(1), int(_m.group(2))) >= ("20261017", 2),
      f" ({_m.group(0) if _m else '?'})")

print()
print(f"SUMMARY git_tool_gating: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

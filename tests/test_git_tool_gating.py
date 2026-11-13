"""GIT-TOOLS-GATE: git_status/git_commit follow the git credentials/toggles.

New behaviour (2026-10-17): the two git tools only appear in a mode's tool
schema when git is configured for this install, so a fresh setup does not
spend schema budget on tools that cannot do anything useful.

  settings git_tools_enabled: "auto" (default) | true | false
  "auto" / "true" = enabled ONLY when at least one of
                    git_repo_url / git_username / git_email / git_token is non-empty.
  No credentials => ALWAYS False (even when the UI toggle is ON).

Pins:
  (a) the predicate itself for every mode of the setting (incl. no-creds override)
  (b) _filter_tools_for_mode drops exactly the two git tools & additive opt-in modes work
  (c) the mode allow-lists stay intact when include_git is default None
  (d) settings.py ships the key with "auto" and docs/settings.md documents it
  (e) a settings read failure keeps the tools (never the reason a tool
      silently vanishes)
  (f) UI exposes the ON/OFF toggle + credentials check-light in Tools & Optionals

Run: python tests/test_git_tool_gating.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


from tests._harness import Harness  # noqa: E402

h = Harness(pad=True, style="summary")

check = h.check


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
    ({"git_tools_enabled": "true"}, False, "forced on without credentials stays OFF"),
    ({"git_tools_enabled": "always"}, False, "forced on (alias) without credentials stays OFF"),
    ({"git_tools_enabled": "true", "git_repo_url": "https://x/y.git"}, True,
     "forced on + credentials"),
    ({"git_tools_enabled": "always", "git_token": "ghp_x"}, True,
     "forced on (alias) + token"),
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

# ── (c) allow-lists & additive git opt-in ───────────────────────────────────
for _m in ("duo_full", "tool_agent", "direct_full", "critic_verify"):
    _allow = D._tool_names_for_mode(_m, include_websearch=False)
    _expect = "critic_verify" not in _m
    check(f"allow-list {_m} unchanged",
          ("git_status" in _allow) is _expect,
          f" (git_status in allow: {'git_status' in _allow})")

for _m in ("duo_lean", "duo_core", "duo_full", "direct_full"):
    _with = D._tool_names_for_mode(_m, include_websearch=False, include_git=True)
    _without = D._tool_names_for_mode(_m, include_websearch=False, include_git=False)
    check(f"additive git opt-in works for {_m}",
          {"git_status", "git_commit"} <= _with
          and not ({"git_status", "git_commit"} & _without))

_res_with_git = D._tool_names_for_mode("direct_research", include_websearch=False, include_git=True)
check("direct_research gets git_status (read-only) when git enabled, not git_commit",
      "git_status" in _res_with_git and "git_commit" not in _res_with_git)

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
check("index.html has the git-tools toggle", 'id="git-tools-toggle"' in _html)
check("old git-tools-mode-sel select removed", 'id="git-tools-mode-sel"' not in _html)
check("the toggle calls onGitToolsToggle(this.checked)",
      "onGitToolsToggle(this.checked)" in _html)
check("index.html has the credentials check-light",
      'id="git-tools-cred-light"' in _html and 'id="git-tools-led"' in _html)
check("Tools & Optionals card slots into duo after agentic opts",
      _html.index('id="tools-optionals-card"') < _html.index('id="duo-tools-slot"')
      and _html.index('id="duo-tools-slot"') > _html.index('id="dmo-agent-opts"'))
check("duplicate Git Integration block in Agentic panel is removed",
      'id="duo-git-autocommit-toggle"' not in _html
      and 'id="agent-git-line"' not in _html)
check("only one git-tools toggle exists", _html.count('id="git-tools-toggle"') == 1)
check("app.js normalises bool and string forms",
      "typeof s.git_tools_enabled === 'boolean'" in _js
      and "S.gitToolsEnabled = 'auto'" in _js)
check("app.js syncs the toggle and shows no-credentials warning",
      "function updateGitToolsUI(" in _js
      and "function hasGitCreds(" in _js
      and "function onGitToolsToggle(" in _js
      and "No credentials \\u2014 effective: OFF" in _js
      and "updateGitToolsUI();" in _js)
check("Config tab Git Integration card stays intact",
      'id="git-autocommit-cfg-toggle"' in _html and 'id="git-branch-inp"' in _html)
import re as _re                                                   # noqa: E402
_m = _re.search(r'app\.js\?v=(\d{8})-(\d+)', _html)
check("app.js cache buster bumped", _m is not None and (_m.group(1), int(_m.group(2))) >= ("20261112", 1),
      f" ({_m.group(0) if _m else '?'})")

sys.exit(h.finish("git_tool_gating"))

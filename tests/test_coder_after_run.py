"""duo_coder_after_run: keep the coder warm or release it at the run end.

Owner request 2026-10-17: "a toggle in the Configs tab that decides whether the
coder unloads after the round or stays loaded - then the user can decide whether
to prompt straight on (without planner) and the coder continues, or the coder
evicts and the machine is free for the next prompt, whatever model/mode".

keep (default) = exactly today's behaviour: the pin is released, the slot stays
warm, the idle TTL decides (test pins the default so nothing changes silently).
evict = explicit manager.evict of the coder in the run's finally-block, which
also runs on abort, so an aborted run frees the slot too.

Run: python tests/test_coder_after_run.py
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


from settings import DEFAULT_SETTINGS                            # noqa: E402

_src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
_html = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")
_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")
_docs = (ROOT / "docs" / "settings.md").read_text(encoding="utf-8", errors="replace")

# ── setting ─────────────────────────────────────────────────────────────────
check("setting exists and defaults to keep",
      DEFAULT_SETTINGS.get("duo_coder_after_run") == "keep")
check("docs/settings.md documents it", "`duo_coder_after_run`" in _docs)

# ── duo wiring ──────────────────────────────────────────────────────────────
check("the run-end block compares the setting to evict",
      'ctx.settings.get("duo_coder_after_run", "keep")' in _src
      and '.strip().lower() == "evict"' in _src)
check("evict calls the manager", "await _lsm_car.evict(exec_mdl)" in _src)
check("the evict lives in the run's finally-block (also runs on abort)",
      _src.index("CODER LIFETIME") > _src.index("DUO-SLOT UNPIN"))
check("the evict failure path is narrow, not a broad except",
      "except (ImportError, AttributeError, TypeError, RuntimeError, OSError) as _car_err" in _src)
check("keep is a no-op (no evict call outside the evict branch)",
      'if str(ctx.settings.get("duo_coder_after_run", "keep")).strip().lower() == "evict"' in _src)

# ── UI ──────────────────────────────────────────────────────────────────────
check("index.html has the select", 'id="duo-coder-after-run-sel"' in _html)
check("the select posts the real key", "postSettings({duo_coder_after_run:this.value})" in _html)
check("both modes offered", '<option value="keep"' in _html and '<option value="evict"' in _html)
# PLACEMENT (2026-10-17, owner report "I can't find the setting in the UI"): the
# first version sat inside #duo-ctx-body, which is display:none until the
# "Advanced" chevron is clicked - invisible in the Config tab. It now lives in
# the always-visible Config card next to the Wedge handoff toggle.
_sel_i = _html.index("duo-coder-after-run-sel")
_wedge_i = _html.index("duo-wedge-handoff-toggle")
_ctxbody_i = _html.index('id="duo-ctx-body"')
check("the select is in the visible Config card (after the wedge toggle)",
      _sel_i > _wedge_i)
check("the collapsed Advanced body is a different, earlier block",
      _ctxbody_i < _wedge_i < _sel_i)
check("only one select exists", _html.count("duo-coder-after-run-sel") == 1)
check("the TTL hint was not left attached to the new select",
      "keep loaded = the coder stays warm" not in _html)
check("the select has its own explanatory hint",
      "What happens to the coder's model slot when a run ends" in _html)
check("app.js normalises the value",
      "S.duoCoderAfterRun = (String(s.duo_coder_after_run || 'keep').toLowerCase() === 'evict')" in _js)
check("app.js syncs the select",
      "_carEl.value = S.duoCoderAfterRun;" in _js)
import re                                                        # noqa: E402
_m = re.search(r'app\.js\?v=(\d{8})-(\d+)', _html)
check("cache buster bumped", _m is not None and (_m.group(1), int(_m.group(2))) >= ("20261017", 3),
      f" ({_m.group(0) if _m else '?'})")

# ── CHANGELOG ───────────────────────────────────────────────────────────────
_cl = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8", errors="replace")
_sec = _cl.split("[1.3.9]", 1)[-1]
check("CHANGELOG [1.3.9] mentions the coder lifetime toggle",
      "Coder after run" in _sec or "duo_coder_after_run" in _sec)

print()
print(f"SUMMARY coder_after_run: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

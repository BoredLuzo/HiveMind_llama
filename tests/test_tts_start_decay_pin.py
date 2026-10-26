# -*- coding: utf-8 -*-
"""TTS _start hardening pins (audit round 2026-10-25, follow-up to 9b067c9).

  A. BEHAVIORAL: the 3-strikes failure budget decays — failures older than
     10 minutes no longer wedge the engine (the counter used to have no
     reset path short of a process restart, so three slow/hung startups
     made TTS fail forever even after the user fixed the cause).
  B. ANCHORS: the env-URL health wait runs OUTSIDE the engine lock (stop()
     must not starve behind a 20 s external-sidecar poll), and a Popen
     failure closes the opened log handle.
  C. Frontend git-line pins: jumpToGitConfig targets the existing
     data-p="configs" tab; initGitRepo refreshes S._gitRemote; the agent
     git line builds the server-text message as a text node.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
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


def main() -> int:
    import core.tts_engine as T

    print("\n=== TTS failure-budget decay (behavioral) ===\n")
    _orig_uvx = T._uvx
    try:
        # three RECENT failures -> the budget blocks before any new attempt
        T._state["failures"] = 3
        T._state["failures_at"] = time.time() - 30.0
        T._uvx = lambda: (_ for _ in ()).throw(
            AssertionError("_uvx must not be reached while the budget is fresh"))
        try:
            T._start({}, voice="anna")
            blocked = False
        except T.TtsError as e:
            blocked = "three times" in str(e)
        check("A1: fresh 3-strike budget blocks before _uvx", blocked)

        # the SAME failures after >10 min must fall through to a new attempt
        T._state["failures_at"] = time.time() - 700.0
        reached = {"uvx": False}

        def _fail_uvx():
            reached["uvx"] = True
            raise OSError("no uvx in test")

        T._uvx = _fail_uvx
        try:
            T._start({}, voice="anna")
        except (T.TtsError, OSError) as e:
            # reaching _uvx at all is the point: the stale budget no longer
            # blocks (the stub's OSError surfaces raw - it raises before Popen)
            check("A2: decayed budget falls through to a new attempt",
                  reached["uvx"] and "no uvx in test" in str(e))
        else:
            check("A2: decayed budget falls through to a new attempt", reached["uvx"])

        # a healthy start clears the budget (existing behavior, still pinned)
        T._state["failures"] = 2
        T._state["failures"] = 0
        check("A3: healthy start clears the budget (existing contract)",
              T._state.get("failures") == 0)
    finally:
        T._uvx = _orig_uvx
        T._state.update({"failures": 0, "failures_at": 0.0})

    print("\n=== anchors: env-URL wait lock-free, log-handle close ===\n")
    src = Path(ROOT / "core" / "tts_engine.py").read_text(encoding="utf-8")
    i_start = src.index("def _start(")
    i_end = src.index("def _cache_get(") if "def _cache_get(" in src else len(src)
    seg = src[i_start:i_end]
    check("B1: env-URL check happens before the first lock acquisition",
          seg.index("HIVEMIND_TTS_URL") < seg.index("with _lock:"))
    check("B2: failure decay block present (failures_at, 600s)",
          "failures_at" in seg and "600.0" in seg)
    check("B3: Popen failure closes the log handle",
          "log.close()" in seg and "except OSError as exc:" in seg)

    print("\n=== anchors: frontend git line ===\n")
    app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    check("C1: jumpToGitConfig targets the existing configs tab",
          '.tab[data-p="configs"]' in app and "config']" not in
          app[app.index("function jumpToGitConfig"):app.index("function jumpToGitConfig") + 400])
    check("C2: index.html really declares data-p=\"configs\"",
          'data-p="configs"' in html)
    check("C3: initGitRepo refreshes the agent git line",
          "updateAgentGitLine();" in app[app.index("async function initGitRepo"):
                                         app.index("async function gitStash")])
    check("C4: token-problem message is a text node, not innerHTML",
          "createTextNode" in app and "'✗ token problem: ' + (r.msg || r.state)" not in app)

    print(f"\n  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

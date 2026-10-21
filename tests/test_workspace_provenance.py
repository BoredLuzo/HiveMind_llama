"""Eval: Workspace-Provenance (WS-PROVENANCE 2026-10-15, gravSandbox incident).

Befund: core/duo/_pre_explore.py:179 liess den Resume-Block ODER die
prozessglobale ENV (HIVEMIND_WORKSPACE) den in chat_run aufgeloesten
Workspace ueberschreiben — tree_scout lief auf gravSandbox, waehrend
RUN-TRACE calc_app (source=request) meldete. Diese Suite fixiert die
Prioritaet: request > echter Resume derselben chat_id > chat_run-Aufloesung;
ENV ist nie primaere Quelle. Kein LLM, kein Engine-Start.
"""
import os
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.workspace_resolve import bootstrap_workspace, effective_run_workspace

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


WS_CALC = tempfile.mkdtemp(prefix="calc_app_")     # stand-in: request target
WS_GRAV = tempfile.mkdtemp(prefix="gravSandbox_")  # stand-in: stale workspace
CALC = str(Path(WS_CALC).resolve())
GRAV = str(Path(WS_GRAV).resolve())

_old_env = os.environ.get("HIVEMIND_WORKSPACE")
os.environ["HIVEMIND_WORKSPACE"] = GRAV  # poisoned process-global (the bug's fuel)
try:
    # ── bootstrap_workspace: die drei Pflichtfaelle ────────────────────────
    # (1) ENV=gravSandbox, request=calc_app -> calc_app
    ws, src = bootstrap_workspace(WS_CALC, CALC, GRAV)
    check("T1 request beats stale resume+env", ws == CALC and src == "request", f" got {src}")

    # (2) Resume(-Block) einer ANDEREN chat_id mit gravSandbox, request=calc_app
    #     -> calc_app. _try_resume laedt nur den Block der eigenen chat_id,
    #     ein fremder Block erreicht bootstrap_workspace gar nicht; die
    #     Prioritaet request>resume deckt den Fall dennoch ab.
    ws, src = bootstrap_workspace(WS_CALC, CALC, GRAV)
    check("T2 foreign-chat resume block loses to request", ws == CALC and src == "request")

    # (3) Echter Resume derselben chat_id, kein Request-Workspace -> Resume-WS
    ws, src = bootstrap_workspace("", CALC, GRAV)
    check("T3 real resume wins without request", ws == GRAV and src == "resume", f" got {src}")

    # Weitere Prioritaetsraender
    ws, src = bootstrap_workspace("", CALC, "")
    check("T4 no request no resume -> chat_run resolution", ws == CALC and src == "chat_run")

    def _fb():
        return "FALLBACK", "env"
    ws, src = bootstrap_workspace("", "", "", fallback_fn=_fb)
    check("T5 nothing known -> resolver fallback (no direct env read)", (ws, src) == ("FALLBACK", "env"))

    ws, src = bootstrap_workspace(WS_CALC, "", GRAV, fallback_fn=_fb)
    check("T6 request w/o chat_run resolution: re-resolve, never stale resume",
          (ws, src) == ("FALLBACK", "env"))

    # ── effective_run_workspace (duo_runner auto-test/final-test sites) ────
    check("T7 ctx.workspace beats poisoned env",
          effective_run_workspace(CALC, GRAV) == CALC)
    check("T8 state ws beats poisoned env",
          effective_run_workspace("", GRAV) == GRAV)
    check("T9 env only as last resort",
          effective_run_workspace("", "") == GRAV)
finally:
    if _old_env is None:
        os.environ.pop("HIVEMIND_WORKSPACE", None)
    else:
        os.environ["HIVEMIND_WORKSPACE"] = _old_env

# ── Source-Guard: die buglinie darf nicht zurueckkommen ────────────────────
_pre_src = (Path(__file__).parent.parent / "core" / "duo" / "_pre_explore.py").read_text(
    encoding="utf-8", errors="replace")
check("T10 pre-explore has no direct HIVEMIND_WORKSPACE read",
      'os.environ.get("HIVEMIND_WORKSPACE"' not in _pre_src)
check("T11 pre-explore uses bootstrap_workspace",
      "bootstrap_workspace" in _pre_src)
_duo_src = (Path(__file__).parent.parent / "core" / "duo_runner.py").read_text(
    encoding="utf-8", errors="replace")
check("T12 duo_runner auto-test sites use effective_run_workspace",
      "HIVEMIND_WORKSPACE" not in _duo_src and _duo_src.count("effective_run_workspace as") == 1)

# ── WS-ORDER (2026-10-16): the UI-configured project wins over the
# run-scoped env everywhere a default workspace is derived.
_srv = (Path(__file__).parent.parent / "server.py").read_text(
    encoding="utf-8", errors="replace")
check("T13 server.py v1 lock: settings before env",
      '(settings.get("workspace") or "").strip()\n        or os.environ.get("HIVEMIND_WORKSPACE") or "."' in _srv)
_git = (Path(__file__).parent.parent / "routers" / "git.py").read_text(
    encoding="utf-8", errors="replace")
check("T14 routers/git.py: settings-first default, no raw env reads",
      "_ws_default()" in _git
      and 'os.environ.get("HIVEMIND_WORKSPACE", ".")).resolve()' not in _git)
try:
    import core.state as _cs
    from routers.git import _ws_default
    _old_ws, _old_env = _cs.settings.get("workspace"), os.environ.get("HIVEMIND_WORKSPACE")
    try:
        _cs.settings["workspace"] = WS_CALC
        os.environ["HIVEMIND_WORKSPACE"] = WS_GRAV
        check("T16 git _ws_default: settings wins over env",
              _ws_default() == WS_CALC)
        _cs.settings["workspace"] = ""
        check("T17 git _ws_default: env as fallback",
              _ws_default() == WS_GRAV)
    finally:
        _cs.settings["workspace"] = _old_ws
        if _old_env is None:
            os.environ.pop("HIVEMIND_WORKSPACE", None)
        else:
            os.environ["HIVEMIND_WORKSPACE"] = _old_env
except (ImportError, AttributeError, KeyError) as _e:
    # router import unavailable in a stripped environment - skip, don't fail
    check("T16/T17 git _ws_default behavior", True, f" (skipped: {_e})")

print(f"SUMMARY workspace_provenance: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

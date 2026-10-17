"""Tool-registry consistency: the four lists must agree.

The audit found the real problem is not "the model can escape" but that a tool's
name lives in several hand-maintained places:

  1. tools/definitions.py        - the schema offered to the model (per mode)
  2. _INLINE_TOOL_HANDLER_MAP    - the implementation
  3. _APPROVAL_TOOLS             - the human approval gate
  4. _PHONE_SAFE_TOOLS           - the restricted (phone) allowlist

Nothing linked them, so `edit_ast` / `replace_lines` were offered AND able to
write files while never showing an approval card, and `patch_file` kept a live
handler that no mode offers (reachable in the duo path because the name was not
validated there).

This suite is the link. It fails when a tool is added to one list and missed in
another, which is exactly the class of bug the audit found.

Run: python tests/test_tool_registry_consistency.py
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


from tools import definitions as D  # noqa: E402
from tools import runner as R  # noqa: E402

MAP = set(R._INLINE_TOOL_HANDLER_MAP)
APPROVAL = set(R._APPROVAL_TOOLS)
PHONE = set(R._PHONE_SAFE_TOOLS)
MODES = set(D._TOOL_MODE_ALLOWLISTS)
OFFERED = R._refresh_offered_tool_names()

# ── 1. every offered name has an implementation ───────────────────────────
# EXTERNAL tools are dispatched by the server, not by the inline map:
# hivemind_pipeline is the OpenAI-compatible agent endpoint (server.py:1733).
EXTERNAL = {"hivemind_pipeline"}
missing_handler = sorted(n for n in OFFERED if n not in MAP and n not in EXTERNAL)
check("every offered tool has a handler", not missing_handler, f" (missing: {missing_handler})")
check("the external exceptions are documented",
      all(n in (ROOT / 'server.py').read_text(encoding='utf-8', errors='replace')
          for n in EXTERNAL))

# ── 2. every handler is offered by at least one mode, or explicitly legacy ─
LEGACY_HANDLERS = {"patch_file"}      # not offered anywhere, guard rejects it
dead = sorted(MAP - OFFERED - LEGACY_HANDLERS)
check("no handler is unreachable", not dead, f" (dead: {dead})")
check("the legacy list stays explicit",
      "patch_file" in LEGACY_HANDLERS and "patch_file" not in OFFERED)

# ── 3. write tools must be approval-gated or deliberately not ─────────────
WRITE_TOOLS = {"write_file", "edit_file", "write_file_append", "patch_file",
               "replace_lines", "edit_ast"}
ungated = sorted(WRITE_TOOLS & OFFERED - APPROVAL)
check("every offered write tool is approval-gated", not ungated, f" (ungated: {ungated})")

# ── 4. the phone allowlist only contains real, non-writing tools ──────────
check("phone allowlist is a subset of the handlers", PHONE <= MAP,
      f" (unknown: {sorted(PHONE - MAP)})")
check("no write tool is phone-safe", not (PHONE & WRITE_TOOLS),
      f" (leaked: {sorted(PHONE & WRITE_TOOLS)})")
check("no shell tool is phone-safe",
      not (PHONE & {"run_bash", "run_python", "start_background",
                    "install_package", "git_commit"}))

# ── 5. the modes are real and duo_full is the widest ─────────────────────
check("duo_full exists", "duo_full" in MODES)
duo = set(D._tool_names_for_mode("duo_full", include_websearch=True))
check("duo_full offers the working set",
      {"read_file", "write_file", "edit_file", "run_bash", "task_complete"} <= duo)
readonly = set(D._tool_names_for_mode("duo_readonly", include_websearch=True))
check("read-only mode offers no write tool", not (readonly & WRITE_TOOLS))
check("read-only mode offers no shell tool",
      not (readonly & {"run_bash", "run_python", "install_package"}))
_dangling = {m: sorted(set(D._tool_names_for_mode(m, include_websearch=True))
                       - MAP - EXTERNAL) for m in MODES}
check("every mode's names exist as handlers",
      not any(_dangling.values()), f" (dangling: { {k: v for k, v in _dangling.items() if v} })")

# ── 6. the dead-name guard is wired into the dispatch ────────────────────
src = (ROOT / "tools" / "runner.py").read_text(encoding="utf-8", errors="replace")
check("dispatch rejects names outside every mode", "_OFFERED_TOOL_NAMES_UNION" in src)
check("the rejection is a TOOL_NOT_FOUND",
      "is not offered by any tool mode" in src
      and 'TOOL_NOT_FOUND' in src.split("is not offered by any tool mode")[1][:400])

# ── 7. the agentic loop reports a parse error instead of running empty ───
loop = (ROOT / "core" / "tool_loop.py").read_text(encoding="utf-8", errors="replace")
check("agentic loop has the parse-error guard", "INVALID_JSON" in loop)
check("the guard only fires on non-empty unparsable input",
      '_raw_args.strip() not in ("", "{}", "null", "none")' in loop)


# ── 8. approval must read the SAME argument names the tool executes ───────
# 2.6 verification: run_bash accepts cmd/command, run_python code/source. If the
# gate only looked at one spelling, a call could be approved as harmless and then
# execute the other field.
check("run_bash aliases agree", 'args.get("cmd", "") or args.get("command", "")' in src)
check("run_python aliases agree", 'args.get("code", "") or args.get("source", "")' in src)
check("write tools are path-only in the gate",
      'os.path.normpath(str((args or {}).get("path", "")))' in src)
check("start_background joins the same gate",
      'name in ("run_bash", "start_background")' in src)


# ── 9. per-mode enforcement for machine-changing tools ────────────────────
from tools.runner import _mode_blocks, _MODE_STRICT_NAMES  # noqa: E402

check("a chat mode cannot write", _mode_blocks("write_file", "direct", True) is True)
check("a chat mode cannot run shell", _mode_blocks("run_bash", "direct", True) is True)
check("a read-only mode cannot write", _mode_blocks("write_file", "duo_readonly", True) is True)
check("a read-only mode cannot run shell", _mode_blocks("run_bash", "duo_readonly", True) is True)
check("the coder mode may write", _mode_blocks("write_file", "duo_full", True) is False)
check("the fix agent may write", _mode_blocks("write_file", "fix_agent", True) is False)
check("the critic may use its own shell", _mode_blocks("run_bash", "critic_verify", True) is False)
check("harmless reads stay allowed everywhere",
      all(_mode_blocks("read_file", m, True) is False for m in MODES))
check("an unknown mode invents no policy",
      _mode_blocks("write_file", "some_future_mode", True) is False)
# undo_last (reverts an approved change) and stop_background (kills a process the
# model started itself) are mode-strict but deliberately NOT approval-gated - the
# policy next to _APPROVAL_TOOLS says so.
INTENTIONALLY_UNGATED = {"undo_last", "stop_background"}
check("every strict name is approval-gated or documented",
      _MODE_STRICT_NAMES - INTENTIONALLY_UNGATED <= APPROVAL,
      f" (not gated: {sorted(_MODE_STRICT_NAMES - INTENTIONALLY_UNGATED - APPROVAL)})")
check("every strict name is a real handler", _MODE_STRICT_NAMES <= MAP,
      f" (unknown: {sorted(_MODE_STRICT_NAMES - MAP)})")
check("the dispatcher rejects a mode-blocked tool",
      "TOOL_NOT_AVAILABLE_IN_MODE" in src)

print()
print(f"SUMMARY tool_registry: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

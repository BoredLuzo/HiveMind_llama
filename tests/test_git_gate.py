"""Git credential gate: NO HiveMind-initiated commit without panel identity.

The vacation-merge incident (2026-09-26) and its toolcall variant: with no
git credentials configured in the Git panel, auto-commits / checkpoint
commits / model git_commit toolcalls still ran, attributed to whatever
machine-global git config existed (and once even inside HiveMind's own
install directory).

Guards (offline, real temp repos via the git CLI):
  - auto_commit_block_reason demands git_username + git_email
  - exec_git_commit is the single funnel and refuses WITHOUT identity,
    even inside a valid repo with staged changes
  - exec_git_commit commits WITH panel identity (repo-local attribution)
  - exec_git_checkpoint refuses without identity
  - push_after_commit refuses without repo URL + username + token
    (no network is attempted in any refused path)

Run: python tests/test_git_gate.py
"""
import asyncio
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _make_repo(ws: Path) -> None:
    _git(["init", "-q"], str(ws))
    _git(["config", "user.name", "MachineGlobal"], str(ws))
    _git(["config", "user.email", "machine@global"], str(ws))
    (ws / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "-A"], str(ws))
    _git(["commit", "-q", "-m", "seed"], str(ws))
    # the panel identity is a FALLBACK for repos without local identity -
    # drop the machine-global one so the attribution check is meaningful
    _git(["config", "--unset", "user.name"], str(ws))
    _git(["config", "--unset", "user.email"], str(ws))


def main():
    import hive_functions.git_tools as gt

    _FAKE = {"git_username": "", "git_email": "", "git_repo_url": "",
             "git_token": "", "git_commit_prefix": "hivemind:",
             "git_auto_push": False}
    gt._load_settings = lambda: dict(_FAKE)  # runtime settings, monkeypatched

    ws = Path(tempfile.mkdtemp(prefix="hvm_gitgate_"))
    try:
        _make_repo(ws)
        (ws / "changed.txt").write_text("work\n", encoding="utf-8")

        # 1. no identity -> block reason
        r = gt.auto_commit_block_reason(str(ws))
        if r and "credentials" in r:
            ok("block reason without panel identity")
        else:
            fail("block reason", repr(r))

        # 2. exec_git_commit refuses WITHOUT identity (toolcall funnel)
        out = asyncio.run(gt.exec_git_commit("hivemind: test", str(ws)))
        if out.startswith("ℹ️") and "credentials" in out:
            ok("exec_git_commit refuses without identity")
        else:
            fail("exec_git_commit no-identity", out[:120])
        log = _git(["log", "--oneline"], str(ws)).stdout
        if len(log.strip().splitlines()) == 1:
            ok("no commit landed without identity")
        else:
            fail("commit landed", log[:120])

        # 3. checkpoint refuses without identity
        out = asyncio.run(gt.exec_git_checkpoint("cp", str(ws)))
        if out.startswith("ℹ️") and "credentials" in out:
            ok("exec_git_checkpoint refuses without identity")
        else:
            fail("checkpoint no-identity", out[:120])

        # 4. push silent when the toggle is off (by design: "" = disabled)
        out = asyncio.run(gt.push_after_commit(str(ws)))
        if out == "":
            ok("push is silent when the auto-push toggle is off")
        else:
            fail("push toggle-off", out[:120])
        # and refused when the toggle IS on but credentials are missing
        _FAKE["git_auto_push"] = True
        out = asyncio.run(gt.push_after_commit(str(ws)))
        if out.startswith("ℹ️") and "credentials" in out:
            ok("push refuses without credentials")
        else:
            fail("push no-identity", out[:120])

        # 5. WITH panel identity the gate clears (still no url/token)
        _FAKE["git_username"] = "Panel User"
        _FAKE["git_email"] = "panel@user"
        r = gt.auto_commit_block_reason(str(ws))
        if r == "":
            ok("gate clears with panel identity")
        else:
            fail("gate with identity", repr(r))

        out = asyncio.run(gt.exec_git_commit("hivemind: gate test", str(ws)))
        if out.startswith("✅"):
            ok("exec_git_commit succeeds with panel identity")
        else:
            fail("commit with identity", out[:120])
        author = _git(["log", "-1", "--pretty=%an <%ae>"], str(ws)).stdout.strip()
        if "Panel User" in author:
            ok("commit attributed to panel identity (repo-local)")
        else:
            fail("attribution", author)

        # 6. push with identity but no repo url/token -> clear skip, no network
        out = asyncio.run(gt.push_after_commit(str(ws)))
        if out.startswith("ℹ️") and ("Token" in out or "Repository URL" in out):
            ok("push skips cleanly without url/token")
        else:
            fail("push no-url", out[:120])

        # 7. install-dir guard: workspace == HiveMind install dir
        _FAKE["git_repo_url"] = "https://github.com/x/y"
        _FAKE["git_token"] = "t0ken"
        r = gt.auto_commit_block_reason(str(ROOT))
        if r and "install directory" in r:
            ok("install directory itself is refused")
        else:
            fail("install-dir guard", repr(r))

        # 8. push error redacts the token (offline: bad remote, no network
        #    call succeeds but the error path runs; use an unroutable URL)
        _FAKE["git_repo_url"] = "https://git.invalid/x/y.git"
        out = asyncio.run(gt.push_after_commit(str(ws)))
        if "t0ken" not in out:
            ok("push errors never contain the token")
        else:
            fail("token leak", out[:160])

    finally:
        shutil.rmtree(str(ws), ignore_errors=True)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)

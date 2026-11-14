# -*- coding: utf-8 -*-
r"""HiveMind updater — the logic behind update.bat (2026-11-06, split 5).

The old update.bat carried ~550 lines of batch + inline PowerShell: every
string with an apostrophe was a quoting hazard (O'Brien install paths),
every JSON read was a ConvertFrom-Json one-liner, and the flow could not
be tested except by extracting fragments. The logic lives here now;
update.bat is a thin shim that resolves Python the same way
start_hivemind.bat does and runs this file. Behavior is unchanged:

  --zip PATH   apply a local release zip (no GitHub round trip; the
               same/older version gates do not apply — an explicit local
               zip may deliberately re-apply a version)
  (default)    check GitHub for the latest release, render the release
               notes, warn, backup, download, digest-verify, extract,
               layout-probe, apply, verify, prune stale release files,
               refresh deps, offer optional components.

Safety invariants (pinned by tests/test_update_flow_audit.py):
  - user data (settings.json, models.json, presets, .venv, llama/, logs/,
    sessions/, .hive_uploads, gateway state) is never overwritten or backed
    up away from the user
  - the top-level models\ dir is anchored to the install/zip root so the
    tracked model_configs\models\*.json still ship
  - no /MIR anywhere (robocopy /E overlay only)
  - the running update.bat is never overwritten mid-run (staged self-swap
    done by the shim's last line, see stage_new_bat)
  - a downloaded zip must match the release API sha256 digest before it is
    extracted (case-insensitive; missing digest aborts remote download)
  - a watchdog that was running when the update started is stopped for the
    apply and re-armed only after every post-apply step; a FAILED verify
    leaves it off on purpose
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

logger = logging.getLogger("hivemind.updater")

# update.bat runs this file directly (sys.path[0] is deploy/), so the repo
# root must be importable explicitly for the shared version regex.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

REPO = "BoredLuzo/HiveMind_llama"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
TMPDIR_NAME = "_update_tmp"

# ── apply excludes (user data + the running updater) ────────────────────────
# /XF: overwriting the RUNNING update.bat mid-execution makes cmd resume at
# a stale byte offset (the staged copy swaps in at the end). The rest are
# belt-and-suspenders: a wrongly built zip must never clobber settings,
# presets, models or session state — the git token lives in settings.json
# (token leak via bad zip). Gateway state lives in
# %LOCALAPPDATA%\HiveMindGateway and is listed anyway.
APPLY_XF = [
    "update.bat", "settings.json", "presets.json", "models.json",
    "vision_model.json", "routing_weights.json", "memory.json",
    "gateway.toml", "gateway_state.json", "gateway_state.json.corrupt",
    "gateway.disabled", "gateway.lock", "gateway_audit.jsonl",
    "gateway_audit.jsonl.*", "gateway_state.tmp",
]
# /XD (apply): user data. The models exclude is ANCHORED to the zip's
# top-level models dir at call time (apply models dir) — a bare "models"
# also matched model_configs\models, silently skipping the tracked
# model-config JSONs on every update (XD-ANCHOR, audit 2026-11-04).
APPLY_XD = ["sessions", ".hive_uploads", "learning_logs", "logs", ".venv",
            "llama"]

# ── backup excludes ─────────────────────────────────────────────────────────
# models excluded: GGUFs can live here in some setups — gigabytes nobody
# wants in a backup. The exclude is ANCHORED to the install root at call
# time. update_backup_* is excluded so backups never nest.
BACKUP_XD = [".venv", "llama", "logs", "sessions", "learning_logs",
             "__pycache__", TMPDIR_NAME, "update_backup_*"]

WARN_RE = re.compile(r"(?i)\b(warning|breaking|known issues?|migrat\w*"
                     r"|attention|caution|deprecat\w*)\b")
NO_BREAKING_RE = re.compile(r"(?i)\bno\b[^.]*\bbreaking\b")


def info(msg: str) -> None:
    print(f"  [OK] {msg}" if not msg.startswith(("+", "[", "Downloading",
                                               "Applying", "Backing", "  "))
          else msg)


def fail(msg: str) -> "int":
    print(msg)
    return 1


# ── small pure helpers (unit-testable) ──────────────────────────────────────

def read_version(core_dir: Path) -> str | None:
    """HIVEMIND_VERSION from core/version.py — widened capture keeps
    "1.3.0-preview" whole (the old [0-9.]+ saw only "1.3.0").

    The regex lives in core/version.py (one truth for updater +
    update_check). A damaged install without that module returns None,
    exactly like a missing literal did before.
    """
    try:
        from core.version import VERSION_LINE_RE
    except ImportError:
        return None
    vf = core_dir / "version.py"
    try:
        m = VERSION_LINE_RE.search(vf.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return m.group(1) if m else None


def resolve_port(base: Path) -> int:
    """HIVEMIND_PORT env > settings.json server_port > 8001 (mirrors run.py)."""
    env = os.environ.get("HIVEMIND_PORT")
    if env:
        try:
            return int(env)
        except ValueError:
            # Alt behavior: an unparseable port aborted loudly (the PS check
            # failed and counted as "listener"). Falling back silently would
            # check the wrong port and could apply over a live engine.
            print(f"  [ERROR] HIVEMIND_PORT={env!r} is not a number - fix the")
            print("         environment variable or unset it, then run the update again.")
            raise SystemExit(1)
    try:
        data = json.loads((base / "settings.json").read_text(encoding="utf-8"))
        sp = data.get("server_port")
        if sp:
            return int(sp)
    except (OSError, ValueError, TypeError):
        pass
    return 8001


def port_listener(port: int) -> bool:
    """True when something is listening on the port.

    Get-NetTCPConnection is locale-proof (the old netstat findstr broke on
    localized Windows output) and already proven — keep calling it."""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"if (Get-NetTCPConnection -LocalPort {port} -State Listen "
         f"-ErrorAction SilentlyContinue) {{ exit 1 }} else {{ exit 0 }}"],
        capture_output=True, timeout=60)
    return r.returncode == 1


def _numeric_triple(v: str) -> list[int] | None:
    """[major, minor, patch] ints, or None when unparseable (VCMP-GUARD:
    "1.5.0x" would make [int]"0x" throw — the old batch FELL THROUGH both
    version gates and silently applied a same-version or downgrade)."""
    try:
        parts = (v.split("-")[0]).split(".")
        return [int(p) for p in (parts + [0, 0, 0])[:3]]
    except (ValueError, AttributeError, IndexError):
        return None


def _prerelease_ordinal(v: str) -> int | None:
    """preview/alpha/beta/rc/dev.N → N+1; a clean release → 999. None when
    the pre-release tag itself is malformed."""
    # Alt PS -match was case-insensitive and treated ANY unparseable
    # suffix as a clean release (999) — unknown suffixes like -hotfix must
    # not abort the update when the triple differs.
    m = re.search(r"(?:preview|alpha|beta|rc|dev)(?:\.(\d+))?$", v, re.I)
    if m:
        try:
            return int(m.group(1) or 0) + 1
        except ValueError:
            return 999
    return 999


def compare_versions(current: str, tag: str) -> str | None:
    """'newer' | 'same' | 'older' | None (unparseable).

    Numeric triple compare: string equality would apply a DOWNGRADE when
    the installed version is newer (dev checkouts, hotfix builds)."""
    a, b = _numeric_triple(current), _numeric_triple(tag)
    if a is None or b is None:
        return None
    ap, bp = _prerelease_ordinal(current), _prerelease_ordinal(tag)
    if ap is None or bp is None:
        return None
    if b > a:
        return "newer"
    if b == a:
        if bp > ap:
            return "newer"
        if bp < ap:
            return "older"
        return "same"
    return "older"


def digest_ok(zip_path: Path, asset_digest: str) -> bool | None:
    """sha256 check against the release API digest.

    Returns None when the metadata carries no digest (check skipped).
    Both sides lowercased — an uppercase API digest must not fail the
    compare as a false tamper alarm (DIGEST-CASE, audit 2026-11-04)."""
    if not asset_digest.lower().startswith("sha256:"):
        return None
    want = asset_digest[7:].lower()
    h = hashlib.sha256()
    with open(zip_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().lower() == want


def extract_release(zip_path: Path, dest: Path) -> tuple[int, Path | None]:
    """Extract and return (root_folder_count, single_root_or_None).

    MULTI-ROOT (audit 2026-11-04): a zip with two build folders (or
    __MACOSX junk) must not apply an arbitrary one of them."""
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)
    roots = [p for p in dest.iterdir() if p.is_dir()]
    return len(roots), (roots[0] if len(roots) == 1 else None)


def extract_warnings(body: str) -> list[str]:
    """Lines the release author flags (warning / breaking / known issue /
    migration / attention / caution / deprecated), except "no breaking". """
    out = []
    for line in (body or "").split("\n"):
        line = line.rstrip()
        if WARN_RE.search(line) and not NO_BREAKING_RE.search(line):
            out.append(line)
            if len(out) >= 5:
                break
    return out


def zip_version(exroot: Path) -> str | None:
    return read_version(exroot / "core")


# ── robocopy commands (built, not quoted — no cmd parsing left) ─────────────

def backup_cmd(base: Path, bk_name: str) -> list[str]:
    """Backup the current code (user data excluded). /R:1 /W:1: a single
    locked file must not hang the update for hours (robocopy defaults:
    1,000,000 retries x 30s)."""
    return [
        "robocopy", str(base), str(base / bk_name),
        "/E", "/R:1", "/W:1", "/NFL", "/NDL", "/NJH", "/NP", "/NJS",
        "/XD", ".venv", "llama", "logs", str(base / "models"),
        "sessions", "learning_logs", "__pycache__", TMPDIR_NAME,
        "update_backup_*",
    ]


def apply_cmd(exroot: Path, base: Path) -> list[str]:
    """Apply the extracted release over the install. /XD matches against
    the SOURCE tree, so the models exclude anchors to the zip root."""
    cmd = ["robocopy", str(exroot), str(base),
           "/E", "/R:1", "/W:1", "/XF"] + APPLY_XF + ["/XD"] + APPLY_XD \
          + ["/NFL", "/NDL", "/NJH", "/NP", "/NJS"]
    cmd[-1:-1] = [str(exroot / "models")]
    return cmd


def _run_rc(cmd: list[str]) -> int:
    # No timeout: the old batch had none, and a long apply must not be
    # aborted mid-write. Other step timeouts become a clean rc 1 via the
    # __main__ guard.
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode


# ── flow steps ──────────────────────────────────────────────────────────────

def fetch_latest_release(tmpdir: Path) -> tuple[str, str, str] | None:
    """(tag, asset_url, asset_digest) from the GitHub API via curl.

    curl is kept (not urllib): proxy env handling stays identical and the
    Windows 10 1803+ curl presence check is part of the flow."""
    r = subprocess.run(
        ["curl", "-sS", "-L", API_URL, "-o", str(tmpdir / "release.json")],
        capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        print("  [ERROR] Could not reach GitHub. Check your internet connection")
        print("         and try again.")
        return None
    try:
        rel = json.loads((tmpdir / "release.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        rel = None
    asset = next((a for a in (rel or {}).get("assets", [])
                  if str(a.get("name", "")).lower().endswith(".zip")), None)
    if not rel or not asset:
        print("  [ERROR] GitHub returned no release - rate limit or private repo?")
        print("         Try again in a minute, or download the zip manually from")
        print(f"         https://github.com/{REPO}/releases/latest")
        return None
    return str(rel.get("tag_name", "")), \
        str(asset.get("browser_download_url", "")), \
        str(asset.get("digest", ""))


def stop_watchdog(base: Path) -> bool:
    """Stop the watchdog for the apply; True when it was running.

    WATCHDOG-PAUSE (audit 2026-11-03): the watchdog restarted the engine
    within its 20 s pass while the apply ran, so robocopy wrote into a live
    Python process. DYING-GASP (audit 2026-11-04): a pass that was
    mid-restart when stop landed still boots the engine once inside its
    20 s window — the caller must give it that window and re-check the
    port BEFORE anything is applied."""
    wd_was = (Path(os.environ.get("LOCALAPPDATA", "")) /
              "HiveMindGateway" / "watchdog.marker").exists()
    # Belt-and-suspenders (alt behavior): stop whenever the script exists,
    # marker or not — a watchdog without its marker must not restart the
    # engine mid-apply either.
    if (base / "watchdog_engine.bat").exists():
        try:
            subprocess.run(["cmd", "/c", str(base / "watchdog_engine.bat"), "stop"],
                           capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            logger.warning("[WATCHDOG] stop timed out - continuing (recheck below)")
    return wd_was


def arm_watchdog(base: Path) -> None:
    """Re-arm the watchdog (start detached in the install dir).

    Re-arm happens AFTER every post-apply step: the engine must not boot
    while the verify, prune, dependency refresh and optional components
    still run (uv swaps .venv packages under a live interpreter)."""
    if (base / "watchdog_engine.bat").exists():
        subprocess.Popen(
            ["cmd", "/c", "start", "", "/b", "/d", str(base),
             "watchdog_engine.bat"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def refresh_deps(base: Path) -> None:
    """Refresh .venv deps into the existing venv (never removes packages).

    FROZEN (2026-10-18, audit): pyproject.toml + uv.lock install exactly
    the versions this release was tested with. --inexact (2026-11-04):
    optional decider/voice packages installed after the release are not in
    the lock; a plain sync deletes them. Older zips carry only
    requirements.txt; then the pip path is the honest fallback. Failure-
    tolerant on purpose: without uv the update still stands."""
    print("  Refreshing deps  : requirements.txt into .venv")
    uv = shutil.which("uv")
    if not uv:
        cand = Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin" / "uv.exe"
        if cand.exists():
            uv = str(cand)
    if not uv:
        print("  [WARN] uv not found - dependencies were NOT refreshed.")
        print("         If the new release needs new packages, run install.bat once.")
        return
    env = dict(os.environ, UV_NO_DEV="1")
    venv_py = base / ".venv" / "Scripts" / "python.exe"
    if (base / "uv.lock").exists() and (base / "pyproject.toml").exists():
        cmd = [uv, "sync", "--frozen", "--all-extras", "--inexact",
               "--python", str(venv_py), "--project", str(base), "--quiet"]
    else:
        cmd = [uv, "pip", "install", "--python", str(venv_py),
               "-r", str(base / "requirements.txt"), "--quiet"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=3600, env=env)
    if r.returncode != 0:
        print("  [WARN] Dependency refresh failed. HiveMind may miss new imports.")
        print("         Run: install.bat  (it repairs the venv)")
    else:
        info("Dependencies current.")


def refresh_uv_path() -> None:
    """Keep uv's python shim on the user PATH (idempotent, new terminals)."""
    bindir = Path(os.environ.get("USERPROFILE", "")) / ".local" / "bin"
    if not bindir.exists():
        return
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "$d = Join-Path $env:USERPROFILE '.local\\bin'; "
         "$p = [Environment]::GetEnvironmentVariable('Path','User'); "
         "if (($p -split ';') -contains $d) { 'present' } else { "
         "[Environment]::SetEnvironmentVariable('Path', "
         "(($p.TrimEnd(';')) + ';' + $d).TrimStart(';'), 'User'); 'added' }"],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60)
    if (r.stdout or "").strip() == "added":
        print(f"  [uv] added {bindir} to the user PATH (new terminals only).")


def stage_new_bat(exroot: Path, base: Path) -> bool:
    """SELF-UPDATE STAGING (2026-10-03 review): stage the incoming
    update.bat under a neutral name — the running script is never replaced
    mid-run. The SHIM's last physical line swaps it in atomically."""
    src = exroot / "update.bat"
    if not src.exists():
        return False
    shutil.copyfile(src, base / ".update_new.bat")
    return True


def write_build_info_fallback(base: Path, newver: str, tagnum: str) -> None:
    """BUILD_INFO.txt is NOT in git — package_release.bat embeds it into the
    zip. Older release zips carry none, which is how an install could report
    a years-old build (live showed v1.3.1 while v1.3.8 ran). If the applied
    build has none, write one so the start banner tells the truth."""
    bi = base / "BUILD_INFO.txt"
    if not bi.exists():
        bi.write_text(
            f"version: {newver}\n"
            f"build: {tagnum}\n"
            "commit_date: unknown - zip carried no BUILD_INFO\n"
            "written_by: update.bat fallback\n",
            encoding="utf-8")


def run_prune(base: Path, exroot: Path, tagnum: str, tmpdir: Path) -> tuple[int, bool]:
    r"""Prune files the PREVIOUS release shipped but this one dropped.

    robocopy /E only overlays: a module the new release deleted would live
    on forever as a stale shadow. /MIR is not an option (it would delete
    user data). The previous manifest lists what the last update applied;
    files on it that the new zip no longer ships are release files by
    construction and move to update_removed_<ver>\ (trash, reversible).
    Only a pure number may become the pruned count (PRUNE-COUNT GUARD:
    error text must not flow into the count)."""
    trash = base / f"update_removed_{tagnum}"
    manifest = base / "update_manifest_installed.txt"
    had_manifest = manifest.exists()
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(base / "deploy" / "update_prune.ps1"),
         "-Base", str(base), "-Ex", str(exroot),
         "-Trash", str(trash), "-Manifest", str(manifest)],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=600)
    pruned = 0
    m = re.search(r"^\s*(\d+)\s*$", r.stdout or "", re.M)
    if m:
        pruned = int(m.group(1))
    return pruned, had_manifest


# ── main flow ───────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    base = Path(__file__).resolve().parent.parent

    print()
    print("  +=============================================================+")
    print("  |    HIVEMIND - UPDATE                                        |")
    print("  |    Update this installation from the latest GitHub release  |")
    print("  +=============================================================+")
    print()
    print("  What this does:")
    print("    - Checks GitHub for the latest HiveMind release")
    print("    - Backs up the current code to update_backup_(version)\\")
    print("    - Shows warnings from the release notes before applying")
    print("    - Applies the new build over this folder")
    print("    - Refreshes Python dependencies into the existing .venv (uv)")
    print("  Usage:")
    print("    update.bat             update from the latest GitHub release")
    print("    update.bat --zip PATH  apply a LOCAL release zip (e.g. the")
    print("                           HiveMind_v*_release.zip built by")
    print("                           deploy\\package_release.bat) - no GitHub")
    print("                           round trip, same backup/excludes")
    print("  Rollback (both modes): copy the CONTENTS of update_backup_(version)\\")
    print("  back over this folder, then start_hivemind.bat. The backup includes")
    print("  settings.json; user data (models, sessions, logs, .venv) is never")
    print("  touched by the update in the first place.")
    print("  Your settings.json, models.json, .venv, llama\\, logs\\ and")
    print("  sessions\\ are NOT touched. New settings keys pick up their")
    print("  defaults automatically on the next start.")
    print()

    local_zip = None
    if argv and argv[0].lower() == "--zip":
        if len(argv) < 2 or not argv[1]:
            print("  [ERROR] --zip needs the path to a release zip, e.g.")
            print("          update.bat --zip ..\\HiveMind_v1.3.9_release.zip")
            return 1
        local_zip = Path(argv[1])

    # -- 0. curl present? (ships with Windows 10 1803+) ----------------------
    if shutil.which("curl") is None:
        print("  [ERROR] curl.exe not found. HiveMind updates need Windows 10 1803+")
        print("         or a curl.exe on PATH.")
        return 1

    # -- 1. Is an instance running on the configured port? -------------------
    hm_port = resolve_port(base)
    if port_listener(hm_port):
        print(f"  [ERROR] A HiveMind instance is listening on port {hm_port}.")
        print("         Stop it first: close the HiveMind console window, then")
        print("         run this update again.")
        return 1
    info(f"No running instance on port {hm_port}.")

    # -- 2. Current version --------------------------------------------------
    current = read_version(base / "core")
    if not current:
        print("  [ERROR] Could not read HIVEMIND_VERSION from core\\version.py.")
        print("         Is this a HiveMind installation folder?")
        return 1
    print(f"  Installed version : {current}")

    tmpdir = base / TMPDIR_NAME
    if tmpdir.exists():
        shutil.rmtree(tmpdir, ignore_errors=True)
    tmpdir.mkdir(parents=True, exist_ok=True)

    # -- 2b/3. Get the release zip (local or GitHub) -------------------------
    tag = tagnum = asset_url = asset_digest = ""
    if local_zip is not None:
        # Layout check BEFORE anything in this folder is touched: extraction
        # lands in _update_tmp\extracted only — a failure here changes nothing.
        if not local_zip.exists():
            print(f"  [ERROR] Zip not found: {local_zip}")
            return 1
        print(f"  Local zip         : {local_zip}")
        shutil.copyfile(local_zip, tmpdir / "release.zip")
        try:
            n, exroot = extract_release(tmpdir / "release.zip",
                                        tmpdir / "extracted")
        except (OSError, zipfile.BadZipFile, RuntimeError):
            print("  [ERROR] Could not extract the zip. Nothing was changed.")
            return 1
        if exroot is None:
            if n == 0:
                print("  [ERROR] Zip contained no version folder. Nothing was changed.")
            else:
                print(f"  [ERROR] Zip layout wrong: {n} root folders in the zip, expected")
                print("          exactly one HiveMind version folder. Nothing was changed.")
            return 1
        if not (exroot / "core").exists():
            print("  [ERROR] Zip layout wrong: no core\\ folder in the zip. This does not")
            print("          look like a HiveMind release zip. Nothing was changed.")
            return 1
        if not (exroot / "utils").exists():
            print("  [ERROR] Zip layout wrong: no utils\\ folder in the zip. Nothing was changed.")
            return 1
        tagnum = zip_version(exroot) or ""
        if not tagnum:
            print("  [ERROR] Zip carries no core\\version.py HIVEMIND_VERSION. Nothing was changed.")
            return 1
        tag = f"v{tagnum}"
        print(f"  Zip version       : {tagnum}")
    else:
        got = fetch_latest_release(tmpdir)
        if not got:
            return 1
        tag, asset_url, asset_digest = got
        if not tag.startswith("v"):
            print(f'  [ERROR] Unexpected release tag "{tag}" - aborting. Download the')
            print(f"         zip manually from https://github.com/{REPO}/releases/latest")
            return 1
        tagnum = tag[1:]
        print(f"  Latest release    : {tagnum}")

        # -- 4. Compare versions ---------------------------------------------
        vcmp = compare_versions(current, tagnum)
        if vcmp is None:
            print(f'  [ERROR] Could not compare versions "{current}" vs "{tagnum}" - aborting.')
            shutil.rmtree(tmpdir, ignore_errors=True)
            return 1
        if vcmp == "same":
            print()
            print(f"  [OK] Already up to date: {current}")
            shutil.rmtree(tmpdir, ignore_errors=True)
            return 0
        if vcmp == "older":
            print()
            print(f"  [OK] Installed {current} is NEWER than release {tagnum} - nothing to do.")
            print("         (Not downgrading.)")
            shutil.rmtree(tmpdir, ignore_errors=True)
            return 0

        # -- 4a. Render the release notes (probe-verified PS renderer) -------
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", str(base / "deploy" / "render_release_notes.ps1"),
             "-JsonPath", str(tmpdir / "release.json"),
             "-Width", "61", "-Title", f"RELEASE NOTES - {tag}"],
            timeout=300)
        print()

        # -- 4b. Warnings from the release notes -----------------------------
        try:
            rel = json.loads((tmpdir / "release.json").read_text(encoding="utf-8"))
            warnings = extract_warnings(str(rel.get("body", "")))
        except (OSError, ValueError):
            warnings = []
        if warnings:
            print()
            print("  +-------------------------------------------------------------+")
            print("  |  THE RELEASE NOTES WARN ABOUT THIS UPDATE:                  |")
            print("  +-------------------------------------------------------------+")
            for w in warnings:
                print(w)
            print("  +-------------------------------------------------------------+")
            try:
                ans = input("  Continue anyway? [Y/n]: ").strip().lower()
            except EOFError:
                ans = ""  # alt set /p: empty input = default Y (continue)
            if ans == "n":
                print("  Update cancelled. Nothing was changed.")
                shutil.rmtree(tmpdir, ignore_errors=True)
                return 0
        info("3-4 done: latest release fetched + version compared.")

    # -- 5. Backup the current code (user data is excluded) ------------------
    bk = f"update_backup_{tagnum}"
    print(f"  Backing up code   : {bk}\\")
    if _run_rc(backup_cmd(base, bk)) >= 8:
        print("  [ERROR] Backup failed - nothing was changed.")
        return 1
    info("5 done: backup created.")

    # -- 6. Download the release zip -----------------------------------------
    if local_zip is None:
        print(f"  Downloading       : {asset_url}")
        r = subprocess.run(
            ["curl", "-sS", "-L", "--fail", "-o", str(tmpdir / "release.zip"),
             asset_url],
            capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            print("  [ERROR] Download failed. Nothing was changed")
            print(f"         backup is in {bk}")
            return 1
        if not (tmpdir / "release.zip").exists():
            print("  [ERROR] Download produced no file. Nothing was changed.")
            print(f"         backup is in {bk}")
            return 1
        # I1 (audit r2): verify the downloaded asset against the release API
        # sha256 digest - curl exit 0 proves nothing about content identity,
        # and the extract below installs executable code.
        ok = digest_ok(tmpdir / "release.zip", asset_digest)
        if ok is False:
            print("  [ERROR] Release zip FAILED the sha256 digest check. Nothing was changed.")
            print("         The download was tampered with or corrupted. Backup is in", bk)
            return 1
        if ok is None:
            print("  [ERROR] Release metadata carries no sha256 digest - refusing unverified download.")
            print("         Nothing was changed. Backup is in", bk)
            return 1
    info("6 done: release zip downloaded + verified.")

    # -- 7. Extract (zip carries a version root folder) ----------------------
    try:
        nroots, exroot = extract_release(tmpdir / "release.zip",
                                         tmpdir / "extracted")
    except (OSError, zipfile.BadZipFile, RuntimeError):
        print("  [ERROR] Could not extract the zip. Nothing was changed")
        print(f"         backup is in {bk}")
        return 1
    if exroot is None:
        if nroots == 0:
            print("  [ERROR] Zip contained no folder. Nothing was changed.")
        else:
            print(f"  [ERROR] Zip layout wrong: {nroots} root folders in the zip, expected")
            print("          exactly one HiveMind version folder. Nothing was applied.")
        print(f"         backup is in {bk}")
        return 1
    # PRE-APPLY LAYOUT PROBE (audit 2026-11-04): the GitHub flow used to apply
    # whatever the zip carried and only NOTICE a wrong/empty layout in the
    # post-apply verify - by then half the install was overwritten and only a
    # MANUAL rollback helped. The local --zip flow always had these probes;
    # they are cheap and idempotent, so the common path enforces them too.
    if not (exroot / "core").exists():
        print("  [ERROR] Zip layout wrong: no core\\ folder in the zip. This does not")
        print("          look like a HiveMind release zip. Nothing was applied.")
        print(f"         backup is in {bk}")
        return 1
    if not (exroot / "utils").exists():
        print("  [ERROR] Zip layout wrong: no utils\\ folder in the zip. Nothing was applied.")
        print(f"         backup is in {bk}")
        return 1
    zipver = zip_version(exroot) or ""
    if not zipver:
        print("  [ERROR] Zip carries no core\\version.py HIVEMIND_VERSION. Nothing was applied.")
        print(f"         backup is in {bk}")
        return 1
    if zipver.lower() != tagnum.lower():
        print(f"  [ERROR] Zip carries version {zipver} but the release tag is {tagnum} -")
        print("         the asset does not match the release. Nothing was applied.")
        print(f"         backup is in {bk}")
        return 1
    stage_new_bat(exroot, base)
    info("7 done: zip extracted.")

    # -- 8. Apply over this installation --------------------------------------
    print("  Applying update   : over this folder")
    wd_was = stop_watchdog(base)
    # DYING-GASP (audit 2026-11-04): a watchdog pass that was mid-restart
    # when stop landed still boots the engine once, inside its 20 s pass
    # window. Give it that window, then re-check the port UNCONDITIONALLY
    # before anything is applied (alt update.bat checked regardless of the
    # marker — an engine started by ANYTHING must block the apply).
    if wd_was:
        time.sleep(25)
    if port_listener(hm_port):
        print(f"  [ERROR] An engine is listening on port {hm_port} - the watchdog")
        print("         restarted it while the update was preparing. Nothing was")
        print("         applied. Close the HiveMind console window, then run this")
        print(f"         update again. The backup folder {bk} can be deleted.")
        return 1
    if _run_rc(apply_cmd(exroot, base)) >= 8:
        print("  [ERROR] Applying the update failed. Restore by copying the")
        print(f"         contents of {bk}\\ back over this folder.")
        print("         The engine watchdog is STOPPED - after restoring, run")
        print("         watchdog_engine.bat once to re-arm it.")
        return 1
    info("8 done: files applied over this installation.")

    # -- 9. Verify ------------------------------------------------------------
    newver = read_version(base / "core") or ""
    if newver.lower() != tagnum.lower():
        print(f'  [ERROR] Verification failed: version.py reports "{newver}", expected "{tagnum}".')
        print("         The update did not apply cleanly. Restore by copying the")
        print(f"         contents of {bk}\\ back over this folder.")
        # A FAILED verify leaves the watchdog off on purpose: do not
        # auto-restart a possibly mixed tree.
        return 1
    info("9 done: verify passed.")
    write_build_info_fallback(base, newver, tagnum)

    # -- 9a. Prune files the PREVIOUS release dropped -------------------------
    pruned, had_manifest = run_prune(base, exroot, tagnum, tmpdir)
    if had_manifest and pruned:
        print(f"  Pruned          : {pruned} removed release file(s) moved to update_removed_{tagnum}\\")
    if not had_manifest:
        print("  Manifest        : first manifest-managed update - nothing to prune.")
    shutil.rmtree(tmpdir, ignore_errors=True)

    # -- 9a-3. Desktop shortcut refresh (ICON-FIX 2026-11-01) -----------------
    cs = base / "create_shortcut.bat"
    if cs.exists():
        subprocess.run(["cmd", "/c", str(cs), "--quiet"],
                       capture_output=True, timeout=300)

    # -- 9b. Refresh Python dependencies (.venv) ------------------------------
    refresh_deps(base)
    refresh_uv_path()

    # -- 9c. Optional components ----------------------------------------------
    info("deps refreshed + BUILD_INFO written.")
    # Old batch ran this in the SAME console: the [missing] table and the
    # Y/N choice prompt must be visible; capture would hide them (the
    # choice silently waited forever). No timeout: decider/voice downloads
    # can take longer than any fixed bound.
    co = base / "deploy" / "check_optionals.bat"
    if co.exists():
        subprocess.run(["cmd", "/c", str(co)], timeout=None)
    info("optional components checked.")

    # WATCHDOG RE-ARM: only after every post-apply step is done (deps,
    # optionals) — the engine must not boot while uv swaps .venv packages.
    if wd_was:
        arm_watchdog(base)

    print()
    print("  +=============================================================+")
    print("  |    UPDATE DONE                                              |")
    print("  +=============================================================+")
    print(f"  Updated  : {current}  ->  {newver}")
    print("  Kept     : settings.json, models.json, .venv, llama\\, logs\\,")
    print("             sessions\\ (untouched)")
    print(f"  Rollback : copy the CONTENTS of {bk}\\ back over this")
    print("             folder, then start_hivemind.bat")
    print()
    print("  Start HiveMind with start_hivemind.bat. New settings keys")
    print("  pick up their defaults automatically.")
    print()
    print("  What changed / further questions:")
    print(f"    Release notes : https://github.com/{REPO}/releases/tag/v{newver}")
    print(f"    Changelog     : https://github.com/{REPO}/blob/main/CHANGELOG.md")
    print()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.TimeoutExpired as exc:
        print(f"  [ERROR] A step timed out ({exc}). The update did not complete.")
        sys.exit(1)
    except OSError as exc:
        print(f"  [ERROR] {exc}")
        sys.exit(1)

"""Startup update check for HiveMind.

The interactive launcher calls this before the engine starts. It checks the
GitHub release feed (including pre-releases), prints one short ASCII notice
when a newer build exists and asks whether to install it. A "yes" downloads
the release ZIP, verifies GitHub's sha256 digest and hands it to
``update.bat --zip``; the launcher then exits so the user restarts into the
new build.

Design constraints:
- never blocks startup for long (4s network timeout, 24h result cache),
- silent on network/API errors (an offline laptop must still start),
- no automatic install without an explicit "Y",
- pre-releases are offered only to installations already on a pre-release
  (or with HIVEMIND_UPDATE_CHECK_PRERELEASES=1),
- stdlib only, so it also works before the venv is fully repaired.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = "BoredLuzo/HiveMind_llama"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
ROOT = Path(__file__).resolve().parent.parent
# 24h for an "update available"/dismissed result: the user decided, do not
# re-nag. A cached "current" result expires much sooner (2026-11-05, live):
# a release published right after a machine's daily check stayed invisible
# for the rest of the 24h window - the updater reported "current" from cache
# while a new release was already out.
CACHE_TTL_S = 24 * 60 * 60
CACHE_TTL_CURRENT_S = 4 * 60 * 60
USER_AGENT = "HiveMind-UpdateCheck/1.0"
_ALLOWED_DOWNLOAD_HOSTS = ("github.com", "githubusercontent.com")
_MAX_ZIP_BYTES = 256 * 1024 * 1024
_VER_RE = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
_VERSION_LINE_RE = re.compile(
    r"HIVEMIND_VERSION\s*=\s*[\"']([0-9][0-9A-Za-z._-]*)[\"']")
# SPLIT (2026-11-05): the literal lives in core/version.py now.
_VERSION_FILE = "core/version.py"


@dataclass(frozen=True)
class Version:
    core: tuple[int, int, int]
    pre: tuple[tuple[int, object], ...] | None
    raw: str

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None


def parse_version(text: str) -> Version | None:
    """Parse ``1.5.0`` / ``v1.5.0-preview.2``; None for anything else."""
    match = _VER_RE.match(str(text or "").strip())
    if not match:
        return None
    core = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    pre_text = match.group(4)
    pre = None
    if pre_text:
        parts: list[tuple[int, object]] = []
        for part in pre_text.split("."):
            # Semver: numeric identifiers sort below alphanumeric ones.
            parts.append((0, int(part)) if part.isdigit() else (1, part))
        pre = tuple(parts)
    return Version(core=core, pre=pre, raw=str(text).strip().lstrip("vV"))


def is_newer(candidate: str | Version, current: str | Version) -> bool:
    cand = candidate if isinstance(candidate, Version) else parse_version(candidate)
    cur = current if isinstance(current, Version) else parse_version(current)
    if cand is None or cur is None:
        return False
    if cand.core != cur.core:
        return cand.core > cur.core
    if cand.pre is None:
        return cur.pre is not None  # stable beats a pre-release of the same core
    if cur.pre is None:
        return False
    return cand.pre > cur.pre


def read_current_version(root: Path) -> str:
    try:
        text = (root / _VERSION_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    match = _VERSION_LINE_RE.search(text)
    return match.group(1) if match else ""


def _read_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=True, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def _http_json(url: str, timeout: float) -> object:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT,
                      "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if getattr(resp, "status", 200) != 200:
            raise urllib.error.HTTPError(url, resp.status, "non-200", resp.headers, None)
        raw = resp.read(2 * 1024 * 1024)
    return json.loads(raw.decode("utf-8"))


def _asset_url_ok(url: str) -> bool:
    if not url.startswith("https://"):
        return False
    host = url.split("/", 3)[2].split(":", 1)[0].lower()
    return any(host == allowed or host.endswith("." + allowed)
               for allowed in _ALLOWED_DOWNLOAD_HOSTS)


@dataclass
class CheckResult:
    status: str                 # update | current | error | no_version | dismissed
    current: str = ""
    latest: str = ""
    tag: str = ""
    url: str = ""
    digest: str = ""
    name: str = ""
    prerelease: bool = False
    checked_at: float = 0.0
    message: str = ""
    body: str = ""              # release body (first 4 kB) for the warnings digest

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "current": self.current,
            "latest": self.latest,
            "tag": self.tag,
            "url": self.url,
            "digest": self.digest,
            "name": self.name,
            "prerelease": self.prerelease,
            "checked_at": self.checked_at,
            "message": self.message,
            "body": self.body[:4096],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CheckResult":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def select_release(releases: object, current: str,
                   include_prereleases: bool = False) -> CheckResult:
    """Pick the newest suitable release with a ZIP asset.

    Stable releases win over pre-releases. A pre-release is offered only when
    the installed version is itself a pre-release (so preview users keep
    moving) or when the caller explicitly opts in.
    """
    cur = parse_version(current)
    if cur is None:
        return CheckResult(status="no_version", current=current)
    if not isinstance(releases, list):
        return CheckResult(status="error", current=current, message="invalid release feed")

    candidates: list[tuple[Version, dict, dict]] = []
    for rel in releases:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        tag = str(rel.get("tag_name") or "")
        ver = parse_version(tag)
        if ver is None or not is_newer(ver, cur):
            continue
        assets = rel.get("assets")
        if not isinstance(assets, list):
            continue
        asset = next((a for a in assets
                      if isinstance(a, dict)
                      and str(a.get("name") or "").lower().endswith(".zip")
                      and _asset_url_ok(str(a.get("browser_download_url") or ""))),
                     None)
        if asset is None:
            continue
        candidates.append((ver, rel, asset))

    if not candidates:
        return CheckResult(status="current", current=cur.raw,
                           checked_at=time.time())
    stable = [c for c in candidates if c[0].pre is None]
    if stable:
        pool = stable
    elif include_prereleases or cur.is_prerelease:
        pool = candidates
    else:
        return CheckResult(status="current", current=cur.raw,
                           checked_at=time.time())
    ver, rel, asset = max(pool, key=lambda c: c[0].core + (() if c[0].pre is None else (c[0].pre,)))
    # The key above is not comparable across pre/stable shapes; max() is only
    # used inside a homogeneous pool, so the simpler key is enough.
    return CheckResult(
        status="update",
        current=cur.raw,
        latest=ver.raw,
        tag=str(rel.get("tag_name") or ""),
        url=str(asset.get("browser_download_url") or ""),
        digest=str(asset.get("digest") or ""),
        name=str(asset.get("name") or ""),
        prerelease=ver.is_prerelease,
        checked_at=time.time(),
        body=str(rel.get("body") or "")[:4096],
    )


def check_for_update(root: Path = ROOT, *, timeout: float = 4.0,
                     include_prereleases: bool = False,
                     cache_path: Path | None = None,
                     fetch_json=None, now: float | None = None) -> CheckResult:
    current = read_current_version(root)
    if not current:
        return CheckResult(status="no_version")
    cache_path = cache_path or (root / "logs" / "update_check.json")
    now = time.time() if now is None else now
    cache = _read_cache(cache_path)
    cached = cache.get("result") if isinstance(cache.get("result"), dict) else None
    cached_status = str((cached or {}).get("status") or "")
    ttl = (CACHE_TTL_S if cached_status in ("update", "dismissed")
           else CACHE_TTL_CURRENT_S)
    if (cached and cache.get("current") == current
            and (now - float(cache.get("checked_at") or 0)) < ttl):
        result = CheckResult.from_dict(cached)
        if result.status == "update":
            dismissed = str(cache.get("dismissed_version") or "")
            dismissed_at = float(cache.get("dismissed_at") or 0)
            if dismissed == result.latest and (now - dismissed_at) < CACHE_TTL_S:
                result.status = "dismissed"
        return result

    fetch = fetch_json or _http_json
    try:
        releases = fetch(RELEASES_API, timeout)
    except (OSError, ValueError, TimeoutError, urllib.error.URLError,
            http.client.HTTPException) as exc:  # offline/rate limit
        if cached and cache.get("current") == current:
            result = CheckResult.from_dict(cached)
            result.message = f"using stale cache: {type(exc).__name__}"
            return result
        return CheckResult(status="error", current=current,
                           message=type(exc).__name__)

    result = select_release(releases, current,
                            include_prereleases=include_prereleases)
    if result.status in ("update", "current"):
        _write_cache(cache_path, {
            "checked_at": now,
            "current": current,
            "result": result.as_dict(),
        })
    return result


def dismiss_update(result: CheckResult, cache_path: Path,
                   now: float | None = None) -> None:
    cache = _read_cache(cache_path)
    cache["dismissed_version"] = result.latest
    cache["dismissed_at"] = time.time() if now is None else now
    _write_cache(cache_path, cache)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, digest: str) -> bool:
    """Verify a downloaded release ZIP against GitHub's ``sha256:`` digest."""
    text = str(digest or "").strip().lower()
    if not text.startswith("sha256:"):
        return False
    want = text.split(":", 1)[1].strip()
    if not re.fullmatch(r"[0-9a-f]{64}", want):
        return False
    try:
        return _sha256_file(path) == want
    except OSError:
        return False


def _download_asset(result: CheckResult, root: Path,
                    timeout: float = 120.0) -> Path | None:
    if not result.url or not _asset_url_ok(result.url):
        print("  [update] release asset URL is not an allowed GitHub host - skipping.")
        return None
    safe_tag = re.sub(r"[^0-9A-Za-z._-]", "_", result.tag or result.latest) or "update"
    dest_dir = root / "logs" / "update_downloads"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"HiveMind_{safe_tag}.zip"
    req = urllib.request.Request(result.url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp, dest.open("wb") as out:
            total = 0
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_ZIP_BYTES:
                    raise OSError("release zip exceeds the size cap")
                out.write(chunk)
    except (OSError, ValueError, TimeoutError, urllib.error.URLError,
            http.client.HTTPException) as exc:
        print(f"  [update] download failed: {type(exc).__name__}: {exc}")
        try:
            dest.unlink()
        except OSError:
            pass
        return None
    if not verify_sha256(dest, result.digest):
        print("  [update] release sha256 missing or mismatched - "
              "refusing to install the download.")
        return None
    return dest


# Module-level compiled (EOD-UI audit P1-2: _NO_BREAK lived only inside
# the lazy-init branch without a global statement -> UnboundLocalError
# on the second warnings_digest call in one process).
_WARN_RE = re.compile(
    r"\b(warnings?|breaking|known issues?|migrat\w*|attention|caution|"
    r"deprecat\w*)\b", re.IGNORECASE)
_NO_BREAK = re.compile(r"\b(no|keine|nicht)\b[^.]*\bbreaking\b", re.IGNORECASE)

def warnings_digest(body: str, limit: int = 3) -> list[str]:
    """Up to `limit` lines the release author flags as important.

    Word-boundary regex (installer audit: the old substring match flagged
    "Keine breaking changes" - a line saying there are NONE). Regexes are
    module-level (EOD-UI audit P1-2: the lazy-init variant raised
    UnboundLocalError on the second call in one process).
    """
    out: list[str] = []
    for raw in str(body or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _WARN_RE.search(line) and not _NO_BREAK.search(line):
            out.append(line[:110])
            if len(out) >= limit:
                break
    return out


def _print_box(result: CheckResult) -> None:
    kind = "pre-release" if result.prerelease else "release"
    line1 = f"HiveMind update available: {result.current} -> {result.latest}"
    line2 = f"{kind}: {result.tag}   https://github.com/{REPO}/releases"
    width = max(len(line1), len(line2)) + 4
    print()
    print("  +" + "-" * width + "+")
    print("  | " + line1.ljust(width - 1) + "|")
    print("  | " + line2.ljust(width - 1) + "|")
    print("  +" + "-" * width + "+")
    # Installer-audit UX gap: the startup path showed no release info at
    # all while update.bat shows the notes — print a small warnings digest.
    for line in warnings_digest(result.body):
        print(f"  ! {line}")


def install_update(result: CheckResult, root: Path = ROOT) -> int:
    update_bat = root / "update.bat"
    if not update_bat.is_file():
        print("  [update] update.bat not found - install manually.")
        return 0
    zip_path = _download_asset(result, root)
    if zip_path is None:
        return 0
    print(f"  [update] applying {result.latest} (backup is kept by update.bat) ...")
    try:
        rc = subprocess.call(["cmd", "/c", str(update_bat), "--zip", str(zip_path)],
                             cwd=str(root))
    except OSError as exc:
        print(f"  [update] could not start update.bat: {exc}")
        return 0
    if rc != 0:
        print("  [update] update.bat failed - the running installation is unchanged.")
        return 0
    try:
        zip_path.unlink()   # audit: the verified zip is no longer needed
    except OSError:
        pass
    print("  [update] installed. Please start start_hivemind.bat again.")
    return 10


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HiveMind startup update check")
    parser.add_argument("--check", action="store_true",
                        help="print the result and exit (no prompt, no install)")
    parser.add_argument("--include-prereleases", action="store_true",
                        help="offer pre-releases even on a stable installation")
    parser.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=4.0, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if os.environ.get("HIVEMIND_NO_UPDATE_CHECK", "").strip().lower() in (
            "1", "true", "yes", "on"):
        return 0
    include_pre = (args.include_prereleases
                   or os.environ.get("HIVEMIND_UPDATE_CHECK_PRERELEASES", "")
                   .strip().lower() in ("1", "true", "yes", "on"))
    root = Path(args.root)
    result = check_for_update(root, timeout=args.timeout,
                             include_prereleases=include_pre,
                             cache_path=root / "logs" / "update_check.json")
    if args.check:
        print(json.dumps(result.as_dict(), ensure_ascii=True, indent=1))
        return 0
    if result.status == "error":
        # OFFLINE-VISIBLE (2026-11-05, owner): a skipped check was silent,
        # so "why was no update offered?" was unanswerable. One short line,
        # startup still proceeds.
        print(f"  [update] check skipped ({result.message or 'offline?'}).")
        return 0
    if result.message.startswith("using stale cache"):
        print(f"  [update] offline - using the last check from "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(result.checked_at))}.")
        return 0
    if result.status == "current":
        age_h = (time.time() - float(result.checked_at or 0)) / 3600.0
        print(f"  [update] you are up to date ({result.current}; "
              f"next automatic check in ~4 h).")
        if age_h > 0:
            pass  # age display stays in the debug JSON (--check)
        return 0
    if result.status != "update":
        return 0
    if not sys.stdin.isatty():
        return 0
    _print_box(result)
    try:
        answer = input("  Install it now? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return 0
    if answer not in ("y", "yes"):
        dismiss_update(result, root / "logs" / "update_check.json")
        return 0
    return install_update(result, root)


if __name__ == "__main__":
    raise SystemExit(main())

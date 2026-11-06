# -*- coding: utf-8 -*-
"""Startup update checker (tools/update_check.py).

Run: python tests/test_update_check.py
"""
import hashlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools.update_check import (
    CheckResult,
    check_for_update,
    dismiss_update,
    is_newer,
    parse_version,
    read_current_version,
    select_release,
    verify_sha256,
    _asset_url_ok,
)

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


def rel(tag, *, draft=False, zip_asset=True, host="github.com"):
    assets = []
    if zip_asset:
        assets.append({
            "name": f"HiveMind_{tag}_release.zip",
            "browser_download_url": f"https://{host}/x/{tag}.zip",
            "digest": "sha256:" + "a" * 64,
        })
    return {"tag_name": tag, "draft": draft, "assets": assets}


# ── version parsing / ordering ────────────────────────────────────────────
check("V1: stable parses", parse_version("1.5.0") is not None)
check("V2: v-prefix strips", parse_version("v1.5.0").raw == "1.5.0")
check("V3: preview parses", parse_version("1.5.0-preview").is_prerelease is True)
check("V4: invalid rejected", parse_version("dev") is None)
check("V5: stable beats its preview", is_newer("1.5.0", "1.5.0-preview"))
check("V6: preview does not beat stable", not is_newer("1.5.0-preview", "1.5.0"))
check("V7: newer core beats older stable", is_newer("1.6.0-preview", "1.5.0"))
check("V8: preview.2 > preview", is_newer("1.5.0-preview.2", "1.5.0-preview"))
check("V9: preview.10 > preview.2", is_newer("1.5.0-preview.10", "1.5.0-preview.2"))

# ── CACHE-TTL-SPLIT (2026-11-05): a cached "current" expires after 4 h, ──
# an "update"/dismissed result after 24 h. Otherwise a release published
# right after a machine's daily check stayed invisible for the whole day.
def _feed(tag):
    def _f(url, timeout=4.0):
        return [rel(tag)]
    return _f

with tempfile.TemporaryDirectory() as td:
    troot = Path(td)
    (troot / "core").mkdir(parents=True, exist_ok=True)
    (troot / "core" / "version.py").write_text('HIVEMIND_VERSION = "1.5.3"\n',
                                     encoding="utf-8")
    cpath = troot / "logs" / "cache.json"
    now = 1_000_000.0
    r1 = check_for_update(troot, fetch_json=_feed("1.5.3"),
                          cache_path=cpath, now=now)
    check("TTL1: fresh check is current", r1.status == "current")
    # 5 h later the cached "current" (4 h TTL) is expired -> re-fetch.
    r2 = check_for_update(troot, fetch_json=_feed("v1.5.4"),
                          cache_path=cpath, now=now + 5 * 3600)
    check("TTL2: cached current expires -> re-fetch finds 1.5.4",
          r2.status == "update" and r2.latest == "1.5.4")
    # An "update" result keeps the 24 h TTL: 5 h later it is still cached.
    r3 = check_for_update(troot, fetch_json=_feed("v1.5.5"),
                          cache_path=cpath, now=now + 10 * 3600)
    check("TTL3: cached update survives 5 h", r3.status == "update"
          and r3.latest == "1.5.4")
    # ...but expires after 24 h.
    r4 = check_for_update(troot, fetch_json=_feed("v1.5.5"),
                          cache_path=cpath, now=now + 30 * 3600)
    check("TTL4: cached update expires after 24 h",
          r4.status == "update" and r4.latest == "1.5.5")
    # Offline with an expired cache (last write >24 h ago): the stale
    # cache is served with a note instead of failing silently.
    def _offline(url, timeout=4.0):
        raise OSError("no network")
    r5 = check_for_update(troot, fetch_json=_offline,
                          cache_path=cpath, now=now + 55 * 3600)
    check("TTL5: offline serves stale cache with a note",
          "stale cache" in r5.message)
check("V10: same version not newer", not is_newer("1.5.0", "1.5.0"))

# ── release selection ────────────────────────────────────────────────────
_r = select_release([rel("v1.5.0")], "1.5.0-preview")
check("S1: stable release offered to preview install",
      _r.status == "update" and _r.latest == "1.5.0", f" got={_r.status}/{_r.latest}")

_r = select_release([rel("v1.6.0-preview")], "1.5.0-preview")
check("S2: preview offered to preview install",
      _r.status == "update" and _r.latest == "1.6.0-preview")

_r = select_release([rel("v1.6.0-preview")], "1.5.0")
check("S3: preview hidden on stable install by default", _r.status == "current")

_r = select_release([rel("v1.6.0-preview")], "1.5.0", include_prereleases=True)
check("S4: preview opt-in works",
      _r.status == "update" and _r.latest == "1.6.0-preview")

_r = select_release([rel("v1.5.1"), rel("v1.6.0-preview")], "1.5.0-preview")
check("S5: stable wins over a newer preview",
      _r.status == "update" and _r.latest == "1.5.1", f" got={_r.latest}")

_r = select_release([rel("v1.5.1", draft=True)], "1.5.0")
check("S6: draft ignored", _r.status == "current")

_r = select_release([rel("v1.5.1", zip_asset=False)], "1.5.0")
check("S7: release without zip ignored", _r.status == "current")

_r = select_release([rel("v1.5.1", host="evil.example")], "1.5.0")
check("S8: non-GitHub asset ignored", _r.status == "current")

_r = select_release([rel("v1.5.0")], "1.5.0")
check("S9: same version -> current", _r.status == "current")

_r = select_release([rel("v1.5.0")], "dev")
check("S10: unparseable current -> no_version", _r.status == "no_version")

check("U1: github URL allowed", _asset_url_ok("https://github.com/a/b.zip"))
check("U2: objects.githubusercontent allowed",
      _asset_url_ok("https://objects.githubusercontent.com/a/b.zip"))
check("U3: http rejected", not _asset_url_ok("http://github.com/a/b.zip"))
check("U4: lookalike host rejected", not _asset_url_ok("https://github.com.evil/a.zip"))

# ── cache behaviour ──────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    (root / "core").mkdir(parents=True, exist_ok=True)
    (root / "core" / "version.py").write_text('HIVEMIND_VERSION = "1.5.0-preview"\n', encoding="utf-8")
    cache = root / "logs" / "update_check.json"
    calls = {"n": 0}

    def fake_fetch(url, timeout):
        calls["n"] += 1
        return [rel("v1.6.0-preview")]

    r1 = check_for_update(root, timeout=0.1, cache_path=cache,
                          fetch_json=fake_fetch, now=1000.0)
    check("C1: first check hits network and finds update",
          r1.status == "update" and calls["n"] == 1)
    r2 = check_for_update(root, timeout=0.1, cache_path=cache,
                          fetch_json=fake_fetch, now=1000.0 + 60)
    check("C2: fresh cache avoids a second network call",
          r2.status == "update" and calls["n"] == 1)
    dismiss_update(r1, cache, now=1000.0 + 60)
    r3 = check_for_update(root, timeout=0.1, cache_path=cache,
                          fetch_json=fake_fetch, now=1000.0 + 120)
    check("C3: dismissed version is not offered again",
          r3.status == "dismissed", f" got={r3.status}")
    r4 = check_for_update(root, timeout=0.1, cache_path=cache,
                          fetch_json=fake_fetch, now=1000.0 + 25 * 3600)
    check("C4: stale cache refetches", calls["n"] == 2 and r4.status == "update")
    (root / "core").mkdir(parents=True, exist_ok=True)
    (root / "core" / "version.py").write_text('HIVEMIND_VERSION = "1.6.0-preview"\n', encoding="utf-8")
    r5 = check_for_update(root, timeout=0.1, cache_path=cache,
                          fetch_json=fake_fetch, now=1000.0 + 25 * 3600 + 10)
    check("C5: version change invalidates cache",
          calls["n"] == 3 and r5.status == "current", f" got={r5.status}")

    # network failure after a stale cache: return the stale result, not error
    def dead_fetch(url, timeout):
        raise OSError("offline")

    r6 = check_for_update(root, timeout=0.1, cache_path=cache,
                          fetch_json=dead_fetch, now=1000.0 + 60 * 3600)
    check("C6: offline uses stale cache", r6.status in ("current", "update"),
          f" got={r6.status}")

with tempfile.TemporaryDirectory() as td:
    blob = Path(td) / "release.zip"
    blob.write_bytes(b"hivemind-release")
    good = "sha256:" + hashlib.sha256(b"hivemind-release").hexdigest()
    check("H1: matching sha256 accepted", verify_sha256(blob, good))
    check("H2: wrong sha256 rejected",
          not verify_sha256(blob, "sha256:" + "0" * 64))
    check("H3: missing digest rejected", not verify_sha256(blob, ""))
    check("H4: malformed digest rejected", not verify_sha256(blob, "sha256:xyz"))

# warnings_digest: double-call + word boundaries (EOD-UI audit P1-2)
from tools.update_check import warnings_digest as _wdigest

_digest_body = chr(10).join([
    "Keine breaking changes gegenueber 1.4.14.",
    "WARNING: backup your settings first.",
    "A groundbreaking feature was added.",
    "Warnings: two hints follow.",
    "Migration required for presets."])
_d1 = _wdigest(_digest_body)
_d2 = _wdigest(_digest_body)
check("D1: the digest survives a second call (no UnboundLocalError)",
      _d1 == _d2 and len(_d1) >= 2, f"({_d1})")
check("D2: WARNING/Warnings/Migration flagged, plural included",
      any("WARNING" in x for x in _d1) and any("Warnings" in x for x in _d1)
      and any("Migration" in x for x in _d1))
check("D3: no-break suppression (German keine) + word boundary (groundbreaking)",
      not any("Keine breaking" in x for x in _d1)
      and not any("groundbreaking" in x for x in _d1))

check("R1: read_current_version", read_current_version(Path(__file__).parent.parent) != "")
check("R2: CheckResult roundtrip",
      CheckResult.from_dict(CheckResult(status="current", current="1.5.0").as_dict()).status
      == "current")

print()
print("=" * 50)
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)

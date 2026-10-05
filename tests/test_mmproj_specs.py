"""mmproj spec guard: every vision spec's projector regex must actually
match something in its repo, and the pinned destination must let
write_models_json derive the right family+size key.

Regression for the 1.2.4 audit: qwen3.5 MTP specs shipped with
mmproj_regex: [] so fresh installs had NO projector for the default duo
family. Sonnet review also flagged the case trap: pick_file compiles the
regex case-SENSITIVE unless it carries (?i) - a lowercase regex against
the upper-case repo filenames (mmproj-F16.gguf) matches nothing, which
is the same bug wearing a fix sticker.

Offline checks (default, part of run_regressions):
  - every mmproj_regex compiles and is case-insensitive ((?i) prefix)
  - every spec with mmproj_regex pins a unique mmproj_dest
  - the dest name encodes the dot-stripped family + size so the writer
    keys it correctly (families/size extraction mirroring write_models_json)

Network check (--network, run manually):
  - for every spec with mmproj_regex: list the actual repo tree on
    HuggingFace and require at least one match

Run: python tests/test_mmproj_specs.py [--network]
"""
import json
import re
import sys
import urllib.request
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


def main():
    import deploy.fetch_models as fm

    vision_specs = [s for s in fm.SPECS if s.get("mmproj_regex")]

    # 1. every mmproj_regex compiles AND is case-insensitive
    bad = []
    for s in vision_specs:
        for rx in s["mmproj_regex"]:
            try:
                re.compile(rx)
            except re.error as e:
                bad.append(f"{s['key']}: {rx} does not compile ({e})")
                continue
            if not rx.startswith("(?i)"):
                bad.append(f"{s['key']}: {rx} is case-sensitive "
                           f"(repo files are e.g. mmproj-F16.gguf)")
    if not bad:
        ok(f"mmproj_regex compile + case-insensitive ({len(vision_specs)} vision specs)")
    else:
        fail("mmproj_regex", "; ".join(bad))

    # 2. every vision spec whose regex targets a GENERIC projector name
    #    (mmproj-BF16/F16/F32 - the collision class) must pin a unique
    #    mmproj_dest; a spec matching an already-unique repo file may
    #    download 1:1 without a dest.
    _GENERIC_RX = re.compile(r"\(\?i\)\^mmproj[-_.](?:bf16|f16|f32)\.gguf\$$")
    dests = [s.get("mmproj_dest") for s in vision_specs if s.get("mmproj_dest")]
    dupes = sorted({d for d in dests if dests.count(d) > 1})
    missing = [s["key"] for s in vision_specs
               if not s.get("mmproj_dest") and any(_GENERIC_RX.match(rx) for rx in s["mmproj_regex"])]
    if not dupes and not missing:
        ok("generic-mmproj specs pin a unique mmproj_dest")
    else:
        fail("mmproj_dest", f"dupes={dupes} missing={missing}")

    # 3. dest encodes family + size (write_models_json key derivation)
    bad = []
    for s in vision_specs:
        d = (s.get("mmproj_dest") or "").lower()
        if not d:
            continue
        fam = s["key"].split(":")[0].replace(".", "")
        if fam not in d.replace(".", ""):
            bad.append(f"{s['key']}: dest '{d}' does not contain family '{fam}'")
            continue
        _tag = s["key"].split(":")[-1] if ":" in s["key"] else ""
        _sm = re.match(r"(\d+(?:\.\d+)?)b", _tag.lower())
        if _sm:
            # mirror write_models_json: match the size token with optional
            # dot in the dest name, THEN strip the dot to form the key
            _raw = _sm.group(1)
            _pat = r"(?:^|[-_.])" + re.escape(_raw) + r"b(?:[-_.]|$)"
            if not re.search(_pat, d):
                bad.append(f"{s['key']}: dest '{d}' does not encode size '{_raw}b'")
    if not bad:
        ok("mmproj_dest encodes family + size (writer key derivation)")
    else:
        fail("mmproj_dest derivation", "; ".join(bad))

    # 4. network: the regex must match at least one real file in the repo
    if "--network" in sys.argv:
        for s in vision_specs:
            try:
                req = urllib.request.Request(
                    f"https://huggingface.co/api/models/{s['repo']}/tree/main",
                    headers={"User-Agent": "HiveMind-Installer"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    files = json.loads(r.read().decode("utf-8"))
                names = [Path(f["path"]).name for f in files if f.get("type") == "file"]
                hits = [n for n in names if any(re.compile(rx).search(n) for rx in s["mmproj_regex"])]
                if hits:
                    ok(f"{s['key']}: regex matches {hits[0]} in {s['repo']}")
                else:
                    fail(f"{s['key']}", f"no mmproj match in {s['repo']} "
                         f"(files: {[n for n in names if 'mmproj' in n.lower()][:5]})")
            except (urllib.error.URLError, json.JSONDecodeError,
                    TimeoutError, OSError) as e:
                fail(f"{s['key']}", f"repo fetch failed: {e}")
    else:
        print("  (skip) network check: run with --network to verify the "
              "regexes against the live repo trees")

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)

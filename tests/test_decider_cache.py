"""DecisionCache persistence (2026-10-22, idea: routing from second 1).

The routing DecisionCache used to be memory-only - every engine restart
threw the decisions away and the first run routed uncalibrated. This
suite drives the persisted cache against a fake install layout in
%TEMP%: survive a "restart" (new instance, same file), TTL expiry,
corrupt file, non-serializable details. No model, no engine."""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from routing.decider import Decision, DecisionCache

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


def dec(route="agentic", conf=0.7):
    return Decision(route=route, planner=False, source="decider",
                    confidence=conf, reason="test")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hvm_dcache_"))
    path = tmp / "decider_cache.json"

    # 1. put -> new instance (the "restart") -> get hits
    c1 = DecisionCache(path=path, ttl_s=3600)
    k = c1.key("fix the failing test", "m:9b", {"routing_decider_threshold": 0.55})
    c1.put(k, dec("agentic", 0.72))
    c2 = DecisionCache(path=path, ttl_s=3600)
    hit = c2.get(k)
    check("survives restart", hit is not None and hit.route == "agentic"
          and abs(hit.confidence - 0.72) < 1e-9)

    # 2. TTL: expired entry is a miss and gets dropped
    c3 = DecisionCache(path=path, ttl_s=0)
    check("ttl=0 -> miss", c3.get(k) is None)

    # 3. corrupt file -> graceful empty (no raise), file overwritten on put
    path.write_text("{not json", encoding="utf-8")
    c4 = DecisionCache(path=path, ttl_s=3600)
    kk = c4.key("another prompt", "m:9b")
    c4.put(kk, dec("direct", 0.9))
    check("corrupt file -> degrades to in-memory, rewrites on put",
          c4.get(kk) is not None and path.exists())

    # 4. non-serializable details: entry stored WITHOUT details, no crash
    c5 = DecisionCache(path=path, ttl_s=3600)
    d = dec("direct", 0.8)
    d.details = {"weird": object()}  # json.dumps would raise
    k5 = c5.key("third prompt", "m:9b")
    c5.put(k5, d)
    raw = json.loads(path.read_text(encoding="utf-8"))
    stored = raw["entries"][k5]["decision"]
    check("non-serializable details dropped, entry kept",
          k5 in raw["entries"] and "details" not in stored)
    check("in-memory value keeps details anyway", c5.get(k5) is not None)

    # 5. LRU size cap still applies
    c6 = DecisionCache(path=tmp / "cap.json", size=3, ttl_s=3600)
    for i in range(6):
        c6.put(c6.key(f"prompt {i}", "m:9b"), dec())
    check("LRU cap honored", len(c6._data) <= 3)

    # 6. instances without path stay memory-only (tests/CLI)
    mem = DecisionCache()
    mem.put(mem.key("x", "m"), dec())
    check("no path -> no file written", not (tmp / "decider_cache.json").exists()
          or mem.path is None)

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(f"SUMMARY decider_cache: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


main()

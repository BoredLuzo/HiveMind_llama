"""R8 /cron proxy: the phone keeps its commands, the engine owns the schedule.

Pure helpers + the gateway migration. The engine side is covered by
tests/test_scheduler_core.py; this file pins the phone-side contract.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from hivemind_gateway.cron import parse_spec  # noqa: E402
from hivemind_gateway.cron_proxy import (  # noqa: E402
    job_line, legacy_to_engine, spec_to_body,
)

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


def main() -> int:
    # spec -> engine body
    every = spec_to_body(parse_spec("every 10m check the mail"))
    check("every spec maps to every/600",
          every["schedule"] == {"kind": "every", "interval_s": 600}
          and every["prompt"] == "check the mail")
    once = spec_to_body(parse_spec("in 30m do a thing"))
    check("in spec maps to once/1800",
          once["schedule"] == {"kind": "once", "interval_s": 1800})
    daily = spec_to_body(parse_spec("at 09:00 morning report"))
    check("at spec maps to daily",
          daily["schedule"] == {"kind": "daily", "at_hm": "09:00"})
    check("phone jobs run simple/readonly/skip",
          all(b["mode"] == "simple" and b["tools_tier"] == "readonly"
              and b["policy"] == "skip"
              for b in (every, once, daily)))

    # legacy gateway state -> engine body
    leg_every = legacy_to_engine(
        {"kind": "every", "interval_s": 600, "prompt": "x", "id": "j1"})
    check("legacy every migrates", leg_every is not None
          and leg_every["schedule"]["kind"] == "every")
    leg_in = legacy_to_engine(
        {"kind": "in", "interval_s": 120, "prompt": "x", "id": "j2"})
    check("legacy in migrates as once", leg_in is not None
          and leg_in["schedule"]["kind"] == "once")
    leg_at = legacy_to_engine(
        {"kind": "at", "at_hm": "07:30", "prompt": "x", "id": "j3"})
    check("legacy at migrates as daily", leg_at is not None
          and leg_at["schedule"] == {"kind": "daily", "at_hm": "07:30"})
    check("legacy garbage skips",
          legacy_to_engine({"kind": "at", "at_hm": "07:30", "prompt": ""})
          is None
          and legacy_to_engine({"kind": "weird", "prompt": "x"}) is None)
    check("migrated names carry the legacy id",
          "j3" in (leg_at or {}).get("name", ""))

    # list rendering keeps the familiar shape
    line = job_line({"id": "j1", "prompt": "check",
                     "schedule": {"kind": "every"},
                     "next_ts": 1793881200, "fires": 3,
                     "last_status": "ok"})
    check("list line: id, kind, next, fires, status, prompt",
          all(t in line for t in ("j1", "every", "next", "3 fires",
                                  "ok", "check")))
    line2 = job_line({"id": "j2", "prompt": "p",
                      "schedule": {"kind": "daily"}, "next_ts": None,
                      "fires": 0, "last_status": ""})
    check("list line: no next_ts renders ?", "next ?" in line2)

    return h.finish("cron_proxy")


if __name__ == "__main__":
    raise SystemExit(main())

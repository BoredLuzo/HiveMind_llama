"""Pure schedule parsing and next-fire calculation for Automations.

The scheduler itself lives in `scheduler.manager`; this module is deliberately
free of engine imports so it can be tested like the gateway cron helpers.
All wall-clock values are interpreted in the job's IANA timezone (empty =
the machine's local timezone).
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_UNIT_S = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_RE_EVERY = re.compile(r"every\s+(\d+)\s*([smhd])\b", re.I)
_RE_IN = re.compile(r"in\s+(\d+)\s*([smhd])\b", re.I)
_RE_AT = re.compile(r"(?:at|daily)\s+(\d{1,2}):(\d{2})\b", re.I)


def parse_schedule(text: str) -> dict | None:
    """Parse a human schedule spec into a stored schedule dict.

    Supported (local time):
      every 10m | in 30m | at 09:00 | daily 09:00
    """
    arg = str(text or "").strip()
    if not arg:
        return None
    m = _RE_EVERY.match(arg)
    if m:
        interval = max(60, int(m.group(1)) * _UNIT_S[m.group(2).lower()])
        return {"kind": "every", "interval_s": interval}
    m = _RE_IN.match(arg)
    if m:
        interval = max(60, int(m.group(1)) * _UNIT_S[m.group(2).lower()])
        return {"kind": "once", "interval_s": interval}
    m = _RE_AT.match(arg)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh > 23 or mm > 59:
            return None
        return {"kind": "daily", "at_hm": f"{hh:02d}:{mm:02d}"}
    return None


def _tz(name: str) -> ZoneInfo:
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().tzinfo  # type: ignore[return-value]


def next_fire_ts(schedule: dict, now: float,
                 tz_name: str = "") -> float | None:
    """Next fire timestamp for a schedule, or None when it never repeats."""
    kind = str((schedule or {}).get("kind") or "")
    if kind in ("every", "once"):
        interval = int((schedule or {}).get("interval_s") or 0)
        return float(now) + max(60, interval)
    if kind == "daily":
        hm = str((schedule or {}).get("at_hm") or "00:00")
        try:
            hh_s, mm_s = hm.split(":", 1)
            hh, mm = int(hh_s), int(mm_s)
        except (TypeError, ValueError):
            return None
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return None
        tz = _tz(tz_name)
        base = datetime.fromtimestamp(float(now), tz)
        target = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        ts = target.timestamp()
        if ts <= float(now):
            # Add one calendar day (not 86400 s: DST-safe).
            from datetime import timedelta
            ts = (target + timedelta(days=1)).timestamp()
        return ts
    return None


def advance_fire_ts(schedule: dict, scheduled_ts: float, now: float,
                    tz_name: str = "") -> float | None:
    """Advance a recurring schedule without drifting on busy/slow ticks.

    `every` keeps wall-clock alignment by jumping whole intervals from the
    original slot; `daily` re-derives the next local wall-clock occurrence.
    """
    kind = str((schedule or {}).get("kind") or "")
    if kind == "once":
        return None
    if kind == "every":
        interval = max(60, int((schedule or {}).get("interval_s") or 0))
        if now < scheduled_ts:
            return scheduled_ts
        steps = int((float(now) - float(scheduled_ts)) // interval) + 1
        return float(scheduled_ts) + steps * interval
    if kind == "daily":
        return next_fire_ts(schedule, now, tz_name)
    return None


def classify_due(next_ts: float | None, now: float,
                 grace_s: float) -> str:
    """Classify a stored next_ts as not_due | due | missed."""
    if next_ts is None:
        return "not_due"
    ts = float(next_ts)
    if float(now) < ts:
        return "not_due"
    if float(now) - ts <= max(0.0, float(grace_s)):
        return "due"
    return "missed"

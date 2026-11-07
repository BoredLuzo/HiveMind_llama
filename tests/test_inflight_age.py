# -*- coding: utf-8 -*-
"""A9c: a leaked in_flight mark must age out.

The coder-round producer has no finally (a cancelled generator skips the
clear), so a slot could stay non-evictable until restart. The mark carries a
timestamp; older than 30 minutes counts as leaked.
"""
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.llama_slots import ModelSlot        # noqa: E402

from tests._harness import Harness  # noqa: E402

h = Harness(style="results", pad=True)
check = h.check


s = ModelSlot(0)
check("1 a fresh slot is not in flight", not s.in_flight_active() and s.evictable)

s.mark_in_flight()
check("2 a marked slot is protected", s.in_flight_active() and not s.evictable)
check("3 the mark is stamped", s.in_flight_since > 0)

# A machine booted <30 min ago has a monotonic clock smaller than 1801; the
# subtraction would go negative and the code correctly treats that as
# "unstamped". Pin a high monotonic clock so the aging rule is what is tested.
with mock.patch("time.monotonic", return_value=10_000.0):
    s.in_flight_since = 10_000.0 - 1801
    check("4 a stale mark no longer blocks eviction",
          not s.in_flight_active() and s.evictable)

s.clear_in_flight()
check("5 clearing resets counter and stamp",
      s.in_flight == 0 and s.in_flight_since == 0.0)

# Conservative for objects created before the timestamp existed.
old = ModelSlot(1)
old.in_flight = 1
old.in_flight_since = 0.0
check("6 an unstamped mark stays protected", old.in_flight_active())

# P1 (deep audit 2026-11-03): after a stale mark ages out, the NEXT mark must
# refresh the stamp, otherwise the slot looks free while a round is running.
s2 = ModelSlot(2)
with mock.patch("time.monotonic", return_value=10_000.0):
    s2.mark_in_flight()
    s2.in_flight_since = 10_000.0 - 1801
    check("7 a stale mark is treated as leaked", not s2.in_flight_active())
    s2.mark_in_flight()
    check("8 a fresh mark after a stale one is active again",
          s2.in_flight == 2 and s2.in_flight_active() and not s2.evictable)

sys.exit(h.finish())

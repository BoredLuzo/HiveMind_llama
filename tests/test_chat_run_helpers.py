# -*- coding: utf-8 -*-
"""Behavior checks: core/chat_run.py helpers (B3/B8, 2026-11-07).

run_stream itself is the orchestration seam; the pure helpers and the two
state dataclasses are testable without a server:

  * _history_budget_tokens: override, ctx-derived budget, floor, fallback
  * approval_gate_force: only a per-run override pins the gate
  * RunTokenLedger / PrefetchCoordinator: independent mutable defaults
"""
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core.chat_run as C  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

# ── _history_budget_tokens ─────────────────────────────────────────────────
check("history budget: explicit override wins",
      C._history_budget_tokens({"session_history_budget_tokens": 12345}) == 12345)

with mock.patch.object(C, "registry_get", return_value="direct_m"), \
     mock.patch.object(C, "_get_num_ctx", return_value=20000):
    derived = C._history_budget_tokens({})
check("history budget: derives 35% of ctx minus reserve",
      derived == int((20000 - 4096) * 0.35), f" ({derived})")

with mock.patch.object(C, "registry_get", return_value="direct_m"), \
     mock.patch.object(C, "_get_num_ctx", return_value=5000):
    floored = C._history_budget_tokens({"session_history_budget_tokens": 0})
check("history budget: small ctx floors at 1024",
      floored == 1024, f" ({floored})")

with mock.patch.object(C, "registry_get", side_effect=RuntimeError("registry down")), \
     mock.patch.object(C, "_get_num_ctx", return_value=99999):
    fallback = C._history_budget_tokens({})
check("history budget: registry failure falls back to 16k ctx",
      fallback == int((16384 - 4096) * 0.35), f" ({fallback})")

with mock.patch.object(C, "registry_get", return_value=""), \
     mock.patch.object(C, "_get_num_ctx", return_value=99999):
    no_model = C._history_budget_tokens({})
check("history budget: no model uses the 16k default",
      no_model == fallback, f" ({no_model})")

# ── approval_gate_force ────────────────────────────────────────────────────
check("approval gate: no overrides -> False", C.approval_gate_force(None) is False)
check("approval gate: empty overrides -> False", C.approval_gate_force({}) is False)
check("approval gate: per-run True pins the run",
      C.approval_gate_force({"duo_action_approval_enabled": True}) is True)
check("approval gate: per-run False does not pin",
      C.approval_gate_force({"duo_action_approval_enabled": False}) is False)
check("approval gate: truthy non-bool is coerced",
      C.approval_gate_force({"duo_action_approval_enabled": 1}) is True)

# ── state dataclasses ──────────────────────────────────────────────────────
a, b = C.RunTokenLedger(), C.RunTokenLedger()
a.real_by_phase["planner"] = 10
a.models["coder"] = "m"
check("ledger: counters start at zero",
      (b.requests, b.token_estimate) == (0, 0), f" ({b.requests}/{b.token_estimate})")
check("ledger: dict fields are per instance",
      b.real_by_phase == {} and b.models == {},
      f" ({b.real_by_phase}/{b.models})")

p, q = C.PrefetchCoordinator(), C.PrefetchCoordinator()
p.vram_cache.add("m")
p.inflight = 3
p.inflight_max = 5
check("prefetch: counters start at zero",
      (q.inflight, q.inflight_max) == (0, 0), f" ({q.inflight}/{q.inflight_max})")
check("prefetch: cache set is per instance", q.vram_cache == set(), f" ({q.vram_cache})")

sys.exit(h.finish("chat_run_helpers"))

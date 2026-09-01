


from __future__ import annotations

import time
from typing import Optional


_PHASE_LABELS: dict[str, str] = {
    "pre_explore":  "Pre-Explore",
    "soft_planner": "Planner",
    "coder_loop":   "Coder",
}


class PhaseTimer:


    def __init__(self) -> None:
        self._phases: dict[str, dict] = {}
        self._order:  list[str]       = []

    # -- Steuerung ------------------------------------------------------------

    def start(self, phase: str) -> None:
        """Starts a phase. A second call overwrites the start timestamp."""
        if phase not in self._order:
            self._order.append(phase)
        self._phases[phase] = {
            "start":   time.monotonic(),
            "end":     None,
            "status":  "running",
            "skipped": False,
        }

    def end(self, phase: str, status: str = "ok") -> None:


        now = time.monotonic()
        if phase not in self._phases:
            if phase not in self._order:
                self._order.append(phase)
            self._phases[phase] = {
                "start":   now,
                "end":     now,
                "status":  status,
                "skipped": False,
            }
            return
        entry = self._phases[phase]
        if entry["end"] is not None:
            return
        entry["end"]    = now
        entry["status"] = status

    def skip(self, phase: str) -> None:


        if phase not in self._order:
            self._order.append(phase)
        _now = time.monotonic()
        self._phases[phase] = {
            "start":   _now,
            "end":     _now,
            "status":  "skipped",
            "skipped": True,
        }

    # -- Abfragen -------------------------------------------------------------

    def is_ended(self, phase: str) -> bool:
        entry = self._phases.get(phase)
        return entry is not None and entry["end"] is not None

    def elapsed(self, phase: str) -> Optional[float]:


        entry = self._phases.get(phase)
        if entry is None:
            return None
        if entry["skipped"]:
            return 0.0
        if entry["end"] is not None:
            return round(entry["end"] - entry["start"], 3)
        if entry["start"] is not None:
            return round(time.monotonic() - entry["start"], 3)
        return None

    # -- Token-Tracking -------------------------------------------------------

    def add_tokens(self, n: int) -> None:
        """Add n output tokens to the currently active (unended) phase."""
        for phase in reversed(self._order):
            ph = self._phases.get(phase)
            if ph and not ph.get("skipped") and ph.get("end") is None:
                ph.setdefault("output_tokens", 0)
                ph["output_tokens"] += n
                return

    def add_real(self, phase: str, tokens: int, gen_ms: float = 0.0) -> None:


        if phase not in self._order:
            self._order.append(phase)
        ph = self._phases.setdefault(phase, {
            "start": None, "end": None, "status": "running", "skipped": False,
        })
        ph.setdefault("real_tokens", 0)
        ph.setdefault("gen_s", 0.0)
        ph["real_tokens"] += int(tokens or 0)
        ph["gen_s"] += float(gen_ms or 0.0) / 1000.0

    # -- Ausgabe ---------------------------------------------------------------

    def snapshot(self) -> dict:
        """
        Serialisierbares Dict aller Phasen + "total"-Aggregat.
        Rueckwaertskompatibel zu server.py (_collect_done_metrics).

        Beispiel:
        {
          "pre_explore":  {"duration_s": 8.2,   "status": "ok"},
          "soft_planner": {"duration_s": 42.1,  "status": "timeout"},
          "coder_loop":   {"duration_s": 185.0, "status": "completed"},
          "total":        {"duration_s": 235.3, "status": "aggregated"},
        }
        """
        out: dict = {}
        for phase in self._order:
            entry = self._phases[phase]
            if entry["skipped"]:
                duration = 0.0
            elif entry["end"] is not None and entry["start"] is not None:
                duration = round(entry["end"] - entry["start"], 2)
            else:
                duration = None
            out[phase] = {
                "duration_s": duration,
                "status":     entry["status"],
                "output_tokens": entry.get("output_tokens", 0),
                "real_tokens":   entry.get("real_tokens", 0),
                "gen_s":         round(entry.get("gen_s", 0.0), 2),
            }

        total_s = sum(
            v["duration_s"] for v in out.values()
            if v["duration_s"] is not None
        )
        total_gen_s = round(sum(v["gen_s"] for v in out.values()), 2)
        total_real = sum(int(v["real_tokens"]) for v in out.values())
        out["total"] = {
            "duration_s": round(total_s, 2),
            "status":     "aggregated",
            "gen_s":      total_gen_s,
            "real_tokens": total_real,
        }
        return out

    def ui_summary(self) -> str:
        """
        Short one-line summary for logs + the phase_summary field in the done event.

        Beispiel:
          "Pre-Explore 8s . Planner 42s (timeout) . Coder 3m05s"
        """
        parts: list[str] = []
        for phase in self._order:
            label = _PHASE_LABELS.get(phase, phase)
            entry = self._phases.get(phase, {})
            status = entry.get("status", "")

            if status == "skipped":
                parts.append(f"{label} -")
                continue

            start = entry.get("start")
            end   = entry.get("end")
            if start is None:
                continue

            raw_s = (end - start) if end is not None else (time.monotonic() - start)
            m, s  = divmod(int(raw_s), 60)
            ts    = f"{m}m{s:02d}s" if m else f"{s}s"
            flag  = f" ({status})" if status not in ("ok", "completed", "running") else ""
            parts.append(f"{label} {ts}{flag}")

        return " . ".join(parts) if parts else ""

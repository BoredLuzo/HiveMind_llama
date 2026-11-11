#!/usr/bin/env python3
"""Live monitoring dashboard for HiveMind benchmarks.

Can be run in terminal or inside Google Colab:
    python bench/watch.py
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def get_gpu_status() -> str:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,temperature.gpu", "--format=csv,noheader"],
            capture_output=True, text=True, check=True, timeout=3
        )
        lines = [line.strip() for line in r.stdout.strip().splitlines() if line.strip()]
        if lines:
            parts = [p.strip() for p in lines[0].split(",")]
            name = parts[0]
            used = parts[1] if len(parts) > 1 else ""
            total = parts[2] if len(parts) > 2 else ""
            temp = f"{parts[3]}°C" if len(parts) > 3 else ""
            return f"{name} | {used} / {total} | {temp}"
    except Exception:
        pass
    return "GPU: n/a"


def is_benchmark_running() -> bool:
    try:
        r = subprocess.run(["tmux", "has-session", "-t", "hmbench"],
                           capture_output=True, timeout=2)
        return r.returncode == 0
    except Exception:
        return False


def get_latest_results_file() -> Path | None:
    results_dir = ROOT / "bench" / "results"
    if not results_dir.is_dir():
        return None
    jsons = sorted(results_dir.glob("run-*.json"), key=os.path.getmtime, reverse=True)
    return jsons[0] if jsons else None


def render_dashboard() -> None:
    running = is_benchmark_running()
    gpu_s = get_gpu_status()
    now_str = datetime.now().strftime("%H:%M:%S")

    # In Colab/Jupyter, clear output smoothly
    is_colab = "google.colab" in sys.modules or "IPython" in sys.modules
    if is_colab:
        try:
            from IPython.display import clear_output
            clear_output(wait=True)
        except Exception:
            pass
    else:
        # ANSI clear terminal
        sys.stdout.write("\033[2J\033[H")

    print("=" * 76)
    print(f"            HIVEMIND BENCHMARK LIVE MONITOR  [{now_str}]")
    print("=" * 76)
    print(f"Status: {'RUNNING (tmux: hmbench)' if running else 'IDLE / FINISHED'}")
    print(f"GPU   : {gpu_s}")

    log_file = ROOT / "bench_full.log"
    recent_lines = []
    current_action = "Warte auf nächsten Task..."

    if log_file.is_file():
        text = log_file.read_text(encoding="utf-8", errors="replace")
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        recent_lines = lines[-12:]
        for line in reversed(lines):
            if "[run " in line and ("PASS" in line or "FAIL" in line):
                break
            elif "[run " in line:
                current_action = line
                break

    print(f"\n--- Aktueller Task ---")
    print(current_action)

    print(f"\n--- Letzte Log-Aktivitäten ---")
    if recent_lines:
        for l in recent_lines[-8:]:
            print(f"  {l}")
    else:
        print("  (Noch keine Aktivitäten in bench_full.log)")

    latest_json = get_latest_results_file()
    if latest_json and latest_json.is_file():
        try:
            d = json.loads(latest_json.read_text(encoding="utf-8"))
            summary = d.get("summary", {})
            results = d.get("results", [])
            print(f"\n--- Zwischenstand: {latest_json.name} ({len(results)} Tasks beendet) ---")
            for cond, s in summary.items():
                pr = s.get("pass_rate", 0.0) * 100
                med_s = s.get("median_seconds", 0.0)
                tps = s.get("median_tok_per_sec", 0.0)
                cached = s.get("cached_share", 0.0) or 0.0
                c_str = f"| KV-Cache: {cached*100:.0f}%" if cached else ""
                print(f"  * {cond:<18}: {pr:>5.1f}% pass | med: {med_s:>5.1f}s | tok/s: {tps:>5.1f} {c_str}")
        except Exception:
            pass

    print("\n" + "=" * 76)
    print("(Aktualisiert alle 10s. Beenden mit Strg+C — Benchmark läuft im Hintergrund weiter!)")


def main():
    try:
        while True:
            render_dashboard()
            time.sleep(10)
    except KeyboardInterrupt:
        print("\nMonitor gestoppt. Benchmark läuft im Hintergrund weiter.")


if __name__ == "__main__":
    main()

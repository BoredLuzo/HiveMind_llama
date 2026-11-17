"""Decode-Geschwindigkeit je Modell aus den llama-server-Logs ziehen.

Warum: `tok/s` in den Token-Stats braucht `phases_ms`, das erst seit 2026-10-20
aufgezeichnet wird. Die llama-server-Logs enthalten aber pro Request
`eval time = ... (XX tokens per second)` - daraus laesst sich eine belastbare
Median-Dekodierrate je Modell ableiten und als Rueckfall bereitstellen.

Aufruf (im live-Ordner):  python deploy/backfill_speeds.py [--dry-run]
Schreibt in token_stats.json:  "speeds": {tag: {"tps": float, "samples": int, "updated": iso}}
"""
from __future__ import annotations

import argparse
import io
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    _reconf = getattr(_s, "reconfigure", None)
    if callable(_reconf):
        try:
            _reconf(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
STATS = ROOT / "token_stats.json"

# 81.50.718.157 I slot print_timing: id  0 | task 14898 |        eval time =   37172.26 ms /   874 tokens (   42.58 ms per token,    23.49 tokens per second)
_EVAL_RE = re.compile(
    r"eval time\s*=\s*[\d.]+\s*ms\s*/\s*(\d+)\s*tokens\s*\([^)]*?([\d.]+)\s*tokens per second"
)
_CMD_RE = re.compile(r"--model\s+(\"[^\"]+\"|\S+)")


def _model_tag(logfile: Path) -> str | None:
    """Tag aus der CMD-Zeile des Logs (GGUF-Dateiname als Stem)."""
    try:
        with io.open(logfile, encoding="utf-8", errors="replace") as fh:
            for _ in range(200):
                line = fh.readline()
                if not line:
                    break
                if "--model" in line and ("llama-server" in line):
                    m = _CMD_RE.search(line)
                    if m:
                        return Path(m.group(1).strip('"')).stem
    except OSError:
        return None
    return None


def _tag_map(models_json: Path | None = None) -> dict[str, str]:
    """GGUF-Stem -> Tag (aus models.json, bevorzugt kanonische Tags vor kurzen Aliasen)."""
    path = models_json or (ROOT / "models.json")
    out: dict[str, str] = {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return out
    if not isinstance(doc, dict):
        return out
    # Sort canonical (-mtp/-ud/-qad/etc.) tags ahead of short base aliases so the canonical tag wins.
    items = [
        (tag, value) for tag, value in doc.items()
        if isinstance(tag, str) and isinstance(value, str)
        and not tag.endswith(("_mmproj", "_drafter"))
    ]
    items.sort(key=lambda kv: ("-mtp" not in kv[0], -len(kv[0]), kv[0]))
    for tag, value in items:
        stem = Path(value).stem
        out.setdefault(stem, tag)
        out.setdefault(stem.lower(), tag)
    return out


def collect(log_dir: Path = LOG_DIR) -> dict[str, list[float]]:
    """Returns {model_stem: [tok/s, ...]} - real decode rates only."""
    samples: dict[str, list[float]] = {}
    logfiles = list(log_dir.glob("llama_server_*.log"))
    arch_dir = log_dir / "archive"
    if arch_dir.is_dir():
        logfiles.extend(arch_dir.glob("llama_server_*.log"))
    for logfile in sorted(logfiles):
        tag = _model_tag(logfile)
        if not tag:
            continue
        bucket = samples.setdefault(tag, [])
        try:
            with io.open(logfile, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if "eval time" not in line:
                        continue
                    m = _EVAL_RE.search(line)
                    if not m:
                        continue
                    ntokens, tps = int(m.group(1)), float(m.group(2))
                    # winzige Batches sind Warmup/Tool-Calls und verzerren stark
                    if ntokens >= 32 and 0 < tps < 5000:
                        bucket.append(tps)
        except OSError:
            continue
    return {k: v for k, v in samples.items() if v}


def write_speeds(samples: dict[str, list[float]], stats_path: Path = STATS) -> dict:
    """Median je Modell in die Stats schreiben, Rest der Datei unangetastet."""
    doc: dict = {}
    if stats_path.exists():
        try:
            doc = json.loads(stats_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            doc = {}
    speeds = doc.setdefault("speeds", {})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for tag, values in samples.items():
        speeds[tag] = {
            "tps": round(statistics.median(values), 1),
            "samples": len(values),
            "updated": now,
        }
    stats_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return speeds


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="median decode rate per model from the logs")
    ap.add_argument("--dry-run", action="store_true", help="show only, write nothing")
    ap.add_argument("--log-dir", default=str(LOG_DIR))
    ap.add_argument("--stats", default=str(STATS))
    args = ap.parse_args(argv)

    log_dir = Path(args.log_dir)
    samples = collect(log_dir)
    # auf die in den Stats benutzten Tags umschreiben
    mj_cand = log_dir.parent / "models.json"
    tags = _tag_map(mj_cand if mj_cand.is_file() else None)
    if tags:
        mapped: dict[str, list[float]] = {}
        for stem, values in samples.items():
            for key in (stem, stem.lower()):
                tag = tags.get(key)
                if tag:
                    mapped.setdefault(tag, []).extend(values)
                    break
            else:
                mapped.setdefault(stem, []).extend(values)
        samples = mapped
    if not samples:
        print("no usable logs found")
        return 1
    print(f"{len(samples)} Modelle, {sum(len(v) for v in samples.values())} Messungen")
    for tag, values in sorted(samples.items(), key=lambda kv: -statistics.median(kv[1])):
        print(f"  {statistics.median(values):7.1f} tok/s  n={len(values):5d}  {tag[:70]}")
    if args.dry_run:
        return 0
    speeds = write_speeds(samples, Path(args.stats))
    print(f"wrote {len(speeds)} entries to {args.stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Deterministic replay of write-path fixtures through TWO code states (2026-09-30).

MANUAL HARNESS — deliberately NOT part of the unit suite the pre-commit hook
runs: it depends on the ../HiveMind_ab_base worktree (393d1ae) and costs
seconds, not milliseconds. Run it explicitly via:
    .venv/Scripts/python.exe tests/replay_write_paths.py [old_tree_root]

CAVEAT (honest scope): fixtures are synthetic, authored by the same agent
that wrote the change — this proves the harness does what it was designed to
do on these cases, NOT that real model output only produces such cases. The
real corpus accrues in logs/write_truncations.jsonl (record_truncation_
fixture); re-run against it as it fills.

Raw tool-call args are NOT persisted anywhere else (sessions store only the
final summary, logs only counts). Fixtures below represent observed classes:
the Sharp case (complete 6.6k write), an over-budget 12k write, truncations
mid-line/mid-escape, a repetition-loop cut — executed through the OLD state
and the NEW state, reporting what lands on disk and what the model sees.
"""
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

NEW_TREE = Path(__file__).resolve().parent.parent
OLD_TREE = Path(sys.argv[1]) if len(sys.argv) > 1 else NEW_TREE.parent / "HiveMind_ab_base"

LOOP_LINE = "    return VALUE_XY\n"


def _content(chars: int) -> str:
    body = ["# replay fixture content\n"]
    n = 0
    i = 0
    while n < chars:
        line = f"def fn_{i}():\n    x_{i} = {i} * 2\n    return x_{i} + {n}\n\n"
        if n + len(line) > chars:
            break
        body.append(line)
        n += len(line)
        i += 1
    return "".join(body)


def _raw(tool: str, content: str) -> str:
    return json.dumps({"path": "t.txt", "content": content})[:-2]  # unclosed: truncated shape


def _raw_complete(tool: str, content: str) -> str:
    return json.dumps({"path": "t.txt", "content": content})


C6K = _content(6600)
C12K = _content(12000)

FIXTURES = [
    {"name": "complete_6.6k (Sharp-Klasse)", "tool": "write_file",
     "raw": _raw_complete("write_file", C6K), "full": C6K},
    {"name": "complete_12k (Hint ignoriert)", "tool": "write_file",
     "raw": _raw_complete("write_file", C12K), "full": C12K},
    {"name": "trunc_mid_line (Roh-JSON cut @ 12k -> 11177c Content)", "tool": "write_file",
     "raw": _raw_complete("write_file", C12K)[:12000], "full": C12K},
    {"name": "trunc_mid_escape (\\u00e4)", "tool": "write_file",
     "raw": _raw("write_file", 'x = "h\u00e4"\n' + C6K), "full": 'x = "h\u00e4"\n' + C6K},
    {"name": "trunc_repetition_loop", "tool": "write_file",
     "raw": _raw("write_file", _content(3000) + LOOP_LINE * 8),
     "full": _content(3000) + LOOP_LINE * 8},
    {"name": "append_continuation (valid)", "tool": "write_file_append",
     "raw": _raw_complete("write_file_append", "next_part = 2\n"), "pre": "append",
     "full": "first_part = 1\nnext_part = 2\n"},
]

# runs INSIDE the target tree (old or new) via subprocess; imports resolve
# against that tree, shared venv. Reports facts, judges nothing.
DRIVER = r"""
import sys, json, asyncio, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve()))
from utils.tool import parse_tool_args
try:
    from utils.tool import salvage_truncated_write_args as salvage
except ImportError:
    salvage = None
from tools.handlers.file_ops import _inline_tool_write_file, _inline_tool_write_file_append

fixtures = json.load(sys.stdin)
out = []
for fx in fixtures:
    ws = Path(tempfile.mkdtemp())
    if fx.get("pre") == "append":
        (ws / "t.txt").write_text("first_part = 1\n", encoding="utf-8")
    rec = {"name": fx["name"]}
    args = parse_tool_args(fx["raw"])
    rec["parsed"] = bool(args)
    if not args and salvage and fx["tool"] in ("write_file", "write_file_append"):
        s = salvage(fx["raw"], fx["tool"])
        if s:
            args = s["args"]
            rec["salvaged"] = True
            rec["salvage_chars"] = len(str(args.get("content", "")))
    if not args:
        rec["outcome"] = "DROPPED (malformed, un_salvageable) -> model retries from zero"
    else:
        h = _inline_tool_write_file if fx["tool"] == "write_file" else _inline_tool_write_file_append
        try:
            res = asyncio.run(h(dict(args), ws, None))
        except Exception as e:
            res = f"[EXC {type(e).__name__}: {e}]"
        rec["result_head"] = res.splitlines()[0][:110]
        disk = (ws / "t.txt").read_text(encoding="utf-8", errors="replace")
        rec["disk_chars"] = len(disk)
        rec["disk_complete"] = (disk == fx["full"])
    out.append(rec)
json.dump(out, sys.stdout)
"""


def run_tree(tree: Path) -> list:
    if not (tree / "tools" / "handlers" / "file_ops.py").exists():
        print(f"!! {tree} ist kein HiveMind-Checkout — Seite übersprungen")
        return []
    payload = json.dumps([{k: v for k, v in fx.items()} for fx in FIXTURES])
    r = subprocess.run([sys.executable, "-c", DRIVER, str(tree)],
                       input=payload, capture_output=True, text=True,
                       encoding="utf-8", cwd=str(tree), timeout=300)
    if r.returncode != 0:
        print(f"!! Driver-Fehler in {tree}:\n{r.stderr[-800:]}")
        return []
    return json.loads(r.stdout)


def main():
    sides = [("NEU ", run_tree(NEW_TREE)), ("ALT ", run_tree(OLD_TREE))]
    for fx in FIXTURES:
        print(f"\n=== {fx['name']}  (Soll: {len(fx['full'])} chars) ===")
        for label, results in sides:
            rec = next((x for x in results if x["name"] == fx["name"]), None)
            if not rec:
                print(f"  {label}: kein Ergebnis")
                continue
            if "outcome" in rec:
                print(f"  {label}: {rec['outcome']}")
            else:
                comp = "VOLLSTÄNDIG" if rec.get("disk_complete") else f"nur {rec.get('disk_chars')} chars"
                salv = f", salvage={rec['salvage_chars']}c" if rec.get("salvaged") else ""
                print(f"  {label}: disk {comp}{salv} | Modell sieht: {rec.get('result_head','')[:90]}")


if __name__ == "__main__":
    main()

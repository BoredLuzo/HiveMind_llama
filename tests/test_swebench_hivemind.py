# -*- coding: utf-8 -*-
"""SWE-bench HiveMind adapter: dataset rows, request body, patch/predictions,
and the benchmark container mapping (no Docker/network needed)."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import bench.external.swebench_hivemind as sw
from tests._harness import Harness

h = Harness(style="plain", pad=True)
check = h.check

check("official image name follows the SWE-bench convention",
      sw.image_for("Astropy__Astropy-12907")
      == "swebench/sweb.eval.x86_64.astropy_1776_astropy-12907:latest",
      f" ({sw.image_for('Astropy__Astropy-12907')})")

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    root = Path(td)
    ds = root / "instances.jsonl"
    rows = [
        {"instance_id": "b__two", "repo": "b/repo", "base_commit": "b" * 40,
         "problem_statement": "fix b"},
        {"instance_id": "a__one", "repo": "a/repo", "base_commit": "a" * 40,
         "problem_statement": "fix a"},
    ]
    ds.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    inst = sw.load_instances(str(ds), n_tasks=1)
    check("local JSONL loads and sorts deterministically",
          len(inst) == 1 and inst[0].instance_id == "a__one", f" ({inst})")
    all_inst = sw.load_instances(str(ds), n_tasks=0)
    check("n_tasks=0 keeps every instance", len(all_inst) == 2)
    only = sw.load_instances(str(ds), n_tasks=0, names=["b__two"])
    check("explicit instance filter", [x.instance_id for x in only] == ["b__two"])

    t = only[0]
    body = sw.hivemind_body(t, root / "ws", "qwen3.6:35b-a3b-ud", 32768, 50)
    check("body uses the real code_duo coder, not a mini agent",
          body["mode"] == "code_duo" and body["duo_agentic_mode"] is True
          and body["duo_coder_tool_tier"] == "core"
          and body["duo_coder_model"] == "qwen3.6:35b-a3b-ud",
          f" ({body})")
    check("body disables planner/chunking and pins the context",
          body["duo_planner"] is False and body["duo_chunking"] is False
          and body["duo_coder_ctx_agentic"] == 32768
          and body["duo_coder_ctx_normal"] == 32768)
    check("prompt carries the official problem statement and test rule",
          "fix b" in body["q"] and "Do not edit" in body["q"]
          and "run_bash" in body["q"], f" ({body['q'][:120]})")

    # ── patch capture ─────────────────────────────────────────────────────
    ws = root / "repo"
    ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"],
                   cwd=ws, check=True)
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=ws, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws, check=True,
                          capture_output=True, text=True).stdout.strip()
    (ws / "a.py").write_text("x = 2\n", encoding="utf-8")
    (ws / "new.py").write_text("y = 3\n", encoding="utf-8")
    patch = root / "patches" / "a.patch"
    ok, detail = sw.make_patch(ws, base, patch)
    text = patch.read_text(encoding="utf-8")
    check("patch captures modified and untracked files",
          ok and "a.py" in text and "new.py" in text, f" ({detail})")

    # ── predictions ──────────────────────────────────────────────────────
    preds = root / "predictions.jsonl"
    sw.write_predictions(preds, [
        {"instance_id": "a__one", "patch_ok": True, "patch": str(patch)},
        {"instance_id": "b__two", "patch_ok": False, "patch": ""},
    ], "qwen3.6:35b-a3b-ud")
    lines = [json.loads(x) for x in preds.read_text(encoding="utf-8").splitlines()]
    check("every instance reaches the official predictions",
          len(lines) == 2 and lines[0]["instance_id"] == "a__one"
          and lines[0]["model_name_or_path"] == "hivemind-qwen3.6:35b-a3b-ud"
          and "a.py" in lines[0]["model_patch"]
          and lines[1]["instance_id"] == "b__two"
          and lines[1]["model_patch"] == "", f" ({lines})")

    # ── benchmark container mapping ───────────────────────────────────────
    map_file = root / "containers.json"
    os.environ["HIVEMIND_BENCH_CONTAINERS_FILE"] = str(map_file)
    sw.register_workspace(ws, "hm-demo")
    data = json.loads(map_file.read_text(encoding="utf-8"))
    check("workspace mapping is keyed by resolved host path",
          str(ws.resolve()) in data
          and data[str(ws.resolve())]["container"] == "hm-demo"
          and data[str(ws.resolve())]["cwd"] == "/testbed",
          f" ({data})")
    sw.unregister_workspace(ws)
    check("unregister removes the mapping",
          str(ws.resolve()) not in json.loads(map_file.read_text(encoding="utf-8")))

    check("docker requirement has an explicit error path",
          callable(sw.docker_available))

sys.exit(h.finish("swebench_hivemind"))

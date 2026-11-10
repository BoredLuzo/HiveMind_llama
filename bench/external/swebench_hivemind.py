# -*- coding: utf-8 -*-
"""SWE-bench Verified adapter for the real HiveMind code_duo agent.

The agent is not reimplemented here. HiveMind still runs its normal
`/stream` code_duo loop; this module only supplies a task container:

1. pull the official SWE-bench image for one instance,
2. copy its `/testbed` checkout to a host workspace,
3. bind-mount that workspace back into a task container,
4. register a benchmark-only route so HiveMind's `run_bash` executes inside
   that container (real dependencies/test runner) while file tools edit the
   host workspace,
5. send the official problem statement to HiveMind,
6. capture `git diff` and write the official predictions JSONL.

The official SWE-bench evaluator remains the judge. This adapter does not
grade the patch itself.

Docker is required. A plain Colab runtime has no usable Docker daemon; run
this on a GPU VM/notebook that provides Docker (and ~120 GB disk for the full
Verified set).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.bench_container import mapping_path  # noqa: E402

DEFAULT_DATASET = "SWE-bench/SWE-bench_Verified"
DEFAULT_MODEL = "qwen3.6:35b-a3b-ud"
VERIFIER_ACTIVATE = (
    "source /opt/miniconda3/bin/activate testbed 2>/dev/null || true; "
    "git config --global --add safe.directory /testbed 2>/dev/null || true; "
)


@dataclass
class Instance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    image: str


def image_for(instance_id: str) -> str:
    return f"swebench/sweb.eval.x86_64.{instance_id.lower()}:latest"


def _instance_from_row(row: dict) -> Instance:
    iid = str(row.get("instance_id") or "").strip()
    repo = str(row.get("repo") or "").strip()
    base = str(row.get("base_commit") or "").strip()
    problem = str(row.get("problem_statement") or "").strip()
    missing = [name for name, value in (
        ("instance_id", iid), ("repo", repo), ("base_commit", base),
        ("problem_statement", problem)) if not value]
    if missing:
        raise ValueError(f"instance row missing {', '.join(missing)}")
    return Instance(instance_id=iid, repo=repo, base_commit=base,
                    problem_statement=problem, image=image_for(iid))


def load_instances(dataset: str, *, n_tasks: int = 5,
                   names: list[str] | None = None) -> list[Instance]:
    p = Path(dataset)
    rows: list[dict]
    if p.is_file():
        text = p.read_text(encoding="utf-8")
        if p.suffix == ".jsonl":
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        else:
            data = json.loads(text)
            rows = data.get("instances", data) if isinstance(data, dict) else data
    else:
        try:
            from datasets import load_dataset
        except ImportError as exc:
            raise RuntimeError(
                "datasets is not installed; `pip install datasets` or pass "
                "a local JSON/JSONL dataset") from exc
        ds = load_dataset(dataset, split="test")
        rows = [dict(x) for x in ds]
    if names:
        wanted = set(names)
        rows = [r for r in rows if r.get("instance_id") in wanted]
    instances = [_instance_from_row(r) for r in rows]
    instances.sort(key=lambda x: x.instance_id)
    if n_tasks and n_tasks > 0:
        instances = instances[:n_tasks]
    return instances


def _run(cmd: list[str], *, timeout: int = 600, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        detail = (r.stderr or r.stdout or "")[-1200:]
        raise RuntimeError(f"{' '.join(cmd[:4])} failed ({r.returncode}): {detail}")
    return r


def docker_available() -> bool:
    try:
        return _run(["docker", "info"], timeout=20, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_image(image: str, *, pull: bool = True) -> None:
    if _run(["docker", "image", "inspect", image], timeout=60,
            check=False).returncode == 0:
        return
    if not pull:
        raise RuntimeError(f"image missing and --skip-pull is set: {image}")
    _run(["docker", "pull", image], timeout=3600)


def prepare_workspace(image: str, workspace: Path) -> None:
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    tmp = f"hm-prep-{uuid.uuid4().hex[:10]}"
    try:
        _run(["docker", "create", "--name", tmp, image], timeout=120)
        _run(["docker", "cp", f"{tmp}:/testbed/.", str(workspace)], timeout=600)
    finally:
        _run(["docker", "rm", "-f", tmp], timeout=60, check=False)
    if not (workspace / ".git").is_dir():
        raise RuntimeError(f"image has no /testbed/.git checkout: {image}")


def start_container(image: str, workspace: Path, name: str) -> None:
    _run(["docker", "rm", "-f", name], timeout=60, check=False)
    _run([
        "docker", "run", "-d", "--name", name,
        "--network", "none",
        "-v", f"{workspace.resolve()}:/testbed",
        "--entrypoint", "tail", image, "-f", "/dev/null",
    ], timeout=300)


def stop_container(name: str, *, keep: bool = False) -> None:
    if not keep:
        _run(["docker", "rm", "-f", name], timeout=120, check=False)


def _write_mapping(data: dict) -> None:
    path = mapping_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def register_workspace(workspace: Path, container: str) -> None:
    path = mapping_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[str(workspace.resolve())] = {
        "container": container,
        "cwd": "/testbed",
        "activate": VERIFIER_ACTIVATE,
    }
    _write_mapping(data)


def unregister_workspace(workspace: Path) -> None:
    path = mapping_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data.pop(str(workspace.resolve()), None)
    data.pop(str(workspace), None)
    _write_mapping(data)


def _prompt(instance: Instance) -> str:
    return (
        instance.problem_statement.strip()
        + "\n\nYou are working on the repository checkout in the workspace. "
          "Inspect the code, implement the fix, and run the relevant tests "
          "with run_bash. Do not edit or create test files. Finish with a "
          "working patch, not just an explanation."
    )


def hivemind_body(instance: Instance, workspace: Path, model: str,
                  ctx: int, tool_rounds: int) -> dict:
    return {
        "q": _prompt(instance),
        "workspace": str(workspace.resolve()),
        "chat_id": uuid.uuid4().hex,
        "mode": "code_duo",
        "until_finished": True,
        "duo_agentic_mode": True,
        "duo_planner": False,
        "duo_chunking": False,
        "duo_tool_rounds": int(tool_rounds),
        "duo_coder_tool_tier": "core",
        "duo_test_feedback_chunk": False,
        "duo_test_feedback_final": False,
        "duo_coder_ctx_agentic": int(ctx),
        "duo_coder_ctx_normal": int(ctx),
        "direct_model": model,
        "duo_coder_model": model,
        "duo_critic_model": model,
        "duo_planner_model": model,
    }


def run_hivemind(base_url: str, body: dict, timeout: int,
                 *, label: str = "", quiet: bool = False) -> dict:
    done: dict = {}
    tokens_in = tokens_out = cached = 0
    error = ""
    last_note = 0.0
    with httpx.stream("POST", base_url.rstrip("/") + "/stream", json=body,
                      timeout=httpx.Timeout(connect=10, read=timeout,
                                            write=30, pool=10)) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            typ = ev.get("type")
            if typ == "done":
                done = ev
            elif typ == "usage_meta":
                tokens_in += int(ev.get("prompt_tokens") or 0)
                tokens_out += int(ev.get("completion_tokens") or 0)
                cached += int(ev.get("cached_tokens") or 0)
                now = time.monotonic()
                if not quiet and now - last_note >= 15:
                    last_note = now
                    print(f"  [HM] {label} in={tokens_in} out={tokens_out} "
                          f"cached={cached}", flush=True)
            elif typ == "error":
                error = str(ev.get("content") or ev)[:800]
    return {
        "done": done,
        "stop_reason": done.get("stop_reason", ""),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cached": cached,
        "net_tokens": max(0, tokens_in - cached) + tokens_out,
        "error": error,
    }


def make_patch(workspace: Path, base_commit: str, out_patch: Path) -> tuple[bool, str]:
    _run(["git", "-C", str(workspace), "add", "-A"], timeout=120)
    r = _run(["git", "-C", str(workspace), "diff", "--cached", "--binary",
              base_commit], timeout=300)
    text = r.stdout or ""
    out_patch.parent.mkdir(parents=True, exist_ok=True)
    out_patch.write_text(text, encoding="utf-8")
    return bool(text.strip()), ("ok" if text.strip() else "empty patch")


def write_predictions(path: Path, rows: list[dict], model: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            patch_text = ""
            if row.get("patch_ok") and row.get("patch"):
                patch_text = Path(row["patch"]).read_text(encoding="utf-8")
            fh.write(json.dumps({
                "instance_id": row["instance_id"],
                "model_name_or_path": f"hivemind-{model}",
                "model_patch": patch_text,
            }) + "\n")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default=DEFAULT_DATASET,
                    help="HF dataset id or local JSON/JSONL")
    ap.add_argument("--n-tasks", type=int, default=5,
                    help="first N instances (0 = all)")
    ap.add_argument("--instance", action="append", default=[],
                    help="explicit instance id (repeatable)")
    ap.add_argument("--out", default="")
    ap.add_argument("--workspace-root", default="")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--hivemind", default="http://127.0.0.1:8001")
    ap.add_argument("--ctx", type=int, default=32768)
    ap.add_argument("--tool-rounds", type=int, default=50)
    ap.add_argument("--timeout", type=int, default=10800)
    ap.add_argument("--skip-pull", action="store_true")
    ap.add_argument("--keep-containers", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--run-eval", action="store_true")
    ap.add_argument("--eval-workers", type=int, default=4)
    ap.add_argument("--run-id", default="")
    ap.add_argument("--swebench-python", default=sys.executable,
                    help="Python that has the official swebench package")
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--shards", type=int, default=1)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not docker_available():
        raise SystemExit(
            "Docker is required (Colab has no usable Docker daemon). "
            "Use a GPU VM/notebook with Docker.")
    names = args.instance or None
    instances = load_instances(args.dataset,
                               n_tasks=0 if names else args.n_tasks,
                               names=names)
    if args.shards > 1:
        instances = instances[args.shard_index::args.shards]
    if not instances:
        raise SystemExit("no instances selected")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out or ROOT / "bench" / "results" /
                   f"swebench-hivemind-{stamp}")
    ws_root = Path(args.workspace_root or ROOT / "bench" / ".external" /
                   f"swebench_hivemind_{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)
    ws_root.mkdir(parents=True, exist_ok=True)
    preds = out_dir / "predictions.jsonl"
    rows: list[dict] = []

    print(f"[STATUS] {len(instances)} SWE-bench instances -> {out_dir}", flush=True)
    for idx, inst in enumerate(instances, 1):
        print(f"\n=== [{idx}/{len(instances)}] {inst.instance_id} ===", flush=True)
        ws = ws_root / inst.instance_id
        name = "hm-" + re.sub(r"[^a-z0-9_.-]+", "-", inst.instance_id.lower())[:50]
        row: dict = {"instance_id": inst.instance_id, "repo": inst.repo,
                     "base_commit": inst.base_commit, "image": inst.image,
                     "container": name, "workspace": str(ws)}
        started = time.monotonic()
        try:
            ensure_image(inst.image, pull=not args.skip_pull)
            prepare_workspace(inst.image, ws)
            start_container(inst.image, ws, name)
            register_workspace(ws, name)
            print(f"[STATUS] container ready; HiveMind generating", flush=True)
            body = hivemind_body(inst, ws, args.model, args.ctx, args.tool_rounds)
            hm = run_hivemind(args.hivemind, body, args.timeout,
                              label=inst.instance_id, quiet=args.quiet)
            row.update(hm)
            patch = out_dir / "patches" / f"{inst.instance_id}.patch"
            ok, detail = make_patch(ws, inst.base_commit, patch)
            row.update({"patch": str(patch), "patch_ok": ok,
                        "patch_detail": detail})
        except (RuntimeError, OSError, ValueError, httpx.HTTPError,
                subprocess.SubprocessError) as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"[:1200]
            print("ERROR:", row["error"], flush=True)
        finally:
            unregister_workspace(ws)
            # Also clean a partially created container if start_container
            # failed after `docker run`.
            stop_container(name, keep=args.keep_containers)
        row["elapsed_s"] = round(time.monotonic() - started, 1)
        rows.append(row)
        (out_dir / "results.json").write_text(
            json.dumps({"tasks": rows}, indent=1), encoding="utf-8")
        write_predictions(preds, rows, args.model)
        passed = sum(1 for r in rows if r.get("patch_ok"))
        print(f"[RESULT] {inst.instance_id}: "
              f"{'PATCH' if row.get('patch_ok') else 'NO-PATCH'} "
              f"stop={row.get('stop_reason', '')} "
              f"elapsed={row['elapsed_s']}s "
              f"net_tokens={row.get('net_tokens', 0)}", flush=True)
        print(f"[PROGRESS] {passed}/{len(rows)} patches written", flush=True)

    print(f"\n[STATUS] predictions: {preds}", flush=True)
    print(f"[STATUS] raw results: {out_dir / 'results.json'}", flush=True)
    if args.run_eval:
        run_id = args.run_id or f"hivemind-{stamp}"
        cmd = [args.swebench_python, "-m", "swebench.harness.run_evaluation",
               "--dataset_name", args.dataset, "--predictions_path", str(preds),
               "--run_id", run_id, "--max_workers", str(args.eval_workers)]
        print("[STATUS] official eval: " + " ".join(cmd), flush=True)
        return _run(cmd, timeout=24 * 3600, check=False).returncode
    return 0 if all(r.get("patch_ok") for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())

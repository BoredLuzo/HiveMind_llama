# -*- coding: utf-8 -*-
"""Run DeepSWE tasks through the real HiveMind engine.

This is NOT the mini-swe-agent/model notebook. For each task:

1. clone the task repository at its pinned base commit into an isolated
   workspace,
2. send the official `instruction.md` to HiveMind's `/stream` in code_duo /
   agentic mode (same model/template/profile as HiveMind),
3. stage the workspace and capture `git diff --binary <base>` as model.patch,
4. build the task's official verifier image (`tests/Dockerfile`) and run
   `/tests/test.sh` in a pristine container with that patch — locally with
   Docker, or on Modal (`--verifier modal`) when there is no usable local
   Docker, e.g. on Colab. HiveMind itself always runs on the host via
   `/stream`; only the verifier is offloaded in the Modal path.

Requires: HiveMind engine reachable, Docker or a Modal token for the
verifier, Linux (or WSL2), git, Python 3.11+.
The hidden verifier is isolated; HiveMind itself runs on the host workspace.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class DeepTask:
    task_id: str
    task_dir: Path
    instruction: str
    repo_url: str
    base_commit: str
    image: str
    # Official verifier resources (task.toml [verifier] / [verifier.environment]).
    verifier_timeout_s: int = 3600
    verifier_cpus: float = 2.0
    verifier_memory_mb: int = 8192


def _meta(d: dict, *keys, default: Any = "") -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def load_task(task_dir: str | Path) -> DeepTask:
    d = Path(task_dir)
    data = tomllib.loads((d / "task.toml").read_text(encoding="utf-8"))
    instr = (d / "instruction.md").read_text(encoding="utf-8")
    repo = str(_meta(data, "metadata", "repository_url", default="")).strip()
    base = str(_meta(data, "metadata", "base_commit_hash", default="")).strip()
    image = str(_meta(data, "environment", "docker_image", default="")).strip()
    task_id = str(_meta(data, "metadata", "task_id", default="") or d.name)
    v_timeout = int(float(_meta(data, "verifier", "timeout_sec", default=3600)))
    v_cpus = float(_meta(data, "verifier", "environment", "cpus", default=2))
    v_mem = int(_meta(data, "verifier", "environment", "memory_mb", default=8192))
    missing = [n for n, v in (("repository_url", repo), ("base_commit_hash", base),
                              ("docker_image", image)) if not v]
    if missing:
        raise ValueError(f"{d.name}: missing {', '.join(missing)} in task.toml")
    return DeepTask(task_id=task_id, task_dir=d, instruction=instr,
                    repo_url=repo, base_commit=base, image=image,
                    verifier_timeout_s=v_timeout, verifier_cpus=v_cpus,
                    verifier_memory_mb=v_mem)


def select_tasks(tasks_root: str | Path, *, n_tasks: int = 5,
                 seed: int = 0, names: list[str] | None = None) -> list[DeepTask]:
    root = Path(tasks_root)
    dirs = sorted(p.parent for p in root.glob("*/task.toml"))
    if names:
        wanted = set(names)
        dirs = [d for d in dirs if d.name in wanted]
    tasks = [load_task(d) for d in dirs]
    if n_tasks and len(tasks) > n_tasks:
        rnd = random.Random(seed)
        tasks = rnd.sample(tasks, n_tasks)
    return tasks


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 3600):
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def prepare_workspace(task: DeepTask, root: Path) -> Path:
    ws = root / task.task_id
    if ws.exists():
        shutil.rmtree(ws)
    ws.parent.mkdir(parents=True, exist_ok=True)
    r = _run(["git", "clone", "--filter=blob:none", "--no-checkout",
              task.repo_url, str(ws)], timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"clone failed: {r.stderr[-800:]}")
    r = _run(["git", "-C", str(ws), "fetch", "--depth", "1", "origin",
              task.base_commit], timeout=900)
    if r.returncode != 0:
        raise RuntimeError(f"fetch {task.base_commit} failed: {r.stderr[-800:]}")
    r = _run(["git", "-C", str(ws), "checkout", "--detach", "FETCH_HEAD"],
             timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"checkout failed: {r.stderr[-800:]}")
    return ws


def hivemind_body(task: DeepTask, ws: Path, model: str, ctx: int) -> dict:
    return {
        "q": task.instruction,
        "workspace": str(ws),
        "chat_id": uuid.uuid4().hex,
        "mode": "code_duo",
        "until_finished": True,
        "duo_agentic_mode": True,
        "duo_planner": False,
        "duo_chunking": False,
        "duo_tool_rounds": 50,
        "duo_coder_tool_tier": "core",
        "duo_websearch_enabled": False,
        "git_tools_enabled": "false",
        "duo_git_autocommit": False,
        "lsp_auto_diagnostics": False,
        "lsp_auto_warm_on_write": False,
        "duo_test_feedback_chunk": False,
        "duo_test_feedback_final": False,
        "duo_coder_ctx_agentic": int(ctx),
        "duo_coder_ctx_normal": int(ctx),
        "direct_model": model,
        "duo_coder_model": model,
        "duo_critic_model": model,
        "duo_planner_model": model,
    }


def run_hivemind(base_url: str, body: dict, timeout: int) -> dict:
    done: dict = {}
    tokens_in = tokens_out = cached = 0
    saw_usage_meta = False
    error = ""
    deadline = (time.monotonic() + float(timeout)) if timeout and timeout > 0 else None
    with httpx.stream("POST", base_url.rstrip("/") + "/stream",
                      json=body,
                      timeout=httpx.Timeout(connect=10, read=timeout,
                                            write=30, pool=10)) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if deadline is not None and time.monotonic() > deadline:
                error = f"wall timeout after {timeout}s"
                break
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "done":
                done = ev
            elif ev.get("type") == "usage_meta":
                saw_usage_meta = True
                p_tok = max(0, int(ev.get("prompt_tokens") or 0))
                c_raw = max(0, int(ev.get("cached_tokens") or 0))
                tokens_in += p_tok
                tokens_out += max(0, int(ev.get("completion_tokens") or 0))
                cached += min(c_raw, p_tok)
            elif ev.get("type") == "error":
                error = str(ev.get("content") or ev)[:500]
    if not saw_usage_meta and isinstance(done, dict) and done:
        p_tok = max(0, int(done.get("prompt_tokens_total") or 0))
        c_raw = max(0, int(done.get("cached_tokens_total") or 0))
        tokens_in = p_tok
        tokens_out = max(0, int(done.get("tokens_generated") or 0))
        cached = min(c_raw, p_tok)
    return {"done": done,
            "stop_reason": done.get("stop_reason") or ("timeout" if "wall timeout" in error else ""),
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cached": cached, "error": error,
            "net_tokens": max(0, tokens_in - cached) + tokens_out}


def make_patch(ws: Path, base: str, out_patch: Path) -> tuple[bool, str]:
    add = _run(["git", "-C", str(ws), "add", "-A"], timeout=120)
    if add.returncode != 0:
        return False, f"git add failed: {add.stderr[-500:]}"
    d = _run(["git", "-C", str(ws), "diff", "--cached", "--binary", base],
             timeout=300)
    if d.returncode != 0:
        return False, f"git diff failed: {d.stderr[-500:]}"
    text = d.stdout or ""
    out_patch.parent.mkdir(parents=True, exist_ok=True)
    out_patch.write_text(text, encoding="utf-8")
    return bool(text.strip()), ("ok" if text.strip() else "empty patch")


def _verifier_tag(task_id: str) -> str:
    return "deepswe-verifier-" + re.sub(r"[^a-z0-9_.-]+", "-",
                                        task_id.lower())[:60]


def run_verifier(task: DeepTask, patch: Path, logs: Path) -> dict:
    tests_dir = task.task_dir / "tests"
    tag = _verifier_tag(task.task_id)
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "artifacts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(patch, logs / "artifacts" / "model.patch")
    b = _run(["docker", "build", "-t", tag, "-f", str(tests_dir / "Dockerfile"),
              str(tests_dir)], timeout=3600)
    if b.returncode != 0:
        return {"ok": False, "error": "docker build failed: " + b.stderr[-1200:]}
    r = _run(["docker", "run", "--rm",
              "--cpus", str(task.verifier_cpus),
              "--memory", f"{task.verifier_memory_mb}m",
              "-v", f"{logs.resolve()}:/logs",
              tag, "/tests/test.sh"], timeout=task.verifier_timeout_s)
    reward = None
    for name in ("reward.json", "reward.txt"):
        p = logs / "verifier" / name
        if p.is_file():
            try:
                reward = (json.loads(p.read_text()).get("reward")
                          if name.endswith(".json") else p.read_text().strip())
            except (ValueError, OSError):
                reward = p.read_text(errors="replace").strip()
            break
    return {"ok": str(reward) in ("1", "1.0", "True", "true"),
            "reward": reward, "exit_code": r.returncode,
            "output": ((r.stdout or "") + (r.stderr or ""))[-2000:]}


def run_verifier_modal(task: DeepTask, patch: Path, logs: Path) -> dict:
    """Grade model.patch on Modal instead of local Docker (Colab-friendly).

    Same protocol as run_verifier, but the verifier container runs on Modal:
    an image is built from the task's `tests/Dockerfile`, an ephemeral volume
    mounted at `/logs` is seeded with `artifacts/model.patch`, `/tests/test.sh`
    runs inside that image and `/logs/verifier/reward.{json,txt}` is read back
    (mirrored into the local `logs` directory). HiveMind itself still runs on
    the host via `/stream`; only grading is remote. Requires `pip install
    modal` and a Modal token; no local Docker. The verifier's CPU/memory/
    timeout limits come from the task's `[verifier]` block, so a Go/Rust build
    does not silently run in Modal's tiny default container. The first image
    build per task takes minutes and consumes Modal credits.
    """
    import modal  # lazy: only the --verifier modal path needs the SDK
    from modal.stream_type import StreamType

    tests_dir = task.task_dir / "tests"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "artifacts").mkdir(parents=True, exist_ok=True)
    staged = logs / "artifacts" / "model.patch"
    shutil.copy2(patch, staged)
    image = modal.Image.from_dockerfile(str(tests_dir / "Dockerfile"),
                                        context_dir=str(tests_dir))
    app = modal.App.lookup(_verifier_tag(task.task_id), create_if_missing=True)
    res: dict = {}
    sandbox = None
    try:
        with modal.Volume.ephemeral() as vol:
            with vol.batch_upload() as batch:
                batch.put_file(str(staged), "/artifacts/model.patch")

            # A Sandbox runs the official test.sh directly in the task image.
            # This avoids @app.function/serialized=True: the host runs Python
            # 3.14 while the verifier image may ship 3.12, and a serialized
            # function cannot cross those versions.
            sandbox = modal.Sandbox.create(
                app=app,
                image=image,
                volumes={"/logs": vol},
                timeout=task.verifier_timeout_s + 300,
                cpu=task.verifier_cpus,
                memory=task.verifier_memory_mb,
                block_network=True,
            )
            proc = sandbox.exec(
                "/tests/test.sh",
                stdout=StreamType.STDOUT,
                stderr=StreamType.DEVNULL,
                timeout=task.verifier_timeout_s,
            )
            out = proc.stdout.read()[-4000:]
            exit_code = proc.wait()

            reward_proc = sandbox.exec(
                "bash", "-lc",
                "cat /logs/verifier/reward.json 2>/dev/null "
                "|| cat /logs/verifier/reward.txt 2>/dev/null || true",
                stdout=StreamType.STDOUT,
                stderr=StreamType.DEVNULL,
                timeout=60,
            )
            reward_raw = reward_proc.stdout.read().strip()
            reward_proc.wait()
            reward_file = ("reward.json" if reward_raw.startswith("{")
                           else "reward.txt" if reward_raw else None)
            res = {"exit_code": exit_code, "output": out,
                   "reward_file": reward_file, "reward_raw": reward_raw}
    except OSError:
        raise
    except modal.Error as exc:  # auth/build/remote failures -> runner row
        raise RuntimeError(
            f"modal verifier failed: {type(exc).__name__}: {exc}") from exc
    finally:
        if sandbox is not None:
            try:
                sandbox.terminate()
            except modal.Error:
                pass
    reward = None
    if res.get("reward_file") == "reward.json":
        raw = str(res.get("reward_raw") or "")
        try:
            reward = json.loads(raw).get("reward")
        except ValueError:
            reward = raw.strip()
    elif res.get("reward_file"):
        reward = str(res.get("reward_raw") or "").strip()
    if res.get("reward_file"):  # mirror the verifier artifacts locally
        vdir = logs / "verifier"
        vdir.mkdir(parents=True, exist_ok=True)
        (vdir / res["reward_file"]).write_text(
            str(res.get("reward_raw") or ""), encoding="utf-8")
    return {"ok": str(reward) in ("1", "1.0", "True", "true"),
            "reward": reward, "exit_code": res.get("exit_code"),
            "output": str(res.get("output") or "")}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tasks", required=True, help="DeepSWE tasks directory")
    ap.add_argument("--hivemind", default="http://127.0.0.1:8001")
    ap.add_argument("--model", default="qwen3.6:35b-a3b-ud")
    ap.add_argument("--ctx", type=int, default=16384)
    ap.add_argument("--n-tasks", type=int, default=5)
    ap.add_argument("--sample-seed", type=int, default=0)
    ap.add_argument("--task", action="append", default=[],
                    help="explicit task id (repeatable)")
    ap.add_argument("--verifier", choices=("docker", "modal"), default="docker",
                    help="verifier backend: local Docker (default) or Modal "
                         "cloud, e.g. on Colab (no local Docker needed)")
    ap.add_argument("--out", default="")
    ap.add_argument("--workspace-root", default="")
    ap.add_argument("--timeout", type=int, default=10800)
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    tasks = select_tasks(args.tasks, n_tasks=args.n_tasks,
                         seed=args.sample_seed, names=args.task or None)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out or ROOT / "bench" / "results" /
                   f"deepswe-hivemind-{stamp}")
    ws_root = Path(args.workspace_root or ROOT / "bench" / ".external" /
                   f"deepswe_hivemind_{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)
    ws_root.mkdir(parents=True, exist_ok=True)
    verifier = run_verifier_modal if args.verifier == "modal" else run_verifier
    rows: list[dict] = []
    for task_no, task in enumerate(tasks, 1):
        row = {"task": task.task_id, "ok": False, "verifier": args.verifier}
        task_started = time.monotonic()
        print(f"\n=== [{task_no}/{len(tasks)}] {task.task_id} ===", flush=True)
        if args.dry_run:
            print("repo:", task.repo_url, "base:", task.base_commit)
            print("image:", task.image)
            rows.append({"task": task.task_id, "dry_run": True})
            continue
        try:
            ws = prepare_workspace(task, ws_root)
            body = hivemind_body(task, ws, args.model, args.ctx)
            h = run_hivemind(args.hivemind, body, args.timeout)
            row.update(h)
            patch = ws_root / f"{task.task_id}.patch"
            has_patch, detail = make_patch(ws, task.base_commit, patch)
            row.update({"patch": str(patch), "patch_ok": has_patch,
                        "patch_detail": detail})
            if args.no_verify:
                row["ok"] = has_patch
            elif has_patch:
                v = verifier(task, patch, ws_root / f"{task.task_id}_logs")
                row.update(v)
            else:
                row["error"] = "empty patch"
        except (RuntimeError, OSError, ValueError, httpx.HTTPError, subprocess.SubprocessError) as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"[:800]
            print("ERROR:", row["error"], flush=True)
        rows.append(row)
        (out_dir / "results.json").write_text(
            json.dumps({"tasks": rows}, indent=1), encoding="utf-8")
        elapsed = time.monotonic() - task_started
        passed = sum(1 for r in rows if r.get("ok"))
        print(
            f"[RESULT] {task.task_id}: {'PASS' if row.get('ok') else 'FAIL'} "
            f"reward={row.get('reward')} "
            f"stop={row.get('stop_reason', '')} "
            f"patch={row.get('patch_detail', '')} "
            f"elapsed={elapsed:.1f}s "
            f"net_tokens={row.get('net_tokens', 0)}",
            flush=True,
        )
        print(f"[PROGRESS] {passed}/{len(rows)} tasks passed", flush=True)
    print("\nwrote", out_dir / "results.json", flush=True)
    return 0 if all(r.get("ok") for r in rows if not r.get("dry_run")) else 1


if __name__ == "__main__":
    sys.exit(main())

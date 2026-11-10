# -*- coding: utf-8 -*-
"""Import selected pure-Python tasks from datacurve-ai/deep-swe into bench/tasks_deepswe.

Each imported task becomes a self-contained bench fixture:
  bench/tasks_deepswe/<id>/
    task.json     {"id": "...", "prompt": "...", "check": "python -m pytest ...", "timeout_s": 900}
    repo/         cloned repo at base_commit_hash
    tests/        hidden test files extracted from test.patch
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_TASKS = [
    {
        "task_id": "tomlkit-toml-table-converters",
        "name": "deepswe_tomlkit_converters",
        "repo_url": "https://github.com/python-poetry/tomlkit.git",
        "base_commit": "dd05eebc8ed9e30fc6c223088a5a450cb54c1cab",
        "check": "python -m pytest -q tests/test_convert.py",
        "timeout_s": 600,
        "test_files": ["tests/test_convert.py"],
    },
    {
        "task_id": "httpx-deterministic-cookie-store",
        "name": "deepswe_httpx_cookie_store",
        "repo_url": "https://github.com/encode/httpx.git",
        "base_commit": "b5addb64f0161ff6bfe94c124ef76f6a1fba5254",
        "check": "python -m pytest -q tests/models/test_cookie_store.py",
        "timeout_s": 600,
        "test_files": ["tests/models/test_cookie_store.py"],
    },
    {
        "task_id": "cattrs-partial-structuring-recovery",
        "name": "deepswe_cattrs_partial_structuring",
        "repo_url": "https://github.com/python-attrs/cattrs.git",
        "base_commit": "6bc4708fb9b2ac52d9a18997e923da6a58916102",
        "check": "python -m pytest -q tests/test_partial_structure.py",
        "timeout_s": 600,
        "test_files": ["tests/test_partial_structure.py"],
    },
]


def import_task(deep_swe_dir: Path, out_dir: Path, spec: dict) -> dict:
    task_src = deep_swe_dir / "tasks" / spec["task_id"]
    if not task_src.is_dir():
        raise FileNotFoundError(f"DeepSWE task directory not found: {task_src}")

    target_dir = out_dir / spec["name"]
    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. Read instruction.md
    instr_path = task_src / "instruction.md"
    instruction = instr_path.read_text(encoding="utf-8")

    # 2. Clone repo at base commit
    repo_dst = target_dir / "repo"
    if repo_dst.is_dir():
        shutil.rmtree(repo_dst)

    print(f"Cloning {spec['repo_url']} at {spec['base_commit'][:8]}...")
    subprocess.run(["git", "clone", spec["repo_url"], str(repo_dst)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "checkout", spec["base_commit"]],
                   cwd=str(repo_dst), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 3. Apply test.patch to a temporary copy to extract pristine hidden tests
    with tempfile.TemporaryDirectory() as td:
        patch_file = task_src / "tests" / "test.patch"
        temp_repo = Path(td) / "repo"
        shutil.copytree(repo_dst, temp_repo)
        subprocess.run(["git", "apply", str(patch_file)],
                       cwd=str(temp_repo), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Copy extracted test files into target_dir / "tests"
        tests_dst = target_dir / "tests"
        if tests_dst.is_dir():
            shutil.rmtree(tests_dst)
        tests_dst.mkdir(parents=True, exist_ok=True)

        for tf in spec["test_files"]:
            src_f = temp_repo / tf
            dst_f = tests_dst / tf
            dst_f.parent.mkdir(parents=True, exist_ok=True)
            if src_f.is_file():
                shutil.copy2(src_f, dst_f)
            else:
                raise FileNotFoundError(f"Expected test file not produced by patch: {tf}")

    # Remove .git from repo_dst to keep task workspaces lightweight and pure
    shutil.rmtree(repo_dst / ".git", ignore_errors=True)

    # 4. Write task.json
    task_meta = {
        "id": spec["name"],
        "prompt": instruction,
        "check": spec["check"],
        "timeout_s": spec["timeout_s"],
    }
    (target_dir / "task.json").write_text(json.dumps(task_meta, indent=2) + "\n", encoding="utf-8")
    return task_meta


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deep-swe-dir", default="",
                        help="Path to cloned datacurve-ai/deep-swe repository")
    parser.add_argument("--out", default="bench/tasks_deepswe",
                        help="Target output directory (default: bench/tasks_deepswe)")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    deep_dir = Path(args.deep_swe_dir) if args.deep_swe_dir else None
    tmp_dir = None
    if deep_dir is None or not deep_dir.is_dir():
        tmp_dir = tempfile.mkdtemp(prefix="deep_swe_clone_")
        print("Cloning datacurve-ai/deep-swe repository (shallow)...")
        subprocess.run(["git", "clone", "--depth", "1", "https://github.com/datacurve-ai/deep-swe.git", tmp_dir],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deep_dir = Path(tmp_dir)

    try:
        imported = []
        for spec in DEFAULT_TASKS:
            meta = import_task(deep_dir, out_dir, spec)
            imported.append(meta)
            print(f"  [OK] Imported {meta['id']} ({spec['repo_url']})")

        manifest = {
            "source": "datacurve-ai/deep-swe",
            "imported_count": len(imported),
            "tasks": imported,
        }
        (out_dir / "SOURCE.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"\nSuccessfully imported {len(imported)} DeepSWE tasks into {out_dir}")
    finally:
        if tmp_dir and Path(tmp_dir).exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""HiveMind DeepSWE adapter: task parsing, request body, patch capture,
verifier backend selection (local Docker vs Modal, no network/token used)."""
import ast
import contextlib
import inspect
import io
import json
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import bench.external.deepswe_hivemind as dsm
from bench.external.deepswe_hivemind import (
    build_parser, hivemind_body, load_task, make_patch, run_verifier_modal,
    select_tasks,
)
from tests._harness import Harness

h = Harness(style="plain", pad=True)
check = h.check

TOML = '''schema_version = "1.3"
[metadata]
task_id = "demo-task"
repository_url = "https://github.com/example/repo"
base_commit_hash = "0123456789abcdef"
[environment]
docker_image = "public.ecr.aws/example/demo:1"
[verifier]
timeout_sec = 1800.0
[verifier.environment]
cpus = 2.0
memory_mb = 8192
'''

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    root = Path(td)
    for name in ("alpha", "beta", "gamma"):
        d = root / name
        d.mkdir()
        (d / "task.toml").write_text(TOML.replace("demo-task", name),
                                      encoding="utf-8")
        (d / "instruction.md").write_text("Do the thing.\n", encoding="utf-8")
    t = load_task(root / "alpha")
    check("task.toml metadata is parsed", t.task_id == "alpha"
          and t.repo_url.endswith("/repo") and t.base_commit == "0123456789abcdef")
    check("task.toml verifier resources are parsed",
          t.verifier_timeout_s == 1800 and t.verifier_cpus == 2.0
          and t.verifier_memory_mb == 8192,
          f" ({t.verifier_timeout_s}/{t.verifier_cpus}/{t.verifier_memory_mb})")
    check("instruction is read", t.instruction.strip() == "Do the thing.")
    only = select_tasks(root, n_tasks=5, names=["beta"])
    check("named task selection", [x.task_id for x in only] == ["beta"])
    sample = select_tasks(root, n_tasks=2, seed=0)
    check("deterministic sample size", len(sample) == 2)
    body = hivemind_body(t, root / "ws", "qwen3.6:35b-a3b-ud", 16384)
    check("HiveMind body is code_duo/agentic with the requested model",
          body["mode"] == "code_duo" and body["duo_agentic_mode"] is True
          and body["duo_coder_model"] == "qwen3.6:35b-a3b-ud"
          and body["duo_planner"] is False
          and body["workspace"].endswith("ws"))

    ws = root / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=ws, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=ws, check=True)
    (ws / "a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.py"], cwd=ws, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=ws, check=True)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ws, check=True,
                          capture_output=True, text=True).stdout.strip()
    (ws / "a.py").write_text("x = 2\n", encoding="utf-8")
    (ws / "new.py").write_text("y = 3\n", encoding="utf-8")
    patch = root / "model.patch"
    ok, detail = make_patch(ws, base, patch)
    text = patch.read_text(encoding="utf-8")
    check("patch captures modified and new files", ok and "a.py" in text
          and "new.py" in text, f"({detail})")
    subprocess.run(["git", "reset", "--hard", base], cwd=ws, check=True)
    subprocess.run(["git", "clean", "-fd"], cwd=ws, check=True)
    check("empty workspace gives no patch", not make_patch(ws, base, root / "empty.patch")[0])

    # ── --verifier {docker,modal} CLI ─────────────────────────────────────
    args = build_parser().parse_args(["--tasks", "t"])
    check("cli: --verifier defaults to docker", args.verifier == "docker")
    args = build_parser().parse_args(["--tasks", "t", "--verifier", "modal"])
    check("cli: --verifier modal accepted", args.verifier == "modal")
    try:
        build_parser().parse_args(["--tasks", "t", "--verifier", "vllm"])
        check("cli: unknown --verifier rejected", False, " (no SystemExit)")
    except SystemExit:
        check("cli: unknown --verifier rejected", True)

    # ── lazy modal import + modal-path wiring (source checks) ─────────────
    check("lazy: importing the adapter does not import modal",
          "modal" not in sys.modules)
    tree = ast.parse(inspect.getsource(dsm))
    top_modal = any(
        (isinstance(n, ast.Import) and any(a.name == "modal" for a in n.names))
        or (isinstance(n, ast.ImportFrom) and n.module == "modal")
        for n in tree.body)
    check("source: modal is not imported at module top level", not top_modal)
    lazy = any(
        isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and fn.name == "run_verifier_modal"
        and any(isinstance(s, ast.Import)
                and any(a.name == "modal" for a in s.names)
                for s in ast.walk(fn))
        for fn in ast.walk(tree))
    check("source: modal imported lazily inside run_verifier_modal", lazy)
    fn_src = inspect.getsource(dsm.run_verifier_modal)
    check("source: image built from tests/Dockerfile with context_dir",
          "from_dockerfile" in fn_src and '"Dockerfile"' in fn_src
          and "context_dir" in fn_src)
    check("source: /tests/test.sh runs on /logs volume with staged patch",
          '"/tests/test.sh"' in fn_src and '"/logs"' in fn_src
          and "/artifacts/model.patch" in fn_src and "/logs/verifier" in fn_src)

    # ── run_verifier_modal against a fake modal SDK (no token, no net) ────
    delta = root / "delta"
    (delta / "tests").mkdir(parents=True)
    (delta / "task.toml").write_text(TOML.replace("demo-task", "delta"),
                                     encoding="utf-8")
    (delta / "instruction.md").write_text("Do the delta thing.\n",
                                          encoding="utf-8")
    (delta / "tests" / "Dockerfile").write_text(
        "FROM python:3.11-slim\nCOPY test.sh /tests/test.sh\n",
        encoding="utf-8")
    dtask = load_task(delta)
    mpatch = root / "modal_model.patch"
    mpatch.write_text("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n",
                      encoding="utf-8")

    class _FakeBatch:
        def __init__(self, rec): self._rec = rec
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def put_file(self, local, remote, mode=None):
            self._rec["put_file"] = (str(local), str(remote))

    class _FakeVolume:
        def __init__(self, rec): self._rec = rec
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def batch_upload(self): return _FakeBatch(self._rec)

    class _FakeProc:
        def __init__(self, out, code=0):
            self.stdout = types.SimpleNamespace(read=lambda: out)
            self._code = code

        def wait(self):
            return self._code

    class _FakeSandbox:
        def __init__(self, rec):
            self._rec = rec

        def exec(self, *args, **kwargs):
            cmd = " ".join(str(a) for a in args)
            self._rec.setdefault("exec_cmds", []).append(cmd)
            if "/tests/test.sh" in cmd:
                if self._rec.get("exec_error"):
                    raise self._rec["exec_error"]
                return _FakeProc(self._rec.get("test_out", ""),
                                 self._rec.get("test_exit", 0))
            return _FakeProc(self._rec.get("reward_raw", ""), 0)

        def terminate(self):
            self._rec["terminated"] = True

    class _FakeApp:
        def __init__(self, name, rec):
            self.name = name
            self._rec = rec
            rec["app_name"] = name

    def _fake_modal_module(rec):
        m = types.ModuleType("modal")
        cls = rec.get("modal_error_cls")  # one class per rec so except matches

        if cls is None:
            class _Error(Exception):
                pass
            cls = _Error
            rec["modal_error_cls"] = cls
        m.Error = cls

        def _from_dockerfile(path, context_dir=None):
            rec["dockerfile"] = str(path)
            rec["context_dir"] = str(context_dir)
            return "fake-image"

        m.Image = types.SimpleNamespace(from_dockerfile=_from_dockerfile)
        m.Volume = types.SimpleNamespace(ephemeral=lambda: _FakeVolume(rec))

        def _app_lookup(name, create_if_missing=False):
            return _FakeApp(name, rec)
        m.App = types.SimpleNamespace(lookup=_app_lookup)

        def _sandbox_create(**kw):
            rec["sandbox_kwargs"] = kw
            if rec.get("sandbox_error"):
                raise rec["sandbox_error"]
            return _FakeSandbox(rec)
        m.Sandbox = types.SimpleNamespace(create=_sandbox_create)

        st = types.ModuleType("modal.stream_type")
        st.StreamType = types.SimpleNamespace(STDOUT="stdout", DEVNULL="devnull")
        m.stream_type = st
        return m

    @contextlib.contextmanager
    def _fake_modal(rec):
        saved = sys.modules.get("modal")
        saved_st = sys.modules.get("modal.stream_type")
        fake = _fake_modal_module(rec)
        sys.modules["modal"] = fake
        sys.modules["modal.stream_type"] = fake.stream_type
        try:
            yield
        finally:
            if saved is None:
                sys.modules.pop("modal", None)
            else:
                sys.modules["modal"] = saved
            if saved_st is None:
                sys.modules.pop("modal.stream_type", None)
            else:
                sys.modules["modal.stream_type"] = saved_st

    def _modal_run(rec, mlogs):
        with _fake_modal(rec):
            return run_verifier_modal(dtask, mpatch, mlogs)

    rec = {"test_exit": 0, "test_out": "3 passed",
           "reward_raw": '{"reward": 1.0}'}
    mlogs = root / "modal_logs"
    res = _modal_run(rec, mlogs)
    check("modal: image built from the task's tests/Dockerfile",
          rec["dockerfile"] == str(delta / "tests" / "Dockerfile")
          and rec["context_dir"] == str(delta / "tests"),
          f" ({rec.get('dockerfile')}/{rec.get('context_dir')})")
    check("modal: model.patch written to logs/artifacts",
          (mlogs / "artifacts" / "model.patch").read_text(encoding="utf-8")
          == mpatch.read_text(encoding="utf-8"))
    check("modal: patch uploaded to /logs/artifacts/model.patch",
          rec["put_file"] == (str(mlogs / "artifacts" / "model.patch"),
                              "/artifacts/model.patch"),
          f" ({rec.get('put_file')})")
    kw = rec["sandbox_kwargs"]
    check("modal: sandbox mounts /logs and uses the task image",
          list(kw["volumes"]) == ["/logs"] and kw["image"] == "fake-image",
          f" ({kw})")
    check("modal: sandbox gets the task's timeout/cpu/memory",
          kw["timeout"] == 2100 and kw["cpu"] == 2.0
          and kw["memory"] == 8192, f" ({kw})")
    check("modal: sandbox blocks network like the official verifier",
          kw["block_network"] is True, f" ({kw})")
    check("modal: test.sh runs in the sandbox",
          rec["exec_cmds"] and rec["exec_cmds"][0].endswith("/tests/test.sh"),
          f" ({rec.get('exec_cmds')})")
    check("modal: reward file is read back through cat",
          len(rec["exec_cmds"]) > 1 and "reward" in rec["exec_cmds"][1],
          f" ({rec.get('exec_cmds')})")
    check("modal: sandbox is terminated after grading",
          rec.get("terminated") is True)
    check("modal: app tagged as the verifier image name",
          rec["app_name"].startswith("deepswe-verifier-")
          and "delta" in rec["app_name"], f" ({rec.get('app_name')})")
    check("modal: reward.json parsed like the Docker path",
          res["ok"] is True and res["reward"] == 1.0 and res["exit_code"] == 0
          and res["output"] == "3 passed"
          and {"ok", "reward", "exit_code", "output"} <= set(res),
          f" ({res})")
    check("modal: verifier reward file mirrored into local logs",
          (mlogs / "verifier" / "reward.json").read_text(encoding="utf-8")
          == '{"reward": 1.0}')

    rec = {"test_exit": 1, "test_out": "2 failed", "reward_raw": "0\n"}
    res = _modal_run(rec, root / "modal_logs2")
    check("modal: reward.txt fallback with failing reward",
          res["ok"] is False and res["reward"] == "0" and res["exit_code"] == 1,
          f" ({res})")
    rec = {"test_exit": 0, "test_out": "o", "reward_raw": "not json"}
    res = _modal_run(rec, root / "modal_logs3")
    check("modal: malformed reward.json degrades to raw text",
          res["ok"] is False and res["reward"] == "not json", f" ({res})")

    rec = {}
    _fake_modal_module(rec)
    rec["sandbox_error"] = rec["modal_error_cls"]("no auth token")
    try:
        _modal_run(rec, root / "modal_logs4")
        check("modal: SDK failure surfaces as RuntimeError", False,
              " (no exception)")
    except RuntimeError as exc:
        check("modal: SDK failure surfaces as RuntimeError",
              "modal verifier failed" in str(exc) and "no auth token" in str(exc),
              f" ({exc})")

    # ── main() dispatch: --verifier selects the verifier backend ──────────
    calls: list[str] = []

    def _stub_verifier(tag):
        def _v(task, patch, logs):
            calls.append(tag)
            return {"ok": True, "reward": 1, "exit_code": 0, "output": ""}
        return _v

    _saved = {n: getattr(dsm, n) for n in ("prepare_workspace", "run_hivemind",
                                           "make_patch", "run_verifier",
                                           "run_verifier_modal")}
    try:
        dsm.prepare_workspace = lambda task, wroot: wroot / task.task_id
        dsm.run_hivemind = lambda base_url, body, timeout: {
            "done": {"stop_reason": "end_turn"}, "stop_reason": "end_turn",
            "tokens_in": 10, "tokens_out": 5, "cached": 0, "error": "",
            "net_tokens": 15}
        dsm.make_patch = lambda ws_, base_, out_: (True, "ok")
        dsm.run_verifier = _stub_verifier("docker")
        dsm.run_verifier_modal = _stub_verifier("modal")
        sel_out = root / "sel_out"
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = dsm.main(["--tasks", str(root), "--task", "alpha",
                             "--out", str(sel_out),
                             "--workspace-root", str(root / "sel_ws"),
                             "--verifier", "modal"])
        check("main: --verifier modal selects the Modal verifier",
              code == 0 and calls == ["modal"], f" ({calls})")
        rows = json.loads((sel_out / "results.json").read_text(
            encoding="utf-8"))["tasks"]
        check("main: row records verifier=modal with its result",
              rows[0].get("verifier") == "modal" and rows[0].get("reward") == 1,
              f" ({rows[0].get('verifier')}/{rows[0].get('reward')})")
        calls.clear()
        with contextlib.redirect_stdout(buf):
            code = dsm.main(["--tasks", str(root), "--task", "alpha",
                             "--out", str(sel_out),
                             "--workspace-root", str(root / "sel_ws")])
        check("main: default --verifier keeps the Docker path",
              code == 0 and calls == ["docker"], f" ({calls})")
    finally:
        for n, fn in _saved.items():
            setattr(dsm, n, fn)

sys.exit(h.finish("deepswe_hivemind"))

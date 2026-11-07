# -*- coding: utf-8 -*-
"""Behavior tests for context/project_state.py (was at 0% coverage).

Covers the pure dataclass logic (add_build_step truncation + history
condensation, to_dict/from_dict roundtrip) and ProjectStateManager
persistence against a temp dir: create/save/load/delete, the corrupt-JSON
recovery path, chat_id sanitization and the build_project_context prompt
rendering. No engine, no model, no repo writes.
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from context.project_state import (  # noqa: E402
    BuildStep,
    ProjectState,
    ProjectStateManager,
    build_project_context,
)
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def _step(action, path=None, ok=True, summary="s"):
    return BuildStep(timestamp="2026-11-07T00:00:01", action=action,
                     file_path=path, success=ok, summary=summary)


def _mk_state(**over):
    base = dict(chat_id="c", workspace_path="/tmp/w", project_name="w",
                created_at="2026-11-07T00:00:00")
    base.update(over)
    return ProjectState(**base)


tmp = Path(tempfile.mkdtemp(prefix="hvm_projstate_"))
try:
    # --- manager: create() defaults + sanitization + persistence -------------
    nested = tmp / "nested" / "deeper"
    mgr = ProjectStateManager(nested)
    check("manager creates missing storage dir", nested.is_dir())

    st = mgr.create("chat 1", str(tmp / "ws"))
    check("create fills dataclass defaults",
          st.total_runs == 0 and st.plan_version == 0
          and st.open_tasks == [] and st.build_history == []
          and st.last_run_success is None)
    check("project_name falls back to workspace basename",
          st.project_name == "ws")
    check("created_at is stamped", bool(st.created_at))
    check("state file uses sanitized chat_id",
          (nested / "chat1.project.json").exists())
    check("explicit project_name wins",
          mgr.create("c2", str(tmp / "ws2"), "Alfa").project_name == "Alfa")
    check("path sanitization keeps only [alnum-_]",
          mgr._path("a/b\\c:d!e-9_z").name == "abcde-9_z.project.json"
          and mgr._path("a/b\\c:d!e-9_z").parent == nested)

    st2 = mgr.create("chat 1", "/some/other/path")
    check("create() returns the existing state unchanged",
          st2.workspace_path == st.workspace_path
          and st2.created_at == st.created_at)

    # --- roundtrip: mutate, save, load with a FRESH manager -----------------
    st.add_build_step("edit", "src/a.py", True, "first edit")
    st.open_tasks = ["t1", "t2"]
    st.completed_tasks = ["t0"]
    st.plan_summary = "plan v1"
    st.plan_version = 2
    st.repomap_hash = "abc123"
    st.total_runs = 3
    st.last_run_timestamp = "2026-11-07T10:00:00"
    st.last_run_success = True
    mgr.save(st)
    check("atomic save leaves no .tmp behind",
          not list(nested.glob("*.tmp")))

    loaded = ProjectStateManager(nested).load("chat 1")
    check("roundtrip preserves scalars",
          loaded.chat_id == "chat 1" and loaded.total_runs == 3
          and loaded.plan_summary == "plan v1" and loaded.plan_version == 2
          and loaded.repomap_hash == "abc123"
          and loaded.last_run_success is True
          and loaded.last_run_timestamp == "2026-11-07T10:00:00")
    check("roundtrip preserves task lists",
          loaded.open_tasks == ["t1", "t2"]
          and loaded.completed_tasks == ["t0"])
    check("roundtrip rebuilds BuildStep objects",
          len(loaded.build_history) == 1
          and isinstance(loaded.build_history[0], BuildStep)
          and loaded.build_history[0].action == "edit"
          and loaded.build_history[0].file_path == "src/a.py"
          and loaded.build_history[0].success is True)
    check("re-saved dict is stable",
          loaded.to_dict() == json.loads(
              (nested / "chat1.project.json").read_text(encoding="utf-8")))

    # --- from_dict tolerates missing optional keys ---------------------------
    bare = ProjectState.from_dict({"chat_id": "x", "workspace_path": "/w",
                                   "project_name": "p",
                                   "created_at": "2026-11-07T00:00:00"})
    check("from_dict defaults for missing optionals",
          bare.build_history == [] and bare.total_runs == 0
          and bare.plan_version == 0 and bare.last_run_success is None)

    # --- corrupt / invalid state files recover to None ----------------------
    (nested / "corrupt.project.json").write_text("{not json", encoding="utf-8")
    check("corrupt JSON -> load returns None",
          mgr.load("corrupt") is None)
    (nested / "halfkey.project.json").write_text('{"chat_id": "x"}',
                                                 encoding="utf-8")
    check("missing required keys -> load returns None",
          mgr.load("halfkey") is None)
    (nested / "badhist.project.json").write_text(json.dumps({
        "chat_id": "x", "workspace_path": "/w", "project_name": "p",
        "created_at": "t", "build_history": [1, 2]}), encoding="utf-8")
    check("non-dict build_history entries -> load returns None",
          mgr.load("badhist") is None)
    check("missing file -> load returns None", mgr.load("never_seen") is None)

    # --- delete --------------------------------------------------------------
    check("delete removes the state file",
          mgr.delete("chat 1") is True
          and not (nested / "chat1.project.json").exists())
    check("delete of absent state -> False", mgr.delete("chat 1") is False)

    # --- add_build_step: truncation + condensation ---------------------------
    long_state = _mk_state()
    long_state.add_build_step("create", None, True, "x" * 150)
    check("summary truncated to 100 chars",
          len(long_state.build_history[0].summary) == 100)

    hist = _mk_state()
    for i in range(20):
        hist.add_build_step("edit", None, i != 2, f"step {i}")  # step 2 fails
    check("history capped at max_history without condensation",
          len(hist.build_history) == 20
          and all(s.action != "summary" for s in hist.build_history))
    hist.add_build_step("test", None, True, "step 20")
    check("21st step condenses the first 5 into one summary step",
          len(hist.build_history) == 17
          and hist.build_history[0].action == "summary"
          and hist.build_history[0].success is False  # early step 2 failed
          and "5 fruehe Steps" in hist.build_history[0].summary)
    check("condensed-away steps are gone",
          all(s.summary != "step 0" for s in hist.build_history[1:])
          and hist.build_history[1].summary == "step 5")

    # --- build_project_context ----------------------------------------------
    check("None -> empty context", build_project_context(None) == "")

    new_ctx = build_project_context(_mk_state(total_runs=0))
    check("fresh project renders the New-project header",
          new_ctx.startswith("## New project: w\nWorkspace: /tmp/w"))

    def ctx_for(success, plan_version=2, steps=(), open_tasks=(),
               completed=()):
        s = _mk_state(total_runs=3, plan_version=plan_version,
                      plan_summary="last plan text",
                      last_run_timestamp="2026-11-07T10:00:00",
                      last_run_success=success,
                      open_tasks=list(open_tasks),
                      completed_tasks=list(completed))
        s.build_history = list(steps)
        return build_project_context(s)

    ok_ctx = ctx_for(True)
    check("run header carries name/workspace/runs",
          "## Project: w" in ok_ctx and "**Workspace:** /tmp/w" in ok_ctx
          and "**Runs so far:** 3" in ok_ctx)
    check("plan version > 1 has no (new) marker",
          "**Plan version:** 2\n" in ok_ctx.replace(" (new)", "\n")
          and " (new)" not in ok_ctx)
    check("successful last run labelled",
          "2026-11-07T10:00:00" in ok_ctx and "(Success)" in ok_ctx)
    check("failed last run labelled",
          "(Failed/Aborted)" in ctx_for(False))
    check("unknown last run labelled",
          "(Unknown)" in ctx_for(None))
    check("plan summary included", "**Last plan:** last plan text" in ok_ctx)

    steps = [_step("edit", f"f{i}.py", ok=i != 6, summary=f"summary {i}")
             for i in range(7)]
    many_ctx = ctx_for(True, steps=steps)
    check("only the last 5 build steps are rendered",
          "summary 6" in many_ctx and "summary 2" in many_ctx
          and "summary 1" not in many_ctx and "summary 0" not in many_ctx)
    check("step overflow line counts the hidden ones",
          "... and 2 more steps" in many_ctx)
    check("step icons and file paths rendered",
          "- [FAIL] edit (f6.py): summary 6" in many_ctx
          and "- [OK] edit (f5.py): summary 5" in many_ctx)
    check("step without file path omits parens",
          "- [OK] test: s" in ctx_for(True, steps=[_step("test")]))

    tasks_ctx = ctx_for(True, open_tasks=["alpha", "beta"],
                        completed=["done1", "done2"])
    check("open tasks rendered as unchecked boxes",
          "- [ ] alpha" in tasks_ctx and "- [ ] beta" in tasks_ctx)
    check("completed tasks rendered with count",
          "### Completed (2 tasks)" in tasks_ctx
          and "- [x] done2" in tasks_ctx)
    check("no open tasks -> explicit note",
          "No open tasks documented." in ctx_for(True))

    full_completed = ctx_for(True, completed=[f"c{i}" for i in range(7)])
    check("completed list shows only the last 5",
          "- [x] c6" in full_completed and "- [x] c2" in full_completed
          and "- [x] c1" not in full_completed
          and "### Completed (7 tasks)" in full_completed)
finally:
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(h.finish("project_state_behavior"))

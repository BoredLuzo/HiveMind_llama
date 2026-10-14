"""CHUNK-RESUME REGRESSION (2026-10-14, follow-up to bc12b5a — Sonnet review):

1. STRUCTURAL: every `_di += 1` chunk-advance site in duo_runner.py writes a
   chunk_checkpoint resume block within the following few statements. The
   original persist existed only on the skip branch; this test keeps the
   coverage from regressing when new paths are added.
2. DURABILITY: a resume-block write for a chat_id WITHOUT a session file
   creates the standalone context file, the write is verified, and a failed
   write is logged loudly (never silently claimed as saved).
3. INTAKE: a resume block with 1/N chunks done resolves with chunk 2 as the
   next chunk (resume never re-runs done chunks).
4. NUZZLE: the smoke-test nudge demands at least one real assertion.
"""
import ast
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


print("=== 1. structural: every _di += 1 persists a chunk checkpoint ===")

_src = (Path(__file__).resolve().parent.parent / "core" / "duo_runner.py").read_text(
    encoding="utf-8")
_lines = _src.splitlines()
_inc = [i + 1 for i, l in enumerate(_lines) if l.strip() == "_di += 1"]
_pers = [i + 1 for i, l in enumerate(_lines)
         if "_halt_with_resume(" in l and "chunk_checkpoint" in l]
check("chunk-advance sites exist (test not vacuous)", len(_inc) >= 8,
      f"({len(_inc)} sites)")
_uncovered = [il for il in _inc if not any(0 < pl - il <= 6 for pl in _pers)]
check("every chunk advance persists within 6 lines", not _uncovered,
      str(_uncovered))
# and the advance count matches the checkpoint count (no stray persists)
check("checkpoint calls == advance sites", len(_pers) == len(_inc),
      f"({len(_pers)} vs {len(_inc)})")

print("=== 2. durability: standalone context file + honest logging ===")

import context.chat as _chat
import context.resume as _resume

_tmp = Path(tempfile.mkdtemp(prefix="hvm_resume_test_"))
_chat._SESSIONS_DIR = _tmp
_chat._chats_cache = {}
_chat._cache_lock = __import__("threading").Lock()
_chat._file_signature_matches = None
_chat._build_file_signature = None
_resume.build_resume_block = None  # will be replaced below

# minimal build_resume_block mirroring chunking.py's shape
def _fake_build(workspace, chunks_total, chunks_done, chunks_remaining,
                written_files, last_summary, plan_msgs, explore_ctx):
    return {
        "workspace": workspace,
        "explore_ctx": explore_ctx,
        "ts": 0.0,
        "files": [],
        "resume": {
            "chunks_total": chunks_total,
            "chunks_done": chunks_done,
            "chunks_remaining": chunks_remaining,
            "written_files": written_files,
            "last_summary": last_summary,
            "plan_msgs": plan_msgs,
            "saved_at": "test",
        },
    }

_resume.build_resume_block = _fake_build

_captured = []
class _Cap(logging.Handler):
    def emit(self, record):
        _captured.append(record.getMessage())
_h = _Cap()
logging.getLogger("hivemind.resume").addHandler(_h)
logging.getLogger("hivemind.resume").setLevel(logging.DEBUG)

_chat_id = "standalone-regression-test"
_resume._write_resume_block(
    chat_id=_chat_id,
    workspace=str(_tmp),
    chunks_total=3,
    chunks_done=["chunk one"],
    chunks_remaining=[{"title": "chunk two"}],
    written_files=[],
    last_summary="chunk one",
    plan_msgs=[],
    explore_ctx="x" * 40,
)
_standalone = _tmp / f"standalone_{_chat_id}.context.json"
check("standalone context file created for sessionless chat", _standalone.exists())
_saved = json.loads(_standalone.read_text(encoding="utf-8")) if _standalone.exists() else {}
check("block content persisted under resume key",
      bool((_saved.get("resume") or {}).get("chunks_done")))
check("save is logged as saved", any("Block saved" in m for m in _captured))

# failed write must be logged loudly: point the path resolution at None
_orig = _chat._ctx_path_for_chat
_chat._ctx_path_for_chat = lambda cid: None
_captured.clear()
_resume._write_resume_block(
    chat_id="doomed-chat",
    workspace=str(_tmp),
    chunks_total=3,
    chunks_done=["a"],
    chunks_remaining=[{"title": "b"}],
    written_files=[],
    last_summary="x",
    plan_msgs=[],
    explore_ctx="y" * 40,
)
_chat._ctx_path_for_chat = _orig
check("failed write logged as NOT persisted (loud, not silent-saved)",
      any("NOT persisted" in m for m in _captured)
      and not any("Block saved" in m for m in _captured if "NOT" not in m))

print("=== 3. intake: 1/N done -> next chunk is chunk 2 ===")

from hive_functions.chunking import load_resume_data, build_resume_block  # noqa: E402

_ctx = _fake_build(workspace=str(_tmp), chunks_total=3,
                   chunks_done=["chunk one"],
                   chunks_remaining=[{"title": "chunk two"}, {"title": "chunk three"}],
                   written_files=[], last_summary="chunk one", plan_msgs=[],
                   explore_ctx="x" * 40)
_ctx["halt_reason"] = "chunk_checkpoint"
_data = load_resume_data(_ctx)
check("resume data resolves", _data is not None)
_r = (_data or {}).get("resume", {})
check("1/3 done recognized", len(_r.get("chunks_done", [])) == 1)
check("next chunk is chunk TWO (not a re-run of chunk 1)",
      (_r.get("chunks_remaining") or [{}])[0].get("title") == "chunk two")

print("=== 4. smoke nudge demands a real assertion ===")

_nudge_src = (Path(__file__).resolve().parent.parent / "core" / "tool_executor.py").read_text(
    encoding="utf-8")
check("smoke nudge requires a real assertion", "at least one real assertion" in _nudge_src)
check("nudge states non-failing files do not count", "cannot fail" in _nudge_src)

shutil.rmtree(_tmp, ignore_errors=True)
print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)

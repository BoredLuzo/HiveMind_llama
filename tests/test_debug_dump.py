"""DEBUG-DUMP (dump instrumentierung): request dumps only fire when Setting
debug_dump_request is set; dumps land in the telemetry dir with role and
chat id in the filename; failures never raise. Also: the per-run flag lives
on DuoRoundState (one dump per RUN, not per round)."""
import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import context.chat  # noqa: F402  (make sure module import works standalone)
from core import telemetry as _tel  # noqa: E402
from core.agentic_duo_state import DuoRoundState  # noqa: E402

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


print("=== debug dump gate, file shape, per-run flag ===")

_tmp = Path(tempfile.mkdtemp(prefix="hvm_dump_test_"))
_orig_dir = _tel._LOG_DIR
_tel._LOG_DIR = _tmp

check("should_dump: None = off", _tel.should_dump({}) is False)
check("should_dump: absent key = off", _tel.should_dump({"x": 1}) is False)
check("should_dump: True = on", _tel.should_dump({"debug_dump_request": True}) is True)
check("should_dump: truthy string = on",
      _tel.should_dump({"debug_dump_request": "1"}) is True)

# DISCOVERABILITY (2026-10-16): the switch was read but had no default, no doc
# and no UI entry - it only worked on a hand-edited settings.json. The key must
# stay in DEFAULT_SETTINGS (off) and in the settings documentation.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from settings import DEFAULT_SETTINGS as _DEF          # noqa: E402

check("DEFAULT_SETTINGS carries debug_dump_request",
      "debug_dump_request" in _DEF)
check("debug_dump_request default is off",
      _DEF.get("debug_dump_request") is False)
check("defaults keep dumping off end-to-end",
      _tel.should_dump(dict(_DEF)) is False)
_doc = (Path(__file__).resolve().parent.parent / "docs" / "settings.md"
        ).read_text(encoding="utf-8", errors="replace")
check("docs/settings.md documents debug_dump_request",
      "`debug_dump_request`" in _doc)

_path = _tel.dump_request("coder", "chat-xyz.01", [{"role": "system", "content": "s"}],
                          [{"type": "function", "function": {"name": "read_file"}}])
check("dump file written", bool(_path) and Path(_path).exists())
check("filename carries role and chat id",
      "dump_coder_chat-xyz.01_" in Path(_path).name, f"({Path(_path).name})")
_saved = json.loads(Path(_path).read_text(encoding="utf-8"))
check("messages persisted as sent", _saved["messages"][0]["content"] == "s")
check("tools persisted", _saved["tools"][0]["function"]["name"] == "read_file")

check("dump with chat_id None does not raise",
      _tel.dump_request("planner", None, [{"role": "user", "content": "q"}]) is not None
      or any(Path(_tmp).glob("dump_planner_*")))

check("duo round state: first-request flag defaults False",
      DuoRoundState().request_dump_done is False)

_tel._LOG_DIR = _orig_dir
import shutil
shutil.rmtree(_tmp, ignore_errors=True)

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)

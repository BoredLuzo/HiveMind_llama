"""IDE-Kette: Editor-Kontext landet im Prompt und im Run-Record."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


from infra import editor_context as ec  # noqa: E402
from context.chat_util import _make_messages  # noqa: E402
from core.state import settings as _live_settings  # noqa: E402

WS = str(ROOT)
# Der Prompt-Bauer holt sich den Workspace aus den Settings - hier auf das
# Repo zeigen, damit der Test unabhaengig von der lokalen Konfiguration ist.
_prev_ws = _live_settings.get("workspace")
_live_settings["workspace"] = WS
BLOCK = "[EDITOR CONTEXT"


class _Mem:
    def as_context_string(self):
        return ""

    def get_session_messages(self):
        return []


class _Pipeline:
    def __init__(self):
        self.memory = _Mem()


# ── 1. Ohne Kontext bleibt der Prompt unveraendert ──────────────────────
ec.store().clear(WS)
msgs = _make_messages(_Pipeline(), "SYSTEM", "hello", None, False, False)
check("no context -> no block", BLOCK not in msgs[0]["content"])
check("system prompt still first", msgs[0]["role"] == "system")

# ── 2. Mit Kontext: Block steht VOR dem System-Prompt ───────────────────
ctx = ec.parse_context({
    "workspace": WS, "file": "src/main.rs", "cursor_line": 12,
    "selection": "fn main() {}",
    "diagnostics": [{"file": "src/main.rs", "line": 12, "severity": "error",
                     "message": "unused variable"}],
})
ec.store().put(ctx)
msgs = _make_messages(_Pipeline(), "SYSTEM", "hello", None, False, False)
sys_text = msgs[0]["content"]
check("block injected into the system prompt", BLOCK in sys_text)
check("block comes before the prompt", sys_text.index(BLOCK) < sys_text.index("SYSTEM"))
check("open file named", "src/main.rs" in sys_text)
check("selection carried", "fn main() {}" in sys_text)
check("diagnostic carried", "unused variable" in sys_text)

# ── 3. Block ueberlebt keine falsche Workspace-Zuordnung ────────────────
check("other workspace -> empty block", ec.context_block_for("/definitely/not/here") == "")
check("meta for other workspace is None", ec.context_meta_for("/definitely/not/here") is None)

# ── 4. Meta fuer den Run-Record ─────────────────────────────────────────
meta = ec.context_meta_for(WS)
check("meta has file", isinstance(meta, dict) and meta.get("file") == "src/main.rs", f"({meta})")
check("meta counts the selection", int(meta.get("selection_chars") or 0) > 0)
check("meta counts diagnostics", int(meta.get("diagnostics") or 0) == 1)
check("meta carries no code", "fn main" not in json.dumps(meta))

# ── 5. token_stats speichert das Feld ───────────────────────────────────
import infra.token_stats as ts  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    stats_path = Path(tmp) / "token_stats.json"
    original = ts._TOKEN_STATS_FILE, ts._token_stats_lock
    ts._TOKEN_STATS_FILE = stats_path
    ts._token_stats_lock = __import__("threading").Lock()   # ohne Lock schreibt _record_run nicht
    try:
        ts._record_run({
            "run_id": "r1", "tokens": 100, "elapsed_s": 1.0,
            "stop_reason": "completed", "mode": "direct",
            "ui_mode": "chat", "models": {"direct": "x"},
            "editor": {"file": "src/main.rs", "selection_chars": 12,
                       "open_files": 1, "diagnostics": 1, "age_s": 0.5},
        })
        data = json.loads(stats_path.read_text(encoding="utf-8")) if stats_path.exists() else {}
        runs = []
        for day in (data.get("daily") or {}).values():
            runs.extend(day.get("run_list") or [])
        rec = next((r for r in runs if r.get("run_id") == "r1"), None)
        check("run record written", rec is not None, f"({list(data)[:4]})")
        if rec:
            check("editor field stored", isinstance(rec.get("editor"), dict)
                  and rec["editor"].get("file") == "src/main.rs", f"({rec.get('editor')})")
    finally:
        ts._TOKEN_STATS_FILE, ts._token_stats_lock = original

# ── 6. Der agentische Coder-Prompt haengt den Block an ──────────────────
duo_src = (ROOT / "core" / "duo_helpers.py").read_text(encoding="utf-8", errors="replace")
check("duo coder prompt uses the editor block",
      "editor_context as _ec_mod" in duo_src and "EDITOR CONTEXT (2026-10-21)" in duo_src)
chat_src = (ROOT / "context" / "chat_util.py").read_text(encoding="utf-8", errors="replace")
check("direct/pipeline prompts use the editor block",
      "editor_context as _editor_ctx" in chat_src)
run_src = (ROOT / "core" / "chat_run.py").read_text(encoding="utf-8", errors="replace")
check("chat_run records the editor meta", '"editor": _editor_meta' in run_src)
check("chat_run logs the injection", "[EDITOR-CONTEXT] run carries" in run_src)

ec.store().clear(WS)
if _prev_ws is None:
    _live_settings.pop("workspace", None)
else:
    _live_settings["workspace"] = _prev_ws
print()
print(f"SUMMARY editor_injection: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

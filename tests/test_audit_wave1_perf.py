# -*- coding: utf-8 -*-
"""Wave 1 Workstream A4 performance & correctness regression tests.

Covers:
  1. hive_functions/pre_explore/contracts.py: clean JSON/TOML extraction
     (no preamble-cut truncation), URL preservation, and _validate_contract /
     _build_contract_dict sanitization of non-numeric complexity_score and
     off-typed fields.
  2. core/tool_exec_helpers.py: _handle_ask_user skips evict_model when
     _ask_user_gate != "open"; _track_file_changes skips disk reads for
     read-only tools and tracks edit_ast/replace_lines; _note_successful_write
     resets error counters for replace_lines and edit_ast.
  3. deploy/fetch_whisper.py & deploy/fetch_woof.py: .fetch_state.json atomic
     writes are throttled to at most once every 2.0 s or 8 MiB transferred
     while initial (running, 0%) and terminal (done/error) writes stay
     unconditional.

Run: python tests/test_audit_wave1_perf.py
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


# ── 1. contracts.py ──────────────────────────────────────────────────────────

def test_contracts() -> None:
    from hive_functions.pre_explore import contracts

    clean_json = json.dumps({
        "partition": "core:sz1",
        "role": "Handles v2 orchestration",
        "exports": ["fn1", "fn2"],
        "files_read": ["core/a.py", "core/b.py"],
        "touched_by_task": True,
        "complexity_score": 0.7,
        "data_flow": "Posts to http://localhost:8080/api",
        "imports_internal": ["utils"],
    })
    parsed = contracts._extract_contract(clean_json, "fallback")
    check("contracts: clean JSON extracted (not truncated by preamble cut)",
          isinstance(parsed, dict)
          and parsed.get("partition") == "core:sz1"
          and parsed.get("exports") == ["fn1", "fn2"]
          and parsed.get("complexity_score") == 0.7
          and parsed.get("data_flow") == "Posts to http://localhost:8080/api",
          f"got={parsed!r}")

    with_preamble = (
        "Sure, here is the contract:\n"
        "<function=write_contract>\n"
        "<parameter=json>\n"
        '{"partition": "api:sz1", "role": "HTTP routes", "exports": ["route_a"], "complexity_score": "high"}\n'
        "</parameter>\n</function>"
    )
    parsed_pre = contracts._extract_contract(with_preamble, "api")
    check("contracts: preamble before marker still stripped and parsed",
          isinstance(parsed_pre, dict)
          and parsed_pre.get("partition") == "api:sz1"
          and parsed_pre.get("complexity_score") == 0.7,
          f"got={parsed_pre!r}")

    clean_toml = (
        '[contract]\n'
        'partition = "deploy:sz1"\n'
        'role = "Fetchers"\n'
        'exports = ["download"]\n'
        'files_read = ["deploy/fetch_woof.py"]\n'
        'touched_by_task = "yes"\n'
        'complexity_score = "medium"\n'
        'data_flow = "Streams model files"\n'
    )
    parsed_toml = contracts._extract_contract(clean_toml, "deploy")
    check("contracts: clean TOML with string complexity_score extracted cleanly",
          isinstance(parsed_toml, dict)
          and parsed_toml.get("partition") == "deploy:sz1"
          and parsed_toml.get("complexity_score") == 0.5,
          f"got={parsed_toml!r}")

    # _validate_contract / _build_contract_dict edge cases
    c_med = contracts._validate_contract({"complexity_score": "medium"}, "p")
    c_high = contracts._validate_contract({"complexity_score": "high"}, "p")
    c_low = contracts._validate_contract({"complexity_score": "low"}, "p")
    c_none = contracts._validate_contract({"complexity_score": None}, "p")
    c_bool = contracts._validate_contract({"complexity_score": True}, "p")
    c_nan = contracts._validate_contract({"complexity_score": float("nan")}, "p")
    c_hi_clamp = contracts._validate_contract({"complexity_score": 4.2}, "p")
    c_lo_clamp = contracts._validate_contract({"complexity_score": -0.5}, "p")
    check("contracts: complexity_score sanitized & clamped without raising",
          c_med["complexity_score"] == 0.5
          and c_high["complexity_score"] == 0.7
          and c_low["complexity_score"] == 0.3
          and c_none["complexity_score"] == 0.5
          and c_bool["complexity_score"] == 0.5
          and c_nan["complexity_score"] == 0.5
          and c_hi_clamp["complexity_score"] == 1.0
          and c_lo_clamp["complexity_score"] == 0.0,
          f"got={(c_med['complexity_score'], c_high['complexity_score'], c_none['complexity_score'])}")

    off_typed = contracts._build_contract_dict({
        "partition": "",
        "role": None,
        "exports": "alpha, beta",
        "files_read": None,
        "imports_internal": [1, None, "core"],
        "touched_by_task": "YES",
        "complexity_score": "garbage",
        "data_flow": None,
        "hint": "fallback hint",
    }, "fallback_p")
    check("contracts: off-typed fields coerced cleanly",
          off_typed["partition"] == "fallback_p"
          and off_typed["role"] == ""
          and off_typed["exports"] == ["alpha", "beta"]
          and off_typed["files_read"] == []
          and off_typed["imports_internal"] == ["1", "core"]
          and off_typed["touched_by_task"] == "yes"
          and off_typed["complexity_score"] == 0.5
          and off_typed["data_flow"] == "fallback hint",
          f"got={off_typed!r}")

    non_dict = contracts._validate_contract(None, "p_none")  # type: ignore[arg-type]
    check("contracts: non-dict input falls back safely",
          non_dict["partition"] == "p_none" and non_dict["complexity_score"] == 0.5)


# ── 2. core/tool_exec_helpers.py ─────────────────────────────────────────────

def test_tool_exec_helpers() -> None:
    from core import tool_exec_helpers as teh
    from tools.runner import _ask_user_gate

    class _Hooks:
        def __init__(self):
            self.evictions: list[str] = []
            self.events: list[dict] = []

        async def evict_model(self, model: str) -> None:
            self.evictions.append(model)

        async def emit(self, ev: dict) -> None:
            self.events.append(ev)

    # _handle_ask_user under throttled_autonomous -> NO eviction
    tok = _ask_user_gate.set("throttled_autonomous")
    try:
        hooks_t = _Hooks()
        port_ref_t = [8001]
        asyncio.run(teh._handle_ask_user(
            "ask_user", {"question": "Proceed?"}, hooks_t, "coder:7b", port_ref_t, "run-t",
        ))
        check("_handle_ask_user does NOT evict or emit when gate != open",
              hooks_t.evictions == []
              and port_ref_t[0] == 8001
              and hooks_t.events == [],
              f"evict={hooks_t.evictions} port={port_ref_t} events={hooks_t.events}")
    finally:
        _ask_user_gate.reset(tok)

    # _handle_ask_user under open -> evicts and emits
    tok2 = _ask_user_gate.set("open")
    try:
        hooks_o = _Hooks()
        port_ref_o = [8001]
        asyncio.run(teh._handle_ask_user(
            "ask_user", {"question": "Proceed?"}, hooks_o, "coder:7b", port_ref_o, "run-o",
        ))
        check("_handle_ask_user evicts and emits when gate == open",
              hooks_o.evictions == ["coder:7b"]
              and port_ref_o[0] is None
              and any(e.get("type") == "agent_asking" for e in hooks_o.events),
              f"evict={hooks_o.evictions} port={port_ref_o} events={hooks_o.events}")
    finally:
        _ask_user_gate.reset(tok2)

    # _track_file_changes skips disk reads for read-only tools
    with tempfile.TemporaryDirectory(prefix="hm_w1a4_") as td:
        ws = Path(td)
        target = ws / "sample.py"
        target.write_text("def foo():\n    return 1\n", encoding="utf-8")

        to_thread_calls = 0
        orig_to_thread = teh.asyncio.to_thread

        async def _spy_to_thread(fn, *args, **kwargs):
            nonlocal to_thread_calls
            to_thread_calls += 1
            return await orig_to_thread(fn, *args, **kwargs)

        hooks_ro = _Hooks()
        res_ro = types.SimpleNamespace(last_run_bash_failure=None, changed_since_failure=set())
        fc_ro: dict = {}
        with mock.patch.object(teh.asyncio, "to_thread", side_effect=_spy_to_thread):
            for tool_name in ("read_file", "list_dir", "get_signatures",
                              "find_files", "search_code", "find_references"):
                asyncio.run(teh._track_file_changes(
                    tool_name, {"path": "sample.py"}, "ok", res_ro, fc_ro, [], hooks_ro, False, str(ws),
                ))
        check("_track_file_changes skips disk reads for read-only tools",
              to_thread_calls == 0 and fc_ro == {} and hooks_ro.events == [],
              f"to_thread={to_thread_calls} fc={fc_ro}")

        # _track_file_changes handles edit_ast & replace_lines
        hooks_wr = _Hooks()
        res_wr = types.SimpleNamespace(last_run_bash_failure={"cmd": "pytest"}, changed_since_failure=set())
        fc_wr: dict = {}
        asyncio.run(teh._track_file_changes(
            "edit_ast",
            {"path": "sample.py", "target_type": "function", "target_name": "foo",
             "new_code": "def foo():\n    x = 2\n    return x\n"},
            "[edit_ast: sample.py replaced function foo (lines 1-2)]",
            res_wr, fc_wr, [], hooks_wr, False, str(ws),
        ))
        check("_track_file_changes tracks edit_ast lines & emits file_change",
              fc_wr.get("sample.py") == {"op": "edited", "lines_added": 3, "lines_removed": 2}
              and "sample.py" in res_wr.changed_since_failure
              and bool(hooks_wr.events and hooks_wr.events[-1].get("content")),
              f"fc={fc_wr} events={hooks_wr.events}")

        fc_rl: dict = {}
        hooks_rl = _Hooks()
        asyncio.run(teh._track_file_changes(
            "replace_lines",
            {"path": "sample.py"},
            "[replace_lines: sample.py (+4/-1)]",
            res_wr, fc_rl, [], hooks_rl, False, str(ws),
        ))
        check("_track_file_changes tracks replace_lines",
              fc_rl.get("sample.py") == {"op": "edited", "lines_added": 4, "lines_removed": 1},
              f"fc={fc_rl}")

    # _note_successful_write resets counters for replace_lines and edit_ast
    for wtool, wres in (
        ("replace_lines", "[replace_lines: sample.py (+1/-1)]"),
        ("edit_ast", "[edit_ast: sample.py replaced function foo (lines 1-2)]"),
    ):
        rs = types.SimpleNamespace(parse_errors=2)
        res_s = types.SimpleNamespace(verify_mutation_serial=0)
        tot = [4]
        teh._note_successful_write(wtool, wres, res_s, rs, tot)
        check(f"_note_successful_write resets counters for successful {wtool}",
              tot[0] == 0 and rs.parse_errors == 1 and res_s.verify_mutation_serial == 1,
              f"tot={tot[0]} parse={rs.parse_errors} serial={res_s.verify_mutation_serial}")

    rs_fail = types.SimpleNamespace(parse_errors=2)
    res_fail = types.SimpleNamespace(verify_mutation_serial=0)
    tot_fail = [4]
    teh._note_successful_write("edit_ast", "[edit_ast error: target not found]", res_fail, rs_fail, tot_fail)
    check("_note_successful_write does NOT reset counters on failed edit_ast",
          tot_fail[0] == 4 and rs_fail.parse_errors == 2 and res_fail.verify_mutation_serial == 0)


# ── 3. deploy/fetch_whisper.py & deploy/fetch_woof.py throttling ─────────────

class _ChunkedResponse(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def getcode(self):
        return 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def test_fetch_throttling() -> None:
    import deploy.fetch_whisper as fw
    import deploy.fetch_woof as fwoof

    with tempfile.TemporaryDirectory(prefix="hm_fetch_thr_") as td:
        root = Path(td)

        # fetch_whisper: 20 MiB in 256 KiB chunks (80 reads) -> throttled to 8 MiB boundaries (2 calls)
        blob_20m = b"W" * (20 * 1024 * 1024)
        wh_calls: list[tuple] = []

        def _fake_prog(state, percent=0.0, model="", message=""):
            wh_calls.append((state, round(percent, 1), model, message))

        wh_dest = root / "stt" / "models" / "m.bin"
        with (
            mock.patch.object(fw, "_progress", side_effect=_fake_prog),
            mock.patch.object(fw.time, "monotonic", return_value=100.0),
            mock.patch("urllib.request.urlopen", side_effect=lambda *a, **kw: _ChunkedResponse(blob_20m)),
        ):
            ok = fw._download("https://example.invalid/m.bin", wh_dest, min_bytes=1024)
        check("fetch_whisper: 20 MiB download throttles _progress to 2 calls (8 MiB & 16 MiB)",
              ok and len(wh_calls) == 2 and all(c[0] == "running" for c in wh_calls),
              f"calls={len(wh_calls)} {wh_calls}")

        # fetch_whisper time trigger: small 1 MiB download with >= 2.0 s between chunks
        blob_1m = b"T" * (1024 * 1024)  # 4 chunks of 256 KiB
        wh_calls_t: list[tuple] = []
        t_seq = iter([0.0, 2.5, 2.6, 5.0, 5.1])

        with (
            mock.patch.object(fw, "_progress", side_effect=lambda s, p=0.0, m="", msg="": wh_calls_t.append((s, p))),
            mock.patch.object(fw.time, "monotonic", side_effect=lambda: next(t_seq, 10.0)),
            mock.patch("urllib.request.urlopen", side_effect=lambda *a, **kw: _ChunkedResponse(blob_1m)),
        ):
            ok_t = fw._download("https://example.invalid/t.bin", root / "stt" / "models" / "t.bin", min_bytes=1024)
        check("fetch_whisper: 2.0s elapsed triggers throttled _progress even below 8 MiB",
              ok_t and len(wh_calls_t) == 2,
              f"calls={len(wh_calls_t)} {wh_calls_t}")

        # fetch_whisper main() always writes initial running (0%) and terminal done (100%)
        main_calls: list[tuple] = []
        with (
            mock.patch.object(fw, "_progress", side_effect=lambda s, p=0.0, m="", msg="": main_calls.append((s, p))),
            mock.patch.object(fw, "install_binaries", return_value=True),
            mock.patch.object(fw, "install_models", return_value=True),
            mock.patch.object(fw, "check", return_value=0),
        ):
            rc = fw.main(["--model", "tiny"])
        check("fetch_whisper main() writes initial running 0% and terminal done 100%",
              rc == 0
              and len(main_calls) >= 2
              and main_calls[0] == ("running", 0.0)
              and main_calls[-1] == ("done", 100.0),
              f"calls={main_calls}")

        # fetch_woof: 20 MiB woof_int8.npz + 8 small files -> initial + 2 byte-threshold + terminal done = 4 writes
        woof_dir = root / "woof"
        payloads = {rel: f"ok:{rel}".encode("utf-8") for rel in fwoof.REQUIRED_FILES}
        payloads["woof_int8.npz"] = b"Z" * (20 * 1024 * 1024)
        woof_writes: list[tuple] = []
        real_write_state = fwoof._write_state

        def _spy_write_state(target, state, percent, message=""):
            woof_writes.append((state, round(percent, 1), message))
            return real_write_state(target, state, percent, message)

        def _fake_woof_open(req, timeout=60):
            url = getattr(req, "full_url", str(req))
            for rel, blob in payloads.items():
                if url.endswith("/" + rel):
                    return _ChunkedResponse(blob)
            raise AssertionError(f"unexpected url {url}")

        with (
            mock.patch.object(fwoof, "MIN_WEIGHTS_BYTES", 1024),
            mock.patch.object(fwoof, "_write_state", side_effect=_spy_write_state),
            mock.patch.object(fwoof.time, "monotonic", return_value=10.0),
            mock.patch("urllib.request.urlopen", side_effect=_fake_woof_open),
        ):
            fwoof.download(woof_dir)
        check("fetch_woof: 20 MiB download throttles running writes + keeps initial & terminal",
              len(woof_writes) == 4
              and woof_writes[0][0] == "running" and woof_writes[0][1] == 0.0
              and woof_writes[1][0] == "running"
              and woof_writes[2][0] == "running"
              and woof_writes[-1][0] == "done" and woof_writes[-1][1] == 100.0,
              f"writes={woof_writes}")

        # fetch_woof error state always written unconditionally
        woof_err_writes: list[tuple] = []
        with (
            mock.patch.object(fwoof, "_write_state", side_effect=lambda t, s, p, m="": woof_err_writes.append((s, p))),
            mock.patch("urllib.request.urlopen", side_effect=OSError("net down")),
        ):
            try:
                fwoof.download(root / "woof_err")
            except OSError:
                pass
        check("fetch_woof: error terminal state written unconditionally",
              len(woof_err_writes) == 2
              and woof_err_writes[0] == ("running", 0.0)
              and woof_err_writes[1] == ("error", 0.0),
              f"writes={woof_err_writes}")


if __name__ == "__main__":
    test_contracts()
    test_tool_exec_helpers()
    test_fetch_throttling()
    sys.exit(h.finish("audit_wave1_perf"))

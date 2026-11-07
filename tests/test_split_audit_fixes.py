# -*- coding: utf-8 -*-
r"""Split-audit regression pins (2026-11-07).

Every finding from the 8-round deep audit of the split work (range
b2216b4..fe76b25) that a functional test could NOT catch (the e2e smoke
builds its own RunContext; the live dispatch path, duplicated blocks and
sync-back gaps only fire under specific conditions) is pinned here as a
source guard or a direct behavioral probe:

  a  run_stream dispatch: _build_run_context must pass complexity
     (stream.py dispatches solely on ctx.complexity — losing the kwarg
     sent every live run to run_direct)
  b  the assistant answer + chat-context sidecar are written exactly
     ONCE (duo_epilogue is the single write point; the runner rest was
     duplicated in the 2.2 carve)
  c  the final verify gate exists exactly once and sits BEFORE the P3
     final test (the 2.3 carve left the inline block in place AND
     delegated after the epilogue)
  d  _explore_has_contents is threaded into the round-input carry
     (wrong pre-explore rule text in round 0 otherwise)
  e  _touched_block_persisted travels as the STATE FIELD the helper
     reads (a carry key the helper never reads = feature dead)
  f  compress-cycle stop paths sync _loop_detected before returning
     (the runner's outer stop funnel keys on it — runs kept going
     after the 'run stopped' emit)
  g  ctx_overrides.default is evaluated again (lost in the 0361af0 move)
  h  the first regex hit in core/version.py is the real literal (the
     docstring once carried a matching line — every probe reads hit #1)
  i  updater digest check accepts an uppercase 'SHA256:' prefix
  j  the MoE phase returns the MiB int, not a bool (pre-flight VRAM
     under-estimation)
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


CHAT_RUN = (ROOT / "core" / "chat_run.py").read_text(encoding="utf-8", errors="replace")
RUNNER = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8", errors="replace")
EPI = (ROOT / "core" / "duo_epilogue.py").read_text(encoding="utf-8", errors="replace")
FG = (ROOT / "core" / "duo_finalgate.py").read_text(encoding="utf-8", errors="replace")
RI = (ROOT / "core" / "duo_round_input.py").read_text(encoding="utf-8", errors="replace")
CC = (ROOT / "core/duo_compress_cycle.py").read_text(encoding="utf-8", errors="replace")
IP = (ROOT / "core/duo_inloop_planner.py").read_text(encoding="utf-8", errors="replace")
PH = (ROOT / "backend" / "llama_start_phases.py").read_text(encoding="utf-8", errors="replace")
UPD = (ROOT / "deploy" / "updater.py").read_text(encoding="utf-8", errors="replace")
VER = (ROOT / "core" / "version.py").read_text(encoding="utf-8", errors="replace")
VIS = (ROOT / "core" / "chat_vision_pre.py").read_text(encoding="utf-8", errors="replace")
PYB = (ROOT / "deploy" / "pyright_baseline.py").read_text(encoding="utf-8", errors="replace")

# ── a. dispatch complexity ──────────────────────────────────────────────────
_i = CHAT_RUN.find("def _build_run_context():")
_seg = CHAT_RUN[_i: CHAT_RUN.find("return RunContext", _i) + 4000] if _i >= 0 else ""
check("a1 _build_run_context passes complexity=complexity",
      "complexity=complexity," in _seg)
check("a2 complexity_source kwarg is correctly indented (no carve artifact)",
      "\n            complexity_source=_complexity_source," in _seg)

# ── b. single session write + sidecar ──────────────────────────────────────
check("b1 the duo runner writes no assistant session itself",
      'add_to_session("assistant"' not in RUNNER)
check("b2 the epilogue is the single write point",
      EPI.count('ctx.memory.add_to_session("assistant", _duo_session_out)') == 1)
check("b3 the sidecar block exists exactly once (epilogue)",
      EPI.count("SIDECAR-SESSION-AFTER-ASSISTANT") == 1
      and "SIDECAR-SESSION-AFTER-ASSISTANT" not in RUNNER)

# ── c. final verify gate once, before P3 ───────────────────────────────────
check("c1 the verify-gate body exists exactly once (module only)",
      RUNNER.count("Run not marked completed") == 0
      and FG.count("Run not marked completed") == 1)
check("c2 the runner carries no inline timeout-token rest",
      RUNNER.count("Run-Timeout-Guard") == 0
      and FG.count("Run-Timeout-Guard") == 1)
check("c3 the delegation fires before the P3 final test",
      RUNNER.index("_duo_final_verify_gate(ctx, _fg_ps)")
      < RUNNER.index("P3: Final Test after all chunks"))

# ── d/e. round-input threading ─────────────────────────────────────────────
_ri = RUNNER[RUNNER.find("_ri_ps = _DuoRoundInputState("):]
_ri = _ri[:_ri.find("async for _ev in _duo_coder_round_input")]
check("d _explore_has_contents travels in the carry",
      '"_explore_has_contents": _explore_has_contents' in _ri)
check("e1 _touched_block_persisted travels as the state field",
      "_touched_block_persisted=_touched_block_persisted," in _ri
      and '"_touched_block_persisted"' not in _ri)
check("e2 the helper reads the field (not the carry)",
      "_touched_block_persisted = ps._touched_block_persisted" in RI)

# ── f. compress stop syncs ─────────────────────────────────────────────────
_cc_lines = CC.splitlines()
_stop_lines = [n for n, l in enumerate(_cc_lines) if l.strip() == "ps.stop = True"]
_synced = 0
for n in _stop_lines:
    _window = "\n".join(_cc_lines[max(0, n - 25): n])
    _synced += 1 if "ps._loop_detected = True" in _window else 0
check("f every compress stop syncs _loop_detected before returning",
      len(_stop_lines) == 2 and _synced == 2, f" stops={len(_stop_lines)} synced={_synced}")

# ── g. ctx_overrides.default ────────────────────────────────────────────────
from core.ctx_overrides import _ctx_override_lookup  # noqa: E402
check("g1 numeric ctx_overrides.default applies again",
      _ctx_override_lookup({"default": "4096"}, "qwen3.5:4b", None) == 4096)
check("g2 an explicit role still wins over default",
      _ctx_override_lookup({"default": "4096", "duo_coder": "8192"},
                           "x", "duo_coder") == 8192)

# ── h. version probe hits the literal ───────────────────────────────────────
from core.version import HIVEMIND_VERSION  # noqa: E402
_m = re.search(r"HIVEMIND_VERSION\s*=\s*.([0-9][0-9.a-z-]*)", VER)
check("h the first probe hit equals the real literal",
      _m is not None and _m.group(1) == HIVEMIND_VERSION,
      f" hit={_m.group(1) if _m else None!r} literal={HIVEMIND_VERSION!r}")

# h2: the update.bat shipped with <=1.5.5 reads server.py, not core/version.py.
# A text-only anchor in server.py keeps the upgrade path for existing installs.
_SRV = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
_m_srv = re.search(r"HIVEMIND_VERSION\s*=\s*.([0-9][0-9.a-z-]*)", _SRV)
check("h2 <=1.5.5 updater can still read server.py",
      _m_srv is not None and _m_srv.group(1) == HIVEMIND_VERSION,
      f" hit={_m_srv.group(1) if _m_srv else None!r} literal={HIVEMIND_VERSION!r}")

# ── i. digest prefix case-insensitive ──────────────────────────────────────
from deploy import updater as UPDMOD  # noqa: E402
import tempfile  # noqa: E402
import hashlib  # noqa: E402
_zp = Path(tempfile.mkdtemp(prefix="hvm_auditfix_")) / "z.zip"
_zp.write_bytes(b"payload")
_h = hashlib.sha256(b"payload").hexdigest()
check("i1 uppercase SHA256: prefix is verified, not skipped",
      UPDMOD.digest_ok(_zp, "SHA256:" + _h) is True)
check("i2 a real mismatch still fails",
      UPDMOD.digest_ok(_zp, "sha256:" + "0" * 64) is False)
check("i3 no digest skips",
      UPDMOD.digest_ok(_zp, "") is None)

# ── j. MoE MiB int ──────────────────────────────────────────────────────────
# The slot attribute is only the bool marker (`slot._moe_cache_used = True`);
# the size lives in the local `_moe_cache_used = int(_moe_cache_mib)`.
check("j _phase_moe returns the MiB int (not the slot bool)",
      "moe_cache_used=_moe_cache_used" in PH
      and "int(slot._moe_cache_used" not in PH
      and "_moe_cache_used = int(_moe_cache_mib)" in PH)

# ── l-o. follow-ups found in the re-review (2026-11-07) ────────────────────
check("l the finalize carry passes ctx.this_file (skills dir)",
      '"this_file": ctx.this_file' in RUNNER
      and '"this_file": Path(__file__)' not in RUNNER)
check("m _coder_port travels as the state field",
      "_coder_port=_coder_port," in _ri)
check("m2 _final_verdict travels in the carry",
      '"_final_verdict": final_verdict' in _ri)
check("n inloop planner syncs _duo_timed_out",
      "_duo_timed_out = ps._duo_timed_out" in IP
      and "ps._duo_timed_out = _duo_timed_out" in IP)
check("o vision outer VRAM handler covers httpx",
      "IndexError, httpx.HTTPError) as _ve" in VIS)

# ── p-q. process gaps from the re-review (2026-11-07) ──────────────────────
check("p auto-stop diag reads the runner carry (no module-scope locals probe)",
      'ps.carry.get("_drop_names"' in EPI and "_dd_locals" not in EPI)
_PY_TARGETS = [
    "core/duo_finalize.py", "core/duo_epilogue.py", "core/duo_finalgate.py",
    "core/duo_round_input.py", "core/duo_dtool_setup.py",
    "core/duo_inloop_planner.py", "core/duo_coder_load.py",
    "core/duo_compress_cycle.py", "core/duo_coder_preloop.py",
    "core/duo_task_complete_gate.py",
    "core/chat_shortcuts.py",
    "core/chat_vision_pre.py", "core/chat_history_seed.py",
    "core/chat_route.py", "core/chat_route_decision.py",
    "core/chat_tools_route.py",
    "core/llm_stream.py", "core/ctx_overrides.py", "core/run_context.py",
    "core/stream.py", "learning/soul_evolution.py",
    "backend/llama_start_phases.py", "deploy/updater.py",
]
check("q pyright baseline covers every split module",
      all(t in PYB for t in _PY_TARGETS),
      f" missing={[t for t in _PY_TARGETS if t not in PYB]}")

# ── bonus: ordering pins that only make sense together ─────────────────────
check("k1 the epilogue runs before the finalize tail (single-write order)",
      RUNNER.index("_duo_session_epilogue(ctx, _epi_ps)")
      < RUNNER.index("_duo_run_finalize(ctx, _fin_ps)"))
check("k2 the compress stop emits name the 3x fail ladder",
      CC.count("repeatedly fails to shrink the context (3x)") == 2)

print()
print(f"SUMMARY split_audit_fixes: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)

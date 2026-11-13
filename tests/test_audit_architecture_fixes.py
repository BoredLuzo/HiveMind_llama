# -*- coding: utf-8 -*-
"""Regression checks for architecture/audit fixes (2026-11-13)."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests._harness import Harness  # noqa: E402

h = Harness(style="results", pad=True)
check = h.check

# ── 1. Fresh-install mode="chat" routes to simple + normalized in app.js ─────
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
check("1a app.js normalizes mode='chat' to 'simple'",
      "s.mode === 'chat' ? 'simple'" in JS)
check("1b chat_route_decision accepts mode in ('simple', 'chat')",
      'elif mode in ("simple", "chat"):'
      in (ROOT / "core" / "chat_route_decision.py").read_text(encoding="utf-8"))

# ── 2. UI Optionals suffix (+Subagent) & duo_critic Follow Duo-Coder ─────────
check("2a _CODER_TIER_HINTS.core does not hardcode subagent",
      ", subagent." not in JS.split("_CODER_TIER_HINTS = {", 1)[1][:300])
check("2b _activeOptionalsSuffix includes +Subagent when S.subagentLite",
      "parts.push('+Subagent')" in JS
      and "updateDuoCoderTierHint" in JS.split("function syncSubagentUI()", 1)[1][:280])
check("2c refreshAllSelects preserves Follow Duo-Coder & saveAgentModelDirect allows ''",
      "agent === 'duo_critic'" in JS.split("function refreshAllSelects()", 1)[1][:700]
      and "key !== 'duo_critic'" in JS.split("async function saveAgentModelDirect(", 1)[1][:150])

_cfg_src = (ROOT / "routers" / "config.py").read_text(encoding="utf-8")
check("2d routers/config handles duo_critic empty model & seed sampling bound & all_model direct_model clear",
      'elif key == "duo_critic" and "model" in d:' in _cfg_src
      and '"seed": (-1.0, 2147483647.0, True)' in _cfg_src
      and 'settings.pop("direct_model", None)' in _cfg_src.split("async def set_all_model", 1)[1][:600])

# ── 3. models_registry._merge recursive dict merge ───────────────────────────
from model_configs.models_registry import _merge  # noqa: E402

_merged = _merge(
    {"sampling": {"thinking": {"temperature": 0.6, "preserve_thinking": True, "top_p": 0.95}}},
    {"sampling": {"thinking": {"temperature": 0.4}}},
)
check("3a _merge preserves nested sampling keys",
      _merged["sampling"]["thinking"] == {"temperature": 0.4, "preserve_thinking": True, "top_p": 0.95})

# ── 4. LlamaClient _build_payload cache_prompt & preserve_thinking ───────────
from backend.llama_client import _build_payload  # noqa: E402

_p_nocache = _build_payload(
    [{"role": "user", "content": "hi"}], 0.5, 100, False,
    sampler={"cache_prompt": True, "preserve_thinking": True},
    cache_prompt=False, thinking=True,
)
check("4a explicit cache_prompt=False overrides sampler cache_prompt=True",
      _p_nocache["cache_prompt"] is False)
check("4b preserve_thinking moves into chat_template_kwargs",
      "preserve_thinking" not in _p_nocache
      and _p_nocache.get("chat_template_kwargs", {}).get("preserve_thinking") is True)

# ── 5. Split GGUF indexing & _sibling_has_mmproj isolation ───────────────────
from backend.llama_models import _parse_gguf_filename, _quant_token, _SPLIT_SHARD_RE  # noqa: E402
from backend.gguf_meta import _sibling_has_mmproj, _total_gguf_size_bytes  # noqa: E402

check("5a split-GGUF shard regex + parser strips -00001-of-00003 before quant",
      "qwen3.5:4b" in _parse_gguf_filename("Qwen3.5-4B-Q4_K_M-00001-of-00003.gguf")
      and _quant_token("Qwen3.5-4B-Q4_K_M-00001-of-00003.gguf") == "q4_k_m"
      and bool(_SPLIT_SHARD_RE.search("Model-00002-of-00003")))

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    d = Path(td)
    m_main = d / "Qwen3.5-4B-Q4_K_M.gguf"
    m_other = d / "LFM3-1.7B-Q4_K_M.gguf"
    mm_generic = d / "mmproj-F16.gguf"
    mm_qwen = d / "mmproj-Qwen3.5-4B-F16.gguf"
    m_main.write_bytes(b"x")
    m_other.write_bytes(b"y")
    mm_generic.write_bytes(b"0" * (2 * 1024 * 1024))
    mm_qwen.write_bytes(b"1" * (2 * 1024 * 1024))
    check("5b generic mmproj does not leak across multi-model shared dir",
          _sibling_has_mmproj(m_other) is False)
    check("5c matching named mmproj resolves for target model only",
          _sibling_has_mmproj(m_main) is True)
    s1 = d / "Big-35B-Q4-00001-of-00002.gguf"
    s2 = d / "Big-35B-Q4-00002-of-00002.gguf"
    s1.write_bytes(b"a" * 100)
    s2.write_bytes(b"b" * 250)
    check("5d split GGUF total size sums all shards",
          _total_gguf_size_bytes(s1, 100) == 350)

# ── 6. patch_diff EOF \ No newline fixes ─────────────────────────────────────
from tools.patch_diff import parse_unified_diff, apply_hunks_to_text  # noqa: E402

_eof_patch = (
    "--- a/f.txt\n"
    "+++ b/f.txt\n"
    "@@ -1,2 +1,2 @@\n"
    " a\n"
    "-b\n"
    "+c\n"
    "\\ No newline at end of file\n"
)
_fps = parse_unified_diff(_eof_patch)
check("6a trailing no-newline marker parsed after counts reach zero",
      _fps[0].hunks[0].new_ends_without_newline is True
      and apply_hunks_to_text("a\nb\n", _fps[0].hunks) == "a\nc")

_append_nl_patch = (
    "--- a/g.txt\n"
    "+++ b/g.txt\n"
    "@@ -1 +1,2 @@\n"
    " last\n"
    "\\ No newline at end of file\n"
    "+added\n"
)
_fps2 = parse_unified_diff(_append_nl_patch)
check("6b '+' line after EOF no-newline context restores trailing newline",
      apply_hunks_to_text("last", _fps2[0].hunks) == "last\nadded\n")

# ── 7. write_file_append Python 3.12 compatibility & notebook JSON valid ─────
_fops_src = (ROOT / "tools" / "handlers" / "file_ops.py").read_text(encoding="utf-8")
check("7a file_ops.py avoids Python 3.13-only Path.read_text(..., newline=)",
      "read_text(" not in _fops_src.split("def _append_and_size", 1)[1][:600])

nb = json.loads((ROOT / "bench" / "external" / "hivemind_bench_colab_autopilot.ipynb").read_text(encoding="utf-8"))
_nb_src = "\n".join("".join(c.get("source", [])) for c in nb.get("cells", []))
check("7b Colab notebook has 4B 100k + 35B MTP 120k (3 core + 1 polyglot)",
      "ctx_small=122880" in _nb_src and '"35b-mtp"' in _nb_src and "core_runs=3, polyglot_runs=1" in _nb_src)

# ── 8. scheduler double-fire claim & llm_transport CallExpression/RemoteError
from scheduler.manager import SchedulerManager  # noqa: E402

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td2:
    sm = SchedulerManager(Path(td2) / "jobs.json", "http://127.0.0.1:8001")
    job = sm.store.add({"name": "j", "prompt": "hi", "schedule": {"kind": "every", "minutes": 5}})
    sm._in_flight_jobs.add(job["id"])
    ok, msg = sm.run_now(job["id"])
    check("8a run_now blocks while job id is claimed in _in_flight_jobs",
          ok is False and "busy" in msg.lower())

_tr_src = (ROOT / "core" / "llm_transport.py").read_text(encoding="utf-8")
check("8b llm_transport sets enable_thinking=False on CallExpression & catches RemoteProtocolError",
      '_ctk["enable_thinking"] = False' in _tr_src
      and "except (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError):" in _tr_src)
_tl_src = (ROOT / "core" / "tool_loop.py").read_text(encoding="utf-8")
check("8c tool_loop forwards _smp['preserve_thinking'] into chat_template_kwargs",
      '_smp.get("preserve_thinking")' in _tl_src
      and '["preserve_thinking"] = True' in _tl_src)

# ── 9. tools/runner mode/phone check before READ_REQUIRED ────────────────────
from tools.runner import _run_inline_tool  # noqa: E402

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td3:
    ws3 = Path(td3)
    (ws3 / "x.py").write_text("a = 1\n", encoding="utf-8")
    _out = asyncio.run(_run_inline_tool(
        "edit_file", {"path": "x.py", "old_text": "a = 1", "new_text": "a = 2"},
        str(ws3), tool_mode="direct_research",
    ))
    check("9a TOOL_NOT_ALLOWED precedes READ_REQUIRED for disallowed mode",
          "TOOL_NOT_ALLOWED" in _out and "READ_REQUIRED" not in _out)

# ── 10. Pre-Colab blockers & audit follow-up fixes ──────────────────────
_vram_src = (ROOT / "core" / "duo" / "_vram.py").read_text(encoding="utf-8")
check("10a duo/_vram reads duo_coder_tool_thinking_auto_mode (no ctx.duo_config prefix)",
      'ctx.settings.get("duo_coder_tool_thinking_auto_mode"' in _vram_src
      and '"ctx.duo_config.coder_tool_thinking_auto_mode"' not in _vram_src)

_dr_tail = (ROOT / "core" / "direct_runner.py").read_text(encoding="utf-8")
check("10b plain direct yields run_meta + usage_meta (including prompt-only)",
      'yield await ctx.emit({"type": "usage_meta", "phase": "direct"' in _dr_tail
      and "if _d_ct_sum > 0 or _d_pt_sum > 0:" in _dr_tail)

import core.llm_transport as T  # noqa: E402
import deploy.fetch_models as fm  # noqa: E402
import model_configs.models_registry as _mr_ref  # noqa: E402

_app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
_dis_with_pt = T.disable_thinking_in_payload(
    {"thinking": True, "chat_template_kwargs": {"enable_thinking": True, "preserve_thinking": True}}
)
_strip_with_pt = T.strip_payload_extras(
    {"thinking": True, "thinking_budget": 900, "cache_prompt": True,
     "chat_template_kwargs": {"enable_thinking": True, "preserve_thinking": True}}
)
_pt_only_usage = T.parse_usage({"prompt_tokens": 150, "completion_tokens": 0, "cache_n": 100})
check("10c llm_transport clears preserve_thinking and counts prompt-only usage",
      "preserve_thinking" not in (_dis_with_pt.get("chat_template_kwargs") or {})
      and "preserve_thinking" not in (_strip_with_pt.get("chat_template_kwargs") or {})
      and _strip_with_pt.get("thinking") is False
      and "thinking_budget" not in _strip_with_pt
      and _pt_only_usage is not None and _pt_only_usage["prompt_tokens"] == 150
      and _pt_only_usage["cached_tokens"] == 100)

_cr_src = (ROOT / "core" / "chat_run.py").read_text(encoding="utf-8")
check("10d chat_run ledger records prompt-only usage_meta safely",
      'data.get("type") == "usage_meta" and (data.get("completion_tokens") or data.get("prompt_tokens"))' in _cr_src
      and "if _ct_n > 0:" in _cr_src)

_boot_src = (ROOT / "bench" / "external" / "rent_bootstrap.sh").read_text(encoding="utf-8")
check("10e rent_bootstrap extracts numeric llama-b build id cleanly",
      "llama-b\\([0-9][0-9]*\\)" in _boot_src)

_br_src = (ROOT / "bench" / "runner.py").read_text(encoding="utf-8")
check("10f bench/runner preserves net_tokens=0 and reserves Aider headroom",
      "int(_net_tok) if _net_tok is not None else tok_total" in _br_src
      and "int(cfg.ctx_tokens) - 12288" in _br_src)

_ml_src = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
check("10g manager_load downgrades Linux mmap+mlock -> mmap when mlock=false or RLIMIT tight",
      "LINUX-MTP-MMAP-FALLBACK" in _ml_src and '_load_mode = "mmap"' in _ml_src)

_pa_src = (ROOT / "tools" / "handlers" / "patch_apply.py").read_text(encoding="utf-8")
check("10h apply_patch forwards _run_settings_snapshot to auto_test_note",
      "settings=_run_settings_snapshot.get(None)" in _pa_src)

with tempfile.TemporaryDirectory(prefix="hm_split_w_") as _td_sp:
    _mdir = Path(_td_sp) / "models"
    _mdir.mkdir()
    (_mdir / "Qwen3.6-35B-A3B-Q4_K_M-00001-of-00002.gguf").write_bytes(b"1")
    (_mdir / "Qwen3.6-35B-A3B-Q4_K_M-00002-of-00002.gguf").write_bytes(b"2")
    _out_mj = Path(_td_sp) / "models.json"
    fm.write_models_json(_mdir, _out_mj)
    _mapped = json.loads(_out_mj.read_text(encoding="utf-8"))
    check("10i write_models_json keeps shard 00001 and ignores shard 00002",
          any("00001-of-00002" in v for v in _mapped.values())
          and not any("00002-of-00002" in v for v in _mapped.values()))

_srv_src = (ROOT / "server.py").read_text(encoding="utf-8")
check("10j server /stream and app.js pass subagent_lite_enabled",
      'if "subagent_lite_enabled" in body:' in _srv_src
      and "subagent_lite_enabled: S.subagentLite !== false" in _app_js)

_mr_ref.refresh()
_prof_a = _mr_ref.get_profile("qwen3.5:4b-mtp")
if _prof_a and isinstance(_prof_a.get("sampling"), dict):
    _prof_a["sampling"]["_mutated_test"] = 123
    _prof_b = _mr_ref.get_profile("qwen3.5:4b-mtp")
    check("10k models_registry.get_profile returns isolated deepcopy",
          "_mutated_test" not in (_prof_b.get("sampling") or {}))

sys.exit(h.finish("audit_architecture_fixes"))

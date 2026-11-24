# -*- coding: utf-8 -*-
"""Stage 3B Workstream C3 regression tests:
  1. Model profile override API (GET/PUT/DELETE /models/profile/{name}, POST /models/rescan)
     via FastAPI TestClient, strict schema & path-traversal validation, atomic USER_DIR saves.
  2. Frontend static & Node VM checks:
     - DOMParser allowlist _sanitizeChatHtml (C6)
     - Delegated data-* handlers replacing model-ID inline onclicks
     - rAF coalescing + final flush in _toolGenStream / _tgPanelFlush / _toolGenDone
     - _liveMdTrailT cleanup in _liveMdReset / forced _liveMdRender
     - Collapsible searchable Advanced / Engine Settings drawer (#adv-engine-settings-card)
     - Model Profiles modal (#model-profiles-modal) & GGUF rescan wiring
     - Protected AGENTS.md invariants ("zu groß", feature-search stems, node --check)

Run: python tests/test_ui_advanced_and_profiles.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402
from tests.ui_sources import all_static_js_files, read_all_ui_js  # noqa: E402

h = Harness(pad=True, style="summary")
check = h.check


def _write_gguf_v3(path: Path, kv_items: list[tuple[str, int, object]]) -> Path:
    """Write a minimal valid GGUF v3 header with typed KV entries."""
    def _s(text: str) -> bytes:
        b = text.encode("utf-8")
        return struct.pack("<Q", len(b)) + b

    out = bytearray()
    out += b"GGUF"
    out += struct.pack("<IQQ", 3, 0, len(kv_items))
    for key, vtype, val in kv_items:
        out += _s(key)
        out += struct.pack("<I", vtype)
        if vtype == 4:
            out += struct.pack("<I", int(val))  # type: ignore[arg-type]
        elif vtype == 7:
            out += struct.pack("<?", bool(val))
        elif vtype == 8:
            out += _s(str(val))
        elif vtype == 9:
            etype, items = val  # type: ignore[misc]
            out += struct.pack("<IQ", etype, len(items))
            for item in items:
                if etype == 4:
                    out += struct.pack("<I", int(item))
                elif etype == 8:
                    out += _s(str(item))
                else:
                    raise ValueError(f"unsupported array etype {etype}")
        else:
            raise ValueError(f"unsupported vtype {vtype}")
    path.write_bytes(bytes(out))
    return path


# ── 1. Backend API tests: routers/models.py via FastAPI TestClient ─────────
def _test_models_profile_api() -> None:
    import backend.gguf_meta as gm
    import backend.llama_models as lm
    import core.state as cstate
    import model_configs.models_registry as mr
    import routers.models as rm

    app = FastAPI()
    app.include_router(rm.router)
    client = TestClient(app)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        shipped_dir = tmp / "shipped"
        user_dir = tmp / "user"
        models_dir = tmp / "ggufs"
        shipped_dir.mkdir()
        user_dir.mkdir()
        models_dir.mkdir()

        (shipped_dir / "ship-model_7b.json").write_text(
            json.dumps({
                "model": "ship-model:7b",
                "reasoning": "on",
                "moe": True,
                "moe_cpu_experts": 28,
                "num_ctx": 16384,
                "kv_cache_type": "q4_0",
                "capabilities": {"thinking": True, "vision": False, "tool_call": True},
            }),
            encoding="utf-8",
        )
        drop_gguf = _write_gguf_v3(
            models_dir / "DropIn-Lab-4B-Q4_K_M.gguf",
            [
                ("general.architecture", 8, "llama"),
                ("general.name", 8, "DropIn Lab 4B"),
                ("llama.block_count", 4, 24),
                ("llama.context_length", 4, 32768),
                ("llama.embedding_length", 4, 2048),
                ("llama.attention.head_count", 4, 16),
                ("llama.attention.head_count_kv", 4, 4),
                ("tokenizer.chat_template", 8, "{% if tools %}<tool_call>{% endif %}"),
                ("tokenizer.ggml.tokens", 9, (8, ["hello", "<think>"])),
            ],
        )

        orig_models_dir = mr._MODELS_DIR
        orig_user_dir = mr.USER_DIR
        orig_lm_dir = lm.MODELS_DIR
        orig_models_cache = list(rm.S_models_cache) if isinstance(rm.S_models_cache, list) else rm.S_models_cache
        orig_state_cache = list(cstate.S_models_cache) if isinstance(cstate.S_models_cache, list) else cstate.S_models_cache
        try:
            mr._MODELS_DIR = shipped_dir
            mr.USER_DIR = user_dir
            mr.refresh()
            lm.MODELS_DIR = models_dir
            lm.invalidate_caches()
            gm.clear_cache()

            # 1a: GET shipped profile
            r_ship = client.get("/models/profile/ship-model:7b")
            d_ship = r_ship.json()
            check("1a GET /models/profile shipped returns source, profile, file",
                  r_ship.status_code == 200
                  and d_ship.get("ok") is True
                  and d_ship.get("source") == "shipped"
                  and d_ship.get("file") == "ship-model_7b.json"
                  and (d_ship.get("profile") or {}).get("moe_cpu_experts") == 28
                  and "chat_template" not in (d_ship.get("gguf") or {}),
                  f"got={d_ship}")

            # 1b: GET gguf_auto profile strips chat_template
            with mock.patch("backend.llama_models.resolve_model_path", return_value=drop_gguf):
                r_gguf = client.get("/models/profile/dropin-lab:4b")
            d_gguf = r_gguf.json()
            check("1b GET /models/profile gguf_auto returns metadata without chat_template",
                  r_gguf.status_code == 200
                  and d_gguf.get("ok") is True
                  and d_gguf.get("source") == "gguf_auto"
                  and d_gguf.get("file") is None
                  and (d_gguf.get("gguf") or {}).get("architecture") == "llama"
                  and (d_gguf.get("gguf") or {}).get("has_thinking") is True
                  and (d_gguf.get("gguf") or {}).get("has_tool_call") is True
                  and "chat_template" not in (d_gguf.get("gguf") or {}),
                  f"got={d_gguf}")

            # 1c: Path traversal and invalid slug checks -> 400
            bad_names_http = [
                "..%2F..%2Fetc%2Fpasswd",
                "foo%2Fbar",
                "foo%5Cbar",
                ".hidden",
                "bad;cmd",
                "x..y",
            ]
            bad_http_ok = True
            for bad in bad_names_http:
                for method in ("GET", "PUT", "DELETE"):
                    kw = {"json": {"num_ctx": 4096}} if method == "PUT" else {}
                    resp = client.request(method, f"/models/profile/{bad}", **kw)
                    if resp.status_code != 400:
                        bad_http_ok = False
                        break
            direct_bad = asyncio.run(rm.get_model_profile_override("../../etc/passwd"))
            check("1c GET/PUT/DELETE invalid/traversal model slugs return HTTP 400",
                  bad_http_ok and direct_bad.status_code == 400)

            # 1d: Strict validation rejects bad payloads with HTTP 400
            bad_payloads = [
                {"mmproj_filename": "../evil.gguf"},
                {"mtp_draft_filename": "a/b.gguf"},
                {"dflash_draft_filename": "..\\evil.gguf"},
                {"dspark_draft_filename": "bad:name.gguf"},
                {"chat_template": "../tpl.jinja"},
                {"dflash_draft_alternates": ["../evil.gguf"]},
                {"kv_cache_type": "bad"},
                {"load_mode": "invalid"},
                {"lazy_mode": "sometimes"},
                {"reasoning": "maybe"},
                {"ubatch": 16},
                {"ubatch": 8192},
                {"mtp_draft_n_max": 0},
                {"mtp_draft_n_max": 5},
                {"cache_ram_mib": 65536},
                {"vram_gb_override": 0},
                {"vram_gb_override": True},
                {"mtp_draft_p_min": 1.2},
                {"dflash_draft_p_min": -0.1},
                {"flash_attn": "maybe"},
                {"moe": "yes"},
                {"extra_args": ["ok", "bad\nline"]},
                {"capabilities": {"thinking": "yes"}},
                {"capabilities": {"unknown": True}},
                {"sampling": {"thinking": {"temperature": 9.9}}},
                {"sampling": {"evil_key": 1}},
                {"unsupported_dropin_key": 123},
            ]
            val_ok = True
            failed_case = None
            for payload in bad_payloads:
                resp = client.put("/models/profile/ship-model:7b", json=payload)
                if resp.status_code != 400:
                    val_ok = False
                    failed_case = (payload, resp.status_code, resp.text)
                    break
            bad_json_resp = client.put(
                "/models/profile/ship-model:7b",
                content=b"{not-json",
                headers={"Content-Type": "application/json"},
            )
            check("1d PUT /models/profile rejects unsafe filenames, bad enums, out-of-range, malformed JSON with 400",
                  val_ok and bad_json_resp.status_code == 400, f"failed_case={failed_case}")

            # 1e: PUT valid user override saves atomically to USER_DIR and preserves identity & shipped fields
            put_body = {
                "profile": {
                    "model": "hijack-attempt:99b",
                    "kv_cache_type": "q8_0",
                    "flash_attn": "on",
                    "ubatch": 512,
                    "moe_cpu_experts": 20,
                    "mtp": True,
                    "mtp_draft_n_max": 2,
                    "dflash_draft_p_min": 0.45,
                    "extra_args": "--ctx-checkpoints 4",
                    "sampling": {"thinking": {"temperature": 0.6, "top_p": 0.95}},
                }
            }
            r_put = client.put("/models/profile/ship-model:7b", json=put_body)
            d_put = r_put.json()
            saved_file = user_dir / str(d_put.get("file") or "")
            check("1e PUT /models/profile saves user override atomically and switches source to 'user'",
                  r_put.status_code == 200
                  and d_put.get("ok") is True
                  and d_put.get("model") == "ship-model:7b"
                  and d_put.get("source") == "user"
                  and saved_file.is_file()
                  and not list(user_dir.glob("*.tmp*"))
                  and not (user_dir / "hijack-attempt_99b.json").exists()
                  and (d_put.get("profile") or {}).get("kv_cache_type") == "q8_0"
                  and (d_put.get("profile") or {}).get("flash_attn") is True
                  and (d_put.get("profile") or {}).get("ubatch") == 512
                  and (d_put.get("profile") or {}).get("moe_cpu_experts") == 20
                  and (d_put.get("profile") or {}).get("num_ctx") == 16384
                  and (d_put.get("profile") or {}).get("extra_args") == ["--ctx-checkpoints", "4"],
                  f"got={d_put}")

            # 1f: GET now reports source == 'user' and merged profile
            r_after_put = client.get("/models/profile/ship-model:7b").json()
            check("1f GET /models/profile reflects user override after PUT",
                  r_after_put.get("source") == "user"
                  and (r_after_put.get("profile") or {}).get("kv_cache_type") == "q8_0")

            # 1f2: PUT with explicit nulls & partial capabilities must NOT wipe shipped fields
            r_null = client.put(
                "/models/profile/ship-model:7b",
                json={
                    "kv_cache_type": "q8_0",
                    "gpu_layers": 40,
                    "num_ctx": None,
                    "flash_attn": None,
                    "mmproj_filename": None,
                    "sampling": None,
                    "capabilities": {"thinking": None, "vision": True, "tool_call": None},
                },
            )
            d_null = r_null.json()
            prof_null = d_null.get("profile") or {}
            raw_saved = json.loads((user_dir / str(d_null.get("file") or "")).read_text(encoding="utf-8"))
            check("1f2 PUT with null fields stays sparse and preserves shipped num_ctx/moe/capabilities",
                  r_null.status_code == 200
                  and prof_null.get("gpu_layers") == 40
                  and prof_null.get("num_ctx") == 16384
                  and prof_null.get("moe_cpu_experts") == 28
                  and (prof_null.get("capabilities") or {}).get("thinking") is True
                  and (prof_null.get("capabilities") or {}).get("vision") is True
                  and (prof_null.get("capabilities") or {}).get("tool_call") is True
                  and "num_ctx" not in raw_saved
                  and "sampling" not in raw_saved,
                  f"prof={prof_null} saved={raw_saved}")

            # 1g: DELETE removes user override and reverts to shipped
            r_del = client.delete("/models/profile/ship-model:7b")
            d_del = r_del.json()
            r_del2 = client.delete("/models/profile/ship-model:7b").json()
            check("1g DELETE /models/profile removes user override and reverts source to 'shipped'",
                  r_del.status_code == 200
                  and d_del.get("ok") is True
                  and d_del.get("deleted") is True
                  and d_del.get("source") == "shipped"
                  and (d_del.get("profile") or {}).get("moe_cpu_experts") == 28
                  and r_del2.get("ok") is True
                  and r_del2.get("deleted") is False,
                  f"del1={d_del} del2={r_del2}")

            # 1h: POST /models/rescan clears caches, rescans, and updates S_models_cache in place
            cache_id_before = id(rm.S_models_cache)
            with (
                mock.patch("backend.llama_models.invalidate_caches", wraps=lm.invalidate_caches) as m_inv,
                mock.patch("backend.gguf_meta.clear_cache", wraps=gm.clear_cache) as m_gguf_clr,
                mock.patch("model_configs.models_registry.refresh", wraps=mr.refresh) as m_ref,
                mock.patch("backend.llama_models.list_available_models", return_value=["dropin-lab:4b", "ship-model:7b"]) as m_list,
            ):
                r_scan = client.post("/models/rescan")
                d_scan = r_scan.json()
                check("1h POST /models/rescan clears caches, calls list_available_models(True), updates cache in-place",
                      r_scan.status_code == 200
                      and d_scan.get("ok") is True
                      and d_scan.get("count") == 2
                      and d_scan.get("models") == ["dropin-lab:4b", "ship-model:7b"]
                      and m_inv.call_count == 1
                      and m_gguf_clr.call_count == 1
                      and m_ref.call_count == 1
                      and m_list.call_args == mock.call(True)
                      and id(rm.S_models_cache) == cache_id_before
                      and rm.S_models_cache == ["dropin-lab:4b", "ship-model:7b"]
                      and cstate.S_models_cache == ["dropin-lab:4b", "ship-model:7b"],
                      f"got={d_scan}")

            # 1i: Existing GET /models still works and enriches profiles with source
            with mock.patch("backend.llama_models.list_available_models", return_value=["ship-model:7b"]):
                r_models = client.get("/models")
                d_models = r_models.json()
                profs = {p["name"]: p for p in (d_models.get("profiles") or [])}
                check("1i GET /models remains compatible and includes profile source",
                      r_models.status_code == 200
                      and d_models.get("models") == ["ship-model:7b"]
                      and profs.get("ship-model:7b", {}).get("source") == "shipped")
        finally:
            mr._MODELS_DIR = orig_models_dir
            mr.USER_DIR = orig_user_dir
            mr.refresh()
            lm.MODELS_DIR = orig_lm_dir
            lm.invalidate_caches()
            gm.clear_cache()
            if isinstance(rm.S_models_cache, list) and isinstance(orig_models_cache, list):
                rm.S_models_cache[:] = orig_models_cache
            if isinstance(cstate.S_models_cache, list) and isinstance(orig_state_cache, list):
                cstate.S_models_cache[:] = orig_state_cache


# ── 2. Frontend static & Node VM tests ─────────────────────────────────────
def _test_frontend_sources() -> None:
    app_js = read_all_ui_js()
    idx_html = (ROOT / "index.html").read_text(encoding="utf-8")

    # 2a: DOMParser sanitizer structure
    check("2a _sanitizeChatHtml uses DOMParser allowlist instead of regex blacklist",
          "new DOMParser().parseFromString" in app_js
          and "_CHAT_DROP_TAGS" in app_js
          and "_CHAT_ALLOW_TAGS" in app_js
          and "_isSafeSanitizedUrl" in app_js
          and "_sanitizeStyleAttr" in app_js
          and ".replace(/<script[\\s\\S]*?(?:<\\/script>|$)/gi" not in app_js)

    # 2b: No inline onclick model-ID interpolations at the 3 migrated sites
    check("2b inline onclick model-ID interpolations removed from applyEvalSuggestion/unloadModel/toggleAvailModel",
          'onclick="applyEvalSuggestion' not in app_js
          and 'onclick="unloadModel' not in app_js
          and 'onclick="toggleAvailModel' not in app_js
          and "btn.onclick = null" not in app_js
          and "data-model=\"" in app_js
          and "btn.disabled = true" in app_js
          and "list._availDelegated" in app_js
          and "listEl._unloadDelegated" in app_js
          and "c._evalDelegated" in app_js)

    # 2c: rAF coalescing + final flush + _liveMdTrailT cleanup
    check("2c rAF coalescing + _tgPanelFlush cancellation + _liveMdTrailT cleanup present",
          "_tgPanelRafId" in app_js
          and "_tgSchedulePanelStream" in app_js
          and "_tgCancelPanelRaf" in app_js
          and "_tgPanelFlush()" in app_js
          and "if (_liveMdTrailT) { clearTimeout(_liveMdTrailT); _liveMdTrailT = null; }" in app_js
          and "if (force && _liveMdTrailT) { clearTimeout(_liveMdTrailT); _liveMdTrailT = null; }" in app_js)

    # 2d: Advanced / Engine Settings drawer in index.html + app.js
    adv_tag_pos = idx_html.find('<details id="adv-engine-settings-card"')
    adv_tag_end = idx_html.find('>', adv_tag_pos) if adv_tag_pos >= 0 else -1
    adv_open_attr = (" open" in idx_html[adv_tag_pos:adv_tag_end].lower()) if adv_tag_end > adv_tag_pos else True
    check("2d #adv-engine-settings-card collapsed by default + wired to /settings/schema",
          adv_tag_pos >= 0
          and not adv_open_attr
          and 'id="adv-settings-search"' in idx_html
          and 'id="adv-settings-group"' in idx_html
          and 'id="adv-settings-modified-only"' in idx_html
          and "fetch('/settings/schema')" in app_js
          and "loadAdvancedSettingsSchema" in app_js
          and "postSettings(patch)" in app_js
          and "'adv-engine-settings-card', 'Advanced / Engine Settings'" in app_js)

    # 2e: Model Profiles modal + #cfg-defaults-panel preserved
    check("2e #model-profiles-modal wired to /models/profile/ and /models/rescan; #cfg-defaults-panel preserved",
          'id="model-profiles-modal"' in idx_html
          and 'id="cfg-defaults-panel"' in idx_html
          and 'id="mp-rescan-btn"' in idx_html
          and 'id="mp-save-btn"' in idx_html
          and 'id="mp-reset-btn"' in idx_html
          and "openModelProfilesModal" in idx_html
          and "'/models/profile/' + encodeURIComponent(" in app_js
          and "fetch('/models/rescan'" in app_js
          and "loadModelDefaults" in app_js
          and "saveModelDefaults" in app_js)

    # 2f: Protected AGENTS.md invariants
    check("2f protected AGENTS.md invariants ('zu groß', German stems, INTENT_*_KWS) preserved",
          "zu groß" in app_js
          and "vorlesen" in app_js
          and "lautstaerke" in app_js
          and "INTENT_TOOL_KWS" in app_js
          and "INTENT_MEMORY_KWS" in app_js
          and "INTENT_EVOLVE_KWS" in app_js)

    # 2g: Node syntax check + VM behavioral checks
    node = shutil.which("node")
    if not node:
        h.skip("2g node syntax & VM behavioral checks", "node binary not on PATH")
        return

    _syn_bad = []
    for _jf in all_static_js_files():
        _r = subprocess.run([node, "--check", str(_jf)], capture_output=True, text=True)
        if _r.returncode != 0:
            _syn_bad.append(f"{_jf.name}: {_r.stderr[:200]}")
    check("2g node --check static JS files pass", not _syn_bad, "; ".join(_syn_bad))

    # Extract functions from static/app.js for behavioral Node checks
    def _slice_block(start_sig: str, end_sig: str) -> str:
        s = app_js.find(start_sig)
        e = app_js.find(end_sig, s)
        assert s >= 0 and e > s, f"missing slice {start_sig!r} .. {end_sig!r}"
        return app_js[s:e]

    def _extract_fn(sig: str) -> str:
        idx = app_js.find(sig)
        assert idx >= 0, f"missing {sig}"
        end = app_js.find("\n}", idx)
        assert end >= 0, f"unterminated {sig}"
        return app_js[idx:end + 2]

    sanitize_bundle = "\n\n".join([
        _extract_fn("function _escHtml(s)"),
        _slice_block("var _CHAT_DROP_TAGS = new Set([", "// END_CHAT_SANITIZER"),
    ])

    sanitize_js = sanitize_bundle + r"""
// Fallback when DOMParser is undefined: escapes raw HTML safely
const fallbackOut = _sanitizeChatHtml('<script>alert(1)</script><img src=x onerror=alert(2)>');

// Minimal DOM + DOMParser shim to exercise the allowlist node walker directly
function mkNode(type, tag, text) {
  return {
    nodeType: type,
    tagName: tag ? tag.toUpperCase() : undefined,
    nodeValue: text || '',
    attributes: [],
    childNodes: [],
    setAttribute(k, v) {
      const low = String(k).toLowerCase();
      const idx = this.attributes.findIndex(a => a.name === low);
      const item = { name: low, value: String(v) };
      if (idx >= 0) this.attributes[idx] = item;
      else this.attributes.push(item);
    },
    getAttribute(k) {
      const f = this.attributes.find(a => a.name === String(k).toLowerCase());
      return f ? f.value : null;
    },
    appendChild(ch) {
      if (ch.nodeType === 11) {
        ch.childNodes.forEach(gc => this.childNodes.push(gc));
      } else {
        this.childNodes.push(ch);
      }
      return ch;
    },
    get innerHTML() {
      return this.childNodes.map(ch => {
        if (ch.nodeType === 3) return _escHtml(ch.nodeValue);
        if (ch.nodeType === 1) {
          const t = ch.tagName.toLowerCase();
          const attrs = ch.attributes.map(a => ` ${a.name}="${_escHtml(a.value)}"`).join('');
          if (t === 'br' || t === 'hr' || t === 'img') return `<${t}${attrs}>`;
          return `<${t}${attrs}>${ch.innerHTML}</${t}>`;
        }
        return '';
      }).join('');
    }
  };
}
global.document = {
  createElement: (t) => mkNode(1, t),
  createTextNode: (txt) => mkNode(3, null, txt),
  createDocumentFragment: () => mkNode(11, null),
};
global.DOMParser = function() {
  return {
    parseFromString() {
      const body = mkNode(1, 'body');
      const scr = mkNode(1, 'script');
      scr.appendChild(mkNode(3, null, 'alert(1)'));
      const svg = mkNode(1, 'svg');
      svg.setAttribute('onload', 'alert(2)');
      const imgBad = mkNode(1, 'img');
      imgBad.setAttribute('src', 'javascript:alert(3)');
      imgBad.setAttribute('onerror', 'alert(4)');
      const imgGood = mkNode(1, 'img');
      imgGood.setAttribute('src', 'data:image/png;base64,AAAA');
      imgGood.setAttribute('alt', 'ok');
      const lnkBad = mkNode(1, 'a');
      lnkBad.setAttribute('href', 'java\tscript:alert(5)');
      lnkBad.appendChild(mkNode(3, null, 'badlink'));
      const lnkGood = mkNode(1, 'a');
      lnkGood.setAttribute('href', 'https://example.com');
      lnkGood.setAttribute('target', '_blank');
      lnkGood.appendChild(mkNode(3, null, 'goodlink'));
      const divStyled = mkNode(1, 'div');
      divStyled.setAttribute('class', 'tool-call-row');
      divStyled.setAttribute('data-tid', 't1');
      divStyled.setAttribute('style', 'color:red; background:url(javascript:alert(6))');
      divStyled.appendChild(mkNode(3, null, 'hi'));
      [scr, svg, imgBad, imgGood, lnkBad, lnkGood, divStyled].forEach(n => body.appendChild(n));
      return { body };
    }
  };
};
const domOut = _sanitizeChatHtml('dummy');
console.log(JSON.stringify({ fallbackOut, domOut }));
"""
    p_san = subprocess.run([node, "-"], input=sanitize_js, capture_output=True, text=True, encoding="utf-8", timeout=15)
    san_out = json.loads(p_san.stdout.strip()) if p_san.returncode == 0 and p_san.stdout.strip() else {}
    dom_out = san_out.get("domOut", "")
    check("2h Node VM _sanitizeChatHtml strips XSS vectors and keeps safe HiveMind markup",
          p_san.returncode == 0
          and "<script" not in san_out.get("fallbackOut", "")
          and "<script" not in dom_out
          and "<svg" not in dom_out
          and "onerror" not in dom_out
          and "onload" not in dom_out
          and "javascript:" not in dom_out
          and "url(" not in dom_out
          and 'src="data:image/png;base64,AAAA"' in dom_out
          and 'href="https://example.com"' in dom_out
          and 'rel="noopener noreferrer"' in dom_out
          and 'data-tid="t1"' in dom_out
          and 'style="color:red"' in dom_out,
          f"rc={p_san.returncode} stderr={p_san.stderr[:200]} out={san_out}")

    # 2i: Node VM test for rAF coalescing + final flush + _liveMdTrailT cleanup
    _tg_end = "// END_TOOLGEN_STREAM" if "// END_TOOLGEN_STREAM" in app_js else "function _flushTokenQueueSync()"
    stream_bundle = "\n\n".join([
        _extract_fn("function _escHtml(s)"),
        "var esc = _escHtml;",
        _slice_block("var _liveMdMaxLen", "var _tokenQueue = [];"),
        _slice_block("var _tgCards = {};", _tg_end),
    ])
    stream_js = stream_bundle + r"""
let scheduledRafs = [];
let cancelledRafs = [];
let nextRaf = 1;
let clearedTimeouts = [];
let nextTimer = 100;
global.requestAnimationFrame = (fn) => { const id = nextRaf++; scheduledRafs.push({ id, fn }); return id; };
global.cancelAnimationFrame = (id) => { cancelledRafs.push(id); };
global.setTimeout = () => nextTimer++;
global.clearTimeout = (id) => { clearedTimeouts.push(id); };
global.S = { curAgent: { tid: 't1' } };
global.Node = { TEXT_NODE: 3 };
global._mdRender = (s) => String(s || '');
global._TOOL_ICONS = { write_file: 'W' };
global.scrollBtmIfNearBottom = () => {};
global._cpEnsureFollowListener = () => {};
global._cpFiles = {};
global._cpActive = null;
let addCalls = [];
global._cpAddOrUpdateFile = (path, txt, op, mode) => { addCalls.push({ path, txt, op, mode }); };
function mkMockEl(tag) {
  const el = {
    tagName: tag, className: '', dataset: {}, style: {}, children: [], isConnected: true,
    _html: '', _tc: '',
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    set textContent(v) { this._tc = v; },
    get textContent() { return this._tc; },
    appendChild(ch) { ch.parentNode = this; this.children.push(ch); return ch; },
    removeChild(ch) { this.children = this.children.filter(x => x !== ch); },
    addEventListener() {},
    querySelector(sel) {
      if (!this._sub) this._sub = { '.tc-name': mkMockEl('span'), '.tc-icon': mkMockEl('span'), '.tg-path': mkMockEl('span'), '.tg-chars': mkMockEl('span') };
      return this._sub[sel] || mkMockEl('span');
    },
  };
  return el;
}
const bodyEl = mkMockEl('div');
global.document = {
  createElement: mkMockEl,
  getElementById: (id) => (id === 'ab-t1' ? bodyEl : null),
  body: { classList: { contains: () => false } },
};
// Stream 10 chunks before any rAF runs -> must coalesce to 1 rAF and 0 sync _cpAddOrUpdateFile calls
const chunks = ['{"path":"src/', 'app.py","content":"', 'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'];
chunks.forEach((c) => _toolGenStream({ name: 'write_file', index: 0, content: c }));
const syncCallsBeforeRaf = addCalls.length;
const rafCountAfter10Chunks = scheduledRafs.length;
// Run the single scheduled rAF
scheduledRafs[0].fn();
const callsAfterRaf = addCalls.length;
const txtAfterRaf = addCalls[callsAfterRaf - 1] && addCalls[callsAfterRaf - 1].txt;
// Now push trailing chunk without running rAF, then call _toolGenDone()
_toolGenStream({ name: 'write_file', index: 0, content: 'TAIL"}' });
const pendingRafId = _tgPanelRafId;
_toolGenDone();
const txtAfterDone = addCalls[addCalls.length - 1] && addCalls[addCalls.length - 1].txt;
const rafCancelledOnDone = cancelledRafs.includes(pendingRafId);

// Verify _liveMdTrailT cleanup in _liveMdRender(..., true) and _liveMdReset
const mdBody = mkMockEl('div');
mdBody._liveRaw = 'hello';
_liveMdTrailT = 777;
_liveMdRender(mdBody, true);
const trailClearedOnForce = (_liveMdTrailT === null && clearedTimeouts.includes(777));
_liveMdTrailT = 888;
_liveMdReset(mdBody);
const trailClearedOnReset = (_liveMdTrailT === null && clearedTimeouts.includes(888));

console.log(JSON.stringify({
  syncCallsBeforeRaf,
  rafCountAfter10Chunks,
  callsAfterRaf,
  txtAfterRaf,
  txtAfterDone,
  rafCancelledOnDone,
  trailClearedOnForce,
  trailClearedOnReset,
}));
"""
    p_str = subprocess.run([node, "-"], input=stream_js, capture_output=True, text=True, encoding="utf-8", timeout=15)
    st_out = json.loads(p_str.stdout.strip()) if p_str.returncode == 0 and p_str.stdout.strip() else {}
    check("2i Node VM rAF coalescing batches chunks, flushes tail on _toolGenDone, clears _liveMdTrailT",
          p_str.returncode == 0
          and st_out.get("syncCallsBeforeRaf") == 0
          and st_out.get("rafCountAfter10Chunks") == 1
          and st_out.get("callsAfterRaf") == 1
          and st_out.get("txtAfterRaf") == "abcdefgh"
          and st_out.get("txtAfterDone") == "abcdefghTAIL"
          and st_out.get("rafCancelledOnDone") is True
          and st_out.get("trailClearedOnForce") is True
          and st_out.get("trailClearedOnReset") is True,
          f"rc={p_str.returncode} stderr={p_str.stderr[:200]} out={st_out}")


    check("2j Model Profiles modal exposes gpu_layers and sends sparse payload; schema choices & update channel match runtime",
          'id="mp-gpu-layers"' in idx_html
          and "mp-gpu-layers" in app_js
          and "putIfSet('gpu_layers'" in app_js)

    import deploy.fetch_llamacpp as fl
    import settings_defaults as sd
    meta = sd.SETTINGS_UI_META
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td_ch:
        sf = Path(td_ch) / "settings.json"
        ch_missing = fl._configured_update_channel(sf)
        sf.write_text(json.dumps({"llama_update_channel": "pinned"}), encoding="utf-8")
        ch_pinned = fl._configured_update_channel(sf)
        sf.write_text(json.dumps({"llama_update_channel": "latest"}), encoding="utf-8")
        ch_latest = fl._configured_update_channel(sf)
    check("2k SETTINGS_UI_META choices match UI/gateway and fetch_llamacpp honors llama_update_channel",
          meta.get("telegram_approval_mode", {}).get("choices") == ["deny", "ask", "off"]
          and "auto" in (meta.get("mode", {}).get("choices") or [])
          and "heuristic" in (meta.get("routing_planner", {}).get("choices") or [])
          and ch_missing == "latest"
          and ch_pinned == "pinned"
          and ch_latest == "latest",
          f"meta_tg={meta.get('telegram_approval_mode')} ch={(ch_missing, ch_pinned, ch_latest)}")


if __name__ == "__main__":
    _test_models_profile_api()
    _test_frontend_sources()
    sys.exit(h.finish("ui_advanced_and_profiles"))

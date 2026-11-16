"""Regression tests for coherent Image Processing across Direct, Code Duo, Pipeline, Backend, and UI."""

import asyncio
import contextlib
import re
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

passed = 0
failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name} ({detail})")


def test_attach_and_error_sentinels() -> None:
    from core.duo_helpers import (
        attach_images_to_last_user,
        build_image_desc_block,
        is_vision_error_text,
        resolve_image_plan,
    )

    data_url = "data:image/png;base64,iVBORw0KGgo="
    raw_b64 = "AAAA/9j/4AAQ"
    msgs = [{"role": "user", "content": "look at this"}]
    attach_images_to_last_user(msgs, [data_url, raw_b64])
    parts = msgs[0]["content"]
    check("attach preserves data URL", isinstance(parts, list) and parts[0]["image_url"]["url"] == data_url, str(parts))
    check(
        "attach wraps raw base64",
        isinstance(parts, list) and parts[1]["image_url"]["url"] == f"data:image/jpeg;base64,{raw_b64}",
        str(parts),
    )
    check(
        "attach keeps user text part",
        isinstance(parts, list) and parts[2]["text"] == "look at this",
        str(parts),
    )

    for marker in (
        "[Vision fallback: no vision model configured.]",
        "[Vision preprocessing error: timeout]",
        "[Vision-Preprocessing-Error: connection refused]",
    ):
        check(f"sentinel detected ({marker[:26]})", is_vision_error_text(marker), marker)
        check(f"desc block suppresses sentinel ({marker[:20]})", build_image_desc_block(marker) == "", marker)

    ctx_err = type("Ctx", (), {
        "effective_images": [],
        "images": [],
        "image_description": "[Vision fallback: failed]",
    })()
    plan_err = resolve_image_plan("p", "c", {"duo_image_mode": "preprocess"}, ctx_err)
    check(
        "resolve_image_plan ignores sentinel description",
        plan_err.get("planner") == "none" and plan_err.get("coder") == "none",
        str(plan_err),
    )


def test_duo_role_and_mode_coherence() -> None:
    from core.duo_helpers import (
        duo_steer_images_policy,
        effective_duo_image_mode,
        resolve_duo_role_models,
    )

    # Planner reuses Coder when duo_planner_use_coder_ctx=True and duo_planner_model is unset.
    _agents_base = {
        "duo_coder": {"model": "qwen3.5:9b-coder"},
        "duo_planner": {"model": "lfm3:1.7b-qad"},
    }
    p, c = resolve_duo_role_models({
        "agents": _agents_base,
        "duo_planner_use_coder_ctx": True,
    })
    check("planner reuses coder model when shared ctx enabled", p == "qwen3.5:9b-coder" and c == "qwen3.5:9b-coder", f"{p}, {c}")

    p2, c2 = resolve_duo_role_models({
        "agents": _agents_base,
        "duo_planner_use_coder_ctx": False,
    })
    check("planner keeps own model when shared ctx disabled", p2 == "lfm3:1.7b-qad" and c2 == "qwen3.5:9b-coder", f"{p2}, {c2}")

    # Derived mode: None + enabled describer -> "preprocess"; off -> "direct"
    check(
        "effective mode derived preprocess when vision_cfg active",
        effective_duo_image_mode({"duo_image_mode": None}, {"enabled": True, "model": "gemma-4:e4b-it-qat"}) == "preprocess",
    )
    check(
        "effective mode derived direct when vision_cfg off",
        effective_duo_image_mode({"duo_image_mode": None}, {"enabled": False, "model": "gemma-4:e4b-it-qat"}) == "direct",
    )

    # Steer policy must follow derived preprocess mode AND duo_coder_images_off.
    with mock.patch("backend.llama_manager_utils.resolve_mmproj_strict", return_value="/tmp/mmproj.gguf"):
        allow_pre, reason_pre = duo_steer_images_policy(
            "gemma-4:e4b-it-qat", {"duo_image_mode": None}, {"enabled": True, "model": "gemma-4:e4b-it-qat"}
        )
        check("steer blocks images in derived preprocess mode", (not allow_pre) and "preprocess" in reason_pre.lower(), f"{allow_pre}, {reason_pre}")

        allow_off, reason_off = duo_steer_images_policy(
            "gemma-4:e4b-it-qat", {"duo_image_mode": "direct", "duo_coder_images_off": True}, {"enabled": False}
        )
        check("steer blocks images when duo_coder_images_off=True", (not allow_off) and "off" in reason_off.lower(), f"{allow_off}, {reason_off}")

        allow_ok, reason_ok = duo_steer_images_policy(
            "gemma-4:e4b-it-qat", {"duo_image_mode": "direct", "duo_coder_images_off": False}, {"enabled": False}
        )
        check("steer allows images on multimodal direct coder", allow_ok and reason_ok == "", f"{allow_ok}, {reason_ok}")

    with mock.patch("backend.llama_manager_utils.resolve_mmproj_strict", return_value=None):
        allow_nomm, reason_nomm = duo_steer_images_policy(
            "lfm3:1.7b-qad", {"duo_image_mode": "direct", "duo_coder_images_off": False}, {"enabled": False}
        )
        check("steer blocks images when coder lacks mmproj", (not allow_nomm) and "mmproj" in reason_nomm.lower(), f"{allow_nomm}, {reason_nomm}")


def test_chat_vision_pre_mode_isolation() -> None:
    from core import chat_vision_pre as cvp

    class _FakeServer:
        S_models_cache = [
            {"id": "lfm3:1.7b-qad", "capabilities": []},
            {"id": "gemma-4:e4b-it-qat", "capabilities": ["vision"]},
        ]
        _registry = {}
        _vision_cfg = {"enabled": True, "model": "gemma-4:e4b-it-qat", "mode": "sequential"}

    class _FakeLoader:
        @staticmethod
        def _bk_evict(_m):
            return False

    class _FakeManager:
        _slots = {}

        async def evict(self, _m):
            return None

    async def _run_case(mode: str, run_settings: dict, direct_capable: bool, prepro_return: str):
        calls = {"prepro": 0, "evict": []}
        fake_server = _FakeServer()
        events = []

        async def _emit(ev):
            events.append(ev)
            return ev

        async def _fake_preprocess(images, user_input):
            calls["prepro"] += 1
            return prepro_return

        async def _fake_evict(self, mdl):
            calls["evict"].append(mdl)

        _FakeManager.evict = _fake_evict  # type: ignore[method-assign]

        def _fake_profile(mdl):
            if mdl == "gemma-4:e4b-it-qat":
                return {"capabilities": {"vision": True}}
            return {"capabilities": {"vision": direct_capable}}

        class _FakeAgent:
            def __init__(self, model: str):
                self.model = model

        fake_pipeline = type("Pipeline", (), {
            "agents": {"direct": _FakeAgent(str(run_settings.get("direct_model", "")))}
        })()

        ps = cvp._ChatVisionPreState(
            images=["ZmFrZQ=="],
            user_input="check image",
            mode=mode,
            _ws_str=str(ROOT),
            _vram_budget=8.0,
            _run_settings=dict(run_settings),
            effective_images=["ZmFrZQ=="],
            carry={"emit": _emit},
        )

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.dict(sys.modules, {
            "server": fake_server,
            "vram": mock.MagicMock(loader=_FakeLoader),
            "vram.loader": _FakeLoader,
            "backend.llama_server_manager": mock.MagicMock(manager=_FakeManager()),
            "backend.llama_vram_table": mock.MagicMock(vram_of=lambda _m: 4.0),
        }))
        stack.enter_context(mock.patch.object(cvp, "_preprocess_images_to_text", _fake_preprocess))
        stack.enter_context(mock.patch.object(cvp, "_model_profile", _fake_profile))
        stack.enter_context(mock.patch.object(cvp, "_pick_direct_model", return_value="gemma-4:e4b-it-qat"))
        stack.enter_context(mock.patch.object(cvp, "should_evict_vision_after_prepro", return_value=True))
        stack.enter_context(mock.patch.object(cvp._state, "pipeline", fake_pipeline))
        stack.enter_context(mock.patch.object(cvp, "settings", dict(run_settings)))
        stack.enter_context(mock.patch("backend.llama_manager_utils.resolve_mmproj_strict", return_value="/tmp/mmproj.gguf"))
        with stack:
            async for _ev in cvp._chat_vision_pre(ps):
                pass
        return ps, calls, events

    # 1. Code Duo + Direct ignores enabled describer even when Direct model is text-only.
    ps1, c1, _ = asyncio.run(_run_case(
        "code_duo",
        {
            "duo_image_mode": "direct",
            "direct_model": "lfm3:1.7b-qad",
            "duo_coder_model": "gemma-4:e4b-it-qat",
            "duo_image_to_coder": True,
        },
        direct_capable=False,
        prepro_return="should not run",
    ))
    check("code_duo direct skips describer even if direct model is text-only", c1["prepro"] == 0, str(c1))
    check("code_duo direct preserves raw ps.images without image_description", len(ps1.images) == 1 and ps1.image_description == "", f"{ps1.images}, {ps1.image_description!r}")

    # 2. Code Duo + Preprocess runs describer even when Direct model IS multimodal, and evicts describer.
    ps2, c2, _ = asyncio.run(_run_case(
        "code_duo",
        {
            "duo_image_mode": "preprocess",
            "direct_model": "gemma-4:e4b-it-qat",
            "duo_coder_model": "qwen3.5:4b-mtp",
        },
        direct_capable=True,
        prepro_return="A screenshot showing a stack trace.",
    ))
    check("code_duo preprocess runs describer even if direct is multimodal", c2["prepro"] == 1, str(c2))
    check("code_duo preprocess populates image_description", "stack trace" in ps2.image_description, ps2.image_description)
    check("code_duo preprocess clears effective_images", ps2.effective_images == [], str(ps2.effective_images))
    check("code_duo preprocess evicts describer afterwards", c2["evict"] == ["gemma-4:e4b-it-qat"], str(c2["evict"]))

    # 3. Pipeline skips chat_vision_pre preprocessing (it runs its own Vision-Agent).
    ps3, c3, _ = asyncio.run(_run_case(
        "pipeline",
        {"vision_agent_enabled": True, "pipeline_vision_direct": False, "direct_model": "lfm3:1.7b-qad"},
        direct_capable=False,
        prepro_return="dup",
    ))
    check("pipeline skips duplicate chat_vision_pre", c3["prepro"] == 0, str(c3))

    # 4. Vision error sentinel from _preprocess_images_to_text never leaks into ps.image_description.
    ps4, c4, _ = asyncio.run(_run_case(
        "direct",
        {"direct_model": "lfm3:1.7b-qad", "vision_agent_enabled": True},
        direct_capable=False,
        prepro_return="[Vision preprocessing error: timeout after 30s]",
    ))
    check("direct preprocess error sentinel cleared from image_description", ps4.image_description == "", repr(ps4.image_description))

    # 5. Direct explicit "Describe first" honors vision_agent_enabled even on a multimodal model.
    ps5, c5, _ = asyncio.run(_run_case(
        "direct",
        {"direct_model": "gemma-4:e4b-it-qat", "vision_agent_enabled": True},
        direct_capable=True,
        prepro_return="Concise diagram summary.",
    ))
    check("direct explicit Describe first runs on multimodal model", c5["prepro"] == 1 and ps5.effective_images == [], f"{c5}, {ps5.effective_images}")


def test_mmproj_always_and_automap() -> None:
    from backend import llama_manager_utils as lmu
    from routing import model_automap as ma

    with mock.patch("settings.load_settings", return_value={"vision_mmproj_always": True}):
        check("vision_mmproj_always covers llava", lmu._needs_mmproj("llava:7b", vision=False))
        check("vision_mmproj_always covers qwen3.6", lmu._needs_mmproj("qwen3.6:27b", vision=False))
        check("vision_mmproj_always covers gemma-4", lmu._needs_mmproj("gemma-4:e4b-it-qat", vision=False))

    with mock.patch("settings.load_settings", return_value={"vision_mmproj_always": False}):
        check("text-only skips projector for gemma-4 when always=False", not lmu._needs_mmproj("gemma-4:e4b-it-qat", vision=False))
        check("image request loads projector for gemma-4 when always=False", lmu._needs_mmproj("gemma-4:e4b-it-qat", vision=True))

    orig_data = ma._automap_data
    try:
        ma._automap_data = {"assignments": {"direct": "lfm3:1.7b-qad", "analyst": "lfm3:1.7b-qad"}}
        res = ma.get_automap(
            "inspect this screenshot",
            available=["lfm3:1.7b-qad", "gemma-4:e4b-it-qat"],
            has_images=True,
        )
        check("automap overrides text-only persisted direct model for images", res["assignments"]["direct"] == "gemma-4:e4b-it-qat", str(res["assignments"]))
        check("automap overrides text-only persisted analyst model for images", res["assignments"]["analyst"] == "gemma-4:e4b-it-qat", str(res["assignments"]))
    finally:
        ma._automap_data = orig_data


def test_backend_source_invariants() -> None:
    direct_src = (ROOT / "core" / "direct_runner.py").read_text(encoding="utf-8")
    check("direct_runner does not read duo_coder_always_vision", "duo_coder_always_vision" not in direct_src)
    check("direct_runner does not read duo_coder_images_off", "duo_coder_images_off" not in direct_src)

    preloop_src = (ROOT / "core" / "duo_coder_preloop.py").read_text(encoding="utf-8")
    check(
        "duo_coder_preloop checks _plan_port_vision for raw coder",
        "(not _need_raw_vision or _plan_port_vision)" in preloop_src
        and "(_need_raw_vision and not _plan_port_vision)" in preloop_src,
    )
    check(
        "duo_coder_preloop reloads coder after planner eviction",
        "if _cached_coder_port is not None:" in preloop_src,
    )

    atl_src = (ROOT / "core" / "agentic_tool_loop.py").read_text(encoding="utf-8")
    check(
        "agentic_tool_loop _retracted checks len(dtool_msgs) != _before_len",
        "len(dtool_msgs) != _before_len" in atl_src,
    )

    duo_src = (ROOT / "core" / "duo_runner.py").read_text(encoding="utf-8")
    check(
        "duo_runner pre-prompt gate allows lazy mmproj upgrade via resolve_mmproj_strict",
        "resolve_mmproj_strict" in duo_src,
    )

    pipe_src = (ROOT / "core" / "pipeline_runner.py").read_text(encoding="utf-8")
    check(
        "pipeline_runner ignores stale pipeline_vision_roles when Describe first is active",
        'dict(ctx.settings.get("pipeline_vision_roles") or {}) if _pipeline_vision_direct else {}' in pipe_src,
    )
    check(
        "pipeline_runner shares _vision_agent_out with image_desc_full_pipeline",
        "_pipe_desc = (_vision_agent_out or ctx.image_description" in pipe_src,
    )

    chat_run_src = (ROOT / "core" / "chat_run.py").read_text(encoding="utf-8")
    check(
        "chat_run runs _run_duo_preprocess_branch on dynamic auto->code_duo handoff",
        "_run_duo_preprocess_branch" in chat_run_src,
    )


def test_ui_coherence_invariants() -> None:
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    # Single coder-always-vision toggle inside #duo-image-sec (legacy id removed).
    check("no duplicate legacy coder-always-vision-toggle id", 'id="coder-always-vision-toggle"' not in html)
    check("duo coder always-vision toggle exists", 'id="coder-always-vision-toggle-duo"' in html)

    # Auto mode exposes both Direct and Agentic Planner/Coder controls without stomping Direct visionEnabled.
    check("auto mode shows duo-image-sec alongside direct-image-sec", "var _showDuo = (_isDuo || _isAuto);" in app_js)
    m_sdim = re.search(r"function setDuoImageMode\([^)]*\)\s*\{([\s\S]*?)\n\}", app_js)
    check("setDuoImageMode does not overwrite visionEnabled", bool(m_sdim and "vision/config" not in m_sdim.group(1)))

    # All three modes use the same 2-button Direct / Describe-first switcher (no legacy duo radios).
    for btn_id in ("im-direct", "im-preprocess", "duo-im-direct", "duo-im-preprocess", "pipe-im-direct", "pipe-im-preprocess"):
        check(f"unified switcher button present ({btn_id})", f'id="{btn_id}"' in html, btn_id)
    duo_chunk = html[html.find('id="duo-image-sec"'):html.find('id="im-preprocess-sec"')]
    check("no legacy radio inputs inside duo-image-sec", 'type="radio"' not in duo_chunk)
    pipe_chunk = html[html.find('id="im-pipeline-sec"'):html.find('<!-- Complexity Force Override -->')]
    check("pipeline renders live role status spans like duo", 'id="pipe-img-analyst-status"' in pipe_chunk and 'id="pipe-image-note"' in pipe_chunk)

    # Layout ordering: #duo-image-sec -> #im-preprocess-sec -> #im-pipeline-sec -> #full-imgdesc-row
    pos_flow = html.find('id="vision-flow-card"')
    pos_direct = html.find('id="direct-image-sec"')
    pos_duo = html.find('id="duo-image-sec"')
    pos_prepro = html.find('id="im-preprocess-sec"')
    pos_pipe = html.find('id="im-pipeline-sec"')
    pos_full = html.find('id="full-imgdesc-row"')
    check(
        "image sections remain ordered inside vision-accordion",
        -1 not in (pos_flow, pos_direct, pos_duo, pos_prepro, pos_pipe, pos_full)
        and pos_flow < pos_direct < pos_duo < pos_prepro < pos_pipe < pos_full,
        f"{pos_flow}, {pos_direct}, {pos_duo}, {pos_prepro}, {pos_pipe}, {pos_full}",
    )

    # No stale <!-- Image Processing --> orphan comment above Iterations.
    check("no stale Image Processing marker before Iterations", not re.search(r"<!--\s*Image Processing\s*-->\s*\n\s*<!--\s*Iterations", html))

    # Warning text under #duo-image-prepro-warn points to visible Describer selector.
    m_warn = re.search(r'id="duo-image-prepro-warn"[^>]*>([^<]+)<', html)
    warn_txt = m_warn.group(1) if m_warn else ""
    check("duo-image-prepro-warn does not tell users to click a hidden toggle", "Describer" in warn_txt and "Enable" not in warn_txt, warn_txt)

    # togglePlannerUseCoderCtx refreshes Duo Image UI badge.
    m_tp = re.search(r"function togglePlannerUseCoderCtx\([^)]*\)\s*\{([\s\S]*?)\n\}", app_js)
    check("togglePlannerUseCoderCtx calls updateDuoImageUI", bool(m_tp and "updateDuoImageUI" in m_tp.group(1)))

    # Follow-up queue + steer carry images rather than dropping them.
    check(
        "steerFollowUp forwards images payload",
        "body: JSON.stringify({ text: item.text || '', images: imgsPayload })" in app_js,
    )


def main() -> int:
    print("=== test_image_coherence ===")
    test_attach_and_error_sentinels()
    test_duo_role_and_mode_coherence()
    test_chat_vision_pre_mode_isolation()
    test_mmproj_always_and_automap()
    test_backend_source_invariants()
    test_ui_coherence_invariants()
    print(f"\nSUMMARY image_coherence: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

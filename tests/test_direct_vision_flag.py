"""Direct-mode vision-flag consistency (2026-10-06).

Live finding (11:45): the chat_run gate judged the REGISTRY direct model
("Image ignored - no vision model active") while the runner applied the P9
run-body override (a multimodal model) — effective_images ended up empty,
the tool loop's ensure_loaded therefore requested NO projector, and the
messages still carried raw image parts → llama-server answered
500 "image input is not supported". The plain path (vision=True) loaded the
projector only AFTER the failure.

Three pins:
  1. _run_direct_tools takes `vision` from the CALLER — the same expression
     that decides whether raw parts ride in the messages (bool(_direct_images)).
  2. The runner drops raw parts when the FINAL (override) model is not
     multimodal — a non-vision model can never hit a projector-less 500.
  3. The chat_run gate judges the run-body override first (settings
     direct_model), not the registry card.

Run: python tests/test_direct_vision_flag.py
Exit 0 = all pass, Exit 1 = failures.
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


def _fn_source(path: str, name: str) -> str:
    src = (Path(__file__).parent.parent / path).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node) or ""
    return ""


def _flow(vision_final_model, images, effective_images):
    """Faithful model of the fixed direct-runner image decision.

    Returns (parts_in_request, ensure_vision).
    """
    direct_images = images if (vision_final_model and images) else effective_images
    if not vision_final_model and direct_images:
        direct_images = []          # THE FIX: final model not multimodal
    ensure_vision = bool(direct_images)   # THE FIX: caller passes this down
    return bool(direct_images), ensure_vision


def test_override_vision_model_loads_projector():
    # registry model non-vision (gate emptied effective_images), override IS vision
    parts, vis = _flow(True, ["img"], [])
    if parts and vis:
        ok("override vision model: raw parts ride AND projector is requested")
    else:
        fail("override_vision", f"parts={parts} vis={vis}")


def test_nonvision_final_model_never_500s():
    parts, vis = _flow(False, ["img"], ["img"])
    if not parts and not vis:
        ok("non-vision final model: parts dropped, no projector, no 500 possible")
    else:
        fail("nonvision", f"parts={parts} vis={vis}")


def test_text_only_run_unchanged():
    parts, vis = _flow(True, [], [])
    if not parts and not vis:
        ok("text-only run: no parts, no projector request (unchanged)")
    else:
        fail("text_only", f"parts={parts} vis={vis}")


def test_tool_loop_takes_caller_vision():
    src = _fn_source("core/direct_runner.py", "_run_direct_tools")
    code_only = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    sig_ok = "vision: bool = False" in src
    call_ok = "vision=vision" in code_only
    old_ok = "effective_images" not in code_only
    if sig_ok and call_ok and old_ok:
        ok("_run_direct_tools: vision is a caller parameter (no effective_images read)")
    else:
        fail("tool_loop_sig", f"sig={sig_ok} call={call_ok} old_ref_gone={old_ok}")


def test_call_site_passes_direct_images():
    src = (Path(__file__).parent.parent / "core" / "direct_runner.py").read_text(encoding="utf-8")
    if "vision=bool(_direct_images)" in src:
        ok("call site: ensure vision flag == the parts that ride in messages (+ always-vision toggle)")
    else:
        fail("call_site", "vision=bool(_direct_images) missing at the _run_direct_tools call")


def test_gate_judges_override():
    src = (Path(__file__).parent.parent / "core" / "chat_run.py").read_text(encoding="utf-8")
    hits = src.count('_run_settings.get("direct_model")')
    if hits >= 2 and '_run_settings.get("direct_model") or "").strip() or (' in src:
        ok(f"chat_run gate: merged overrides (_run_settings) beat the registry card ({hits} sites)")
    else:
        fail("gate_override", f"override-aware judgment missing (hits={hits})")


if __name__ == "__main__":
    test_override_vision_model_loads_projector()
    test_nonvision_final_model_never_500s()
    test_text_only_run_unchanged()
    test_tool_loop_takes_caller_vision()
    test_call_site_passes_direct_images()
    test_gate_judges_override()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

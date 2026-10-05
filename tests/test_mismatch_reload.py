"""Ctx-Mismatch-Reload + planerloser _planner_model-Fix (2026-09-10).

Live (23:46-23:50): jeder agentic Run starb mit "[DUO] Cached port ctx
mismatch (cached=10240, needed=32768) -> LD-SET 2497 -> loop_detected" nach 0
Tool-Runden. Ursache: der Ctx-Mismatch-Zweig räumte nur den gecachten
Coder-Port auf — der ensure_loaded-Aufruf hing am `else` der Cache-Prüfung und
wurde bei Mismatch nie erreicht. Fix: der Reload läuft jetzt immer, wenn
`_dport` noch None ist (Mismatch ODER leerer Cache).

Zweiter Fix: ohne Planner UND ohne Chunking wurde `_planner_model` nie
definiert — der Worker-Evict nach dem Explore lief in einen
UnboundLocalError (als breite Warning gefangen, Evict/Cleanup übersprungen).
Default jetzt `_planner_model = coder_mdl`.

Run: python tests/test_mismatch_reload.py
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


# ── Faithful reproduction of the branch structure ───────────────────────────
def _coder_port_flow(cached_port, cached_ctx, target_ctx, ensure_loaded_succeeds=True):
    """Models the fixed loader: mismatch clears the cache, then a reload runs
    whenever _dport is still None. Returns (port, reloaded, stopped)."""
    port = None
    reloaded = False
    if cached_port is not None:
        if cached_ctx is not None and cached_ctx != target_ctx:
            cached_port = None      # mismatch: cache cleared
            cached_ctx = None
        else:
            port = cached_port      # cache hit
    if port is None:                # THE FIX: reload on mismatch AND cold cache
        reloaded = True
        port = target_ctx if ensure_loaded_succeeds else None
    stopped = port is None          # _ld_setter / loop_detected stop
    return port, reloaded, stopped


def test_mismatch_triggers_reload():
    port, reloaded, stopped = _coder_port_flow(8101, 10240, 32768)
    if reloaded and port is not None and not stopped:
        ok("Ctx-Mismatch (10240 vs 32768) löst Reload aus, Run läuft weiter")
    else:
        fail("mismatch_reload", f"port={port} reloaded={reloaded} stopped={stopped}")


def test_mismatch_reload_failure_stops():
    port, reloaded, stopped = _coder_port_flow(8101, 10240, 32768,
                                               ensure_loaded_succeeds=False)
    if reloaded and port is None and stopped:
        ok("Reload scheitert (VRAM) -> sauberer Stop NACH dem Reload-Versuch")
    else:
        fail("mismatch_reload_fail", f"reloaded={reloaded} stopped={stopped}")


def test_cache_hit_skips_reload():
    port, reloaded, stopped = _coder_port_flow(8101, 32768, 32768)
    if port == 8101 and not reloaded and not stopped:
        ok("Cache-Hit (gleiche Ctx) übernimmt Port ohne Reload")
    else:
        fail("cache_hit", f"port={port} reloaded={reloaded}")


def test_cold_cache_loads():
    port, reloaded, stopped = _coder_port_flow(None, None, 32768)
    if reloaded and port is not None and not stopped:
        ok("Kalter Cache lädt normal (alter Pfad unverändert)")
    else:
        fail("cold_cache", f"port={port} reloaded={reloaded}")


# ── AST source guard: ensure_loaded must live under `if _dport is None:` ────
def _loader_structure_ok(path):
    """The reload loop (for ... ensure_loaded) must be reachable when
    `_dport is None` evaluates the cache-check result — i.e. there must exist
    an If node with test `_dport is None` whose body contains the
    ensure_loaded for-loop AND the cache write-back. The terminal stop check
    (`_ld_setter`) must NOT contain the loader."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    loader_ifs, stop_ifs = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.unparse(node.test)
        body_src = "\n".join(ast.unparse(stmt) for stmt in node.body)
        if test_src == "_dport is None":
            if "ensure_loaded" in body_src and "_cached_coder_port = _dport" in body_src:
                loader_ifs.append(node)
            if "_ld_setter(2497)" in body_src and "ensure_loaded" in body_src:
                stop_ifs.append(node)
    return bool(loader_ifs), not stop_ifs


def test_ast_loader_under_dport_none():
    src = Path(__file__).parent.parent / "core" / "duo_runner.py"
    try:
        has_loader, stop_clean = _loader_structure_ok(src)
    except SyntaxError as e:
        fail("ast_loader", f"SyntaxError: {e}")
        return
    if has_loader and stop_clean:
        ok("AST: ensure_loaded-Reload hängt an `if _dport is None:`, Stop-Check ist loader-frei")
    else:
        fail("ast_loader", f"has_loader={has_loader} stop_clean={stop_clean}")


def test_source_guards():
    root = Path(__file__).parent.parent
    checks = [
        ("core/duo_runner.py", "MISMATCH-RELOAD-FIX (2026-09-10)"),
        ("core/duo_runner.py", "PLANNER-MODEL-DEFAULT (2026-09-10)"),
        ("core/duo_runner.py", "_planner_model = coder_mdl"),
        ("core/duo_runner.py", "reload via ensure_loaded "),
    ]
    bad = []
    for rel, needle in checks:
        try:
            txt = (root / rel).read_text(encoding="utf-8")
        except OSError as e:
            bad.append(f"{rel}:{needle} (read fail {e})")
            continue
        if needle not in txt:
            bad.append(f"{rel}:{needle}")
    if not bad:
        ok("source_guards (Fix-Marker + Planner-Default vorhanden)")
    else:
        fail("source_guards", f"fehlt: {bad}")


def test_planner_default_defined_unconditionally():
    """`_planner_model = coder_mdl` must sit BEFORE the planner phase block
    (so planerless runs have it) — textual order check against the phase gate."""
    txt = (Path(__file__).parent.parent / "core" / "duo_runner.py").read_text(encoding="utf-8")
    i_default = txt.find("_planner_model = coder_mdl")
    i_gate = txt.find("if (ctx.duo_config.chunking or ctx.duo_config.planner) and not ctx.aborted()")
    if 0 <= i_default < i_gate:
        ok("Planner-Default steht vor der Planner-Phase (planerlose Runs abgedeckt)")
    else:
        fail("planner_default_order", f"default@{i_default} gate@{i_gate}")


if __name__ == "__main__":
    test_mismatch_triggers_reload()
    test_mismatch_reload_failure_stops()
    test_cache_hit_skips_reload()
    test_cold_cache_loads()
    test_ast_loader_under_dport_none()
    test_source_guards()
    test_planner_default_defined_unconditionally()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)

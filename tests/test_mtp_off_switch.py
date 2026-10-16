"""HIVEMIND_MTP_SPEC off-switch: =0 must switch draft-mtp OFF for every
model, unset/1 must add it only for models whose profile says mtp:true.

Regression for the inverted condition in backend/manager_load.py: the
off-switch tested the ENABLE list ("in ('0','false','off')") and appended
--spec-type draft-mtp when the env var was set to 0 - i.e. it turned the
switch into a global force-on for every model, MTP layers or not.

Both gate inputs come from the registry, so both are exercised without a
slot/engine start:
  - non-MTP model  : model_configs/models/qwen3.5_0.8b-ud.json  (mtp:false)
  - MTP model      : model_configs/models/qwen3.5_4b-mtp.json   (mtp:true)

The gate expression is read from the module source and evaluated against
the real registry, so a silently reintroduced inversion fails here.

Run: python tests/test_mtp_off_switch.py
"""
import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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


# ── source guard: the gate must keep its polarity ───────────────────────────
def gate_source() -> str:
    """Return the source of the MTP gate condition, straight from the AST.

    Parsing the module (instead of scanning lines) also proves the file is
    syntactically intact."""
    src = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "HIVEMIND_MTP_SPEC" in ast.dump(node.test):
            seg = ast.get_source_segment(src, node.test)
            if seg:
                return " ".join(seg.split())
    raise AssertionError("MTP gate not found in backend/manager_load.py")


PROBE = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, r"{root}")
    from backend.llama_config import _try_registry_mtp

    GATE = os.environ["MTP_GATE_SRC"]

    def gate(model, env_value):
        _mtp_from_registry = _try_registry_mtp(model)
        _is_mtp = bool(_mtp_from_registry)
        # snapshot/restore the env var so cases stay independent
        _saved = os.environ.get("HIVEMIND_MTP_SPEC")
        try:
            os.environ.pop("HIVEMIND_MTP_SPEC", None)
            if env_value is not None:
                os.environ["HIVEMIND_MTP_SPEC"] = env_value
            return eval(GATE)
        finally:
            os.environ.pop("HIVEMIND_MTP_SPEC", None)
            if _saved is not None:
                os.environ["HIVEMIND_MTP_SPEC"] = _saved

    for model, envv in (("qwen3.5:0.8b-ud", None),
                        ("qwen3.5:0.8b-ud", "0"),
                        ("qwen3.5:0.8b-ud", "1"),
                        ("qwen3.5:4b-mtp", None),
                        ("qwen3.5:4b-mtp", "0"),
                        ("qwen3.5:4b-mtp", "false"),
                        ("qwen3.5:4b-mtp", "1")):
        print("%s|%s|%s" % (model, envv, int(bool(gate(model, envv)))))
    """
).format(root=str(ROOT))


def main():
    # (a) static polarity: the condition must test OFF values with "not in"
    seg = gate_source()
    check("gate uses 'not in' (off-list, not enable-list)", "not in" in seg,
          f" segment={seg!r}")
    check("gate also requires _is_mtp (MTP model only)",
          "_is_mtp" in seg and " and " in seg, f" segment={seg!r}")

    # (b) behavioural truth table against the real registry
    r = subprocess.run([sys.executable, "-c", PROBE], capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       cwd=str(ROOT), timeout=120,
                       env={**os.environ, "MTP_GATE_SRC": seg})
    if r.returncode != 0:
        check("gate probe runs", False, f"rc={r.returncode} {r.stderr.strip()[:300]}")
        return
    got = {}
    for line in r.stdout.strip().splitlines():
        model, envv, flag = line.rsplit("|", 2)
        got[(model, envv)] = flag == "1"

    cases = [
        ("qwen3.5:0.8b-ud", "None", False),   # non-MTP model, unset
        ("qwen3.5:0.8b-ud", "0", False),      # non-MTP model, off-switch
        ("qwen3.5:0.8b-ud", "1", False),      # non-MTP model, switch on
        ("qwen3.5:4b-mtp", "None", True),     # MTP model, unset -> on
        ("qwen3.5:4b-mtp", "0", False),       # MTP model, off-switch
        ("qwen3.5:4b-mtp", "false", False),   # MTP model, off-switch (alias)
        ("qwen3.5:4b-mtp", "1", True),        # MTP model, switch on
    ]
    for model, envv, want in cases:
        have = got.get((model, envv))
        check(f"gate {model} HIVEMIND_MTP_SPEC={envv} -> draft-mtp={'on' if want else 'off'}",
              have is want, f" got {have}")


main()
print()
print(f"passed={passed} failed={failed}")
sys.exit(1 if failed else 0)

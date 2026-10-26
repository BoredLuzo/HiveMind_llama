# -*- coding: utf-8 -*-
"""Manager lock pin: LlamaServerManager must own an asyncio.Lock under
`self._lock`, and every backend class that touches `self._lock` must resolve
it through the manager's class composite.

Why: the `self._lock = asyncio.Lock()` line was dropped from
LlamaServerManager.__init__ in the working tree while 16 call sites in
manager_load / manager_evict / manager_health / manager_process kept using
it - every load, evict and health pass would die with AttributeError before
doing any work. This suite pins the runtime object, the AST-level provider
wiring, and one real acquire/release cycle. No model is loaded, no engine is
started, no network is touched.

Standalone suite in the run_regressions style: exit 0 = clean, 1 = failed.
"""
import ast
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
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


# ── (a) the runtime object (no model, no engine) ─────────────────────────
from backend.llama_server_manager import LlamaServerManager  # noqa: E402

mgr = LlamaServerManager()
_lock = getattr(mgr, "_lock", None)
check("manager exposes _lock", _lock is not None)
check("_lock is an asyncio.Lock", isinstance(_lock, asyncio.Lock),
      f"(got {type(_lock).__name__ if _lock is not None else 'missing'})")
other_lock = getattr(LlamaServerManager(), "_lock", None)
check("_lock is per instance, not a shared class attribute",
      other_lock is not None and other_lock is not _lock)


# ── (b) AST scan: every user file is wired through the composite ─────────
def _is_self_attr(node, attr):
    return (isinstance(node, ast.Attribute) and node.attr == attr
            and isinstance(node.value, ast.Name) and node.value.id == "self")


def _lock_assigns(scope):
    """Assignments to self._lock anywhere inside `scope`."""
    out = []
    for node in ast.walk(scope):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(_is_self_attr(t, "_lock") for t in targets):
            out.append(node)
    return out


def _lock_uses(scope):
    """Reads of self._lock (includes .release()/.acquire()), not the assign."""
    return [n for n in ast.walk(scope)
            if _is_self_attr(n, "_lock") and isinstance(n.ctx, ast.Load)]


def _classes_of(tree):
    return [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]


backend = ROOT / "backend"
users = {}       # class name -> modules that read self._lock
definers = {}    # class name -> modules that assign self._lock
values = {}      # class name -> source of every assigned value
parse_problems = []

for _path in sorted(backend.glob("*.py")):
    try:
        _tree = ast.parse(_path.read_text(encoding="utf-8", errors="replace"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        parse_problems.append(f"{_path.name}: {exc}")
        continue
    for _cls in _classes_of(_tree):
        if _lock_uses(_cls):
            users.setdefault(_cls.name, set()).add(_path.name)
        _assigns = _lock_assigns(_cls)
        if _assigns:
            definers.setdefault(_cls.name, set()).add(_path.name)
            values.setdefault(_cls.name, []).extend(
                ast.unparse(n.value) for n in _assigns if n.value is not None)

check("every backend module parses for the scan", not parse_problems,
      f"({parse_problems})")
check("backend classes still read self._lock", bool(users), f"({sorted(users)})")

_mgr_path = backend / "llama_server_manager.py"
try:
    _mgr_tree = ast.parse(_mgr_path.read_text(encoding="utf-8", errors="replace"))
except (OSError, SyntaxError) as exc:
    _mgr_tree = None
    check("llama_server_manager.py parses", False, f"({exc})")

_manager_cls = None
if _mgr_tree is not None:
    _manager_cls = next((c for c in _classes_of(_mgr_tree)
                         if c.name == "LlamaServerManager"), None)
check("the manager module still defines LlamaServerManager",
      _manager_cls is not None)

_composite = {ast.unparse(b).split(".")[-1]
              for b in (_manager_cls.bases if _manager_cls is not None else [])}
check("the manager class still inherits the mixin composite",
      {"LlamaLoadMixin", "LlamaEvictMixin", "LlamaProcessMixin",
       "LlamaHealthMixin"} <= _composite, f"({sorted(_composite)})")

_orphans = sorted({mod for _cls, mods in users.items()
                   if _cls not in _composite and _cls not in definers
                   for mod in mods})
check("every file reading self._lock is part of the class composite "
      "(inherited or defines it)", not _orphans, f"({_orphans})")

mro_names = {c.__name__ for c in LlamaServerManager.__mro__}
_unwired = sorted(set(users) - mro_names)
check("every class reading self._lock is in the manager MRO", not _unwired,
      f"({_unwired})")

check("exactly one class defines self._lock", set(definers) == {"LlamaServerManager"},
      f"({sorted(definers)})")
check("the composite root still assigns asyncio.Lock()",
      values.get("LlamaServerManager") == ["asyncio.Lock()"],
      f"({values.get('LlamaServerManager')})")
_init = next((n for n in (_manager_cls.body if _manager_cls is not None else [])
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "__init__"), None)
check("the definition lives in LlamaServerManager.__init__",
      _init is not None and bool(_lock_assigns(_init)))


# ── (c) a real acquire/release cycle, still without loading a model ──────
async def _exercise(lock):
    order = []

    async def _contender():
        async with lock:
            order.append("contender")

    async with lock:
        order.append("held")
        waiter = asyncio.create_task(_contender())
        await asyncio.sleep(0.02)
        blocked = not waiter.done() and order == ["held"]
    await asyncio.wait_for(waiter, timeout=2.0)
    return order, blocked


if isinstance(_lock, asyncio.Lock):
    _order, _blocked = asyncio.run(_exercise(_lock))
    check("async with mgr._lock acquires and releases", _order == ["held", "contender"],
          f"({_order})")
    check("a second acquirer waits while the lock is held", _blocked)
    check("the lock is free again after the block", not _lock.locked())
else:
    check("async with mgr._lock acquires and releases", False, "(no lock)")

print()
print(f"SUMMARY manager_lock: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

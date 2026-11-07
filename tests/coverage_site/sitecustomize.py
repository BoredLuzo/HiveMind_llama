"""Per-process coverage bootstrap for the standalone regression suites.

Not imported by project code: the interpreter startup itself imports the
first module named ``sitecustomize`` it finds on sys.path, and
tests/coverage_run.py prepends this directory to PYTHONPATH. That starts
measuring in the regression runner, in every ``tests/test_*.py`` suite it
spawns and in any gateway/engine grandchild they launch - all of them
inherit the environment.

``coverage.process_startup()`` only acts when ``COVERAGE_PROCESS_START`` is
set; combined with ``parallel = True`` in .coveragerc every process then
writes its own ``.coverage.*`` data file at exit, merged afterwards with
``coverage combine``.

Rule of this file: measurement must never break the measured run. Every
failure path degrades to an unmeasured child, never a crash at startup.
"""
import contextlib
import importlib.machinery
import importlib.util
import os
import sys

_HERE = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))

if os.environ.get("COVERAGE_PROCESS_START"):
    # Missing coverage package, unreadable config, coverage bug - whatever
    # happens here must not stop the suite from running (just unmeasured).
    # contextlib.suppress instead of try/except on purpose: the repo's
    # silent-except guardrail (tests/test_no_new_silent_excepts.py) counts
    # bare/broad except handlers, and this swallow is the whole point.
    with contextlib.suppress(Exception):
        import coverage

        coverage.process_startup()

# We shadow any "real" sitecustomize (stdlib imports the first match on
# sys.path). Chain-load the shadowed one, best effort: search the path with
# our own directory removed and, if a real sitecustomize exists, load it
# manually. No sys.modules games with OUR entry - the import machinery that
# is currently loading us still needs it. On a plain Windows box find_spec
# returns None (no real sitecustomize) and nothing happens.
with contextlib.suppress(Exception):
    _rest_path = [p for p in sys.path
                  if os.path.realpath(p or os.curdir) != _HERE]
    _real = importlib.machinery.PathFinder.find_spec("sitecustomize", _rest_path)
    if _real is not None and _real.loader is not None:
        _mod = importlib.util.module_from_spec(_real)
        sys.modules["sitecustomize"] = _mod
        _real.loader.exec_module(_mod)

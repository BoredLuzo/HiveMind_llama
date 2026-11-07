# -*- coding: utf-8 -*-
"""Shared stdlib-only harness for the standalone regression suites (W3).

Every tests/test_*.py used to carry its own copy of the `check()` counter +
summary boilerplate. This module is the ONE implementation; it imports
nothing from the project, adds no checks of its own and does not change any
assertion - suites keep their output shape and exit codes:

    from tests._harness import Harness

    h = Harness()                      # boxed summary, exit 0/1
    check = h.check                    # check(name, cond, extra="")
    ...
    sys.exit(h.finish("suite_name"))   # prints the summary, returns 0|1

Summary styles (the exact shapes the suites had before consolidation):
  box     (default)  blank line + "=" * width + counts line + "=" * width
  summary            blank line + "SUMMARY <suite>: n passed, m failed"
  plain              blank line + "<suite>: n passed, m failed[ <suffix>]"

Constructor knobs reproduce the historical per-suite line formats:
  width=N     box width (50 default, gguf tag pinning used 60)
  fail_code=N exit code on failure (1 default, langrun guard used 2)
  pad=True    always one space between name and extra (wait_ready kept a
              trailing space on lines with an empty extra)
  gap=N       spaces between PASS/FAIL and the name (1 default; ok()/fail()
              always print the wide 2-space gguf format)
"""


class Harness:
    """Pass/fail counter + printer for one standalone suite."""

    def __init__(self, *, style="box", width=50, fail_code=1, pad=False, gap=1):
        self.style = style
        self.width = width
        self.fail_code = fail_code
        self.pad = pad
        self.gap = gap
        self.passed = 0
        self.failed = 0

    def _record(self, name, cond, extra, gap):
        if cond:
            self.passed += 1
            token = "PASS"
        else:
            self.failed += 1
            token = "FAIL"
        pre = " " if self.pad else ""
        print(f"  {token}{' ' * gap}{name}{pre}{extra}")
        return cond

    def check(self, name, cond, extra=""):
        """Record one assertion; print '  PASS/FAIL <name><extra>'; return cond."""
        return self._record(name, cond, extra, self.gap)

    def ok(self, name):
        """Wide PASS line ('  PASS  name') - historical gguf-tag-pinning format."""
        return self._record(name, True, "", 2)

    def fail(self, name, msg=""):
        """Wide FAIL line ('  FAIL  name  msg') - historical gguf-tag-pinning format."""
        return self._record(name, False, "  " + msg, 2)

    def finish(self, suite="", *, suffix=""):
        """Print the suite summary and return the process exit code (0 = clean)."""
        p, f = self.passed, self.failed
        print()
        if self.style == "box":
            bar = "=" * self.width
            print(bar)
            print(f"  {p} passed, {f} failed  (total {p + f})")
            print(bar)
        elif self.style == "summary":
            print(f"SUMMARY {suite}: {p} passed, {f} failed")
        elif self.style == "plain":
            print(f"{suite}: {p} passed, {f} failed" + (f" {suffix}" if suffix else ""))
        else:
            raise ValueError(f"unknown summary style: {self.style!r}")
        return 0 if f == 0 else self.fail_code

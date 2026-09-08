#!/usr/bin/env python3
"""Browser tool: workspace file:// URLs are served over loopback HTTP.

file:// used to be rejected with a bare "http/https only" error, which sent
the model into broken retry chains when it wanted to verify a local web app.
Workspace file:// URLs are now mapped onto a loopback file server (so ES
modules and fetch work); outside-workspace paths get a guided rejection.
"""
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.browser import _guard_browser_url, _plan_file_navigation, _stop_file_server

_ok = True


def _check(name, cond):
    global _ok
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    _ok = _ok and cond


def main() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / "app").mkdir()
        (ws / "app" / "index.html").write_text("<h1>hive-fileserve</h1>", encoding="utf-8")

        http_url, err = _plan_file_navigation((ws / "app" / "index.html").as_uri(), str(ws))
        _check("in-workspace file:// mapped to loopback http",
               not err and http_url.startswith("http://127.0.0.1:"))
        _check("mapped path preserved", not err and http_url.endswith("/app/index.html"))

        served = ""
        if not err and http_url:
            with urllib.request.urlopen(http_url, timeout=5) as r:
                served = r.read().decode("utf-8")
        _check("served content matches", "hive-fileserve" in served)

        outside = Path(tmp).parent / ("outside_" + next(iter(tempfile._get_candidate_names())) + ".html")
        outside.write_text("x", encoding="utf-8")
        try:
            _, err2 = _plan_file_navigation(outside.as_uri(), str(ws))
            _check("outside-workspace rejected with guidance",
                   "outside the workspace" in err2 and "http.server" in err2)
        finally:
            outside.unlink(missing_ok=True)

        _, err3 = _plan_file_navigation((ws / "app" / "missing.html").as_uri(), str(ws))
        _check("missing file reported", err3.startswith("file not found:"))

        _check("guard still blocks javascript:", bool(_guard_browser_url("javascript:alert(1)")))
        _check("guard allows loopback http", _guard_browser_url("http://127.0.0.1:12345/x") is None)

    _stop_file_server()
    print("PASS" if _ok else "FAIL")
    return 0 if _ok else 1


if __name__ == "__main__":
    sys.exit(main())

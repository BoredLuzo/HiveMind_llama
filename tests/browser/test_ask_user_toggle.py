# -*- coding: utf-8 -*-
"""A6 browser test: ask_user_auto_proceed toggle (opt-in, needs an engine).

Run against a live engine:
    set HIVEMIND_BROWSER_BASE=http://127.0.0.1:8001
    python tests/browser/test_ask_user_toggle.py

Checks: click the real checkbox -> /settings reflects it -> reload keeps it
-> original value restored. Skips (exit 0) when Playwright or the engine is
unavailable, so it can live in the tree without breaking the regression run.
"""
import json
import os
import sys
import urllib.request

BASE = os.environ.get("HIVEMIND_BROWSER_BASE", "http://127.0.0.1:8001").rstrip("/")


def _api_flag() -> bool:
    with urllib.request.urlopen(BASE + "/settings", timeout=5) as resp:
        return bool(json.load(resp).get("ask_user_auto_proceed"))


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright is not installed")
        return 0
    try:
        _api_flag()
    except OSError as exc:
        print(f"SKIP: engine not reachable at {BASE} ({exc})")
        return 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(BASE + "/", wait_until="domcontentloaded", timeout=30000)
        cb = page.locator("#ask-user-auto-proceed-cb")
        before = cb.is_checked()
        cb.evaluate("el => el.click()")
        page.wait_for_timeout(1000)
        after_click = cb.is_checked()
        api_after = _api_flag()
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(1000)
        after_reload = page.locator("#ask-user-auto-proceed-cb").is_checked()
        if after_reload != before:
            page.locator("#ask-user-auto-proceed-cb").evaluate("el => el.click()")
            page.wait_for_timeout(800)
        restored = _api_flag()
        browser.close()

    checks = [
        ("toggle flips", after_click != before),
        ("settings API follows", api_after == after_click),
        ("reload keeps the value", after_reload == after_click),
        ("original restored", restored == before),
    ]
    failed = 0
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
        failed += 0 if ok else 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

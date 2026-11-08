# Browser tests (opt-in)

These require a **running HiveMind engine** and Playwright with a browser.
They are deliberately not in `tests/run_regressions.py` (CI has no engine).

```bat
set HIVEMIND_BROWSER_BASE=http://127.0.0.1:8001
python tests/browser/test_ask_user_toggle.py
```

`test_ask_user_toggle.py` (A6): the settings checkbox must persist
`ask_user_auto_proceed`, survive a reload, and be restorable. It uses the
live `/settings` API as the source of truth and restores the original value.

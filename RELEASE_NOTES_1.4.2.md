# HiveMind 1.4.2

A hotfix release. If 1.4.1 gave you a broken settings sidebar, this is the fix -
and nothing else about your install, models or settings changes.

## The 1.4.1 sidebar bug

1.4.1 removed the per-role preload switches (judge in agentic, analyst, coder).
When those rows were cut out of `index.html`, one closing `</div>` stayed
behind. That one tag closed the Configs panel right after the "Startup
Preload" card, several hundred lines too early.

What that did in the browser:

- Keep-Alive, Voice, Smart Preload, Model Config, Energy, Git and Telegram
  rendered **outside** any panel - permanently visible under every tab and
  outside the scroll container. The sidebar looked "stuck" and could not be
  scrolled any further.
- The Memory, Soul, Models and Chats panels ended up as children of `<body>`
  instead of `#sidebar`, so those tabs showed nothing.

The fix is the deletion of that single line. The panel nesting now matches
1.4.0 exactly, which is why the UI is back without any CSS or JS change.

## Prefetch card works again

`/prefetch/stats` was 404 since 1.4.1: the route sat in the debug router, and
that router is gated behind `debug_api_enabled` (off by default) so the debug
internals stay private. The Prefetch card in the settings panel is normal UI,
not debug data, so the route moved to the always-mounted core router. The card
shows its lead time and averages again, and the console is clean.

## Also in this release

`main` already carried the internal work done after the 1.4.1 tag, so it ships
here:

- `core/duo_runner.py` was cut down: the 21 module-level helpers and the stable
  callbacks now live in `core/duo/preflight.py` (pure move, AST-verified,
  re-exported).
- The stuck handler initializes `loop_detected` before it runs - the
  `UnboundLocalError` that aborted every run after the first tool call.
- The verify gate marks a completion accepted after one veto as unverified and
  reports it instead of silently passing.

## Upgrading

Run `update.bat` (or `install.bat` for a fresh install). `settings.json`,
models, sessions and logs are not touched. If you are on 1.4.1 with the broken
sidebar, the update is enough - no reinstall, no config reset.

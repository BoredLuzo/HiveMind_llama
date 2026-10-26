# Contributing

This file is part of the repository, not of the release zip - users download the
zip, they do not need the dev setup.

## Branches

| Branch | Meaning |
|---|---|
| `main` | **Released state.** Every release tag points at a commit on `main`. Nothing lands here except a release merge. |
| `dev` | **Integration.** All work happens here; `main` is fast-forwarded from `dev` when a release is cut. |
| anything else | scratch branches, kept for history, never merged by accident. |

Release flow:

```bat
git switch dev
:: bump HIVEMIND_VERSION in server.py, set the CHANGELOG heading to the same
:: version, run the suite + the pre-push gate, commit

git switch main
git merge --ff-only dev            :: main only ever moves forward
git tag -a v1.4.1 -m "HiveMind 1.4.1"
git push origin main v1.4.1

git switch dev                     :: keep working on dev
```

`git merge --ff-only` is deliberate: if it refuses, someone committed straight to
`main` and that must be sorted out before a release, not papered over with a merge
commit. Tags live on `main`, so `git checkout v1.4.1` gives exactly what was
released.

## Pre-commit hook

`scripts/hooks/pre-commit` runs ruff (`F821` undefined names, `F811` redefinitions,
`F841` unused locals) plus the fast test suites. It is not active until you point
git at it once:

```bat
git config core.hooksPath scripts/hooks
```

`ruff` is expected in `.venv\Scripts\ruff.exe` or on `PATH`; without it the hook
skips linting with a notice instead of failing. To bypass it in an emergency:
`git commit --no-verify` (and say so in the commit message).

## Before a release

1. `python tests\run_regressions.py` - the whole suite, no LLM, no models.
2. `C:\Users\...\prepush_check.ps1` (the local gate) - 13 checks including a clean
   working tree, so it has to run **after** the commit.
3. Bump `HIVEMIND_VERSION` in `server.py` and make the CHANGELOG heading match -
   the gate compares them.
4. `deploy\package_release.bat` builds the zip from `git archive HEAD`, so the tag
   must exist first (`BUILD_INFO.txt` records it).
5. Publish: `gh release create <tag> <zip> --title "HiveMind <ver>"
   --notes-file RELEASE_NOTES_<ver>.md --target main`.

## What does not ship

`tests/`, `scripts/`, `docs/audit/`, `RELEASE_NOTES_*.md`, `CONTRIBUTING.md`,
`.gitignore` and `.gitattributes` are `export-ignore` - the release zip contains
the program, the launchers and the user docs only.

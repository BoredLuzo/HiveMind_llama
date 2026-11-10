# Contributing

This file is part of the repository, not of the release zip - users download the
zip, they do not need the dev setup.

## Releases

Cutting and publishing are two steps and only one of them is automated.

- Preparing is mechanical: version bump, CHANGELOG block, release notes, gate,
  `deploy\package_release.bat`, tag. An agent may do all of that.
- Publishing is a decision: `gh release create` plus `deploy\promote_main.bat`
  only run after the maintainer has tested the build and said so explicitly.
  A release that ships untested costs more than a late one.

## Branches

| Branch | Meaning |
|---|---|
| `main` | **Released state.** Every release tag points at a commit on `main`. Nothing lands here except a release merge. |
| `dev` | **Integration.** All work happens here; `main` is fast-forwarded from `dev` when a release is cut. |
| anything else | scratch branches, kept for history, never merged by accident. |

After publishing a release, run `deploy\promote_main.bat`: it
fast-forwards `main` to `dev` and pushes, then switches back.
It never merges, so a stray commit on `main` stops it.

Release flow:

```bat
git switch dev
:: bump HIVEMIND_VERSION in core/version.py, set the CHANGELOG heading to the same
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

## Type checking

`deploy\pyright_baseline.py` runs pyright over the tree and compares the result
with the frozen `deploy\pyright_baseline.json`. Existing errors are the
baseline; only NEW errors fail the gate. When a change fixes typing debt,
refresh the baseline in the same commit:

```bat
python deploy\pyright_baseline.py           :: gate: 0 NEW
python deploy\pyright_baseline.py --write   :: refresh after fixes
```

CI runs the same gate in the `types` job of `linux-core.yml`. Do not lower the
baseline by suppressing errors; fix the annotation or leave the entry frozen.

## Coverage measurement

`tests\coverage_run.py` wraps the regression runner and measures the runner
plus every suite subprocess it spawns (gateway/engine grandchildren included):

```bat
uv sync --group dev                       :: once - coverage lives in the dev group
python tests\coverage_run.py --changed    :: measured run, flags pass through
python tests\coverage_run.py --report     :: run + combine + report in one go
python -m coverage combine                :: or merge .coverage.* manually...
python -m coverage report --skip-covered  :: ...then show the table
```

Every measured process writes its own parallel `.coverage.*` file (via
`tests\coverage_site\sitecustomize.py` + `COVERAGE_PROCESS_START`), which
`coverage combine` merges; the wrapper clears stale data files first. Without
`coverage` installed the wrapper still runs the suite, unmeasured. Normal
runs and the release zip are untouched: `.coveragerc`, the helper scripts and
`requirements-dev.txt` are `export-ignore`, the data files are gitignored.

## Before a release

1. `python tests\run_regressions.py` - the whole suite, no LLM, no models.
2. `C:\Users\...\prepush_check.ps1` (the local gate) - 13 checks including a clean
   working tree, so it has to run **after** the commit.
3. Bump `HIVEMIND_VERSION` in `core/version.py` and make the CHANGELOG heading match -
   the gate compares them.
4. `deploy\package_release.bat` builds the zip from `git archive HEAD`, so the tag
   must exist first (`BUILD_INFO.txt` records it).
5. Publish: `gh release create <tag> <zip> --title "HiveMind <ver>"
   --notes-file RELEASE_NOTES_<ver>.md --target main`.

## What does not ship

`tests/`, `scripts/`, `RELEASE_NOTES_*.md`, `CONTRIBUTING.md`, `.gitignore`
and `.gitattributes` are `export-ignore` - the release zip contains the
program, the launchers and the user docs only. Internal audit/handoff and
benchmark notes are kept outside the checkout (`../local_docs/`), not tracked.

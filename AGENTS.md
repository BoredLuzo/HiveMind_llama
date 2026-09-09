# AGENTS.md — HiveMind repo conventions

Working rules for coding agents (and humans) in this repo.

## Language
- Everything in the repo is English: code, comments, docstrings, log/UI
  strings, docs.
- Functional keyword/regex matchers are **English-only as of 2026-09-09**
  (input detection). German halves were removed deliberately ("vorerst") —
  restore via git history if needed. Intentionally kept as-is:
  - `core/duo_runner.py` legacy marker regexes + `static/app.js` legacy
    "zu groß" alternation: they parse OLD persisted session output.
  - `server.py` / `vision/preprocess.py` `_VISION_POISON_MARKERS`: match leaks
    of the old German message scaffolding from in-flight sessions.
  - `infra/mcp_server.py` `_looks_like_error_text`: detects German-locale tool
    error text.
  - Memory data keys (`herkunft`, `wohnort`, `beruf`, `alter`, `projekt`,
    `sprache`): stored-data schema compatibility — do not rename.

## Quality gate (before every commit)

```
python tests/run_regressions.py   # standalone suites, all must PASS
python -m ruff check .            # ruff.toml: E9/F821/F601/F811/F841/W605
```

- `tests/test_no_new_silent_excepts.py` counts silent except handlers against
  a frozen baseline (`tests/lint_baseline.json`). Prefer narrowing the
  exception over widening the baseline; if a new broad handler is genuinely
  justified, update via `--update`.
- New behavior => new standalone suite `tests/test_<area>.py` (script with
  `sys.exit(0|1)`), registered in `tests/run_regressions.py`.

## Generated files
- `docs/settings.md` is GENERATED: after `settings.py` changes run
  `python deploy/gen_settings_docs.py`.

## Dependencies (dual source of truth)
- **Windows** (`install.bat`) and **Linux** (`deploy/install_linux.sh`):
  primary path is **uv** (`uv python install 3.14` + `uv sync --all-extras`
  from **`pyproject.toml`**). `uv.lock` is gitignored and generated locally
  on install — do not commit it.
- **`requirements.txt`** is the fallback path (Windows: pre-uv release
  folders; Linux: systems without uv / manual venv setup).
- Adding/changing a dependency means updating **both files**
  (`pyproject.toml` + `requirements.txt`).

## Git
- No push without explicit user go.
- Commit identity is repo-local `BoredLuzo <BoredLuzo@users.noreply.github.com>`
  — never the public/global identity.

## Settings & live install
- `settings.json` is NOT tracked (user config, differs per install).
- The live install at `..\HiveMind_install` mirrors this repo: sync changed
  source files there after fixes and restart the server (old code keeps
  running in RAM until then).

## Functional matcher inventory (English-only since 2026-09-09)
- `core/duo_helpers.py`: `_READ_ONLY_KEYWORDS`, `_IMPL_OVERRIDE_RE`, `_NEGATOR_RE`
- `static/app.js`: `INTENT_TOOL_KWS`, `INTENT_MEMORY_KWS`, `INTENT_EVOLVE_KWS`
- `context/chat_util.py`: chat patterns, websearch triggers, memory extraction
  (keys stay `name`/`herkunft`/`wohnort`/`beruf`/`alter`/`projekt`/`sprache`)
- `infra/mcp_server.py`: `_REGEX_ROUTES` natural-language aliases
- `vision/preprocess.py`: task-type trigger keywords
- `server.py`: `_SYMBOL_HINT_STOPWORDS`

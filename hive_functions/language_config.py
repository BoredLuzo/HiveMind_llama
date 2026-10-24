


from __future__ import annotations

LANGUAGE_RUNNERS: dict[str, dict] = {
    "python": {
        "extensions":    [".py"],
        "detect_files":  ["pyproject.toml", "setup.py", "requirements.txt"],
        # DETECT-FALLBACK (2026-10-17, live): a plain folder with .py files and
        # NO pyproject/setup/requirements was reported as "no test runner", so
        # the verify gate stayed "untested" even after the coder wrote and ran a
        # Python test file (owner: "the last test is still reported as
        # untested"). Test-looking .py files are the marker that matters here.
        "detect_files_fallback": ["test_*.py", "*_test.py", "tests/*.py"],
        # MODULE FORM (2026-10-23): `python -m pytest` works whenever pytest
        # is importable by the ENGINE's interpreter — a bare `pytest` exe is
        # not on PATH everywhere (live box: "Der Befehl pytest nicht
        # gefunden"), which turned every auto-test into a false red.
        "test_cmd":      "python -m pytest -x -q 2>&1",
        # TESTMAP (2026-10-23): scoped variant for the edit→test mapping.
        # {paths} is a shell-quoted list of test files; runners without a
        # safe scoped form (cargo/go/mvn/ctest need project context) simply
        # leave this key out and the mapping falls back to the full suite.
        "test_cmd_scoped": "python -m pytest -x -q {paths} 2>&1",
        "run_cmd":       "python {file}",
        "start_cmd":     None,
        # switched. Exit propagation is now handled runner-side
        # (; exit $LASTEXITCODE), siehe tools/handlers.py.
        "lint_cmd":      "python -m py_compile {file}",
        "install_cmd":   "python -m pip install {package}",
    },
    "javascript": {
        "extensions":    [".js", ".mjs", ".cjs"],
        "detect_files":  ["package.json"],
        "test_cmd":      "npm test 2>&1",
        # npm passes args after -- to the test script; jest/vitest/mocha all
        # accept path filters there. If the script ignores them the full
        # suite runs — slower, never wrong. run_tests re-runs the full
        # command when a scoped run reports "no tests found".
        "test_cmd_scoped": "npm test -- {paths} 2>&1",
        "run_cmd":       "node {file}",
        "start_cmd":     "npm start",
        "lint_cmd":      "node --check {file}",
        "install_cmd":   "npm install {package}",
    },
    "typescript": {
        "extensions":    [".ts", ".tsx"],
        "detect_files":  ["tsconfig.json"],
        "test_cmd":      "npm test 2>&1",
        "test_cmd_scoped": "npm test -- {paths} 2>&1",
        "run_cmd":       "npx ts-node {file}",
        "start_cmd":     "npm run dev",
        "lint_cmd":      "npx tsc --noEmit 2>&1 | Select-Object -First 30",
        "install_cmd":   "npm install {package}",
    },
    "astro": {
        "extensions":    [".astro"],
        "detect_files":  ["astro.config.mjs", "astro.config.ts", "astro.config.js"],
        "test_cmd":      "npm test 2>&1",
        "run_cmd":       None,
        "start_cmd":     "npm run dev",
        "lint_cmd":      "npx astro check 2>&1 | Select-Object -First 30",
        "install_cmd":   "npm install {package}",
    },
    "docker": {
        "extensions":    [],
        "detect_files":  ["docker-compose.yml", "docker-compose.yaml", "Dockerfile"],
        "test_cmd":      "docker compose up --build -d && docker compose ps && docker compose logs --tail=30",
        "run_cmd":       "docker compose up --build",
        "start_cmd":     "docker compose up -d",
        "lint_cmd":      "docker compose config 2>&1",
        "install_cmd":   None,
    },
    "java": {
        "extensions":    [".java"],
        "detect_files":  ["pom.xml", "build.gradle"],
        "test_cmd":      "mvn test -q 2>&1",
        "run_cmd":       "mvn exec:java -q",
        "start_cmd":     None,
        "lint_cmd":      "javac {file} 2>&1",
        "install_cmd":   None,
    },
    "rust": {
        "extensions":    [".rs"],
        "detect_files":  ["Cargo.toml"],
        "test_cmd":      "cargo test 2>&1",
        "run_cmd":       "cargo run 2>&1",
        "start_cmd":     None,
        "lint_cmd":      "cargo check 2>&1 | Select-Object -First 30",
        "install_cmd":   "cargo add {package}",
    },
    "go": {
        "extensions":    [".go"],
        "detect_files":  ["go.mod"],
        "test_cmd":      "go test ./... 2>&1",
        "run_cmd":       "go run {file}",
        "start_cmd":     None,
        "lint_cmd":      "go vet ./... 2>&1",
        "install_cmd":   "go get {package}",
    },
    "csharp": {
        "extensions":    [".cs"],
        "detect_files":  ["**/*.csproj", "**/*.sln"],
        "test_cmd":      "dotnet test 2>&1",
        "run_cmd":       "dotnet run",
        "start_cmd":     None,
        "lint_cmd":      "dotnet build 2>&1 | Select-Object -Last 20",
        "install_cmd":   "dotnet add package {package}",
    },
    "cpp": {
        "extensions":    [".cpp", ".cc", ".cxx"],
        "detect_files":  ["CMakeLists.txt"],
        "test_cmd":      "cmake --build build && cd build && ctest 2>&1",
        "run_cmd":       "cmake --build build && ./build/{file}",
        "start_cmd":     None,
        "lint_cmd":      "g++ -fsyntax-only {file} 2>&1",
        "install_cmd":   None,
    },
}


def detect_language(file_path: str) -> str | None:
    """Detects the language from the file extension."""
    from pathlib import Path
    ext = Path(file_path).suffix.lower()
    for lang, cfg in LANGUAGE_RUNNERS.items():
        if ext in cfg["extensions"]:
            return lang
    return None


def detect_project_languages(workspace: str) -> list[str]:


    from pathlib import Path
    ws = Path(workspace)
    found = []
    for lang, cfg in LANGUAGE_RUNNERS.items():
        for pattern in cfg.get("detect_files", []):
            if "*" in pattern:
                if next(ws.glob(pattern), None):
                    found.append(lang)
                    break
            else:
                if (ws / pattern).exists():
                    found.append(lang)
                    break
    # DETECT-FALLBACK (2026-10-17): second pass for projects without a manifest.
    # A folder whose only markers are test-looking files (test_*.py ...) still
    # HAS a runner - without this the verify gate answers "no test runner
    # detected" and the run stays "untested" (live owner report).
    if not found:
        for lang, cfg in LANGUAGE_RUNNERS.items():
            for pattern in cfg.get("detect_files_fallback", []) or []:
                if next(ws.glob(pattern), None):
                    found.append(lang)
                    break
    if "docker" in found:
        found = ["docker"] + [l for l in found if l != "docker"]
    return found


def build_test_hint(workspace: str) -> str:

    # RUN_TESTS-FIRST (audit 2026-10-24): the old concrete run_bash example
    # outranked the "MUST run_tests" rule for small models — they copied the
    # example and the verify ladder then parsed echo-prone stdout heuristics
    # instead of the genuine [TEST-RESULT] marker.
    langs = detect_project_languages(workspace)
    if not langs:
        return "Detect the test command from project files and run it with run_bash."
    hints = []
    for lang in langs[:3]:
        hints.append(f"  run_tests (full suite)  # {lang}")
    if not hints:
        return "Detect the test command from project files and run it with run_bash."
    return "After implementing, verify with:\n" + "\n".join(hints)


def get_test_cmd(lang: str, file: str = "") -> str | None:
    cfg = LANGUAGE_RUNNERS.get(lang)
    if not cfg:
        return None
    cmd = cfg.get("test_cmd")
    return cmd.format(file=file) if cmd else None


def get_run_cmd(lang: str, file: str = "") -> str | None:
    cfg = LANGUAGE_RUNNERS.get(lang)
    if not cfg:
        return None
    cmd = cfg.get("run_cmd")
    return cmd.format(file=file) if cmd else None


def get_start_cmd(lang: str) -> str | None:
    cfg = LANGUAGE_RUNNERS.get(lang)
    return cfg.get("start_cmd") if cfg else None

# POSIX-TOOL-LADDER (2026-09-08): the command templates above were written for
# the Windows runner (python / Select-Object pipes). On POSIX, python is
# python3 and PowerShell pipe cmdlets do not exist — translate at import time.
import re as _re_lc
import sys as _sys_lc

if _sys_lc.platform != "win32":
    for _runner in LANGUAGE_RUNNERS.values():
        for _key in ("test_cmd", "test_cmd_scoped", "run_cmd", "start_cmd", "lint_cmd", "install_cmd"):
            _val = _runner.get(_key)
            if not _val:
                continue
            _val = _val.replace("Select-Object -First 30", "head -30")
            _val = _val.replace("Select-Object -Last 20", "tail -20")
            _val = _re_lc.sub(r"(?<![\w./-])python(?=\s|$)", "python3", _val)
            _runner[_key] = _val

"""Shared runtime state + init_runtime_deps (tools/handlers Paket)."""
from __future__ import annotations

_GIT_TOOLS_AVAILABLE = False
_LANGUAGE_RUNNERS = None
_TestResult = None
_WEBSEARCH_AVAILABLE = False
_detect_language = None
_edit_ast_file = None
_find_references_report = None
_get_signatures_report = None
_run_test_suite = None
_safe_web_fetch = None
_safe_web_search = None
edit_ast_file = None
exec_git_commit = None
find_references_report = None
get_signatures_report = None

def init_runtime_deps(
    get_signatures_report_fn=None,
    find_references_report_fn=None,
    edit_ast_file_fn=None,
    detect_language=None,
    language_runners=None,
    safe_web_search=None,
    safe_web_fetch=None,
    websearch_available=False,
    git_available=False,
    exec_git_commit_fn=None,
    run_test_suite_fn=None,
    TestResult_class=None):
    """Initialisiert Runtime-Dependencies (von server.py beim Startup aufzurufen)."""
    global _get_signatures_report, _find_references_report, _edit_ast_file
    global _detect_language, _LANGUAGE_RUNNERS
    global _safe_web_search, _safe_web_fetch, _WEBSEARCH_AVAILABLE
    global get_signatures_report, find_references_report, edit_ast_file
    global _GIT_TOOLS_AVAILABLE, exec_git_commit
    global _run_test_suite, _TestResult
    _get_signatures_report = get_signatures_report_fn or _get_signatures_report
    _find_references_report = find_references_report_fn or _find_references_report
    _edit_ast_file = edit_ast_file_fn or _edit_ast_file
    _detect_language = detect_language or _detect_language
    _LANGUAGE_RUNNERS = language_runners or _LANGUAGE_RUNNERS
    _safe_web_search = safe_web_search or _safe_web_search
    _safe_web_fetch = safe_web_fetch or _safe_web_fetch
    _WEBSEARCH_AVAILABLE = websearch_available or _WEBSEARCH_AVAILABLE
    _GIT_TOOLS_AVAILABLE = git_available
    exec_git_commit = exec_git_commit_fn if exec_git_commit_fn is not None else exec_git_commit
    _run_test_suite = run_test_suite_fn if run_test_suite_fn is not None else _run_test_suite
    _TestResult = TestResult_class if TestResult_class is not None else _TestResult
    get_signatures_report = _get_signatures_report
    find_references_report = _find_references_report
    edit_ast_file = _edit_ast_file

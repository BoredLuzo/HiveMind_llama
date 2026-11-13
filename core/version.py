"""Single source of truth for the HiveMind version.

SPLIT (2026-11-05 maintainability audit): this lived in server.py, and the
import cycle ran through it (infra/mcp_client, infra/lsp_client imported the
running server just to read the version). A leaf module with one literal
breaks that edge. All version probes (update_check.py, update.bat,
package_release.bat, prepush_check.ps1, start_hivemind.bat) parse THIS file
now — keep the literal line shaped so the existing regexes match unchanged:

    HIVEMIND_VERSION = "<major.minor.patch>"
"""
import re

# ONE regex for the Python side (updater + update_check import it). The
# .bat/.ps1 probes parse the same shape but cannot import Python.
VERSION_LINE_RE = re.compile(
    r"HIVEMIND_VERSION\s*=\s*[\"']([0-9][0-9A-Za-z._-]*)[\"']", re.I)

HIVEMIND_VERSION = "1.6.0-beta"

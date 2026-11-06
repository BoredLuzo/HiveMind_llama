"""Single source of truth for the HiveMind version.

SPLIT (2026-11-05 maintainability audit): this lived in server.py, and the
import cycle ran through it (infra/mcp_client, infra/lsp_client imported the
running server just to read the version). A leaf module with one literal
breaks that edge. All version probes (update_check.py, update.bat,
package_release.bat, prepush_check.ps1, start_hivemind.bat) parse THIS file
now — keep the literal line shaped so the existing regexes match unchanged:

    HIVEMIND_VERSION = "1.5.5"
"""

HIVEMIND_VERSION = "1.5.5"

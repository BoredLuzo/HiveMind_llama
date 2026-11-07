"""Agent intent detection and tool-request detection (extracted from server.py)."""
import re

AGENT_TRIGGER_MAP = {
    "analyst":     ["break down", "analyze", "analyse", "look into", "explain in detail",
                    "analysiere", "analyse das", "zerlege", "untersuche", "erklaere mir genau",
                    "schau dir an"],
    "refiner":     ["refine", "improve", "polish", "optimize", "revise",
                    "verbessere", "verfeinere", "optimiere", "ueberarbeite", "mach besser", "korrigiere"],
    "critic":      ["critique", "review critically", "find flaws", "problems", "devil's advocate",
                    "kritisiere", "hinterfrage", "was ist falsch", "schwaechen", "probleme", "kritik", "was stimmt nicht", "devils advocate"],
    "synthesizer": ["summarize", "summary", "conclusion", "synthesize", "combine",
                    "fasse zusammen", "zusammenfassung", "fazit", "synthese", "kombiniere",
                    "was ist das ergebnis", "abschliessend"],
}

_TOOL_REQUEST_PATTERNS = [
    r'[A-Za-z]:\\[^\s"\'<>|*?]+\.\w+',
    r'/(?:[^\s"\'<>|*?]+/)+[^\s"\'<>|*?]+\.\w+',
    r'(?:^|[\s"\'])(?:\./|\.\./)[\w\-\.]+\.(?:java|py|ts|js|go|rs|cpp|c|h|cs|kt|rb|php|swift|vue|json|yaml|yml|toml|xml|sql|sh|bat|ps1)',
]

_TOOL_REQUEST_KEYWORDS = [
    "read file", "read the file", "open file", "open the file", "show the file",
    "write to ", "write file", "write to the file", "update the file",
    "modify the file", "edit the file", "save to file",
    "run the code", "run file", "run it", "run this", "execute",
    "git status", "git diff", "git log",
    "search in ", "grep ",
    "lese ", "lies ", "lese die ", "lies die ", "oeffne ", "zeig mir den inhalt",
    "was steht in ", "was ist in ", "schau in die datei", "schau dir die datei an",
    "zeig die datei", "zeig mir die datei", "inhalt von ",
    "schreibe in ", "schreib in ", "schreibe die datei", "schreib die datei",
    "aendere die datei", "aendere den code", "fuege ein", "fuege hinzu",
    "erstelle die datei", "erstell die datei",
    "fuehre aus", "fuehre das aus", "fuehre es aus", "fuehre mal aus", "fuehre den code aus",
    "suche in ", "such in ", "durchsuche ",
]


def detect_tool_request(text: str) -> bool:
    lower = text.lower()
    if any(kw in lower for kw in _TOOL_REQUEST_KEYWORDS):
        return True
    for pat in _TOOL_REQUEST_PATTERNS:
        if re.search(pat, text):
            return True
    return False


# SIMPLE-TOOL-ROUTE (A3, 2026-11-07): a short read/list/inspect prompt must
# not be hijacked into Code Duo just because agentic mode is persisted. The
# match is deliberately narrow: short, tool-shaped, and WITHOUT a coding verb
# - "read file X and fix it" stays a coding task. Over-matching here would
# steal real coding runs from the duo path.
_SIMPLE_TOOL_PATTERNS = (
    r"\b(list|show|display|print)\b[^.\n]{0,40}\b(files?|director(?:y|ies)|folders?|contents?)\b",
    r"\b(read|open|cat|view)\b[^.\n]{0,40}\bfiles?\b",
    r"\b(read|open|show|cat|view)\b[^.\n]{0,60}\.[a-z0-9]{1,6}\b",
    r"\bgit\s+(status|diff|log|show)\b",
    r"\b(find|search|grep)\b[^.\n]{0,40}\b(files?|for|in)\b",
    r"(?:^|\s)(?:ls|dir|pwd)(?:\s|$)",
    r"\b(liste|zeig|lies|lese|oeffne)\b[^.\n]{0,40}\b(datei|dateien|ordner|verzeichnis|inhalt)\b",
    r"\b(lies|lese|oeffne|zeig)\b[^.\n]{0,60}\.[a-z0-9]{1,6}\b",
)
_CODING_VERBS = (
    "fix", "implement", "refactor", "rewrite", "debug", "add ", "create ",
    "change ", "modify ", "update ", "migrate", "optimi", "improve",
    "write ", "build ", "feature", "bug", "test",
    "repariere", "implementiere", "refaktoriere", "schreibe", "aendere",
    "\u00e4ndere", "optimiere", "erweitere", "debugge", "baue ", "teste",
)


def is_simple_tool_prompt(text: str, *, max_words: int = 24) -> bool:
    """True for a short read/list/inspect prompt (A3 route guard).

    Deterministic and cheap: no model call, no state. Coding verbs veto the
    match so "read app.py and fix the bug" still routes to Code Duo.
    """
    low = (text or "").strip().lower()
    if not low or len(low.split()) > max_words:
        return False
    if any(verb in low for verb in _CODING_VERBS):
        return False
    return any(re.search(pat, low) for pat in _SIMPLE_TOOL_PATTERNS)


def detect_agent_intent(text):
    """Detects which pipeline agent the user wants to address."""
    lower = text.lower()
    for agent, triggers in AGENT_TRIGGER_MAP.items():
        if any(t in lower for t in triggers):
            return agent
    m = re.match(
        r'^@?(analyst|refiner|critic|kritiker|synthesizer)\s*[:\-]?\s*(.+)',
        text.strip(), re.IGNORECASE
    )
    if m:
        name_map = {"analyst":"analyst","refiner":"refiner",
                    "critic":"critic","kritiker":"critic","synthesizer":"synthesizer"}
        return name_map.get(m.group(1).lower())
    return None


def get_question_from_intent(text, agent):
    """Extracts the actual question from an intent trigger."""
    lower = text.lower()
    for trigger in AGENT_TRIGGER_MAP.get(agent, []):
        if lower.startswith(trigger):
            return text[len(trigger):].strip().lstrip(':').strip()
    m = re.match(r'^@?\w+\s*[:\-]?\s*(.+)', text.strip(), re.DOTALL)
    if m:
        return m.group(1).strip()
    return text

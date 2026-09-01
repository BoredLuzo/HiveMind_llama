"""Central regex patterns — shared across modules."""
import re

_RE_WIN_PATH  = re.compile(r'[A-Za-z]:\\[\w\\.\-]+')
_RE_UNIX_PATH = re.compile(r'/(?!v\d/|dev/|proc/|sys/)[\w][\w.\-]*/[\w/.\-]{3,}')
_RE_PATH_KEY  = re.compile(r'[A-Za-z]:\\[\w\\.\-]+|/[\w/.\-]{5,}')
_RE_SEARCH_REPLACE_BLOCK = re.compile(r'<{7} SEARCH\n(.*?)\n={7}\n(.*?)\n>{7} REPLACE', re.DOTALL)
_RE_SEARCH_REPLACE_BLOCK_LOOSE = re.compile(r'<{7}\s*SEARCH\s*\n(.*?)\n?={7}\n?(.*?)\n?>{7}\s*REPLACE', re.DOTALL)
# often emit differing marker numbers/line endings).
_RE_SEARCH_REPLACE_BLOCK_LENIENT = re.compile(
    r'<{5,}[ \t]*SEARCH\b[^\n]*\n(.*?)\n[ \t]*={3,}[ \t]*\n(.*?)\n[ \t]*>{5,}[ \t]*REPLACE',
    re.DOTALL,
)
_RE_REL_PATH_HINT = re.compile(r'\b[\w.\-/\\]+\.[A-Za-z0-9]{1,8}\b')

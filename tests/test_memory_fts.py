# -*- coding: utf-8 -*-
"""Repo-insight retrieval uses SQLite FTS5 (bm25) instead of hash-bin cosine.

The old retriever hashed every token into one of 96 signed bins and compared
vectors: lexical overlap only, and bins saturate as documents grow. FTS5 gives
real term ranking (porter stemming, prefix match, bm25) with no new dependency.

Pinned here:
  1. the query sanitiser never lets FTS5 syntax through (quotes, operators)
  2. ranking prefers the document with more term overlap
  3. stemming and prefix find the same insight (auth -> authentication)
  4. a query with no overlap returns nothing - no cosine noise
  5. the hash fallback still works when FTS5 is missing
  6. the path boost survives, capped so it cannot invent a hit
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hive_functions import memory as M  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def _mem() -> "M.Memory":
    m = M.Memory()
    m.remember_repo_insight(
        "The login flow lives in auth/session.py and validates tokens",
        "auth/session.py")
    m.remember_repo_insight(
        "Tetris score is stored in a module-level variable", "game.js")
    m.remember_repo_insight(
        "Authentication failures are logged to logs/auth.log", "auth/login.py")
    return m


# ── 1. the sanitiser ────────────────────────────────────────────────────────
for raw, why in (
        ('auth" OR 1=1', "quote"),
        ("auth) OR (", "parens"),
        ("*", "wildcard only"),
        ("AND OR NOT", "operators"),
        ("", "empty"),
        ("!!!", "punctuation only")):
    try:
        expr = M._fts_query(raw)
        check(f"1 sanitiser survives {why}", isinstance(expr, str))
    except Exception as exc:                       # noqa: BLE001
        check(f"1 sanitiser survives {why}", False, f"({exc})")
check("1g quotes never reach the expression", '"' not in M._fts_query('a"b').replace('"a"', "").replace('"b"', ""))
check("1h a term gets a prefix wildcard", M._fts_query("authentication") == '"authentication"*')

# ── 2. ranking ─────────────────────────────────────────────────────────────
if not M._fts_available():
    print("  SKIP ranking (this sqlite has no FTS5)")
else:
    m = _mem()
    hits = m.query_repo_insights("authentication failures logs", top_k=2)
    check("2a the best match is first",
          bool(hits) and hits[0]["insight"].startswith("Authentication failures"))
    check("2b the score is scaled to the best hit", bool(hits) and hits[0]["score"] == 1.0)
    check("2c a second hit scores relative to it",
          len(hits) < 2 or 0 < hits[1]["score"] <= 1.0)

    # ── 3. stemming / prefix ───────────────────────────────────────────────
    stem = m.query_repo_insights("auth", top_k=2)
    check("3a 'auth' finds both auth insights", len(stem) == 2,
          f"({[h['insight'][:24] for h in stem]})")
    long_form = m.query_repo_insights("authentication", top_k=2)
    check("3b the long form finds the same insight", bool(long_form))
    phrase = m.query_repo_insights("token validation", top_k=1)
    check("3c a two-word query ranks the token insight",
          bool(phrase) and "tokens" in phrase[0]["insight"])

    # ── 4. no false hits ───────────────────────────────────────────────────
    check("4a a query without overlap returns nothing",
          m.query_repo_insights("zzz-nothing-matches", top_k=3) == [])
    check("4b an empty query returns nothing", m.query_repo_insights("   ") == [])

    # ── 6. path boost ──────────────────────────────────────────────────────
    plain = m.query_repo_insights("auth", top_k=2)
    boosted = m.query_repo_insights("auth", trigger_path="auth/session.py", top_k=2)
    check("6a a matching trigger path ranks its insight first",
          bool(boosted) and boosted[0]["insight"].startswith("The login flow"))
    check("6b the boost does not exceed the scale",
          all(h["score"] <= 1.08 for h in boosted))
    check("6c plain ranking is unaffected",
          plain[0]["insight"].startswith("Authentication failures"))

# ── 5. the hash fallback still answers when FTS5 is absent ─────────────────
_orig = M._FTS_STATE
try:
    M._FTS_STATE = False
    m = _mem()
    fb = m.query_repo_insights("authentication failures", top_k=1)
    check("5a the cosine fallback still returns a hit", bool(fb))
    check("5b the fallback keeps the old score scale",
          bool(fb) and 0.0 < fb[0]["score"] <= 1.1)
finally:
    M._FTS_STATE = _orig

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)




from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

_http_client: "Optional[_httpx.AsyncClient]" = None
_client_lock: "Optional[asyncio.Lock]"        = None


def _ensure_lock() -> "asyncio.Lock":
    global _client_lock
    if _client_lock is None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError(
                "websearch._ensure_lock() called outside an event loop. "
                "Make sure websearch is only used in async contexts."
            )
        _client_lock = asyncio.Lock()
    return _client_lock


async def _get_client_async() -> "_httpx.AsyncClient":
    global _http_client
    async with _ensure_lock():
        if _http_client is None or _http_client.is_closed:
            _http_client = _httpx.AsyncClient(
                timeout=_httpx.Timeout(
                    connect=5.0, read=_FETCH_TIMEOUT, write=10.0, pool=5.0
                ),
                follow_redirects=False,
                # UA-FIX (2026-09-02): a generic "Mozilla/5.0 (compatible;
                # HiveMind-Agent/1.0)" UA gets 403'd by many sites (e.g.
                # Wikipedia, openai.com). Use a real browser UA to reduce false
                # 403s on public pages. Some strict sites still block — see README.
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                                       "Chrome/126.0.0.0 Safari/537.36"},
                limits=_httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
    return _http_client


async def shutdown():
    global _http_client, _client_lock
    if _http_client is not None and not _http_client.is_closed:
        try:
            await _http_client.aclose()
        except Exception:
            pass
    _http_client = None
    _client_lock = None


logger = logging.getLogger("hivemind.websearch")

_SEARXNG_HOST        = "http://localhost:8888"
_SEARXNG_ENABLED     = False
# ENGINE-SET (2026-10-17, replaces the 2026-09-18 pin): the old default pinned
# "bing,duckduckgo", which on a German-market instance answered every English
# query with German shop pages ("Nintendo 64 technical specifications" ->
# nintendo.com/de-de, 24 rounds of retries) and hid the engines that do return
# the right page. Empty = let SearXNG use every engine IT has enabled; the
# shipped settings.yml enables ddg + bing + mojeek and disables the blocked ones.
_SEARXNG_ENGINES     = ""
# Language: "all" made the engines fall back to the instance's region. "en"
# keeps English queries English (the coder writes English); a caller that wants
# otherwise passes its own tag via configure().
_SEARXNG_LANGUAGE    = "en"
_MAX_RESULTS_DEFAULT = 5
_FETCH_TIMEOUT       = 10.0
_SEARCH_TIMEOUT      = 8.0

# UA-FALLBACK (2026-09-02): some sites (Wikipedia) return 403 for browser UAs on
# certain IPs but accept a descriptive bot UA. Used as the second web_fetch
# attempt when the browser UA gets an HTTP 403.
# NO-CONTACT-URL (2026-09-18): no operator URL in the UA — every 403 retry
# sends this header to third-party sites and it must not identify the owner.
_FETCH_UA_FALLBACK = [
    "Mozilla/5.0 (compatible; HiveMind-local/1.0)",
]

_SEARCH_CACHE: dict[str, tuple[float, str]] = {}
_CACHE_TTL = 300

try:
    import trafilatura as _trafilatura
    _HAS_TRAFILATURA = True
except ImportError:
    _trafilatura = None
    _HAS_TRAFILATURA = False


def _silence_trafilatura_logging() -> None:
    """trafilatura emits noisy ERROR/WARNING records whenever fetched content
    is not parseable HTML (e.g. raw.githubusercontent.com source files, JSON,
    PDFs). We have our own fallback for those cases, so mute the library."""
    if not _HAS_TRAFILATURA:
        return
    try:
        for _name in (
            "trafilatura",
            "trafilatura.core",
            "trafilatura.utils",
            "trafilatura.htmlprocessing",
            "trafilatura.downloads",
            "trafilatura.spider",
            "trafilatura.settings",
        ):
            logging.getLogger(_name).setLevel(logging.CRITICAL)
    except Exception:
        pass


_silence_trafilatura_logging()

try:
    import httpx as _httpx
    _HAS_HTTPX = True
except ImportError:
    _httpx = None
    _HAS_HTTPX = False


def configure(
    host: str | None = None,
    enabled: bool | None = None,
    engines: str | None = None,
    language: str | None = None,
):
    global _SEARXNG_HOST, _SEARXNG_ENABLED, _SEARXNG_ENGINES, _SEARXNG_LANGUAGE
    if host     is not None: _SEARXNG_HOST     = host.rstrip("/")
    if enabled  is not None: _SEARXNG_ENABLED  = enabled
    # Allow explicit "" to clear pinned engine/language overrides back to instance defaults.
    if engines  is not None: _SEARXNG_ENGINES  = str(engines).strip()
    if language is not None: _SEARXNG_LANGUAGE = str(language).strip()
    _SEARCH_CACHE.clear()


_RESCUE_ENGINES = "yahoo,duckduckgo web"
_TRACKING_PARAMS = frozenset({
    "msockid", "fbclid", "gclid", "mc_cid", "mc_eid", "igshid",
})
_LOW_SIGNAL_TITLE_RE = re.compile(r"^link to\s+\S+\.\S+$", re.IGNORECASE)
_SITE_OP_RE = re.compile(r"(?:^|\s)(-?site):([^\s\"']+)", re.IGNORECASE)
_QUERY_OP_STRIP_RE = re.compile(r"(?:^|\s)(?:-?site|filetype|inurl|intitle):[^\s\"']+", re.IGNORECASE)
_WS_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "by", "did", "do",
    "does", "for", "from", "how", "in", "is", "it", "its", "of", "on", "or",
    "that", "the", "these", "this", "those", "to", "was", "were", "what",
    "when", "where", "which", "who", "why", "with", "you", "your",
    "aber", "am", "an", "auf", "aus", "bei", "das", "dem", "den", "der",
    "des", "die", "ein", "eine", "einem", "einen", "einer", "eines", "für",
    "fuer", "im", "in", "ist", "mit", "nach", "oder", "sind", "und", "von",
    "vor", "wann", "war", "warum", "was", "welche", "welcher", "wer", "wie",
    "wird", "wo", "zu", "über", "ueber",
})


def _clean_result_url(url: str) -> str:
    """Strip engine tracking parameters (msockid, utm_*, ...) while keeping real query args."""
    raw = str(url or "").strip()
    if not raw or "?" not in raw:
        return raw
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    try:
        parts = urlsplit(raw)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return raw
    kept = [
        (k, v) for k, v in pairs
        if k.lower() not in _TRACKING_PARAMS and not k.lower().startswith("utm_")
    ]
    if len(kept) == len(pairs):
        return raw
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))


def _url_dedup_key(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = p.path.rstrip("/") if p.path != "/" else "/"
        return f"{host}{path}?{p.query}"
    except ValueError:
        return url.strip().lower()


def _dedup_results(results: list) -> list:
    """Clean tracking parameters and merge duplicate URLs returned across engines."""
    by_key: dict[str, dict] = {}
    ordered: list[dict] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        r = dict(item)
        url = _clean_result_url(str(r.get("url") or ""))
        if not url:
            continue
        r["url"] = url
        engines_raw = r.get("engines")
        if not isinstance(engines_raw, (list, tuple, set)):
            eng = str(r.get("engine") or "").strip()
            eng_set = {eng} if eng else set()
        else:
            eng_set = {str(e).strip() for e in engines_raw if str(e).strip()}
        key = _url_dedup_key(url)
        prev = by_key.get(key)
        if prev is None:
            r["_engines_set"] = eng_set
            by_key[key] = r
            ordered.append(r)
        else:
            prev["_engines_set"].update(eng_set)
            if len(str(r.get("title") or "")) > len(str(prev.get("title") or "")):
                prev["title"] = r.get("title", "")
            if len(str(r.get("content") or "")) > len(str(prev.get("content") or "")):
                prev["content"] = r.get("content", "")
            try:
                prev["score"] = max(float(prev.get("score") or 0.0), float(r.get("score") or 0.0))
            except (TypeError, ValueError):
                pass
    return ordered


def _extract_site_constraints(query: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return ``(positive_sites, negative_sites)`` as ``(host, path_prefix)`` pairs."""
    pos: list[tuple[str, str]] = []
    neg: list[tuple[str, str]] = []
    for m in _SITE_OP_RE.finditer(str(query or "")):
        op = m.group(1).lower()
        val = m.group(2).strip().rstrip(".,;:)]}")
        val = re.sub(r"^https?://", "", val, flags=re.IGNORECASE)
        host, _, rest = val.partition("/")
        host = host.lower().strip()
        if host.startswith("www."):
            host = host[4:]
        if not host:
            continue
        path_prefix = ("/" + rest.lstrip("/").rstrip("/")).lower() if rest.strip("/") else ""
        target = (host, path_prefix)
        if op.startswith("-"):
            neg.append(target)
        else:
            pos.append(target)
    return pos, neg


def _url_matches_site(url: str, host_rule: str, path_rule: str) -> bool:
    from urllib.parse import urlsplit
    try:
        p = urlsplit(str(url or ""))
        u_host = (p.hostname or "").lower()
    except ValueError:
        return False
    if u_host.startswith("www."):
        u_host = u_host[4:]
    if not u_host:
        return False
    if u_host != host_rule and not u_host.endswith("." + host_rule):
        return False
    if path_rule:
        u_path = (p.path or "/").lower().rstrip("/")
        if u_path != path_rule and not u_path.startswith(path_rule + "/"):
            return False
    return True


def _filter_site_constraints(query: str, results: list) -> tuple[list, bool]:
    """Enforce explicit ``site:`` / ``-site:`` operators on returned URLs."""
    pos, neg = _extract_site_constraints(query)
    if not pos and not neg:
        return results, False
    out = []
    for r in results:
        u = str(r.get("url") or "")
        if any(_url_matches_site(u, h, p) for h, p in neg):
            continue
        if pos and not any(_url_matches_site(u, h, p) for h, p in pos):
            continue
        out.append(r)
    return out, bool(pos)


def _extract_query_terms(query: str) -> list[str]:
    """Extract deduplicated meaningful content terms from a search query."""
    cleaned = _QUERY_OP_STRIP_RE.sub(" ", " " + str(query or ""))
    raw_tokens = [
        t.strip("._-+#\"'()[]{}:;!?")
        for t in re.findall(r"[a-z0-9\u00c0-\u024f_.+#-]+", cleaned.lower())
    ]
    raw_tokens = [t for t in raw_tokens if t]
    seen: set[str] = set()
    core: list[str] = []
    for t in raw_tokens:
        if len(t) >= 2 and t not in _WS_STOPWORDS and t not in seen:
            seen.add(t)
            core.append(t)
    if core:
        return core
    fallback: list[str] = []
    for t in raw_tokens:
        if t not in seen:
            seen.add(t)
            fallback.append(t)
    return fallback


def _term_in_text(term: str, text_lower: str) -> bool:
    """True when ``term`` matches ``text_lower`` (word-bounded for short tokens)."""
    if not term or not text_lower:
        return False
    if len(term) <= 3 and term.isalnum():
        if re.search(rf"(?<![a-z0-9\u00c0-\u024f]){re.escape(term)}(?![a-z0-9\u00c0-\u024f])", text_lower):
            return True
    elif term in text_lower:
        return True
    parts = [p for p in re.split(r"[._/+\\-]+", term) if len(p) >= 2]
    if len(parts) >= 2:
        return all(_term_in_text(p, text_lower) for p in parts)
    return False


def _rank_and_filter_results(query: str, results: list) -> list:
    """Reject 0-overlap off-topic fallback junk and rank results by query relevance."""
    if not results:
        return []
    terms = _extract_query_terms(query)
    if not terms:
        return results
    scored: list[tuple[float, int, dict]] = []
    for idx, r in enumerate(results):
        title = str(r.get("title") or "")
        url = str(r.get("url") or "")
        snippet = str(r.get("content") or "")
        snippet_clean = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", snippet)).strip()
        tl = title.lower()
        ul = url.lower()
        sl = snippet_clean.lower()

        title_hits = 0
        url_hits = 0
        snip_hits = 0
        matched_any = 0
        for term in terms:
            ht = _term_in_text(term, tl)
            hu = _term_in_text(term, ul)
            hs = _term_in_text(term, sl)
            if ht:
                title_hits += 1
            if hu:
                url_hits += 1
            if hs:
                snip_hits += 1
            if ht or hu or hs:
                matched_any += 1

        if matched_any == 0:
            # Drop completely off-topic fallback pages (e.g. generic portal/shopping junk).
            continue

        try:
            raw_score = min(float(r.get("score") or 0.0), 8.0)
        except (TypeError, ValueError):
            raw_score = 0.0
        eng_count = len(r.get("_engines_set") or ())
        coverage = matched_any / len(terms)
        rank = (
            coverage * 6.0
            + title_hits * 2.5
            + url_hits * 1.2
            + snip_hits * 0.7
            + raw_score * 0.25
            + min(eng_count, 4) * 0.4
            + (0.5 if len(snippet_clean) >= 25 else 0.0)
            - (1.5 if _LOW_SIGNAL_TITLE_RE.match(title.strip()) else 0.0)
        )
        scored.append((rank, -idx, r))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored]


def _build_search_params(
    query: str,
    max_results: int = _MAX_RESULTS_DEFAULT,
    *,
    engines_override: str = "",
) -> dict:

    _params: dict = {"q": query, "format": "json", "safesearch": "1"}
    _eng = str(engines_override or _SEARXNG_ENGINES or "").strip()
    if _eng:
        _params["engines"] = _eng
    if _SEARXNG_LANGUAGE:
        _params["language"] = _SEARXNG_LANGUAGE
    return _params


def _cap_per_host(results: list, cap: int = 2) -> list:
    """Post-filter: a single hostname must not fill the whole result list.
    Without this, one engine having a keyword-salad day (or the github engine
    flooding ~30 repos) buries every other perspective under one domain."""
    from urllib.parse import urlparse as _urlparse
    _seen: dict = {}
    _out: list = []
    for _r in results:
        try:
            _h = (_urlparse(str(_r.get("url", ""))).hostname or "").lower()
        except ValueError:
            _h = ""
        _seen[_h] = _seen.get(_h, 0) + 1
        if _seen[_h] <= cap:
            _out.append(_r)
    return _out



# ── Wikipedia direct (2026-10-17) ───────────────────────────────────────────
# Live finding: a run asked for "Nintendo 64 technical specifications ...
# site:wikipedia.org" eight times, got shop/homepage links from the configured
# engine every time (``site:`` is not honoured by it), and burned the whole
# websearch budget. The instance's wikipedia engine also returns 0 results.
# The MediaWiki search API needs no engine at all, so a ``site:wikipedia.org``
# query - and any query where SearXNG returns nothing - goes there directly.

_WIKI_SITE_RE = re.compile(r"site:\s*([a-z0-9-]{2,12})\.wikipedia\.org", re.IGNORECASE)
_WIKI_SITE_ANY = re.compile(r"site:\s*[a-z.-]*wikipedia\.org", re.IGNORECASE)
_WIKI_UA = ("HiveMind/1.6 (local assistant; +"
            "https://github.com/BoredLuzo/HiveMind_llama)")
# Errors the fallback may swallow (httpx is optional at import time).
_WIKI_ERR = ((_httpx.HTTPError, ValueError, KeyError, TypeError, OSError)
             if _HAS_HTTPX else (ValueError, KeyError, TypeError, OSError))


def _wiki_lang_and_terms(query: str) -> tuple[str, str, bool]:
    """(lang, terms, had_site_operator) for the Wikipedia fallback."""
    q = str(query or "")
    m = _WIKI_SITE_RE.search(q)
    lang = (m.group(1).lower() if m else "en")
    if lang in ("www",):
        lang = "en"
    explicit = bool(_WIKI_SITE_ANY.search(q))
    terms = _WIKI_SITE_ANY.sub(" ", q)
    terms = re.sub(r"\s+", " ", terms).strip() or q.strip()
    return lang, terms, explicit


def _wiki_tokens(text: str) -> set:
    s = re.sub(r"[^a-zA-Z0-9\u00c0-\u024f ]+", " ", str(text or "").lower())
    return {w for w in s.split() if len(w) >= 2}


def _wiki_rank(hits: list, terms: str) -> list:
    """Prefer pages whose title matches the query words ("Nintendo 64" over
    "List of file formats" for a query that starts with Nintendo 64)."""
    qs = _wiki_tokens(terms)
    if not qs:
        return hits

    def _score(h):
        return len(_wiki_tokens(str(h.get("title") or "")) & qs)

    return sorted(hits, key=_score, reverse=True)


async def _wiki_direct(query: str, max_results: int) -> str:
    """Search one Wikipedia via the MediaWiki API. Returns "" on failure."""
    lang, terms, explicit = _wiki_lang_and_terms(query)
    url = f"https://{lang}.wikipedia.org/w/api.php"
    want = max(1, min(int(max_results or 5), 10))
    toks = [w for w in re.findall(r"[^\s]+", terms) if w]
    # Wikipedia search degrades with stuffed queries: "Nintendo 64 technical
    # specifications RDP processor RAM memory" -> "List of file formats", while
    # "Nintendo 64 technical" -> the console page. "MIPS R4300i clock speed"
    # returns nothing at all, "MIPS R4300i" returns the CPU pages. So try the
    # full query and progressively shorter prefixes, keep the set whose top hit
    # actually matches the query words, and stop as soon as two words match.
    attempts: list = []
    for n in (len(toks), 4, 3, 2):
        cand = " ".join(toks[:n]).strip()
        if len(toks[:n]) >= 1 and cand and cand not in attempts:
            attempts.append(cand)
    qs = _wiki_tokens(terms)
    best_hits: list = []
    best_score = -1
    for cand in attempts:
        params = {"action": "query", "list": "search", "srsearch": cand,
                  "srlimit": str(want), "format": "json", "utf8": "1"}
        try:
            # Wikimedia answers 403 to a request without a descriptive UA.
            _client = await _get_client_async()
            resp = await _client.get(
                url, params=params,
                headers={"Accept": "application/json", "User-Agent": _WIKI_UA},
                timeout=_httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0))
            resp.raise_for_status()
            data = resp.json()
        except _WIKI_ERR as _we:
            logger.info("web_search: wikipedia fallback failed (%s)", _we)
            return ""
        hits = (data.get("query") or {}).get("search") or []
        if not hits:
            continue
        ranked = _wiki_rank(hits, terms)
        score = len(_wiki_tokens(str(ranked[0].get("title") or "")) & qs)
        if score > best_score or (score == best_score and len(ranked) > len(best_hits)):
            best_hits, best_score = ranked, score
        if best_score >= 2:
            break
    hits = best_hits[:want]
    if not hits:
        return ""

    from urllib.parse import quote as _q
    lines = [f"Search results for: {query}",
             (f"(source: {lang}.wikipedia.org, direct API search"
              f"{'; site: operator was dropped' if explicit else ''})"), ""]
    for i, h in enumerate(hits, 1):
        title = str(h.get("title") or "").strip()
        snippet = re.sub(r"<[^>]+>", "", str(h.get("snippet") or ""))
        snippet = re.sub(r"\s+", " ", snippet).strip()
        page = f"https://{lang}.wikipedia.org/wiki/{_q(title.replace(' ', '_'))}"
        lines.append(f"[{i}] {title}")
        lines.append(f"    URL: {page}")
        if snippet:
            lines.append(f"    {snippet[:300]}")
        lines.append("")
    first = f"https://{lang}.wikipedia.org/wiki/{_q(str(hits[0].get('title') or '').replace(' ', '_'))}"
    lines.append(f'For full content: web_fetch("{first}")')
    return "\n".join(lines).strip()


async def _fetch_searxng_json(
    query: str,
    max_results: int,
    *,
    engines_override: str = "",
) -> tuple[dict | None, str | None]:
    """Query SearXNG JSON API with one timeout retry. Returns ``(data, error_text)``."""
    for _attempt in range(2):
        try:
            client = await _get_client_async()
            resp = await client.get(
                f"{_SEARXNG_HOST}/search",
                params=_build_search_params(query, max_results, engines_override=engines_override),
                headers={"Accept": "application/json"},
                timeout=_httpx.Timeout(
                    connect=5.0, read=_SEARCH_TIMEOUT, write=5.0, pool=5.0
                ),
            )
            resp.raise_for_status()
            return resp.json(), None
        except _httpx.ConnectError:
            return None, (
                f"[web_search: SearXNG not reachable ({_SEARXNG_HOST}).\n"
                f"Start with: docker run -d --name searxng -p 8888:8080 searxng/searxng]"
            )
        except _httpx.TimeoutException:
            if _attempt == 0:
                logger.warning("web_search: timeout attempt 1 - retry...")
                continue
            return None, f"[web_search: timeout after {_SEARCH_TIMEOUT}s (2 attempts) - SearXNG responds too slowly]"
        except _httpx.HTTPStatusError as e:
            return None, (
                f"[web_search: SearXNG HTTP {e.response.status_code} - "
                f"engines='{engines_override or _SEARXNG_ENGINES}' language='{_SEARXNG_LANGUAGE}'. "
                f"Check the engines configuration or leave engines/language empty.]"
            )
        except Exception as e:
            return None, f"[web_search: error - {type(e).__name__}: {str(e)[:200]}]"
    return None, "[web_search: timeout]"


def _process_searxng_results(query: str, raw_results: list) -> tuple[list, bool]:
    """Deduplicate, strip trackers, enforce ``site:``, rank by relevance, and cap per host."""
    cleaned = _dedup_results(raw_results)
    site_filtered, has_site = _filter_site_constraints(query, cleaned)
    ranked = _rank_and_filter_results(query, site_filtered)
    capped = _cap_per_host(ranked, cap=2)
    return capped, has_site


async def web_search(query: str, max_results: int = _MAX_RESULTS_DEFAULT) -> str:
    if not _SEARXNG_ENABLED:
        return "[web_search: SearXNG disabled — enable it in settings or start SearXNG]"
    if not _HAS_HTTPX:
        return "[web_search: httpx not installed]"

    _cache_key = (
        f"{_SEARXNG_HOST}|{_SEARXNG_ENGINES}|{_SEARXNG_LANGUAGE}|"
        f"{max_results}|{query.strip().lower()}"
    )
    if _cache_key in _SEARCH_CACHE:
        _ts, _cached = _SEARCH_CACHE[_cache_key]
        if time.time() - _ts < _CACHE_TTL:
            logger.debug("web_search CACHE HIT: %r", query)
            return _cached

    # A query that names Wikipedia is an encyclopedia lookup: the MediaWiki API
    # answers it without any search engine, which is exactly the case that used
    # to return shop/homepage links (see _wiki_direct above).
    _has_non_wiki_site = bool(
        _SITE_OP_RE.search(query or "") and not _WIKI_SITE_ANY.search(query or "")
    )
    if (not _has_non_wiki_site
            and (_WIKI_SITE_ANY.search(query or "")
                 or re.search(r"\bwikipedia\b", query or "", re.I))):
        _wd = await _wiki_direct(query, max_results)
        if _wd:
            logger.info("web_search: answered %r from wikipedia direct", query[:80])
            _SEARCH_CACHE[_cache_key] = (time.time(), _wd)
            return _wd

    data, err_msg = await _fetch_searxng_json(query, max_results)
    if err_msg is not None or data is None:
        return err_msg or "[web_search: error]"

    raw_results = data.get("results", []) or []
    unresponsive = data.get("unresponsive_engines", []) or []
    results, has_site = _process_searxng_results(query, raw_results)

    # Rescue pass: if default instance engines failed (CAPTCHA / 429) or only returned
    # off-site / zero-overlap spam (e.g. un-updated container), query healthy fallback engines once.
    if not results and not _SEARXNG_ENGINES and (unresponsive or raw_results):
        rescue_data, _ = await _fetch_searxng_json(query, max_results, engines_override=_RESCUE_ENGINES)
        if rescue_data:
            raw_rescue = rescue_data.get("results", []) or []
            results, has_site = _process_searxng_results(query, raw_rescue)

    if not results:
        # Never substitute Wikipedia articles when the caller explicitly requested another domain.
        if not has_site:
            _wd = await _wiki_direct(query, max_results)
            if _wd:
                _SEARCH_CACHE[_cache_key] = (time.time(), _wd)
                return _wd
        return (
            f"[web_search: No results for '{query}']\n"
            "Do NOT retry the same topic with reshuffled words. Either use a much more "
            "specific query (exact library/product name, version, exact error string) "
            "or continue the task without web data."
        )

    from datetime import datetime as _ws_dt
    _ws_today = _ws_dt.today().strftime("%Y-%m-%d")
    lines = [f"Search results for: {query}\n"
             f"(fetched today: {_ws_today}; pages may carry older "
             "copyright dates - that does not mean today is in the \n past)"]
    _first_url = None
    for i, r in enumerate(results[:max_results], 1):
        title   = r.get("title", "").strip()
        url     = r.get("url", "").strip()
        snippet = r.get("content", "").strip()
        snippet = re.sub(r"<[^>]+>", "", snippet)
        snippet = re.sub(r"\s+", " ", snippet).strip()

        if i == 1 and url:
            _first_url = url

        lines.append(f"[{i}] {title}")
        lines.append(f"    URL: {url}")
        if snippet:
            _snip = snippet[:300]
            if len(snippet) > 300:
                _last = max(_snip.rfind('. '), _snip.rfind('! '), _snip.rfind('? '))
                _snip = _snip[:_last+1] if _last > 100 else _snip
                _snip += '...'
            lines.append(f"    {_snip}")
        lines.append("")

    if _first_url:
        lines.append(f"For full content: web_fetch(\"{_first_url}\")")

    logger.debug("web_search: %d results for %r", len(results[:max_results]), query)
    result = "\n".join(lines).strip()

    _SEARCH_CACHE[_cache_key] = (time.time(), result)
    if len(_SEARCH_CACHE) > 50:
        _oldest = min(_SEARCH_CACHE, key=lambda k: _SEARCH_CACHE[k][0])
        del _SEARCH_CACHE[_oldest]

    return result


def _guard_ip_literal(ip_str: str) -> str | None:
    """IP-literal half of the fetch guard, reusable for RESOLVED addresses
    (audit G3, 2026-10-05). None = allowed, str = block reason."""
    import ipaddress as _ipaddr
    try:
        _ipo = _ipaddr.ip_address(ip_str)
    except ValueError:
        return None  # not an IP literal; nothing to judge here
    if isinstance(_ipo, _ipaddr.IPv6Address) and _ipo.ipv4_mapped:
        _ipo = _ipo.ipv4_mapped
    _cgn = _ipaddr.ip_network("100.64.0.0/10")
    if _ipo.version == 4 and _ipo in _cgn:
        return "private/loopback IPs are not fetchable"
    if (_ipo.is_private or _ipo.is_loopback or _ipo.is_link_local
            or _ipo.is_reserved or _ipo.is_multicast):
        return "private/loopback IPs are not fetchable"
    return None


def _guard_fetch_url(url: str) -> str | None:


    if not url.startswith(("http://", "https://")):
        return f"invalid URL — must start with http:// or https://: {url}"
    from urllib.parse import urlparse as _urlparse
    try:
        _host = (_urlparse(url).hostname or "").lower()
    except Exception:
        _host = ""
    if not _host:
        return f"invalid URL — no hostname: {url}"
    if _host == "localhost" or _host.endswith(".local") or _host.endswith(".internal"):
        return "loopback/local hostnames are not fetchable"
    return _guard_ip_literal(_host)


def _guard_fetch_port(url: str) -> str | None:
    """G3 (audit 2026-10-05): only standard web ports. Without this, a
    fetched URL can probe loopback services (the engine on :8001, SearXNG
    on :8888, anything else local) by hostname."""
    from urllib.parse import urlparse as _urlparse
    _port = _urlparse(url).port
    if _port is not None and _port not in (80, 443):
        return (f"non-standard port {_port} is not fetchable "
                "(web policy: 80/443 only)")
    return None


async def _guard_fetch_resolved(host: str) -> str | None:
    """G3 (audit 2026-10-05): resolve `host` and judge EVERY resolved
    address with the same rules as IP literals. A public name that answers
    with loopback/LAN IPs (wildcard DNS, rebinding) must not become a
    fetch target — the pre-resolution URL guard cannot see this."""
    import socket as _socket

    def _resolve():
        try:
            return _socket.getaddrinfo(host, None)
        except OSError:
            return []  # unresolvable: the request fails on its own

    if hasattr(asyncio, "to_thread"):
        infos = await asyncio.to_thread(_resolve)
    else:  # pragma: no cover - pre-3.9 fallback
        infos = _resolve()
    for _info in infos:
        _addr = str(_info[4][0])
        # strip IPv6 zone index ("fe80::1%eth0") and brackets before parsing
        _err = _guard_ip_literal(_addr.split("%", 1)[0].strip("[]"))
        if _err:
            return _err
    return None


async def _guard_fetch_target(url: str) -> str | None:
    """Full guard for a fetch TARGET: URL checks + port allowlist +
    resolved-IP checks. Used for the initial web_fetch URL and for every
    redirect hop (the hop loop re-runs this, so a redirect to a rebinding
    name is judged on its resolved address too)."""
    _err = _guard_fetch_url(url)
    if _err:
        return _err
    _err = _guard_fetch_port(url)
    if _err:
        return _err
    from urllib.parse import urlparse as _urlparse
    _host = (_urlparse(url).hostname or "").lower()
    if _host:
        return await _guard_fetch_resolved(_host)
    return None


async def web_fetch(url: str, max_chars: int = 4000) -> str:
    if not _HAS_HTTPX:
        return "[web_fetch: httpx not installed]"

    _gerr0 = await _guard_fetch_target(url)
    if _gerr0:
        return f"[web_fetch blocked] {_gerr0}"

    _ua_rounds = [None] + _FETCH_UA_FALLBACK
    for _attempt, _ua in enumerate(_ua_rounds):
        _req_headers = {"User-Agent": _ua} if _ua else None
        try:
            _hop_url = url
            resp = None
            for _hop in range(6):
                _gerr = await _guard_fetch_target(_hop_url)
                if _gerr:
                    return (f"[web_fetch blocked at redirect hop {_hop + 1} - "
                            f"{_hop_url}] {_gerr}")
                client = await _get_client_async()
                # T4 (audit r2): re-resolve RIGHT BEFORE the connect - the
                # classic guard-then-connect TOCTOU (guard query answered
                # public, httpx query answered loopback) now has a sub-second
                # window instead of a per-UA-round one.
                from urllib.parse import urlparse as _up_final
                _final_host = (_up_final(_hop_url).hostname or "").lower()
                if _final_host:
                    _gerr = await _guard_fetch_resolved(_final_host)
                    if _gerr:
                        return f"[web_fetch blocked pre-connect] {_gerr}"
                resp = await client.get(
                    _hop_url,
                    headers=_req_headers,
                    timeout=_httpx.Timeout(
                        connect=5.0, read=_FETCH_TIMEOUT, write=10.0, pool=5.0
                    ),
                    follow_redirects=False,
                )
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    from urllib.parse import urljoin as _urljoin
                    _hop_url = str(_urljoin(_hop_url, resp.headers["location"]))
                    continue
                break
            else:
                return f"[web_fetch: too many redirects (>5) - {url}]"
            resp.raise_for_status()
            body = resp.text
            _content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
            break
        except _httpx.ConnectError:
            return f"[web_fetch: connection error - {url}]"
        except _httpx.TimeoutException:
            if _attempt < len(_ua_rounds) - 1:
                logger.warning("web_fetch: timeout attempt %d - retry: %s", _attempt + 1, url)
                continue
            return f"[web_fetch: timeout after {_FETCH_TIMEOUT}s ({len(_ua_rounds)} attempts) - {url}]"
        except _httpx.HTTPStatusError as e:
            _code = e.response.status_code
            if _code == 403 and _attempt < len(_ua_rounds) - 1:
                logger.warning(
                    "web_fetch: HTTP 403 (%s) - retry with fallback UA: %s",
                    url, _ua_rounds[_attempt + 1],
                )
                continue
            return f"[web_fetch: HTTP {_code} - {url}]"
        except Exception as e:
            return f"[web_fetch: {type(e).__name__}: {str(e)[:200]}]"

    # Only run HTML extraction for real web pages. Raw files (source code,
    # configs, JSON, ...) must NOT go through trafilatura/HTML-stripping:
    # parsing them as HTML spams ERROR logs and their tag-like content gets
    # destroyed by the <[^>]+> removal.
    if _is_html_content(body, _content_type):
        # PHASE-7 (2026-10-14, audit): trafilatura extraction runs in a worker
        # thread — on the event loop a heavy page froze ALL streaming for the
        # whole run.
        text = await asyncio.to_thread(_extract_text, body, url)
    else:
        text = _clean_plain_text(body)

    if not text.strip():
        return f"[web_fetch: no text extractable from {url}]"

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [truncated - {len(text)} chars total. Use a more specific URL or read a section.]"

    return f"[Fetched: {url}]\n\n{text}"


async def check_status() -> dict:
    if not _SEARXNG_ENABLED:
        return {"ok": False, "reason": "disabled", "host": _SEARXNG_HOST,
                "engines": _SEARXNG_ENGINES, "language": _SEARXNG_LANGUAGE}
    if not _HAS_HTTPX:
        return {"ok": False, "reason": "httpx_missing", "host": _SEARXNG_HOST,
                "engines": _SEARXNG_ENGINES, "language": _SEARXNG_LANGUAGE}

    client = await _get_client_async()
    try:
        r = await client.get(
            f"{_SEARXNG_HOST}/healthz",
            timeout=_httpx.Timeout(connect=3.0, read=3.0, write=3.0, pool=3.0),
        )
        if r.status_code < 400:
            try:
                r2 = await client.get(
                    f"{_SEARXNG_HOST}/search",
                    params=_build_search_params("status probe"),
                    headers={"Accept": "application/json"},
                    timeout=_httpx.Timeout(connect=3.0, read=6.0, write=3.0, pool=3.0),
                )
                if r2.status_code < 400:
                    return {"ok": True, "host": _SEARXNG_HOST,
                            "trafilatura": _HAS_TRAFILATURA,
                            "engines": _SEARXNG_ENGINES, "language": _SEARXNG_LANGUAGE}
                return {"ok": False, "reason": "search_http",
                        "status": r2.status_code, "host": _SEARXNG_HOST,
                        "engines": _SEARXNG_ENGINES, "language": _SEARXNG_LANGUAGE,
                        "trafilatura": _HAS_TRAFILATURA}
            except Exception as _se:
                return {"ok": False, "reason": "search_error",
                        "detail": f"{type(_se).__name__}: {str(_se)[:120]}",
                        "host": _SEARXNG_HOST,
                        "engines": _SEARXNG_ENGINES, "language": _SEARXNG_LANGUAGE,
                        "trafilatura": _HAS_TRAFILATURA}
    except Exception:
        pass

    try:
        r = await client.get(
            f"{_SEARXNG_HOST}/search",
            params=_build_search_params("test"),
            timeout=_httpx.Timeout(connect=3.0, read=3.0, write=3.0, pool=3.0),
        )
        if r.status_code < 400:
            return {"ok": True, "host": _SEARXNG_HOST, "trafilatura": _HAS_TRAFILATURA,
                    "engines": _SEARXNG_ENGINES, "language": _SEARXNG_LANGUAGE}
    except Exception:
        pass

    return {"ok": False, "reason": "unreachable", "host": _SEARXNG_HOST,
            "trafilatura": _HAS_TRAFILATURA,
            "engines": _SEARXNG_ENGINES, "language": _SEARXNG_LANGUAGE}


WEB_SEARCH_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current information. Use for: API docs, library references, "
            "recent changelogs, error messages you don't know, framework-specific syntax, "
            "anything that might have changed after your training cutoff. "
            "Returns title + URL + snippet for top results. "
            "ALWAYS follow up with web_fetch on the most relevant URL for full content."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query":       {"type": "string",  "description": "Search query (be specific, e.g. 'FastAPI WebSocket authentication 2024')"},
                "max_results": {"type": "integer", "description": "Number of results to return (default 5, max 10)", "default": 5},
            },
            "required": ["query"],
        },
    },
}

WEB_FETCH_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": "web_fetch",
        "description": (
            "Fetch and read a specific URL. Use AFTER web_search when you need the full content "
            "of a documentation page, GitHub README, or API reference. "
            "Returns cleaned text (no ads, no navigation). Max ~4000 chars."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Full URL to fetch (must start with http:// or https://)"},
            },
            "required": ["url"],
        },
    },
}


def get_tool_defs() -> list:
    return [WEB_SEARCH_TOOL_DEF, WEB_FETCH_TOOL_DEF]


_HTML_SNIFF_RE = re.compile(
    r"<\s*(?:!doctype\s+html|html|head|body|title|meta|link|div|span|"
    r"p\b|a\b|h[1-6]\b|ul\b|ol\b|li\b|table|tr\b|td\b|article|main|"
    r"section|header|footer)\b",
    re.IGNORECASE,
)


def _is_html_content(text: str, content_type: str = "") -> bool:
    """Best-effort decision whether a fetched body should be parsed as HTML."""
    if content_type in ("text/html", "application/xhtml+xml"):
        return True
    # Trust other declared types (text/plain, application/json, ...) - raw
    # files and API payloads are common web_fetch targets.
    if content_type:
        return False
    # No content-type header: sniff the first bytes for real markup.
    return bool(_HTML_SNIFF_RE.search(text[:4096]))


def _clean_plain_text(text: str) -> str:
    """Keep non-HTML bodies (scripts, configs, JSON, READMEs) verbatim."""
    return text.replace("\x00", " ").strip()


def _extract_text(html: str, url: str = "") -> str:
    if _HAS_TRAFILATURA and _trafilatura:
        try:
            result = _trafilatura.extract(
                html, url=url, include_comments=False,
                include_tables=True, no_fallback=False,
            )
            if result and len(result.strip()) > 100:
                return result
        except Exception:
            pass
    return _simple_html_strip(html)


def _simple_html_strip(html: str) -> str:
    text = re.sub(r"<(script|style|nav|header|footer|aside)[^>]*>[\s\S]*?</\1>",
                  "", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text
            .replace("&amp;",  "&").replace("&lt;",   "<")
            .replace("&gt;",   ">").replace("&quot;", '"')
            .replace("&#39;",  "'").replace("&nbsp;", " "))
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()

# -- Safe-Wrapper + Timeout-Resolver (aus server.py extrahiert) ---------------


def _get_websearch_timeout_seconds(profile: str | None = None) -> float:
    """Single place for bounded websearch timeout configuration.
    v0.96.5: Uses a single duo_websearch_timeout_seconds slider (5-60s).
    Profile-based per-profile timeouts deprecated - user controls directly via slider.
    Falls back to profile defaults only if the unified key is missing or < 5."""
    from core import state as _st
    try:
        _base = float(_st.settings.get("duo_websearch_timeout_seconds", 0))
    except Exception:
        _base = 0.0
    # If user set the unified slider, use it directly
    if _base >= 5.0:
        return max(5.0, min(60.0, _base))
    # Fallback: profile-based defaults (migration path for old configs)
    _profile = str(profile or _st.settings.get("duo_runtime_profile", "balanced") or "balanced").strip().lower()
    _defaults = {"fast": 13.0, "critical": 24.0, "balanced": 20.0}
    return _defaults.get(_profile, 20.0)


async def _safe_web_search(query: str, max_results: int, *, phase: str, profile: str | None = None) -> str:
    """Bounded web_search call that never raises to callers."""
    from core import state as _st
    if not _st._WEBSEARCH_AVAILABLE:
        return "[web_search: websearch module unavailable]"
    _timeout_s = _get_websearch_timeout_seconds(profile=profile)
    try:
        _res = await asyncio.wait_for(
            _st._websearch.web_search(query, max_results=max_results),
            timeout=_timeout_s,
        )
        # TAINT-PIPELINE (2026-11-03): text from the pipeline search enters the
        # run prompt without a tool result - mark the source for provenance.
        # This does not force approval cards; the user's toggle governs them.
        try:
            from tools import taint as _taint_ws
            _taint_ws.mark(f"web_search:{phase}")
        except ImportError:
            pass
        return _res
    except asyncio.TimeoutError:
        return f"[web_search: timeout after {_timeout_s:.0f}s ({phase})]"
    except Exception as e:
        return f"[web_search: {type(e).__name__}: {str(e)[:200]}]"


async def _safe_web_fetch(url: str, *, phase: str, profile: str | None = None) -> str:
    """Bounded web_fetch call that never raises to callers."""
    from core import state as _st
    if not _st._WEBSEARCH_AVAILABLE:
        return "[web_fetch: websearch module unavailable]"
    _timeout_s = max(8.0, _get_websearch_timeout_seconds(profile=profile) + 5.0)
    try:
        return await asyncio.wait_for(_st._websearch.web_fetch(url), timeout=_timeout_s)
    except asyncio.TimeoutError:
        return f"[web_fetch: timeout after {_timeout_s:.0f}s ({phase})]"
    except Exception as e:
        return f"[web_fetch: {type(e).__name__}: {str(e)[:200]}]"

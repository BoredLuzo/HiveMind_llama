


from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
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
_SEARXNG_ENGINES     = "google,bing,wikipedia,github"
# "de-DE,en-US" lieferte 400 Bad Request (Live-Test gegen lokalen SearXNG).
_SEARXNG_LANGUAGE    = "all"
_MAX_RESULTS_DEFAULT = 5
_FETCH_TIMEOUT       = 10.0
_SEARCH_TIMEOUT      = 8.0

# UA-FALLBACK (2026-09-02): some sites (Wikipedia) return 403 for browser UAs on
# certain IPs but accept a descriptive bot UA. Used as the second web_fetch
# attempt when the browser UA gets an HTTP 403.
_FETCH_UA_FALLBACK = [
    "Mozilla/5.0 (compatible; HiveMind/1.0; +https://github.com/BoredLuzo/HiveMind_llama)",
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
    # searxng_engines="" and searxng_language="" → engines=&language= in the
    if engines  is not None and str(engines).strip(): _SEARXNG_ENGINES  = engines
    if language is not None and str(language).strip(): _SEARXNG_LANGUAGE = language


def _build_search_params(query: str, max_results: int = _MAX_RESULTS_DEFAULT) -> dict:

    _params: dict = {"q": query, "format": "json"}
    if _SEARXNG_ENGINES:
        _params["engines"] = _SEARXNG_ENGINES
    if _SEARXNG_LANGUAGE:
        _params["language"] = _SEARXNG_LANGUAGE
    return _params


async def web_search(query: str, max_results: int = _MAX_RESULTS_DEFAULT) -> str:
    if not _SEARXNG_ENABLED:
        return "[web_search: SearXNG disabled — enable it in settings or start SearXNG]"
    if not _HAS_HTTPX:
        return "[web_search: httpx not installed]"

    _cache_key = f"{query.strip().lower()}:{max_results}"
    if _cache_key in _SEARCH_CACHE:
        _ts, _cached = _SEARCH_CACHE[_cache_key]
        if time.time() - _ts < _CACHE_TTL:
            logger.debug("web_search CACHE HIT: %r", query)
            return _cached

    for _attempt in range(2):
        try:
            client = await _get_client_async()
            resp = await client.get(
                f"{_SEARXNG_HOST}/search",
                params=_build_search_params(query, max_results),
                headers={"Accept": "application/json"},
                timeout=_httpx.Timeout(
                    connect=5.0, read=_SEARCH_TIMEOUT, write=5.0, pool=5.0
                ),
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except _httpx.ConnectError:
            return (
                f"[web_search: SearXNG not reachable ({_SEARXNG_HOST}).\n"
                f"Start with: docker run -d --name searxng -p 8888:8080 searxng/searxng]"
            )
        except _httpx.TimeoutException:
            if _attempt == 0:
                logger.warning("web_search: timeout attempt 1 - retry...")
                continue
            return f"[web_search: timeout after {_SEARCH_TIMEOUT}s (2 attempts) - SearXNG responds too slowly]"
        except _httpx.HTTPStatusError as e:
            return (
                f"[web_search: SearXNG HTTP {e.response.status_code} - "
                f"engines='{_SEARXNG_ENGINES}' language='{_SEARXNG_LANGUAGE}'. "
                f"Check the engines configuration or leave engines/language empty.]"
            )
        except Exception as e:
            return f"[web_search: error - {type(e).__name__}: {str(e)[:200]}]"
    else:
        return f"[web_search: All attempts failed for '{query}']"

    results = data.get("results", [])
    if not results:
        return f"[web_search: No results for '{query}']"

    lines = [f"Search results for: {query}\n"]
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


def _guard_fetch_url(url: str) -> str | None:


    if not url.startswith(("http://", "https://")):
        return f"invalid URL — must start with http:// or https://: {url}"
    from urllib.parse import urlparse as _urlparse
    import ipaddress as _ipaddr
    try:
        _host = (_urlparse(url).hostname or "").lower()
    except Exception:
        _host = ""
    if not _host:
        return f"invalid URL — no hostname: {url}"
    if _host == "localhost" or _host.endswith(".local") or _host.endswith(".internal"):
        return "loopback/local hostnames are not fetchable"
    try:
        _ipo = _ipaddr.ip_address(_host)
        if isinstance(_ipo, _ipaddr.IPv6Address) and _ipo.ipv4_mapped:
            _ipo = _ipo.ipv4_mapped
        _cgn = _ipaddr.ip_network("100.64.0.0/10")
        if _ipo.version == 4 and _ipo in _cgn:
            return "private/loopback IPs are not fetchable"
        if (_ipo.is_private or _ipo.is_loopback or _ipo.is_link_local
                or _ipo.is_reserved or _ipo.is_multicast):
            return "private/loopback IPs are not fetchable"
    except ValueError:
        pass  # normaler DNS-Hostname
    return None


async def web_fetch(url: str, max_chars: int = 4000) -> str:
    if not _HAS_HTTPX:
        return "[web_fetch: httpx not installed]"

    _gerr0 = _guard_fetch_url(url)
    if _gerr0:
        return f"[web_fetch blocked] {_gerr0}"

    _ua_rounds = [None] + _FETCH_UA_FALLBACK
    for _attempt, _ua in enumerate(_ua_rounds):
        _req_headers = {"User-Agent": _ua} if _ua else None
        try:
            _hop_url = url
            resp = None
            for _hop in range(6):
                _gerr = _guard_fetch_url(_hop_url)
                if _gerr:
                    return (f"[web_fetch blocked at redirect hop {_hop + 1} - "
                            f"{_hop_url}] {_gerr}")
                client = await _get_client_async()
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
    else:
        return f"[web_fetch: all attempts failed - {url}]"

    # Only run HTML extraction for real web pages. Raw files (source code,
    # configs, JSON, ...) must NOT go through trafilatura/HTML-stripping:
    # parsing them as HTML spams ERROR logs and their tag-like content gets
    # destroyed by the <[^>]+> removal.
    if _is_html_content(body, _content_type):
        text = _extract_text(body, url)
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
            params={"q": "test", "format": "json"},
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
        return await asyncio.wait_for(
            _st._websearch.web_search(query, max_results=max_results),
            timeout=_timeout_s,
        )
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

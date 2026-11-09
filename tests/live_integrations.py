# -*- coding: utf-8 -*-
"""Opt-in external integration smoke matrix (C5).

NOT part of the regression gate. Every check needs real credentials, a
running service or a fixture; when its prerequisites are absent it SKIPs
with the reason and touches no network. Nothing secret is ever printed -
tokens are redacted from every message, including transport errors (the
Telegram token is part of the API URL).

    python tests/live_integrations.py --list            # offline: checks + env status
    python tests/live_integrations.py                   # run every configured check
    python tests/live_integrations.py telegram searxng  # run selected checks
    python tests/live_integrations.py --strict          # a SKIP counts as failure

Exit: 0 = no FAIL (skips allowed unless --strict), 1 = a check failed,
2 = unknown check name. docs/integrations.md lists every env var and the
expected result per integration.
"""
from __future__ import annotations

import argparse
import base64
import os
import random
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── env contract (names only; values never printed) ─────────────────────────
ENV_BASE = "HIVEMIND_LIVE_BASE"              # engine URL, e.g. http://127.0.0.1:8001
ENV_TG_TOKEN = "HIVEMIND_TG_TOKEN"           # same var the gateway reads
ENV_TG_OWNER = "HIVEMIND_TG_OWNER_ID"        # numeric Telegram user id of the owner
ENV_TG_SEND = "HIVEMIND_LIVE_TG_SEND"        # =1: also send a test message
ENV_STT_AUDIO = "HIVEMIND_LIVE_STT_AUDIO"    # path to a speech clip (ogg/wav/mp3)
ENV_STT_EXPECT = "HIVEMIND_LIVE_STT_EXPECT"  # optional word the transcript must contain
ENV_TTS = "HIVEMIND_LIVE_TTS"                # =1: synthesise via the engine
ENV_TTS_OUT = "HIVEMIND_LIVE_TTS_OUT"        # optional path to keep the .ogg
ENV_VISION = "HIVEMIND_LIVE_VISION"          # =1: loads a vision model (GPU!)
ENV_VISION_MODEL = "HIVEMIND_LIVE_VISION_MODEL"  # optional direct_model tag
ENV_SEARXNG = "HIVEMIND_LIVE_SEARXNG_URL"    # e.g. http://localhost:8888
ENV_MCP = "HIVEMIND_LIVE_MCP"                # =1: inspect configured MCP servers
ENV_MCP_TOOL = "HIVEMIND_LIVE_MCP_TOOL"      # optional tool name that must be listed
ENV_SEND_FILE = "HIVEMIND_LIVE_SEND_FILE"    # =1: upload a test file to the owner

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

_TOKEN_SHAPE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _flag(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes", "on")


def redact(text: str) -> str:
    """Strip every configured secret and anything token-shaped."""
    out = str(text)
    tok = _env(ENV_TG_TOKEN)
    if tok:
        out = out.replace(tok, "***")
    return _TOKEN_SHAPE.sub("***", out)


def _missing(*names: str) -> str:
    gone = [n for n in names if not _env(n)]
    return ("not configured: set " + ", ".join(gone)) if gone else ""


def _base() -> str:
    return _env(ENV_BASE).rstrip("/")


def _httpx():
    import httpx  # project dependency; imported lazily so --list stays import-light
    return httpx


def _engine_reachable(base: str) -> str:
    """'' when /health answers, else a SKIP reason (engine down is not a FAIL)."""
    httpx = _httpx()
    try:
        r = httpx.get(f"{base}/health", timeout=5.0)
    except httpx.HTTPError as exc:
        return f"engine not reachable at {base} ({type(exc).__name__})"
    if r.status_code != 200:
        return f"engine /health answered HTTP {r.status_code}"
    return ""


# ── checks: each returns (status, message) ──────────────────────────────────
def check_telegram() -> tuple[str, str]:
    why = _missing(ENV_TG_TOKEN, ENV_TG_OWNER)
    if why:
        return SKIP, why
    owner = _env(ENV_TG_OWNER)
    if not owner.lstrip("-").isdigit():
        return FAIL, f"{ENV_TG_OWNER} must be a numeric Telegram user id"
    httpx = _httpx()
    api = f"https://api.telegram.org/bot{_env(ENV_TG_TOKEN)}"
    try:
        me = httpx.get(f"{api}/getMe", timeout=15.0).json()
    except (httpx.HTTPError, ValueError) as exc:
        return FAIL, f"getMe transport error: {type(exc).__name__}"
    if not me.get("ok"):
        return FAIL, f"getMe rejected the token: {me.get('description', 'error')}"
    bot = (me.get("result") or {}).get("username", "?")
    if not _flag(ENV_TG_SEND):
        return PASS, f"token valid (bot @{bot}); set {ENV_TG_SEND}=1 to also message the owner"
    try:
        sent = httpx.post(f"{api}/sendMessage", timeout=15.0, json={
            "chat_id": int(owner),
            "text": "HiveMind integration check: Telegram OK"}).json()
    except (httpx.HTTPError, ValueError) as exc:
        return FAIL, f"sendMessage transport error: {type(exc).__name__}"
    if not sent.get("ok"):
        # Typical: the owner never pressed /start on this bot.
        return FAIL, f"sendMessage to owner failed: {sent.get('description', 'error')}"
    return PASS, f"bot @{bot} delivered a message to the owner"


def check_stt() -> tuple[str, str]:
    why = _missing(ENV_BASE, ENV_STT_AUDIO)
    if why:
        return SKIP, why
    audio = Path(_env(ENV_STT_AUDIO))
    if not audio.is_file():
        return SKIP, f"fixture missing: {ENV_STT_AUDIO} points to no file"
    base = _base()
    down = _engine_reachable(base)
    if down:
        return SKIP, down
    httpx = _httpx()
    t0 = time.time()
    try:
        r = httpx.post(f"{base}/stt", content=audio.read_bytes(),
                       headers={"Content-Type": "application/octet-stream"},
                       timeout=httpx.Timeout(135.0, connect=5.0))
    except httpx.HTTPError as exc:
        return FAIL, f"/stt transport error: {type(exc).__name__}"
    try:
        body = r.json()
    except ValueError:
        return FAIL, f"/stt answered HTTP {r.status_code} without JSON"
    if r.status_code == 409 and body.get("code") == "STT_DISABLED":
        return SKIP, "engine has stt_enabled=false"
    if r.status_code != 200:
        return FAIL, f"/stt HTTP {r.status_code} code={body.get('code')}"
    text = str(body.get("text") or "").strip()
    if not text:
        return FAIL, "/stt returned an empty transcript"
    expect = _env(ENV_STT_EXPECT)
    if expect and expect.lower() not in text.lower():
        return FAIL, f"transcript ({len(text)} chars) lacks the expected word"
    return PASS, (f"{body.get('duration')}s audio -> {len(text)} chars "
                  f"({body.get('language')}) in {time.time() - t0:.1f}s")


def check_tts() -> tuple[str, str]:
    why = _missing(ENV_BASE)
    if why or not _flag(ENV_TTS):
        return SKIP, why or f"opt-in: set {ENV_TTS}=1"
    base = _base()
    down = _engine_reachable(base)
    if down:
        return SKIP, down
    httpx = _httpx()
    t0 = time.time()
    try:
        r = httpx.post(f"{base}/tts", timeout=120.0, json={
            "text": "HiveMind integration check.", "format": "opus"})
    except httpx.HTTPError as exc:
        return FAIL, f"/tts transport error: {type(exc).__name__}"
    if r.status_code != 200:
        try:
            code = (r.json() or {}).get("code")
        except ValueError:
            code = None
        if r.status_code == 409 and code == "TTS_DISABLED":
            return SKIP, "engine has tts_enabled=false"
        return FAIL, f"/tts HTTP {r.status_code} code={code}"
    data = r.content
    if not data.startswith(b"OggS") or len(data) < 1024:
        return FAIL, f"/tts body is not an Ogg/Opus voice note ({len(data)} bytes)"
    out = _env(ENV_TTS_OUT)
    if out:
        Path(out).write_bytes(data)
    return PASS, f"{len(data)} bytes Ogg/Opus in {time.time() - t0:.1f}s" + (
        f", saved to {out}" if out else "")


def _stream(base: str, body: dict, timeout_s: float) -> tuple[list[dict], str]:
    import json
    httpx = _httpx()
    events: list[dict] = []
    tokens: list[str] = []
    with httpx.stream("POST", f"{base}/stream", json=body,
                      timeout=httpx.Timeout(connect=10, read=timeout_s, write=30, pool=5)) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            try:
                ev = json.loads(line[6:])
            except ValueError:
                continue
            events.append(ev)
            if ev.get("type") == "token":
                tokens.append(str(ev.get("content") or ""))
    return events, "".join(tokens)


def check_vision() -> tuple[str, str]:
    why = _missing(ENV_BASE)
    if why or not _flag(ENV_VISION):
        return SKIP, why or f"opt-in (loads a vision model on the GPU): set {ENV_VISION}=1"
    base = _base()
    down = _engine_reachable(base)
    if down:
        return SKIP, down
    from tests.smoke_image import build_png
    # Random code per run: a fixed pattern is hallucinatable (smoke_image audit).
    # Alphabet = glyphs the 5x7 font in smoke_image.py has, minus look-alikes (0/O, 1/I).
    code = "".join(random.choice("ABCDEFTXYZ2345789") for _ in range(4))
    png = build_png(code)
    body: dict = {"q": "Which exact code is written in the attached image? Answer with the code only.",
                  "images": [base64.b64encode(png).decode()], "mode": "chat"}
    if _env(ENV_VISION_MODEL):
        body["direct_model"] = _env(ENV_VISION_MODEL)
    t0 = time.time()
    httpx = _httpx()
    try:
        events, text = _stream(base, body, timeout_s=900)
    except httpx.HTTPError as exc:
        return FAIL, f"/stream transport error: {type(exc).__name__}"
    done = next((e for e in reversed(events) if e.get("type") == "done"), {})
    norm = re.sub(r"[^A-Z0-9]", "", text.upper())
    if code not in norm:
        return FAIL, (f"answer does not name the image code (stop={done.get('stop_reason')}, "
                      f"{len(text)} chars, {time.time() - t0:.0f}s)")
    return PASS, f"model read the image code in {time.time() - t0:.0f}s"


def check_searxng() -> tuple[str, str]:
    why = _missing(ENV_SEARXNG)
    if why:
        return SKIP, why
    url = _env(ENV_SEARXNG).rstrip("/")
    httpx = _httpx()
    try:
        r = httpx.get(f"{url}/search", timeout=20.0,
                      params={"q": "python programming language", "format": "json"})
    except httpx.HTTPError as exc:
        return SKIP, f"SearXNG not reachable ({type(exc).__name__}) - start searxng.bat"
    if r.status_code == 403:
        return FAIL, "SearXNG refused format=json (enable 'json' under search.formats)"
    if r.status_code != 200:
        return FAIL, f"SearXNG HTTP {r.status_code}"
    try:
        results = (r.json() or {}).get("results") or []
    except ValueError:
        return FAIL, "SearXNG answered without JSON"
    if not results:
        return FAIL, "SearXNG returned 0 results (all engines failing/blocked?)"
    return PASS, f"{len(results)} results"


def check_mcp() -> tuple[str, str]:
    why = _missing(ENV_BASE)
    if why or not _flag(ENV_MCP):
        return SKIP, why or f"opt-in: set {ENV_MCP}=1"
    base = _base()
    down = _engine_reachable(base)
    if down:
        return SKIP, down
    httpx = _httpx()
    try:
        data = httpx.get(f"{base}/mcp/servers", timeout=15.0).json()
    except (httpx.HTTPError, ValueError) as exc:
        return FAIL, f"/mcp/servers error: {type(exc).__name__}"
    configured = [s for s in data.get("configured") or [] if s.get("enabled", True)]
    if not data.get("enabled") or not configured:
        return SKIP, "no MCP server enabled in settings (mcp_servers)"
    running = data.get("running") or []
    dead = [s.get("name") for s in running if not s.get("ok")]
    tools = data.get("tools") or []
    if dead:
        return FAIL, f"MCP server(s) dead: {', '.join(map(str, dead))}"
    want = _env(ENV_MCP_TOOL)
    if want and want not in tools:
        return FAIL, f"expected MCP tool not listed ({len(tools)} tools)"
    if not tools:
        # The client connects lazily; zero tools before the first run is a
        # hint, not a proof of failure.
        return FAIL, (f"{len(configured)} server(s) configured but 0 tools listed "
                      "(start one chat run so the client connects, then retry)")
    return PASS, f"{len(configured)} server(s), {len(running)} running, {len(tools)} tools"


def check_send_file() -> tuple[str, str]:
    why = _missing(ENV_TG_TOKEN, ENV_TG_OWNER)
    if why or not _flag(ENV_SEND_FILE):
        return SKIP, why or f"opt-in (uploads a file to the owner): set {ENV_SEND_FILE}=1"
    owner = _env(ENV_TG_OWNER)
    if not owner.lstrip("-").isdigit():
        return FAIL, f"{ENV_TG_OWNER} must be a numeric Telegram user id"
    from core.send_files import SendFileError, read_sendable
    with tempfile.TemporaryDirectory() as ws:
        (Path(ws) / "integration_check.txt").write_text(
            "HiveMind send_file integration check\n", encoding="utf-8")
        (Path(ws) / ".env").write_text("X=1\n", encoding="utf-8")
        # Same guard the gateway runs before uploading: a secret name must be refused.
        try:
            read_sendable(ws, ".env")
            return FAIL, "send_file guard accepted a secret file (.env)"
        except SendFileError:
            pass
        data, name = read_sendable(ws, "integration_check.txt")
    httpx = _httpx()
    api = f"https://api.telegram.org/bot{_env(ENV_TG_TOKEN)}"
    try:
        res = httpx.post(f"{api}/sendDocument", timeout=30.0,
                         data={"chat_id": owner, "caption": "HiveMind send_file check"},
                         files={"document": (name, data)}).json()
    except (httpx.HTTPError, ValueError) as exc:
        return FAIL, f"sendDocument transport error: {type(exc).__name__}"
    if not res.get("ok"):
        return FAIL, f"sendDocument failed: {res.get('description', 'error')}"
    return PASS, f"guard refused .env; {name} ({len(data)} bytes) delivered to the owner"


@dataclass(frozen=True)
class Check:
    name: str
    fn: Callable[[], tuple[str, str]]
    needs: str


CHECKS: tuple[Check, ...] = (
    Check("telegram", check_telegram, f"{ENV_TG_TOKEN}, {ENV_TG_OWNER} [{ENV_TG_SEND}=1]"),
    Check("stt", check_stt, f"{ENV_BASE}, {ENV_STT_AUDIO} [{ENV_STT_EXPECT}], engine stt_enabled"),
    Check("tts", check_tts, f"{ENV_BASE}, {ENV_TTS}=1 [{ENV_TTS_OUT}], engine tts_enabled"),
    Check("vision", check_vision, f"{ENV_BASE}, {ENV_VISION}=1 [{ENV_VISION_MODEL}], GPU"),
    Check("searxng", check_searxng, f"{ENV_SEARXNG}"),
    Check("mcp", check_mcp, f"{ENV_BASE}, {ENV_MCP}=1 [{ENV_MCP_TOOL}], mcp_servers in settings"),
    Check("send_file", check_send_file, f"{ENV_TG_TOKEN}, {ENV_TG_OWNER}, {ENV_SEND_FILE}=1"),
)


def _configured(c: Check) -> bool:
    """Every mandatory env var of the check is set (values never shown)."""
    mandatory = [part.strip().split("=")[0] for part in c.needs.split("[")[0].split(",")]
    names = [m for m in mandatory if m.startswith("HIVEMIND_")]
    return all(_env(n) for n in names)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Opt-in external integration smoke checks.")
    ap.add_argument("checks", nargs="*", help="check names (default: all)")
    ap.add_argument("--list", action="store_true", help="list checks + env status, no network")
    ap.add_argument("--strict", action="store_true", help="a SKIP counts as failure")
    args = ap.parse_args(argv)

    by_name = {c.name: c for c in CHECKS}
    unknown = [n for n in args.checks if n not in by_name]
    if unknown:
        print(f"unknown check(s): {', '.join(unknown)}; known: {', '.join(by_name)}")
        return 2
    selected = [by_name[n] for n in args.checks] if args.checks else list(CHECKS)

    if args.list:
        for c in selected:
            state = "configured" if _configured(c) else "not configured"
            print(f"  {c.name:10s} [{state:14s}] needs: {c.needs}")
        print(f"{len(selected)} check(s); see docs/integrations.md")
        return 0

    counts = {PASS: 0, FAIL: 0, SKIP: 0}
    for c in selected:
        try:
            status, msg = c.fn()
        except (OSError, ValueError, TypeError, KeyError, AttributeError,
                RuntimeError, ImportError) as exc:
            # A crashing check is a FAIL line, never a traceback (a traceback
            # could carry the token-bearing Telegram URL; redact() runs below).
            status, msg = FAIL, f"crashed: {type(exc).__name__}: {exc}"
        counts[status] += 1
        print(f"[{status}] {c.name:10s} {redact(msg)}")
    print(f"\nintegrations: {counts[PASS]} passed, {counts[FAIL]} failed, {counts[SKIP]} skipped")
    if counts[FAIL] or (args.strict and counts[SKIP]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

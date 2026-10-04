# -*- coding: utf-8 -*-
"""Build a ~30-turn chat for the context test (2026-10-04, Sonnet #2).

16d792a7 has two messages - it proves source=json but NOT budget window,
compression or role alternation. This script writes a chat the way the
frontend does (POST /chats, then PUT /chats/{id}):

  - ~30 alternating turns, filler content (each turn unique + grep-able)
  - SECRET-EARLY in turn 3   (user message contains a random code word)
  - SECRET-LATE  near turn 29
  - at >20 messages the compression path engages during the follow-up run

Run C then: POST /stream {chat_id, mode:"chat"} with the question
  "What did I write FIRST in this chat, and what was your last answer
   to me? Quote both exactly."
  - a working BUDGET WINDOW knows SECRET-LATE and may have dropped
    SECRET-EARLY (early known + late forgotten = window broken)
  - the alternation guard must keep gemma-strict templates from
    rejecting the seeded history (role errors would show as 4xx/run
    error, not as a wrong answer)

IMPORTANT: run against the DIRECT model you actually use (agents.direct)
- qwen forgives alternation mistakes, gemma does not.

Usage:
  python tests/make_longchat.py [--base http://localhost:8001]
Prints the chat id + both secrets; store them for grading.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from utils.token import window_by_budget  # noqa: E402


def _req(base: str, method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8001")
    ap.add_argument("--turns", type=int, default=15, help="user turns (x2 = messages)")
    ap.add_argument("--budget", type=int, default=0,
                    help="if set: POST session_history_budget_tokens and print the "
                         "expected windowed message count (window_by_budget, offline)")
    args = ap.parse_args()

    # PROBE WORDS (2026-10-04, Sonnet): hex codes invite paraphrase - the
    # model hallucinated live ("KIVU-E11DB9" vs E14D3B). Words are verbatim-
    # checkable, so a wrong quote is PROOF of context loss, not sloppiness.
    _WORDS = ["ANCHOR", "BANJO", "CANDLE", "DYNAMO", "EMBER", "FALCON"]
    secret_early = "EARLY-" + _WORDS[0] + "-" + str(secrets.randbelow(90) + 10)
    secret_late = "LATE-" + _WORDS[5] + "-" + str(secrets.randbelow(90) + 10)
    msgs = []
    for t in range(1, args.turns + 1):
        if t == 3:
            u = f"Note for later: my early marker word is {secret_early}. Just confirm briefly."
            a = f"Confirmed - your early marker is {secret_early}."
        elif t == args.turns - 1:
            u = f"Another note: my late marker word is {secret_late}. Confirm briefly."
            a = f"Noted - your late marker is {secret_late}."
        else:
            u = f"Filler question {t}: what is {t} + {t * 7}?"
            a = f"{t} + {t * 7} = {t + t * 7}."
        msgs.append({"role": "user", "content": u})
        msgs.append({"role": "assistant", "content": a})

    created = _req(args.base, "POST", "/chats", {
        "title": "longchat-context-test",
        "messages": msgs[:2],
    })
    cid = created["id"]
    _req(args.base, "PUT", f"/chats/{cid}", {"messages": msgs})

    if args.budget:
        _req(args.base, "POST", "/settings", {"session_history_budget_tokens": args.budget})
        kept = window_by_budget([dict(m) for m in msgs], args.budget)
        print(f"[longchat] budget set : session_history_budget_tokens={args.budget}")
        print(f"[longchat] expected   : [HISTORY-SEED] chat={cid} source=json seeded "
              f"{len(kept)} message(s)")
        print(f"[longchat] regime     : {'WINDOW (compression threshold not reached - by design,' if len(kept) <= 20 else 'COMPRESSION (window kept >20, summary block expected,'} "
              f"window first, then compression on the windowed list)")
    print(f"[longchat] chat id   : {cid}")
    print(f"[longchat] messages  : {len(msgs)} ({args.turns} turns)")
    print(f"[longchat] SECRET-EARLY (turn 3) : {secret_early}")
    print(f"[longchat] SECRET-LATE  (turn {args.turns - 1}) : {secret_late}")
    print("\n[longchat] Run C question for /stream {chat_id}:")
    print('  "What was my FIRST note in this chat and what is my LATE marker word? '
          'Quote both exactly."')
    print("[longchat] grading:")
    print(f"  - LATE marker {secret_late} known  -> history reached the model at all")
    print(f"  - EARLY marker {secret_early} forgotten -> budget window works")
    print(f"  - EARLY marker known too -> window NOT dropping (acceptable at big ctx,")
    print(f"    a failure only at small history budgets)")
    print(f"\n[longchat] backup reminder: snapshot the install's sessions/ BEFORE the run.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

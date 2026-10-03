"""History source unification (2026-10-03 continuity fix).

Three pins:
  - load_chat_transcript: the chat json is THE history source; cp/images/
    blob refs are ignored, only user/assistant text comes through, newest
    last, limit respected.
  - the compression summary reaches the prompt: its block is no longer
    role="system" (which _make_messages filters silently).
  - workspace per chat: create stores it, get_chat returns it (json wins
    over the sidecar fallback).

Run: python tests/test_history_source.py
"""
import asyncio
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


def main():
    import core.state as _state
    import context.chat as CC
    from context import compression as CMP
    from routers import chats as C

    ws = Path(tempfile.mkdtemp(prefix="hvm_histsrc_"))
    _state._SESSIONS_DIR = ws
    CC._SESSIONS_DIR = ws
    C._cache_loaded = False
    C._cache_lock = threading.Lock()

    try:
        # ── 1. transcript loader ─────────────────────────────────────────
        cid = "a1b2c3d4"
        big = "y" * 3000
        chat = {
            "id": cid, "title": "t", "created_at": "t", "updated_at": "t",
            "workspace": "C:/proj",
            "messages": [
                {"role": "user", "content": "first question", "ts": 1,
                 "images": [{"preview": "data:image/png;base64,AAA"}]},
                {"role": "assistant", "agent": "Coder", "content": "answer one",
                 "html": "<p>answer one</p>", "ts": 2,
                 "cp": {"f.py": {"content": big, "op": "write", "diffText": "",
                                 "diffs": [{"id": "d", "name": "f", "text": big}],
                                 "view": "file"}}},
                {"role": "user", "content": "follow-up", "ts": 3},
                {"role": "assistant", "agent": "Coder", "content": "final answer", "ts": 4},
            ],
        }
        (ws / "2026-01-01T00-00-00_t_a1b2c3d4.json").write_text(
            json.dumps(chat), encoding="utf-8")

        tr = CC.load_chat_transcript(cid)
        roles = [m["role"] for m in tr]
        if roles == ["user", "assistant", "user", "assistant"]:
            ok("transcript: user/assistant in order, nothing else")
        else:
            fail("transcript roles", str(roles))
        if all(m["content"] in ("first question", "answer one", "follow-up",
                                "final answer") for m in tr):
            ok("transcript: pure text, cp/html/images excluded")
        else:
            fail("transcript purity", json.dumps(tr)[:160])
        tr2 = CC.load_chat_transcript(cid, limit=2)
        if [m["content"] for m in tr2] == ["follow-up", "final answer"]:
            ok("transcript: limit keeps the NEWEST messages")
        else:
            fail("transcript limit", str(tr2))
        if CC.load_chat_transcript("missing00") == []:
            ok("transcript: unknown chat -> empty list")
        else:
            fail("transcript unknown")

        # ── 2. summary block survives the system-role filter ─────────────
        async def _fake_stream(model, msgs, temp, max_tokens):
            yield "compressed summary of older turns"

        CMP._pipeline_chat_stream = _fake_stream
        CMP._registry_get = lambda name: "fake-model"
        msgs = []
        for i in range(25):
            msgs.append({"role": "user", "content": f"q{i} " + "x" * 50})
            msgs.append({"role": "assistant", "content": f"a{i} " + "y" * 50})
        out = asyncio.run(CMP._compress_chat_session(msgs))
        roles_out = [m["role"] for m in out]
        if "system" not in roles_out:
            ok("compressed output has NO system-role message")
        else:
            fail("summary role", str(roles_out))
        joined = " ".join(m["content"] for m in out)
        if "CONVERSATION SUMMARY" in joined and "compressed summary of older turns" in joined:
            ok("summary text reaches the prompt as user-role block")
        else:
            fail("summary content", joined[:160])
        adjacent = [(a, b) for a, b in zip(roles_out, roles_out[1:]) if a == b]
        if not adjacent:
            ok("strict role alternation after summary merge")
        else:
            fail("role alternation", str(adjacent))

        # budget window: newest kept, oldest dropped, under budget
        from utils.token import window_by_budget, CHARS_PER_TOKEN
        long_msgs = [{"role": "user" if i % 2 == 0 else "assistant",
                      "content": ("m%02d " % i) + "z" * 500}
                     for i in range(30)]
        win = window_by_budget(long_msgs, 2000)
        est = sum(len(m["content"]) / CHARS_PER_TOKEN for m in win)
        if win and win[-1]["content"].startswith("m29") and est <= 2000 * 1.1:
            ok(f"budget window keeps newest, drops oldest (kept {len(win)}/30, ~{int(est)} tok)")
        else:
            fail("budget window", f"kept={len(win)} last={win[-1]['content'][:20] if win else None}")

        # history_seed drops a trailing user message (it IS the new prompt)
        seed = CC.history_seed(cid)
        if seed and seed[-1]["role"] == "assistant":
            ok("history_seed drops the trailing user message")
        else:
            fail("history_seed trailing user", str([m['role'] for m in seed]))

        # aborted run: transcript ends user,user -> seed merges, no alternation break
        cid2 = "b2c3d4e5"
        chat2 = {"id": cid2, "title": "aborted", "created_at": "t", "updated_at": "t",
                 "messages": [
                     {"role": "user", "content": "start"},
                     {"role": "assistant", "content": "worked"},
                     {"role": "user", "content": "aborted attempt"},
                     {"role": "user", "content": "retry text"},
                 ]}
        (ws / "2026-01-02T00-00-00_aborted_b2c3d4e5.json").write_text(
            json.dumps(chat2), encoding="utf-8")
        seed2 = CC.history_seed(cid2)
        roles2 = [m["role"] for m in seed2]
        if roles2 == ["user", "assistant", "user"] and seed2[-1]["content"] == "aborted attempt":
            ok("aborted run: trailing retry dropped, alternation intact (u,a,u)")
        else:
            fail("aborted seed", str(roles2) + " / " + str(seed2[-1]["content"][:40]))

        # window_by_budget hard-filters tool roles / tool_calls (no half pairs)
        from utils.token import window_by_budget
        mixed = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1", "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "content": "result1", "tool_call_id": "t1"},
            {"role": "assistant", "content": "a2"},
        ]
        win = window_by_budget(mixed, 4000)
        bad = [m for m in win if m.get("role") not in ("user", "assistant")
               or m.get("tool_calls")]
        if not bad and [m["content"] for m in win] == ["q1", "a2"]:
            ok("budget window hard-filters tool roles and tool_calls")
        else:
            fail("window hard filter", json.dumps(win)[:120])

        # ── 3. workspace per chat ────────────────────────────────────────
        created = asyncio.run(C.create_chat(_FakeRequest({
            "title": "ws test", "workspace": "C:/proj/FireWork",
            "messages": [{"role": "user", "content": "hello"}],
        })))
        new_id = created["id"]
        got = asyncio.run(C.get_chat(new_id))
        if got.get("workspace") == "C:/proj/FireWork":
            ok("create_chat stores + get_chat returns the workspace")
        else:
            fail("workspace roundtrip", repr(got.get("workspace")))

        req = _FakeRequest({"messages": [{"role": "user", "content": "v2"}],
                            "workspace": "C:/proj/Other"})
        asyncio.run(C.update_chat(new_id, req))
        got2 = asyncio.run(C.get_chat(new_id))
        if got2.get("workspace") == "C:/proj/Other" and \
                got2["messages"][0]["content"] == "v2":
            ok("update_chat carries workspace + messages")
        else:
            fail("workspace update", repr(got2.get("workspace")))

    finally:
        shutil.rmtree(str(ws), ignore_errors=True)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


if __name__ == "__main__":
    sys.exit(1 if main() else 0)

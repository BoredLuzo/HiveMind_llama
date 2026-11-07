# -*- coding: utf-8 -*-
"""Behavior tests for context/compression.py - the no-LLM surface.

Only the pure helpers and the deterministic paths are exercised:
rule-based compression, whole-drop integrity, recall-marker eviction with
a fake ToolContextLRU, summary validation, the local fallback summary and
_compress_tool_context/_compress_chat_session in local-only/fallback mode
(the LLM POST is replaced by failing/recording fake clients). No model,
no network.

Run: python tests/test_compression_behavior.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import context.compression as C  # noqa: E402
from context.compression import (  # noqa: E402
    _build_local_fallback_summary,
    _compress_chat_session,
    _compress_rule_based,
    _compress_tool_context,
    _drop_whole_oldest,
    _evict_stale_tool_outputs,
    _merge_drop_marker,
    _validate_compression_summary,
    _weighted_ttl,
    evict_stale_reads_for_path,
    init_compression,
)
from hive_functions.memory import _recall_marker  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


class FakeLRU:
    """ToolContextLRU stand-in: entry dicts with idx/path/kind/evicted."""

    def __init__(self, entries):
        self._entries = [dict(e) for e in entries]
        self.evicted_idx = []

    def alive_by_path(self, path, kind):
        return [e for e in self._entries
                if e.get("path") == path and e.get("kind") == kind
                and not e.get("evicted")]

    def candidates(self):
        return [e for e in self._entries if not e.get("evicted")]

    def mark_evicted(self, idx):
        self.evicted_idx.append(idx)
        for e in self._entries:
            if e.get("idx") == idx:
                e["evicted"] = True


class FakeCompressClient:
    """httpx.AsyncClient stand-in for the compression POST."""

    def __init__(self, payload=None, fail=False):
        self.payload = payload or {}
        self.fail = fail
        self.calls = []

    async def post(self, url, **kw):
        self.calls.append({"url": url, **kw})
        if self.fail:
            raise RuntimeError("connect failed")
        return self

    def json(self):
        return self.payload


def _read_tool(idx, path, content):
    return {"idx": idx, "path": path, "kind": "read_file"}


async def amain():
    # ── _weighted_ttl ────────────────────────────────────────────────────────
    check("ttl: error payload doubles the TTL",
          _weighted_ttl("Traceback (most recent call last)", 300) == 600)
    check("ttl: normal payload keeps the TTL",
          _weighted_ttl("plain tool output", 300) == 300)

    # ── evict_stale_reads_for_path: in-place marker + exclude_idx ───────────
    msgs = [
        {"role": "user", "content": "read the file"},
        {"role": "tool", "name": "read_file", "content": "OLD CONTENT ONE"},
        {"role": "tool", "name": "read_file", "content": "OLD CONTENT TWO"},
    ]
    lru = FakeLRU([_read_tool(1, "src/app.py", "read_file"),
                   _read_tool(2, "src/app.py", "read_file")])
    n = evict_stale_reads_for_path(messages=msgs, lru=lru, path="src/app.py",
                                   exclude_idx=2)
    check("evict_path: newest read kept via exclude_idx", n == 1, f"n={n}")
    check("evict_path: older read replaced with recall marker",
          msgs[1]["content"] == _recall_marker("src/app.py"))
    check("evict_path: excluded read untouched",
          msgs[2]["content"] == "OLD CONTENT TWO")
    check("evict_path: LRU entries marked evicted", lru.evicted_idx == [1])

    # ── evict_stale_reads_for_path: cache-horizon keeps bytes stable ────────
    msgs2 = [
        {"role": "tool", "name": "read_file", "content": "PREFIX CACHED READ"},
        {"role": "user", "content": "next turn"},
    ]
    lru2 = FakeLRU([_read_tool(0, "src/old.py", "read_file")])
    sup = []
    n2 = evict_stale_reads_for_path(messages=msgs2, lru=lru2,
                                    path="src/old.py", cache_horizon=1,
                                    superseded=sup)
    check("evict_path: horizon read not rewritten in place",
          n2 == 0 and msgs2[0]["content"] == "PREFIX CACHED READ")
    check("evict_path: horizon read collected as superseded",
          sup == ["src/old.py"], f"{sup}")
    check("evict_path: horizon LRU entry still marked", lru2.evicted_idx == [0])

    # ── evict_stale_reads_for_path: empty inputs are no-ops ─────────────────
    check("evict_path: no messages -> 0",
          evict_stale_reads_for_path(messages=[], lru=FakeLRU([]),
                                     path="x.py") == 0)
    check("evict_path: no path -> 0",
          evict_stale_reads_for_path(messages=msgs, lru=FakeLRU([]),
                                     path="") == 0)

    # ── _evict_stale_tool_outputs: budget-driven masking ────────────────────
    big = "y" * 3000
    msgs3 = [
        {"role": "user", "content": "do the thing"},
        {"role": "tool", "name": "read_file", "content": big},
    ]
    lru3 = FakeLRU([{"idx": 1, "path": "src/big.py", "kind": "read_file"}])
    n3 = _evict_stale_tool_outputs(messages=msgs3, lru=lru3,
                                   target_token_budget=600,
                                   hard_floor_tokens=100)
    check("evict_tools: over-budget tool output masked", n3 == 1, f"n={n3}")
    check("evict_tools: masked content is the recall marker",
          msgs3[1]["content"] == _recall_marker("src/big.py"))
    check("evict_tools: LRU entry marked", lru3.evicted_idx == [1])
    lru4 = FakeLRU([{"idx": 1, "path": "src/big.py", "kind": "read_file"}])
    small = [{"role": "user", "content": "tiny"},
             {"role": "tool", "name": "x", "content": "ok"}]
    check("evict_tools: under budget -> untouched",
          _evict_stale_tool_outputs(messages=small, lru=lru4,
                                    target_token_budget=10_000,
                                    hard_floor_tokens=100) == 0
          and lru4.evicted_idx == [])

    # ── _evict_stale_tool_outputs: protected working-set file survives ──────
    lru5 = FakeLRU([{"idx": 1, "path": "src/big.py", "kind": "read_file"}])
    focus = [{"role": "user", "content": "edit it"},
             {"role": "tool", "name": "read_file",
              "content": "reading C:/repo/src/big.py now\n" + big}]
    n5 = _evict_stale_tool_outputs(messages=focus, lru=lru5,
                                   target_token_budget=600,
                                   hard_floor_tokens=100,
                                   protect_paths=["src/big.py"])
    check("evict_tools: protected focus file never masked",
          n5 == 0 and lru5.evicted_idx == [] and "y" * 100 in focus[1]["content"])

    # ── _drop_whole_oldest: turn integrity + protected anchors ─────────────
    def tc_msg(i, tid):
        return {"role": "assistant",
                "tool_calls": [{"id": tid, "function": {"name": "read_file",
                                                        "arguments": "{}"}}]}
    base = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first task"},
        tc_msg(2, "a1"),
        {"role": "tool", "tool_call_id": "a1", "content": "C:/tmp/res1.txt"},
        {"role": "user", "content": "second question"},
        tc_msg(5, "b2"),
        {"role": "tool", "tool_call_id": "b2", "content": "C:/tmp/res2.txt"},
    ]
    out, dropped, evicted = _drop_whole_oldest(list(base), 0)
    check("drop_whole: target 0 keeps only protected anchors",
          out == [base[0], base[1]] and dropped == 5, f"{dropped}")
    check("drop_whole: read paths collected for the read guard",
          "C:/tmp/res1.txt" in evicted and "C:/tmp/res2.txt" in evicted,
          f"{evicted}")

    def _no_orphans(out_list):
        """Invariant: a tool result never survives without its call."""
        asst_ids = {tc.get("id")
                    for m in out_list if m.get("role") == "assistant"
                    for tc in (m.get("tool_calls") or [])}
        return all(m.get("tool_call_id") in asst_ids
                   for m in out_list if m.get("role") == "tool")

    # mid budget: the head turn is dropped as ONE unit; the second
    # tool_calls turn survives intact - a tool result is never orphaned.
    out2, dropped2, _ = _drop_whole_oldest(list(base), 14)
    check("drop_whole: surviving tail keeps its tool_calls pair",
          any(m.get("role") == "assistant" and m.get("tool_calls")
              for m in out2)
          and any(m.get("role") == "tool" for m in out2)
          and dropped2 == 3, f"dropped={dropped2}")
    check("drop_whole: tool result never orphaned from its call",
          _no_orphans(out2) and _no_orphans(out))
    check("drop_whole: recent tail dropped before protected head",
          base[4] not in out2 and base[0] in out2)
    # NOTE: the target is int()-truncated, so it must EXCEED the estimate
    # to guarantee that nothing is dropped.
    out_full, dropped_full, _ = _drop_whole_oldest(
        list(base), int(C._estimate_ctx_tokens(list(base))) + 1)
    check("drop_whole: budget above estimate keeps everything",
          out_full == list(base) and dropped_full == 0)

    out3, dropped3, _ = _drop_whole_oldest(
        list(base), 0, protect_prefix=len(base))
    check("drop_whole: fully protected input drops nothing",
          out3 == list(base) and dropped3 == 0)

    # ── _merge_drop_marker ──────────────────────────────────────────────────
    merged = [{"role": "system", "content": "sys"},
              {"role": "user", "content": "question"}]
    _merge_drop_marker(merged, 2)
    check("marker: prepended to first user message",
          merged[1]["content"].startswith("[CONTEXT DROPPED: 2 older message"))
    synthetic = [{"role": "system", "content": "sys"}]
    _merge_drop_marker(synthetic, 1)
    check("marker: synthetic user turn inserted when none left",
          len(synthetic) == 2 and synthetic[1]["role"] == "user")
    untouched = [{"role": "user", "content": "q"}]
    _merge_drop_marker(untouched, 0)
    check("marker: dropped=0 is a no-op", untouched[0]["content"] == "q")

    # ── _compress_rule_based ────────────────────────────────────────────────
    # (a) phase 1: big tool output masked; the drive-letter path is what
    #     _RE_READ_PATH understands, and it feeds the read-guard list.
    msgs4 = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "implement feature"},
        {"role": "user", "content": "[RUNTIME NOTICE] transient hiccup"},
        {"role": "tool", "name": "read_file",
         "content": "reading C:/repo/src/app.py\n" + "z" * 3000},
        {"role": "user", "content": "continue"},
    ]
    out4, paths4 = _compress_rule_based(msgs4, target_tokens=400,
                                        keep_recent_msgs=1)
    joined = "\n".join(str(m.get("content", "")) for m in out4)
    check("rule_based: big tool output masked with recall marker",
          _recall_marker("C:/repo/src/app.py") in joined)
    check("rule_based: first user task survives",
          any(m.get("content") == "implement feature" for m in out4))
    check("rule_based: evicted paths returned for read guard",
          "C:/repo/src/app.py" in paths4, f"{paths4}")

    # (b) phase 2: notice messages dropped once masking is not enough.
    msgs4b = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "implement feature"},
        {"role": "user", "content": "[RUNTIME NOTICE] transient hiccup"},
        {"role": "user", "content": "continue"},
    ]
    out4b, paths4b = _compress_rule_based(msgs4b, target_tokens=10,
                                          keep_recent_msgs=0)
    check("rule_based: notice message removed",
          not any("[RUNTIME NOTICE]" in str(m.get("content", ""))
                  for m in out4b))
    check("rule_based: non-notice turns kept in phase 2",
          any(m.get("content") == "implement feature" for m in out4b)
          and any(m.get("content") == "continue" for m in out4b))
    check("rule_based: empty input -> empty result",
          _compress_rule_based([], 100) == ([], []))

    # ── _validate_compression_summary ───────────────────────────────────────
    good = ("We implemented the login flow. Files changed: app.py now exports "
            "the new handler; tests were updated. Remaining: polish errors.")
    check("validate: valid summary accepted",
          _validate_compression_summary(good, ["src/app.py"], []) is True)
    check("validate: too-short summary rejected",
          _validate_compression_summary("too short", [], []) is False)
    check("validate: hallucinated completion rejected",
          _validate_compression_summary(
              "x" * 90 + " task complete for all modules", [], []) is False)
    check("validate: missing written file rejected",
          _validate_compression_summary("x" * 120, ["src/other.py"], []) is False)
    parts = "alpha beta gamma delta"
    check("validate: dropped partition labels rejected",
          _validate_compression_summary("x" * 120 + " alpha", [], [],
                                        known_partitions=parts.split()) is False)
    check("validate: plan anchor preserved -> accepted",
          _validate_compression_summary(
              "x" * 100 + " refactor login flow module", [], [],
              plan_anchor="refactor login flow") is True)
    check("validate: plan anchor dropped -> rejected",
          _validate_compression_summary("x" * 120, [], [],
                                        plan_anchor="refactor login flow") is False)

    # ── _build_local_fallback_summary ───────────────────────────────────────
    fb = _build_local_fallback_summary("build the parser", ["a.py", "b.py"],
                                       ["step one", "step two"],
                                       'partition = "auth"\npartition = "db"')
    check("fallback: keeps original task", "build the parser" in fb)
    check("fallback: keeps written files", "a.py" in fb and "b.py" in fb)
    check("fallback: keeps partition labels",
          "partition: auth" in fb and "partition: db" in fb)

    # ── _compress_tool_context: local-only, plan reinject, tail cap ─────────
    msgs5 = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "implement feature X"},
        {"role": "assistant", "content": "exploring", "tool_calls": []},
        {"role": "tool", "name": "read_file",
         "content": "reading C:/repo/src/app.py\n" + "q" * 400},
        {"role": "user", "content": "keep going"},
        {"role": "assistant", "content": "working"},
        {"role": "tool", "name": "run_bash", "content": "t" * 300},
    ]
    client = FakeCompressClient()
    out5, condensed, usage = await _compress_tool_context(
        msgs5, "m", 1, client, "sysprompt", "implement feature X",
        ["src/app.py"], ["step 1"], local_only=True,
        plan_anchor_text="Step 1: wire the handler",
        tail_tool_max_chars=64, keep_recent_msgs=2)
    check("tool_ctx: no HTTP call in local-only mode", client.calls == [])
    check("tool_ctx: result starts with system + summary",
          out5[0]["role"] == "system" and out5[1]["role"] == "user")
    check("tool_ctx: plan anchor reinjected into summary",
          "[PLAN - must continue" in out5[1]["content"]
          and "Step 1: wire the handler" in out5[1]["content"])
    check("tool_ctx: task line present",
          "TASK: implement feature X" in out5[1]["content"])
    check("tool_ctx: read paths condensed", "C:/repo/src/app.py" in condensed,
          f"{condensed}")
    check("tool_ctx: usage empty without an LLM call", usage == {})
    tail = out5[-1]["content"]
    check("tool_ctx: tail tool output capped, tail kept",
          tail.startswith("[... first ") and tail.endswith("t" * 64),
          f"len={len(tail)}")

    # ── _compress_tool_context: noop when nothing is condensable ────────────
    noop_msgs = [{"role": "system", "content": "sys"},
                 {"role": "user", "content": "hi"}]
    out6, cond6, meta6 = await _compress_tool_context(
        noop_msgs, "m", 1, FakeCompressClient(), "sys", "hi", [], [],
        keep_recent_msgs=1)
    check("tool_ctx: nothing condensable -> original returned",
          out6 is noop_msgs and meta6 == {"noop": True})

    # ── _compress_tool_context: LLM failure -> local fallback summary ───────
    out7, _, _ = await _compress_tool_context(
        msgs5, "m", 1, FakeCompressClient(fail=True), "sys",
        "implement feature X", ["src/app.py"], [], local_only=False)
    check("tool_ctx: failed POST falls back to local summary",
          "State Reconstruction (compression model unavailable)"
          in out7[1]["content"])

    # ── _compress_tool_context: successful LLM summary + usage accounting ───
    okc = FakeCompressClient(payload={
        "choices": [{"message": {"content": "DENSE SUMMARY TEXT"}}],
        "usage": {"completion_tokens": 42, "prompt_tokens": 100},
    })
    out8, _, usage8 = await _compress_tool_context(
        msgs5, "m", 1, okc, "sys", "implement feature X", [], [])
    check("tool_ctx: LLM summary lands in the summary message",
          "DENSE SUMMARY TEXT" in out8[1]["content"])
    check("tool_ctx: usage tokens tracked",
          usage8["completion_tokens"] == 42 and usage8["prompt_tokens"] == 100
          and "gen_ms" in usage8, f"{usage8}")
    check("tool_ctx: compression POST hit the local endpoint",
          okc.calls[0]["url"].startswith("http://127.0.0.1:1/v1/chat/completions"))

    # ── _compress_chat_session: threshold + fallback wiring ─────────────────
    init_compression(settings_obj={})  # default threshold 20
    short = [{"role": "user", "content": f"u{i}"} for i in range(5)]
    check("session: under threshold -> unchanged",
          await _compress_chat_session(short) is short)

    def session(n):
        out = []
        for i in range(n):
            role = "user" if i % 2 == 0 else "assistant"
            out.append({"role": role, "content": f"{role} message {i} "
                                                "with some padding text"})
        return out

    long_sess = session(30)
    out9 = await _compress_chat_session(long_sess)
    check("session: long session compressed to the keep window",
          len(out9) == C.SESSION_COMPRESS_KEEP, f"len={len(out9)}")
    first_user = next(m for m in out9 if m.get("role") == "user")
    check("session: fallback summary merged (user-role block)",
          "[CONVERSATION SUMMARY - 24 older messages compressed]"
          in first_user["content"], f"{first_user['content'][:80]}")
    check("session: newest turns preserved",
          out9[-1]["content"].startswith("assistant message 29"))

    # threshold override honored
    init_compression(settings_obj={"session_compress_threshold": 100})
    check("session: raised threshold disables compression",
          await _compress_chat_session(long_sess) is long_sess)
    init_compression(settings_obj={})

    return h.finish("compression_behavior")


def main():
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())

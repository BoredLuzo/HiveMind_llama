# Why HiveMind Processes More Tokens (and What Caching Does)

## The short version

HiveMind is not worse in quality: Direct 92.1%, Aider 90.5%, coder-alone
95.2% on the 21-task corpus; the paired difference is not significant
(McNemar p = 1.0). The difference is **cumulative token processing**:
21,228 tokens/cell for Direct, 108,378 for the coder, 946 for Aider.

That number is **not** "unique content" and **not** a local billing unit.
It is the sum of `usage.prompt_tokens + usage.completion_tokens` over every
tool round. Each round resends the conversation; ten rounds x ~10k prompt
tokens = ~100k. Aider reports a single small request.

## Where the tokens go

| Component | Per round | Why |
|---|---:|---|
| Tool schema (`direct_full`) | ~5,050 tokens | 33 tools, 20,211 chars |
| System prompt + memory + date anchors | ~2,000-4,000 | shared context |
| Growing conversation | grows | prior assistant/tool messages are resent |
| Tool results (file contents) | varies | whole files, not line ranges |
| Self-verification | extra rounds | writes a smoke test when no test suite is visible |

The coder adds a second effect: the benchmark tasks have **hidden tests**,
so HiveMind cannot run the real check. Its auto-test path sees "no test
suite" and nudges the coder to write a minimal smoke test, run it, and
iterate. Aider simply edits and stops.

## Does prompt caching help?

Yes - for **time and VRAM**, not for the reported token count.

The logs show, for example:

```
[CACHE] round=qwen3.5:4b-mtp prompt=9029 cached=8543 reuse=95%
[PERF]  p_tok=9029 cache_n=8543 prompt_n=486 prompt_ms=2936
```

- The full prompt is 9,029 tokens, but only 486 are new; 95% come from the
  llama.cpp prefix cache.
- `cache_prompt=True` is set by the ToolLoop; the message list only grows at
  the tail (system -> history -> user -> tool results), so the prefix stays
  reusable.
- The server still reports `prompt_tokens=9029`; the cached subset is a
  separate counter. That is why caching visibly shortens runs (a 58 s coder
  run would otherwise be much slower) but does not shrink the benchmark's
  token column.

**A fair cost comparison should report** wall time, rounds, cache-adjusted
prefill (`prompt_n` / `cache_n`), and unique content tokens - not just the
cumulative prompt count.

## Is Aider more token-efficient?

Structurally, yes:

- no 33-tool schema per round;
- one small request with a repo map / relevant files;
- an edit format instead of a multi-round tool loop;
- no self-test loop on tasks without a visible test suite.

Caveat: Aider's 946 is parsed from its own "Tokens: X sent, Y received"
line. If it makes several requests, the parser may undercount. The order of
magnitude is still real: Direct's minimum of ~15.5k is almost exactly
3 rounds x the 5k schema.

## What to improve

1. **Do not force a smoke test when no test suite exists.** The auto-test
   nudge is the largest avoidable coder cost on hidden-test tasks. Cap it at
   one attempt or skip it when `run_tests` finds nothing.
2. **Adaptive round/token budget.** Small tasks: few rounds; stop after a
   successful edit plus a visible test pass. Do not run to `until_finished`
   by default for assistant-sized work.
3. **Tier the tool schema for the coder.** The coder now defaults to
   `duo_core` (17 tools: read/edit/run/tests + `lsp_diagnostics`, ~2.8k
   schema tokens) instead of `duo_full` (33 tools, ~5k);
   `duo_coder_tool_tier=full` opts into git/web/MCP/browser/LSP extras.
   Direct has `direct` (3), `direct_research` (web+read, ~1.5k),
   `direct_python` (18), `direct_full` (33).
4. **Line-range reads.** `read_file(start_line, end_line)` instead of whole
   files; reads are valuable, but they should be cheap.
5. **Do not re-read the same range.** The shared no-progress guard
   (`core/tool_guards.py`) nudges identical tool calls once; extend it to
   repeated reads of the same path/range if needed.
6. **Keep the cache prefix stable.** Put volatile memory/editor blocks after
   the stable system+tools prefix; keep tool ordering byte-stable.
7. **Measure the right thing.** Add `rounds`, `prompt_n`, `cache_n` and
   `unique_tokens` to the result JSON so the next report can separate
   compute from cumulative prompt size.

## Bottom line

Caching already saves most of the prefill work (95% reuse in the logs), so
the coder's 58 s is not the naive 100k-prefill cost. The remaining problem is
structural: many rounds and a large schema for small tasks. Fix the
verification nudge, add a coder tool tier, and report cache-adjusted cost;
then the comparison with Aider becomes fair in both quality and effort.

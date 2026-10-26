# Audit — the changes just made, and the judge / decision-model question

Two parts: (A) the per-mode guard and the 1.4.0 release prep I finished minutes
ago, (B) the decision-model / judge idea — is it wired, and where would it go.

---

# A. The changes from this session

## A.1 Per-mode tool guard — verified call sites

`_mode_blocks()` only knows what the caller tells it, so every dispatcher call
site matters. All of them, read out of the code:

| Call site | `tool_mode` passed | Covered |
|---|---|---|
| `core/tool_exec_helpers.py:48` (prefetch) | `tool_mode` (propagated) | yes |
| `core/tool_exec_helpers.py:763/769` (`_execute_one_tool`) | `tool_mode` (propagated) | yes |
| `core/tool_executor.py:644` (auto-test) | `tool_mode` (propagated) | yes |
| `core/tool_loop.py:602` (agentic loop) | `self.cfg.tool_mode` | yes |
| `core/duo_runner.py:6634` (critic) | `"critic_verify"` | yes |
| `core/fix_agent.py:231` | `"fix_agent"` | yes |
| `core/subagent_lite.py:219` | **none** | no — but it has its own `_ALLOWED_TOOLS` allowlist check *before* the dispatch, so the hole is closed by a different door |

So the guard is effective in every path that passes a mode, and the one path that
does not has an equivalent check of its own. Behaviour table (also in the test):
`write_file` blocked in `direct`/`duo_readonly`, allowed in `duo_full`/`fix_agent`;
`run_bash` allowed in `critic_verify` (it is in that allowlist); `read_file` never
blocked; unknown mode → no invented policy.

## A.2 New finding (not fixed): the subagent still swallows broken arguments

`core/subagent_lite.py:211-214`:

```python
try:
    args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
except Exception:
    args = {}
```

That is exactly the 2.4 bug I fixed in the agentic loop — broken JSON becomes "no
arguments" and the tool runs on defaults. The subagent is read-only, so the blast
radius is small (a `list_dir` on the workspace root instead of the intended
folder), but it is the same class and should get the same guard. Not touched now
because this session's change set is the 1.4.0 release.

## A.3 Release prep — consistency checked

| Item | State |
|---|---|
| `server.py` `HIVEMIND_VERSION` | `1.4.0` |
| CHANGELOG top | `## [1.4.0] - 2026-10-17` (matches - gate check b) |
| `RELEASE_NOTES_1.4.0.md` | present, used as the release body |
| Zip | 288 entries, `BUILD_INFO` embedded, leak check clean |
| Export-ignored | `/tests/`, `docs/plan_1.4_voice.md`, `docs/roadmap_1.4.md` (verified **inside** the built zip: both absent) |
| Tag | `v1.4.0` **local only** (not pushed) |
| Gate / suite | 13/13, 143/143 |

One correction of my own earlier audit, already committed (`df30c24`): I had
claimed `docs/plan_1.4_voice.md` ships in the zip. It does not.

---

# B. Decision model / judge

## B.1 Is it wired? No.

```
git grep -rn "tinydecide|gliner|decision_model|router_model"   -> no hits in code
git grep -ln  "TinyDecide|GLiNER"                              -> docs/plan_1.4_voice.md only
```

Nothing is installed, fetched, referenced or toggled. The only artifact is the
measurement note in `docs/plan_1.4_voice.md` §0d (which is `export-ignore`, so it
does not even ship). Full opt-in is still true — because there is nothing to opt
into yet.

## B.2 What the "auto judge" is today

`mode = automap` (Auto) decides like this (`core/chat_run.py:1158-1229`):

1. Shortcut tier: word count + `detect_task_type()` (keyword heuristics:
   code/analysis/creative/general) → `trivial`/`simple` without any model.
2. Otherwise `_check_complexity_with_bias()` → `routing/complexity.py` →
   `pipeline._check_complexity()` (`hive_functions/pipeline.py`), which calls the
   **judge agent model** with `PROMPTS["complexity_judge"]` and parses
   `level=… route=… type=… tool=… reason=…`.
3. The verdict lands in `_last_judge_verdict` and steers `complexity`, `route`
   (`direct|tool|pipeline`), `task_type` and `tool_model` (small/large).
4. `get_routing_suggestion()` (learned weights, needs ≥20 runs and confidence
   ≥0.70) can override the mode afterwards, and the UI bias slider nudges it.

Two things in there matter for a small classifier:

- **The judge is an LLM call with a VRAM footprint.** If its model is not loaded,
  `_check_complexity` returns `level=simple, route=direct, reason="judge_cold"`
  immediately. The first Auto message of the day is therefore routed as *simple*
  unless the judge model happens to be warm. A 300-440 ms CPU classifier cannot be
  cold and needs no VRAM.
- **The verdict is exactly a 4-head classification**: `level`, `route`,
  `task_type`, `tool_model`. That is the shape the decision models were built for.

## B.3 Measured (this machine, our own prompts)

| Model | Size / deps | Latency | Hits on 12 realistic prompts |
|---|---|---|---|
| TinyDecide | 6.2 MB, **numpy only** | ~325 ms | 7/12, confidence 0.00-0.53 |
| GLiNER2.5-Decide | 1.3 GB + torch + gliner2 (~2.5 GB env) | ~440 ms | 10/12 |

Both English-only. TinyDecide's own card says it is best on short everyday
messages and that two-step reasoning is near chance — which is what our prompts
are. GLiNER's two misses were defensible (`write a small website` → *write*,
`read settings.py …` → *code*).

## B.4 Where it should plug in (opt-in, fallback-first)

A `judge_engine` setting (`llm` default | `rules` | `tinydecide` | `gliner`), applied
**only** in the Auto path:

1. `rules` — the existing shortcut tier, extended with a few deterministic tests
   (path/extension → code, `site:`/URL → research, pure question → chat). 0 ms.
2. `tinydecide`/`gliner` — the classifier fills `{level, route, task_type,
   tool_model}`; below a confidence threshold (start at 0.55) the LLM judge is
   asked anyway.
3. The LLM judge stays the default and the safety net. The classifier never
   approves anything; it only picks the mode, and the verdict is logged
   (`[JUDGE] engine=… verdict=… conf=…`) so the decision is auditable in the run.
4. Fetch on demand (`deploy/fetch_router.py`), model files gitignored, Voice-card
   style status line, default off.

Risks, in order:

- **A wrong verdict is expensive**: `route=tool` vs `direct` vs `pipeline` changes
  the whole execution shape, and `tool_model` picks a model size. 7/12 is not good
  enough to *replace* the judge — only to *pre-empt* the cheap tier.
- **English only** — the UI is English-first, but a German prompt would be
  misrouted silently. Needs a language guard (route to the LLM judge when the text
  is not ASCII-ish or fails a stopword test).
- **GLiNER costs 1.3 GB + torch + a second Python env** for a router — the same
  reason the TTS sidecar is opt-in. TinyDecide costs nothing (numpy) but is weaker.
- **Calibration on our own data** is the honest prerequisite: `token_stats.json`
  and the chat history have enough (prompt → mode → outcome) tuples to build a
  200-case set and measure both engines against the current judge before any
  toggle ships.

## B.5 Recommendation

1. Build the **rules tier** first (no model, deterministic, immediately better
   than the word-count shortcut and no worse than today).
2. Add `judge_engine=tinydecide` for the short-message band only, with the
   confidence threshold and the LLM fallback, and measure on a real history slice.
3. Only if that slice shows the ambiguous band actually matters, evaluate GLiNER —
   otherwise 1.3 GB of model for a router is a bad trade.
4. Whatever ships, `judge_cold` disappears: the classifier has no cold start, so
   the first Auto message stops being silently "simple".

That work is **not** in 1.4.0. It belongs to 1.5, with `judge_engine` defaulting to
`llm` (today's behaviour).

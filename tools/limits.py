"""Budget constants — the ONLY place with hard numbers (2026-10-14).

Sonnet review of the tool-pipeline audit: no behavior may branch on model
classes or fixed staffels. Everything is DERIVED from measured sizes
(n_ctx, chars-per-token EMA, max_output) via the fractions below, clamped by
named floors/ceilings. `Budgets.resolve()` in core/budgets.py is the single
resolver, run once per run and frozen for the whole run (schema-visible
values must never change mid-run — that would kill the prefix cache).

Settings only OVERRIDE: "budget_<name>" = number, or None/absent = auto.
"""

# ── chars/token ─────────────────────────────────────────────────────────────
# Start value from the live /tokenize probe (code+prose mix, MiniCPM-V 4.7,
# 2026-10-14). A per-model EMA (usage.prompt_tokens vs sent chars) refines
# this at runtime; core/budgets.py owns it.
CPT_DEFAULT = 3.92

# ── prefill-aware caps (2026-10-14, task 4) ─────────────────────────────────
# A tool result lands in the NEXT round's prompt; with --cache-reuse only its
# own tokens need re-processing. At 19 tok/s a 24k-char result (ceiling) is
# ~5 minutes of prefill per read — caps must therefore also respect TIME.
# Worst case: result_chars / cpt / prefill_tps <= PREFILL_RESULT_MAX_SECONDS.
PREFILL_RESULT_MAX_SECONDS = 60.0
# Conservative default until the per-slot EMA has data (slow Vulkan tier).
# core/budgets.py persists a per-model EMA from prompt_n/prompt_ms telemetry.
PREFILL_TPS_DEFAULT = 20.0

# ── fractions of n_ctx (the actual dials) ───────────────────────────────────
# Tool-result cap:      0.12 × n_ctx × cpt, clamp(TOOL_RESULT_MIN/MAX)
TOOL_RESULT_FRAC = 0.12
# Read budget:          files whose estimated size exceeds this are refused
#                       with a range hint; reads inside are capped here.
READ_BUDGET_FRAC = 0.50
# Tool-schema budget:   0.25 × n_ctx × cpt — full schema fits at 16k
#                       (~15.3k chars of 16.1k); smaller ctx auto-shortens.
SCHEMA_FRAC = 0.25
# Injection budget:     0.10 × n_ctx × cpt — shared pool for nudges/anchors/
#                       menus; per-message cap is a third of it.
INJECTION_FRAC = 0.10

# ── named floors / ceilings (the only hard numbers) ─────────────────────────
TOOL_RESULT_MIN, TOOL_RESULT_MAX = 2_000, 24_000
READ_CHAR_MIN, READ_CHAR_MAX = 8_000, 64_000
SCHEMA_MIN, SCHEMA_MAX = 3_000, 40_000
INJECTION_MIN, INJECTION_MAX = 800, 12_000
# anchor window (± lines around the closest fuzzy match): derived from the
# injection budget divided by AVG_LINE_CHARS × 8, clamped.
ANCHOR_MIN, ANCHOR_MAX = 3, 15
# too-large-read signature outline: anchor lines × this multiple
OUTLINE_PER_ANCHOR = 6
# average content line length used to convert char budgets to line counts
AVG_LINE_CHARS = 80

# ── compression headroom (replaces the fixed 0.75/0.70 staffel) ─────────────
# reserve = min(max_out, COMPRESS_RESERVE_CTX_FRAC × n_ctx) + growth tokens.
# trigger when free < COMPRESS_TRIGGER_RESERVE_MULT × reserve,
# rest  when free > COMPRESS_REST_RESERVE_MULT × reserve.
# At 16k ctx / 8k max_out / 800 growth this reproduces the tuned 0.70
# trigger exactly (free < 0.30 × ctx) with rest at ~0.60.
COMPRESS_RESERVE_CTX_FRAC = 0.15
COMPRESS_TRIGGER_RESERVE_MULT = 1.5
COMPRESS_REST_RESERVE_MULT = 2.0
# p90 per-round growth in tokens — settings "budget_compress_growth_tokens"
# overrides; once telemetry has enough data this becomes a measured EMA.
COMPRESS_GROWTH_TOKENS_DEFAULT = 800

# ── legacy fixed values kept only as defaults for non-run contexts ─────────
# (handler fallback when NO run budgets are active — e.g. direct chat tools)
EXEC_RESULT_CAP_FALLBACK = 8_000

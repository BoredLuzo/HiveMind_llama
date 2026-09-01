"""Peer ratings and auto-adapt (extracted from server.py)."""

import asyncio, json, logging, re, time
from pathlib import Path

from core.state import registry_get
from routing.model_automap import detect_task_type, record_run_outcome
from model_configs import (
    append_learning_log,
    read_learning_log,
    get_learned_config,
    get_base_config,
)
from infra.run_counter import _load_run_counter
from hive_functions.prompts import PEER_RATING_PROMPT

logger = logging.getLogger("hivemind.peer_ratings")

# P1-FIX (2026-08-11): extraction break — server.py names were completely missing.
THIS_FILE = Path(__file__).resolve().parent.parent / "server.py"

_bg_rating_sem = None

def _parse_rating_tune(text: str) -> dict:
    """TUNE-Format Parser: score=0.8 strengths=[S1;S2] weaknesses=[W1] temp_delta=0.05 tokens_delta=50 hint=leer reason=..."""
    d: dict = {}
    sm = re.search(r"score=([\d.]+)", text)
    if sm: d["score"] = float(sm.group(1))
    stm = re.search(r"strengths=\[([^\]]*)\]", text)
    if stm:
        raw_s = stm.group(1).strip()
        d["strengths"] = [p.strip().replace("_", " ") for p in raw_s.split(";") if p.strip()] if raw_s else []
    wkm = re.search(r"weaknesses=\[([^\]]*)\]", text)
    if wkm:
        raw_w = wkm.group(1).strip()
        d["weaknesses"] = [p.strip().replace("_", " ") for p in raw_w.split(";") if p.strip()] if raw_w else []
    tm = re.search(r"temp_delta=([-\d.]+)", text)
    if tm: d["suggested_temp_delta"] = float(tm.group(1))
    tkm = re.search(r"tokens_delta=([-\d]+)", text)
    if tkm: d["suggested_tokens_delta"] = int(tkm.group(1))
    hm = re.search(r"hint=([^\s]+)", text)
    if hm:
        _hv = hm.group(1).lower()


async def run_peer_ratings(run_id, user_input, outputs, use_learned, rating_mode: str = "pipeline", has_images: bool = False):


    if _bg_rating_sem and _bg_rating_sem.locked():
        return
    if _bg_rating_sem:
        async with _bg_rating_sem:
            await _run_peer_ratings_inner(run_id, user_input, outputs, use_learned, rating_mode, has_images=has_images)
    else:
        await _run_peer_ratings_inner(run_id, user_input, outputs, use_learned, rating_mode, has_images=has_images)


async def _run_peer_ratings_inner(run_id, user_input, outputs, use_learned, rating_mode: str = "pipeline", has_images: bool = False):
    if rating_mode == "duo":
        # Duo learning: duo_critic rates duo_coder output
        from server import _pipeline_chat_stream
        _duo_coder_out = outputs.get("duo_coder", "")
        if not _duo_coder_out:
            return
        rater_model = registry_get("duo_critic")
        rated_model = outputs.get("duo_coder_model") or registry_get("duo_coder")
        logger.info("[PEER-DUO] coder_out_len=%d rater=%s rated=%s", len(_duo_coder_out), rater_model, rated_model)
        if not rater_model or not rated_model:
            return
        try:
            from backend.llama_server_manager import manager as _lsm
            await _lsm.evict(rated_model)
            from backend.llama_vram_table import vram_of_moe, wait_for_vram_reclaim
            _rater_mib = round(vram_of_moe(rater_model, 4096) * 1024) + 768
            await wait_for_vram_reclaim(target_mib=_rater_mib, timeout_sec=45)
        except Exception as _vr_err:
            logger.warning("[PEER-DUO] Coder-evict/VRAM-reclaim failed: %s", _vr_err)
        rating_input = (
            "Context: completeness and correctness of the generated solution\n\n"
            f"Original question: {user_input}\n\n"
            f"Output from duo_coder:\n{_duo_coder_out[:1200]}\n\n"
            "Rate this output."
        )
        try:
            parts = []
            async for tok in _pipeline_chat_stream(
                rater_model,
                [{"role": "system", "content": PEER_RATING_PROMPT},
                 {"role": "user",   "content": rating_input}],
                0.2, 300
            ):
                parts.append(tok)
            raw   = "".join(parts).strip()
            rating_data: dict = {}
            if "score=" in raw:
                rating_data = _parse_rating_tune(raw)
            else:
                _rmatch = re.search(r'\{[\s\S]*?\}', raw)
                if _rmatch:
                    try:
                        rating_data = json.loads(_rmatch.group(0))
                    except json.JSONDecodeError:
                        rating_data = {}
            if rating_data.get("score") is None:
                _sm = re.search(r"(?<![.\d])(0(?:\.\d+)?|1(?:\.0+)?)(?![.\d])", raw)
                if _sm:
                    rating_data["score"] = float(_sm.group(1))
                else:
                    logger.warning("[PEER-DUO] No parseable score from rater: %r", raw[:160])
            if rating_data.get("score") is not None:
                score = float(rating_data.get("score", 0.5))
                append_learning_log(THIS_FILE, rated_model, {
                    "event":                "peer_rating",
                    "run_id":               run_id,
                    "rater":                "duo_critic",
                    "rater_model":          rater_model,
                    "rated_agent":          "duo_coder",
                    "score":                score,
                    "strengths":            rating_data.get("strengths", []),
                    "weaknesses":           rating_data.get("weaknesses", []),
                    "suggested_temp_delta": rating_data.get("suggested_temp_delta", 0),
                    "notes":                rating_data.get("adapt_reason", ""),
                    "input_preview":        user_input[:100],
                })
                if use_learned:
                    await _maybe_adapt_config(rated_model, "duo_coder", score, rating_data)
        except Exception as _pe:
            logger.warning("[PEER-DUO] Rating run failed: %s", _pe)
        return

    if rating_mode == "direct":
        _direct_out = outputs.get("direct", "")
        if not _direct_out:
            return
        rater_model = registry_get("critic")
        if not rater_model:
            return
        rating_input = (
            f"Context: Quality and completeness of a direct response\n\n"
            f"Original question: {user_input}\n\n"
            f"Direct agent output:\n{_direct_out[:1200]}\n\n"
            "Rate this output."
        )
        try:
            parts = []
            async for tok in _pipeline_chat_stream(
                rater_model,
                [{"role": "system", "content": PEER_RATING_PROMPT},
                 {"role": "user",   "content": rating_input}],
                0.2, 300
            ):
                parts.append(tok)
            raw = "".join(parts).strip()
            rating_data = _parse_rating_tune(raw) if "score=" in raw else {}
            if rating_data.get("score") is not None:
                score = float(rating_data["score"])
                rated_model = registry_get("direct")
                append_learning_log(THIS_FILE, rated_model, {
                    "event":                "peer_rating",
                    "run_id":               run_id,
                    "rater":                "critic",
                    "rater_model":          rater_model,
                    "rated_agent":          "direct",
                    "score":                score,
                    "strengths":            rating_data.get("strengths", []),
                    "weaknesses":           rating_data.get("weaknesses", []),
                    "suggested_temp_delta": rating_data.get("suggested_temp_delta", 0),
                    "input_preview":        user_input[:100],
                })
                if use_learned:
                    await _maybe_adapt_config(rated_model, "direct", score, rating_data)
        except Exception:
            pass
        return

    rating_pairs = [
        ("critic",      "analyst",      "Logical gaps and completeness"),
        ("synthesizer", "analyst",      "Does the analysis hit the core?"),
        ("synthesizer", "refiner",      "Did the refinement bring real improvements?"),
        ("synthesizer", "critic",       "Was the critique constructive and precise?"),
        ("analyst",     "synthesizer",  "Did the final answer fully and correctly integrate all key points?"),
    ]

    _pending_scores     = []
    _detected_task_type = detect_task_type(user_input, has_images=has_images)

    for rater_key, rated_key, context in rating_pairs:
        if rated_key not in outputs or not outputs[rated_key]:
            continue

        rater_model = registry_get(rater_key)
        rating_input = (
            f"Context: {context}\n\n"
            f"Original question: {user_input}\n\n"
            f"Output from {rated_key}:\n{outputs[rated_key][:1200]}\n\n"
            f"Rate this output."
        )

        try:
            parts = []
            from server import _pipeline_chat_stream
            async for tok in _pipeline_chat_stream(
                rater_model,
                [{"role": "system", "content": PEER_RATING_PROMPT},
                 {"role": "user",   "content": rating_input}],
                0.2, 300
            ):
                parts.append(tok)

            raw   = "".join(parts).strip()
            if "score=" in raw:
                rating_data = _parse_rating_tune(raw)
            else:
                match = re.search(r'\{[\s\S]*?\}', raw)
                if not match:
                    continue
                try:
                    rating_data = json.loads(match.group(0))
                except json.JSONDecodeError:
                    continue
            score       = float(rating_data.get("score", 0.5))
            rated_model = registry_get(rated_key)

            append_learning_log(THIS_FILE, rated_model, {
                "event":                "peer_rating",
                "run_id":               run_id,
                "rater":                rater_key,
                "rater_model":          rater_model,
                "rated_agent":          rated_key,
                "score":                score,
                "strengths":            rating_data.get("strengths", []),
                "weaknesses":           rating_data.get("weaknesses", []),
                "suggested_temp_delta": rating_data.get("suggested_temp_delta", 0),
                "notes":                rating_data.get("adapt_reason", ""),
                "input_preview":        user_input[:100],
            })

            if use_learned:
                await _maybe_adapt_config(rated_model, rated_key, score, rating_data)
            _pending_scores.append({"agent": rated_key, "model": rated_model, "score": score})

        except Exception as e:
            append_learning_log(THIS_FILE, registry_get(rated_key), {
                "event": "peer_rating_error", "rater": rater_key,
                "rated_agent": rated_key, "error": str(e),
            })

    if _pending_scores:
        for _ps in _pending_scores:
            _model = _ps.get("model", "")
            _score = _ps.get("score", 0.5)
            if _model:
                record_run_outcome(_model, _detected_task_type, _score > 0.5, mode=rating_mode)
        # On persistently bad scores (< 0.40): immediate soul evolution
        avg_score = sum(s["score"] for s in _pending_scores) / len(_pending_scores)
        if avg_score < 0.40:
            run_count = _load_run_counter()
            append_learning_log(THIS_FILE, registry_get("direct"), {
                "event": "low_score_evolution_trigger",
                "avg_score": round(avg_score, 3),
                "run_count": run_count,
            })
            from server import _maybe_trigger_soul_evolution_forced  # P1-FIX: Lazy-Import
            asyncio.create_task(_maybe_trigger_soul_evolution_forced(run_count))


async def _maybe_adapt_config(model, agent_key, new_score, rating_data):
    log          = read_learning_log(THIS_FILE, model, limit=30)
    peer_ratings = [e for e in log
                    if e.get("event") == "peer_rating" and e.get("rated_agent") == agent_key]

    if len(peer_ratings) < 2:
        return

    recent       = peer_ratings[-5:]
    scores       = [e.get("score", 0.5) for e in recent]
    avg          = sum(scores) / len(scores)
    score_trend  = scores[-1] - scores[0] if len(scores) > 1 else 0
    is_declining = score_trend < -0.1
    is_poor      = avg < 0.45
    is_good      = avg > 0.78

    if not (is_poor or is_declining or is_good):
        return

    current_learned = get_learned_config(THIS_FILE, model, agent_key) or {}
    base            = get_base_config(THIS_FILE, agent_key) or {}
    current_temp    = float(current_learned.get("temperature", base.get("temperature", 0.5)))
    current_tokens  = int(  current_learned.get("max_tokens",  base.get("max_tokens",  400)))
    existing_hint   = current_learned.get("system_prompt_override", "")

    changes = {}
    change_log = []

    if is_poor or is_declining:
        temp_delta = float(rating_data.get("suggested_temp_delta", 0))
        if abs(temp_delta) >= 0.03:
            new_temp = round(max(0.05, min(1.0, current_temp + temp_delta)), 2)
            if new_temp != current_temp:
                changes["temperature"] = new_temp
                change_log.append(f"temp {current_temp:.2f}->{new_temp:.2f}")

        tok_delta = int(rating_data.get("suggested_tokens_delta", 0))
        if abs(tok_delta) >= 50:
            new_tokens = max(100, min(4000, current_tokens + tok_delta))
            if new_tokens != current_tokens:
                changes["max_tokens"] = new_tokens
                change_log.append(f"tokens {current_tokens}->{new_tokens}")

        prompt_hint = rating_data.get("suggested_prompt_hint", "").strip()
        if prompt_hint and len(prompt_hint) > 10 and prompt_hint not in existing_hint:
            from server import get_effective_prompt  # P1-FIX: Lazy-Import (server.py:1332)
            base_prompt = get_effective_prompt(agent_key, None) or ""
            changes["system_prompt_override"] = base_prompt + "\n\n// Learned hint: " + prompt_hint
            change_log.append(f"prompt hint added")

    if not changes and not is_good:
        return

    updated = dict(current_learned)
    updated.update(changes)
    status = "declining" if is_declining else ("poor" if is_poor else "good")
    updated["notes"] = (
        f"[avg={avg:.2f} / {len(recent)} ratings | {status}]"
        + (f" | {', '.join(change_log)}" if change_log else "")
    )
    updated["_avg_score"]    = round(avg, 3)
    updated["_rating_count"] = len(peer_ratings)
    updated["_last_adapted"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    save_learned_config(THIS_FILE, model, agent_key, updated)
    append_learning_log(THIS_FILE, model, {
        "event":     "auto_config_adapt",
        "agent":     agent_key,
        "avg_score": round(avg, 3),
        "status":    status,
        "changes":   change_log,
    })


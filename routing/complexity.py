"""Komplexitaets-Routing (aus server.py extrahiert)."""

# Phase A: Runtime-Dependencies
pipeline = None

def init_complexity(pipeline_obj=None):
    global pipeline
    if pipeline_obj:
        pipeline = pipeline_obj

async def _check_complexity_with_bias(user_input: str, judge_bias: int) -> str:
    """Judge-Entscheidung mit Bias-Korrektur. Bleibt als Wrapper erhalten."""
    if judge_bias <= 10:
        return "simple"
    if judge_bias >= 90:
        return "complex"
    result = await pipeline._check_complexity(user_input)
    if judge_bias >= 65 and result in ("simple", "trivial"):
        return "complex"
    if judge_bias <= 35 and result in ("complex",):
        return "simple"
    return result


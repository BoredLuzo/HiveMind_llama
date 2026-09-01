"""Orchestrator — refactored run_stream() delegating to extracted runners."""

import asyncio


async def run_stream_orchestrated(ctx):
    """Orchestrate a stream run by routing to the appropriate runner.

    Replaces the 7,400-line run_stream() body in server.py.
    The RunContext must be fully populated before calling.
    """
    if ctx.complexity == "code_duo":
        async for event in run_code_duo(ctx):
            yield event
        return

    # Shared preamble for direct/pipeline modes
    yield await ctx.emit({"type": "complexity", "content": ctx.complexity, "source": ctx.complexity_source})
    asyncio.create_task(ctx.auto_memory_from_input(ctx.user_input)) if ctx.auto_memory_from_input else None

    if ctx.complexity in ("trivial", "simple"):
        async for event in run_direct(ctx):
            yield event
    else:
        async for event in run_pipeline(ctx):
            yield event


# Late imports to avoid circular dependencies at module level
from core.direct_runner import run_direct
from core.pipeline_runner import run_pipeline
from core.duo_runner import run_code_duo

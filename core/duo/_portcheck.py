import asyncio


async def _p2_alive(port: int, manager) -> bool:
    for _p2a in range(3):
        if await manager._port_alive(port):
            return True
        if _p2a < 2:
            await asyncio.sleep(0.4)
    return False


async def _port_alive_with_retry(port: int, manager, tries: int = 5) -> bool:
    tries = max(1, int(tries or 1))
    for _attempt in range(tries):
        if await manager._port_alive(port):
            return True
        if _attempt + 1 < tries:
            await asyncio.sleep(0.4)
    return False

"""httpx-Utility-Funktionen (aus server.py extrahiert)."""
import httpx


def make_httpx_timeout(read_s: float = 300.0, *, connect: float = 10.0, write: float = 10.0, pool: float = 5.0) -> httpx.Timeout:
    """Factory fuer httpx.Timeout mit konsistenten Default-Werten."""
    return httpx.Timeout(connect=connect, read=read_s, write=write, pool=pool)

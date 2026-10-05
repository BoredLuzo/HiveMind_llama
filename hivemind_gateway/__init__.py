"""hivemind_gateway — Telegram gateway for HiveMind 1.3.

Own package, own process. Never imported by the HiveMind server; imports
only the stdlib and httpx so a gateway crash cannot touch HiveMind.
"""
__version__ = "0.1.0.wp1"

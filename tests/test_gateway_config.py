"""Gateway WP1: config — validation, fail fast, no secrets in the file.

Also loads the SHIPPED gateway.toml.example so a bad example can never
ship (it would fail on the owner's first start).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hivemind_gateway.config import GatewayConfig, load_gateway_config

passed = 0
failed = 0
ROOT = Path(__file__).parent.parent


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


tmp = Path(tempfile.mkdtemp(prefix="gw_cfg_"))


def _write(body: str) -> Path:
    p = tmp / "gateway.toml"
    p.write_text(body, encoding="utf-8")
    return p


# ── defaults valid ──────────────────────────────────────────────────────
try:
    GatewayConfig().validate()
    check("defaults validate", True)
except ValueError as e:
    check("defaults validate", False, f" ({e})")

# ── shipped example parses + validates ──────────────────────────────────
try:
    cfg = load_gateway_config(ROOT / "gateway.toml.example")
    check("example file valid", True)
    check("example values sane", cfg.hive_base_url.startswith("http://127.0.0.1")
          and cfg.approval_expiry_s == 120)
except (ValueError, OSError) as e:
    check("example file valid", False, f" ({e})")

# ── secrets forbidden (top level AND nested) ────────────────────────────
for name, body in (
    ("top-level", 'token = "123:abc"\n'),
    ("nested", "[telegram]\nbot_token = '123:abc'\n"),
    ("api_key", 'api_key = "x"\n'),
    ("password", 'password = "x"\n'),
):
    try:
        load_gateway_config(_write(body))
        check(f"forbidden: {name}", False)
    except ValueError as e:
        check(f"forbidden: {name}", "forbidden key" in str(e))

# ── non-loopback base url refused ───────────────────────────────────────
try:
    load_gateway_config(_write('hive_base_url = "http://0.0.0.0:8001"\n'))
    check("non-loopback refused", False)
except ValueError as e:
    check("non-loopback refused", "loopback" in str(e))

# ── unknown keys refused (typos are startup errors) ─────────────────────
try:
    load_gateway_config(_write('max_text_chars = 100\nmaxtextchars = 5\n'))
    check("unknown key refused", False)
except ValueError as e:
    check("unknown key refused", "unknown keys" in str(e))

# ── bad values refused ──────────────────────────────────────────────────
try:
    load_gateway_config(_write("max_text_chars = -5\n"))
    check("negative value refused", False)
except ValueError:
    check("negative value refused", True)
try:
    load_gateway_config(_write("approval_expiry_s = 99999\n"))
    check("oversized expiry refused", False)
except ValueError:
    check("oversized expiry refused", True)
try:
    load_gateway_config(_write("max_text_chars = 9001\n"))
    check(">4096 refused", False)
except ValueError:
    check(">4096 refused", True)

# ── valid custom file accepted ──────────────────────────────────────────
cfg = load_gateway_config(_write(
    'hive_base_url = "http://127.0.0.1:8001"\napproval_expiry_s = 90\n'))
check("custom values accepted", cfg.approval_expiry_s == 90)

# ── master switch (fail-closed) ─────────────────────────────────────────
import os
from hivemind_gateway import main as gw_main

check("master switch defaults OFF", GatewayConfig().telegram_enabled is False)
try:
    gw_main.ensure_enabled(GatewayConfig())
    check("disabled config refused", False)
except gw_main.StartupError:
    check("disabled config refused", True)

os.environ["HIVEMIND_GATEWAY_ENABLED"] = "1"
try:
    gw_main.ensure_enabled(GatewayConfig())
    check("env override enables", True)
finally:
    os.environ.pop("HIVEMIND_GATEWAY_ENABLED", None)

os.environ["HIVEMIND_GATEWAY_ENABLED"] = "0"
try:
    gw_main.ensure_enabled(GatewayConfig())
    check("env 0 does not enable", False)
except gw_main.StartupError:
    check("env 0 does not enable", True)
finally:
    os.environ.pop("HIVEMIND_GATEWAY_ENABLED", None)

gw_main.ensure_enabled(load_gateway_config(_write("telegram_enabled = true\n")))
check("toml true enables", True)


# ensure_enabled_config (2026-10-05 universal-user setup): missing /
# disabled / already-enabled gateway.toml for the setup-token flow.
from hivemind_gateway.config import ensure_enabled_config

_t = Path(tempfile.mkdtemp(prefix="gwcfg_ens_"))
msg = ensure_enabled_config(_t)
# I5 (audit r2): the written config also pins hive_base_url to the
# settings port so setup-token + custom port actually reach the engine.
_cfg_txt = (_t / "gateway.toml").read_text()
check("ensure: missing file written",
      "written" in msg
      and "telegram_enabled = true" in _cfg_txt
      and "hive_base_url" in _cfg_txt)
(_t / "gateway.toml").write_text(
    'hive_base_url = "http://127.0.0.1:8001"\n'
    'telegram_enabled = false\n',
    encoding="utf-8")
msg2 = ensure_enabled_config(_t)
check("ensure: disabled flipped, keys kept",
      "-> true" in msg2
      and 'hive_base_url = "http://127.0.0.1:8001"' in
      (_t / "gateway.toml").read_text()
      and "telegram_enabled = true" in (_t / "gateway.toml").read_text())
msg3 = ensure_enabled_config(_t)
check("ensure: already enabled is a noop", "already enabled" in msg3)


print()
print(f"passed={passed} failed={failed}")
sys.exit(0 if failed == 0 else 1)

"""Underdog Woof TTS backend: downloader, runtime wrapper, engine routing, and UI/API pins.

Covers:
  (a) deploy/fetch_woof.py: install checks (LICENSE/NOTICE + MIN_WEIGHTS_BYTES),
      atomic mocked download of all REQUIRED_FILES, and error state cleanup;
  (b) core/tts_woof.py & core/tts_engine.py: offline synthesis via temp
      HIVEMIND_WOOF_DIR stub package, speed scaling, voice/backend routing
      ("woof" setting or explicit "juno"/"mira"), status/attribution strings,
      idle unload, and missing-NumPy -> TTS_NO_NUMPY mapping;
  (c) server.py, index.html, static/app.js, settings, setup_voice.bat, updater,
      and docs wiring for opt-in Underdog Woof support.

Run: python tests/test_tts_woof.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.ui_sources import read_all_ui_js  # noqa: E402
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core import tts_audio, tts_engine, tts_woof  # noqa: E402
from deploy import fetch_woof  # noqa: E402
from settings_source import settings_source as _settings_src  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


class _FakeResponse(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
        return False


def _write_fake_woof_install(base: Path, *, weights_bytes: int = 64) -> None:
    """Populate a valid fake underdog_woof runtime + weight file in ``base``."""
    pkg = base / "underdog_woof"
    pkg.mkdir(parents=True, exist_ok=True)
    (base / "LICENSE").write_text("Underdog Non-Commercial License 1.0\n", encoding="utf-8")
    (base / "NOTICE").write_text("Voice by Underdog Woof - underdog.ai\n", encoding="utf-8")
    (pkg / "__init__.py").write_text(
        """
import numpy as np

class Woof:
    calls = []
    def __init__(self, weights: str, voice: str = "juno"):
        self.weights = weights
        self.voice = voice
        self.sr = 24000
    def __call__(self, text: str):
        Woof.calls.append((self.voice, text))
        n = int(self.sr * 2.0)
        t = np.arange(n, dtype=np.float32) / self.sr
        return (0.25 * np.sin(2.0 * np.pi * 220.0 * t)).astype(np.float32)
""",
        encoding="utf-8",
    )
    for rel in ("normalize.py", "respell.py", "respell.tsv", "runtime.py", "textsplit.py"):
        (pkg / rel).write_text("# stub\n", encoding="utf-8")
    (base / "woof_int8.npz").write_bytes(b"PK\x03\x04" + b"\x00" * max(0, weights_bytes - 4))


def _clear_woof_modules() -> None:
    tts_woof.stop()
    for name in [m for m in sys.modules if m == "underdog_woof" or m.startswith("underdog_woof.")]:
        sys.modules.pop(name, None)


def main() -> int:
    prev_env = os.environ.get("HIVEMIND_WOOF_DIR")
    prev_tts_url = os.environ.get("HIVEMIND_TTS_URL")
    try:
        with tempfile.TemporaryDirectory(prefix="hm_woof_test_") as tmp:
            woof_dir = Path(tmp) / "woof"
            os.environ["HIVEMIND_WOOF_DIR"] = str(woof_dir)
            # Point Pocket sidecar URL nowhere reachable to prove Woof never invokes Pocket
            os.environ["HIVEMIND_TTS_URL"] = "http://127.0.0.1:9"
            _clear_woof_modules()

            # ── (a) fetch_woof install verification & mocked atomic fetch ───────
            check("empty dir is not installed", not fetch_woof.is_installed(woof_dir))
            check("initial fetch_state reports idle", fetch_woof.fetch_state(woof_dir).get("state") == "idle")

            _write_fake_woof_install(woof_dir, weights_bytes=128)
            check("undersized woof_int8.npz fails MIN_WEIGHTS_BYTES check",
                  not fetch_woof.is_installed(woof_dir))

            with mock.patch.object(fetch_woof, "MIN_WEIGHTS_BYTES", 64):
                check("valid weights + runtime + LICENSE/NOTICE is installed",
                      fetch_woof.is_installed(woof_dir))
                (woof_dir / "NOTICE").unlink()
                check("missing NOTICE fails is_installed()", not fetch_woof.is_installed(woof_dir))

            fetch_dir = Path(tmp) / "woof_fetched"
            payloads = {rel: f"content:{rel}".encode("utf-8") for rel in fetch_woof.REQUIRED_FILES}
            payloads["woof_int8.npz"] = b"W" * 128

            def _fake_urlopen(req, timeout=60):
                url = getattr(req, "full_url", str(req))
                for rel, blob in payloads.items():
                    if url.endswith("/" + rel):
                        return _FakeResponse(blob)
                raise urllib.error.URLError(f"unexpected url: {url}")

            with (
                mock.patch.object(fetch_woof, "MIN_WEIGHTS_BYTES", 64),
                mock.patch("urllib.request.urlopen", side_effect=_fake_urlopen),
            ):
                out = fetch_woof.download(fetch_dir)
                check("download() returns target dir", out == fetch_dir)
                check("all REQUIRED_FILES fetched",
                      all((fetch_dir / rel).is_file() for rel in fetch_woof.REQUIRED_FILES))
                check("LICENSE and NOTICE preserved verbatim",
                      (fetch_dir / "LICENSE").read_bytes() == payloads["LICENSE"]
                      and (fetch_dir / "NOTICE").read_bytes() == payloads["NOTICE"])
                check("fetch_state reports done after download",
                      fetch_woof.fetch_state(fetch_dir).get("state") == "done")
                check("no leftover .part files",
                      not list(fetch_dir.rglob("*.part")))

            fail_dir = Path(tmp) / "woof_fail"
            with (
                mock.patch.object(fetch_woof, "MIN_WEIGHTS_BYTES", 64),
                mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")),
            ):
                threw = False
                try:
                    fetch_woof.download(fail_dir)
                except urllib.error.URLError:
                    threw = True
                check("network error raises cleanly", threw)
                check("fetch_state records error on failure",
                      fetch_woof.fetch_state(fail_dir).get("state") == "error")

            # ── (b) tts_woof + tts_engine offline routing & synthesis ───────────
            _write_fake_woof_install(woof_dir, weights_bytes=128)
            check("resolve_voice preserves juno/mira and falls back to juno",
                  tts_woof.resolve_voice("MIRA") == "mira"
                  and tts_woof.resolve_voice("unknown") == "juno")
            check("missing install skips preload safely",
                  not fetch_woof.is_installed(fail_dir))

            with mock.patch.object(fetch_woof, "MIN_WEIGHTS_BYTES", 64):
                check("preload() loads warm Woof instance", tts_woof.preload("mira"))
                check("is_running() true after preload", tts_woof.is_running())
                tts_woof.stop()
                check("stop() unloads warm Woof instance", not tts_woof.is_running())

                wav_raw = tts_woof.synthesize_wav("Hello from Underdog Woof.", voice="juno", auto_download=False)
                check("synthesize_wav returns RIFF WAVE audio",
                      tts_audio.looks_like_wav(wav_raw)
                      and abs(tts_audio.duration_seconds(wav_raw) - 2.0) < 0.1)

                tts_woof._last_used = 1.0  # force expired idle timestamp
                tts_woof.maybe_idle_stop(5.0)
                check("maybe_idle_stop releases idle Woof instance", not tts_woof.is_running())

                # Engine routing pins
                check("Pocket remains default backend",
                      tts_engine.DEFAULT_BACKEND == "pocket"
                      and "woof" in {b["key"] for b in tts_engine.BACKENDS})
                check("resolve_backend respects woof setting",
                      tts_engine.resolve_backend({"tts_backend": "woof"}) == "woof")
                check("explicit Woof voice auto-routes even under pocket backend",
                      tts_engine.resolve_backend({"tts_backend": "pocket"}, voice="mira") == "woof")
                check("voices_for('woof') exposes juno and mira",
                      [v[0] for v in tts_engine.voices_for("woof")[0]] == ["juno", "mira"])

                tts_engine._cache.clear()
                woof_settings = {
                    "tts_enabled": True,
                    "tts_backend": "woof",
                    "tts_voice": "mira",
                    "tts_speed": 1.6,
                }
                res = tts_engine.synthesize("Testing backend routing.", settings=woof_settings)
                wav_eng = res["wav"]
                check("tts_engine.synthesize succeeds on woof backend",
                      tts_audio.looks_like_wav(wav_eng))
                check("meta reports mira voice and clamped speed",
                      res.get("voice") == "mira"
                      and abs(float(res.get("speed", 0)) - tts_audio.MAX_SPEED) < 0.01)
                check("speed > 1.0 compresses WAV duration",
                      tts_audio.duration_seconds(wav_eng) < 1.6)

                # Explicit Woof voice overrides a Pocket backend setting cleanly
                res_override = tts_engine.synthesize(
                    "Override backend routing.",
                    settings={"tts_enabled": True, "tts_backend": "pocket", "tts_voice": "anna"},
                    voice="juno",
                )
                check("explicit Woof voice works even with pocket setting",
                      res_override.get("voice") == "juno"
                      and tts_audio.looks_like_wav(res_override["wav"]))

                st = tts_engine.status(woof_settings)
                check("status() reports woof backend, attribution and installed flag",
                      st.get("backend") == "woof"
                      and st.get("woof_installed") is True
                      and "Underdog Woof" in st.get("attribution", "")
                      and "underdog.ai" in st.get("attribution", "")
                      and [v["key"] for v in st.get("voices", [])] == ["juno", "mira"])

                with mock.patch.dict(sys.modules, {"numpy": None}):
                    tts_engine._cache.clear()
                    err_code = ""
                    try:
                        tts_engine.synthesize("No numpy test", settings=woof_settings)
                    except tts_engine.TtsError as exc:
                        err_code = exc.code
                    check("missing numpy maps to TTS_NO_NUMPY", err_code == "TTS_NO_NUMPY")

            # ── (c) Settings, Server, UI, Setup, Updater & Docs wiring ──────────
            settings_src = _settings_src()
            server_src = (
                (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
                + "\n"
                + (ROOT / "routers" / "voice.py").read_text(encoding="utf-8", errors="replace")
            )
            html_src = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")
            app_src = read_all_ui_js()
            bat_src = (ROOT / "deploy" / "setup_voice.bat").read_text(encoding="utf-8", errors="replace")
            updater_src = (ROOT / "deploy" / "updater.py").read_text(encoding="utf-8", errors="replace")
            prune_src = (ROOT / "deploy" / "update_prune.ps1").read_text(encoding="utf-8", errors="replace")
            lic_doc = (ROOT / "docs" / "voice_licenses.md").read_text(encoding="utf-8", errors="replace")
            setup_doc = (ROOT / "docs" / "voice_setup.md").read_text(encoding="utf-8", errors="replace")

            check("settings defines tts_backend defaulting to pocket",
                  '"tts_backend":' in settings_src and '"pocket"' in settings_src)
            check("server exposes POST /tts/fetch",
                  '@app.post("/tts/fetch")' in server_src or '@router.post("/tts/fetch")' in server_src)
            check("server /tts/voices supports backend param", "backend: str = \"\"" in server_src or "backend: str =" in server_src)
            check("HTML has tts-backend-sel and tts-download-row",
                  'id="tts-backend-sel"' in html_src and 'id="tts-download-row"' in html_src)
            check("JS wires backend toggle, download helper and attribution",
                  "onTtsBackendChange" in app_src and "ttsFetchWoof" in app_src
                  and "TTS_NO_NUMPY" in app_src and "Underdog Woof" in app_src)
            check("setup_voice.bat supports --woof -> deploy\\fetch_woof.py",
                  "--woof" in bat_src and "deploy\\fetch_woof.py" in bat_src)
            check("updater and prune script protect voice_models/",
                  '"voice_models"' in updater_src and "'voice_models'" in prune_src)
            readme_doc = (ROOT / "README.md").read_text(encoding="utf-8", errors="replace")
            check("HTML and JS display clickable Voice by Underdog Woof + underdog.ai + license links",
                  'href="https://underdog.ai"' in html_src
                  and 'Underdog Non-Commercial License 1.0' in html_src
                  and 'href="https://underdog.ai"' in app_src
                  and 'Underdog Non-Commercial License 1.0' in app_src)
            check("voice docs and README document Underdog Woof non-commercial terms + links",
                  "Underdog Woof" in lic_doc and "Underdog Non-Commercial License 1.0" in lic_doc
                  and "https://underdog.ai" in lic_doc
                  and "https://underdog.ai" in readme_doc
                  and "ConwayResearch/Underdog-Woof" in readme_doc
                  and "setup_voice.bat --woof" in setup_doc)
    finally:
        _clear_woof_modules()
        if prev_env is None:
            os.environ.pop("HIVEMIND_WOOF_DIR", None)
        else:
            os.environ["HIVEMIND_WOOF_DIR"] = prev_env
        if prev_tts_url is None:
            os.environ.pop("HIVEMIND_TTS_URL", None)
        else:
            os.environ["HIVEMIND_TTS_URL"] = prev_tts_url

    return h.finish("tts_woof")


if __name__ == "__main__":
    sys.exit(main())

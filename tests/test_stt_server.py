"""The /stt endpoints and the settings surface (source-level contract).

Why source-level: server.py builds the whole engine on import, and the existing
suites read it as text instead of booting the app (tests/test_reaudit_polish.py
does the same). The behaviour of the endpoints lives in core/stt_engine.py and
core/stt_audio.py, which have their own suites with real assertions.

Pins:
  (a) POST /stt takes the RAW request body - no python-multipart dependency;
  (b) the status codes map the engine's error codes (409 disabled, 400 the rest);
  (c) the temp WAV is always cleaned up, even on failure;
  (d) transcripts are not logged unless stt_log_transcripts is on;
  (e) the settings keys exist with the documented defaults (English only);
  (f) the optional `voice` dependency group exists in both dependency files and
      the fetch script covers the binaries + models.

Run: python tests/test_stt_server.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def src(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8", errors="replace")


server = src("server.py")

# ── (a) endpoints ───────────────────────────────────────────────────────────
check("GET /stt/status exists", '@app.get("/stt/status")' in server)
check("POST /stt exists", '@app.post("/stt")' in server)
seg = server.split('@app.post("/stt")', 1)[1].split('@app.post("/gateway/heartbeat"', 1)[0]
check("the audio is the raw request body", "await request.body()" in seg)
check("no multipart upload (no python-multipart needed)",
      "UploadFile" not in seg and "File(" not in seg and "Form(" not in seg)
check("the language comes from the engine, not the request",
      "stt_engine.resolve_language" in seg and "request.form" not in seg)

# ── (b) error mapping ───────────────────────────────────────────────────────
check("disabled -> 409", 'status_code=409' in seg)
check("engine errors -> 400", "status_code=400" in seg)
check("the error payload is the engine's code dict", ".as_dict()" in seg)
check("the status endpoint never breaks the UI",
      (lambda st: "try:" in st and "except " in st and "status_code=200" in st)(
          server.split('@app.get("/stt/status")', 1)[1].split('@app.post("/stt")', 1)[0]))

# ── (c) cleanup / caps / logging ────────────────────────────────────────────
check("the temp WAV is cleaned up in a finally block",
      "finally:" in seg and "stt_audio.cleanup(wav_path)" in seg)
check("the duration cap comes from the settings",
      "stt_max_seconds" in seg)
check("transcripts are debug-only",
      "stt_log_transcripts" in seg and "transcript: %s" in seg)
check("no shell in the endpoint", "shell=True" not in seg)

# ── (e) settings ────────────────────────────────────────────────────────────
settings = src("settings.py")
defaults = {
    "stt_enabled": "False",
    "stt_model": '""',
    "stt_language": '"en"',
    "stt_threads": "0",
    "stt_preload": "False",
    "stt_max_seconds": "300",
    "stt_timeout_s": "120",
    "stt_log_transcripts": "False",
}
for key, want in defaults.items():
    line = next((l for l in settings.splitlines() if f'"{key}"' in l), "")
    check(f"setting {key} defaults to {want}", want in line, f" ({line.strip()[:60]})")

# ── (f) dependencies + fetcher ──────────────────────────────────────────────
check("requirements-voice.txt exists", (ROOT / "requirements-voice.txt").is_file())
voice_req = src("requirements-voice.txt")
check("the voice group ships soundfile + numpy",
      "soundfile" in voice_req and "numpy" in voice_req)
check("requirements.txt mentions the voice extra", "soundfile" in src("requirements.txt"))
check("pyproject has a voice extra",
      "voice = [" in src("pyproject.toml") and "soundfile>=0.13" in src("pyproject.toml"))
fetch = src("deploy/fetch_whisper.py")
check("the fetcher resolves the whisper.cpp release list",
      "api.github.com/repos/ggml-org/whisper.cpp/releases" in fetch)
check("the fetcher handles the b#### build tags, not only /latest",
      "def pick_release" in fetch and "_build_number" in fetch)
check("the fetcher downloads from the model repos",
      "huggingface.co/{repo}/resolve/main/{name}" in fetch)
check("the fetcher supports cpu and blas", '"cpu"' in fetch and '"blas"' in fetch)
check("the fetcher has a check mode", "--check" in fetch and "def check(" in fetch)
check("the fetcher refuses wrong-size downloads", "MIN_MODEL_BYTES" in fetch and "MIN_BINARY_BYTES" in fetch)

# ── docs ────────────────────────────────────────────────────────────────────
check("the plan documents the English-only decision",
      "English only" in src("docs/plan_1.4_voice.md"))
check("docs/settings.md knows the new keys",
      "stt_enabled" in src("docs/settings.md") and "stt_preload" in src("docs/settings.md"))

print()
print(f"SUMMARY stt_server: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)

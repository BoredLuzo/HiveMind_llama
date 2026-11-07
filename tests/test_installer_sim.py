# -*- coding: utf-8 -*-
"""Simulation tests for the Windows installer batches.

A real installer run downloads gigabytes, writes settings.json and touches
the user's Desktop - so these tests never run IN the repo. Each case builds a
throwaway sandbox under %TEMP% with the REAL .bat file copied in, fake
stand-ins for the Python helpers it calls, and a PATH stripped down to
System32 + the test python (no npm / uv / winget / docker / git), then drives
the batch through cmd.exe with piped answers.

Covered here because a real run is unsafe or the failure was live:

- install paths with parentheses ("HiveMind (1)"): cmd's block parser eats a
  ')' that lands in it - the 2026-09 PAREN-PATH-FIX class. Both the model
  setup prompt flow and the install.bat top/optionals flow must survive.
- a failing models-folder mkdir used to vanish behind `2>nul` and the script
  "registered" a folder that was never created.
- install_lsp_packs.bat with a bad pack number used to hit a missing
  `:pack_9` label (localized "batch label not found").
- setup_voice.bat STT / TTS / TTS-failure flows with a fake uvx.
- the optionals-only install path with garbage voice-menu input (9) must
  print the skip hint instead of falling through silently.

Run: python tests/test_installer_sim.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


def build_env(fakebin=None, extra=None):
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    parts = [str(windir / "System32"),
             str(windir / "System32" / "WindowsPowerShell" / "v1.0"),
             str(Path(sys.executable).parent)]
    if fakebin:
        parts.insert(2, str(fakebin))
    env = {**os.environ, "PATH": os.pathsep.join(parts)}
    # The sandbox must see the machine the way a fresh install does: a leaked
    # HIVEMIND_MODELS_DIR would make setup_models.bat skip its folder prompt.
    env.pop("HIVEMIND_MODELS_DIR", None)
    if extra:
        env.update(extra)
    return env


def run_cmd(simdir, script, args=(), stdin="", env=None, timeout=90):
    return subprocess.run(
        ["cmd", "/d", "/c", script, *args],
        cwd=str(simdir), input=stdin, env=env or build_env(),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout)


def assert_no_cmd_crash(out, err, label):
    combined = (out + "\n" + err).lower()
    check(f"{label}: no cmd syntax abort", "syntaktisch" not in combined
          and "was unexpected at this time" not in combined)
    check(f"{label}: no missing-label error",
          "wurde nicht gefunden" not in combined
          and "batch label" not in combined)


FAKE_FETCH_MODELS = (
    "import sys\n"
    "if '--print-essential' in sys.argv:\n"
    "    print('  [1] demo-4b (fake catalog entry for the sim)')\n"
    "    print('  [2] demo-9b (fake catalog entry for the sim)')\n"
    "sys.exit(0)\n"
)


def case_setup_models_paren_path():
    label = "setup_models on 'HiveMind (1)' path"
    sim = Path(tempfile.mkdtemp(prefix="hmsim_paren_")) / "HiveMind (1)"
    sim.mkdir(parents=True)
    try:
        shutil.copy2(ROOT / "setup_models.bat", sim / "setup_models.bat")
        (sim / "deploy").mkdir()
        (sim / "deploy" / "fetch_models.py").write_text(FAKE_FETCH_MODELS,
                                                        encoding="utf-8")
        (sim / "deploy" / "apply_model_defaults.py").write_text(
            "import sys\nsys.exit(0)\n", encoding="utf-8")
        (sim / "deploy" / "add_model.py").write_text(
            "import sys\nsys.exit(0)\n", encoding="utf-8")
        r = run_cmd(sim, "setup_models.bat", stdin="\nA\n")
        check(f"{label}: clean abort exit code", r.returncode == 0,
              f"(rc={r.returncode})")
        check(f"{label}: paren default folder accepted",
              "Target folder:" in r.stdout)
        check(f"{label}: default models folder created",
              (sim / "models").is_dir())
        assert_no_cmd_crash(r.stdout, r.stderr, label)
    finally:
        shutil.rmtree(sim.parent, ignore_errors=True)


def case_setup_models_mkdir_failure():
    label = "setup_models mkdir failure"
    sim = Path(tempfile.mkdtemp(prefix="hmsim_mkdir_"))
    try:
        shutil.copy2(ROOT / "setup_models.bat", sim / "setup_models.bat")
        (sim / "blocker").write_text("a file, not a folder\n",
                                     encoding="utf-8")
        r = run_cmd(sim, "setup_models.bat", args=["blocker\\sub"],
                    stdin="A\n")
        check(f"{label}: fails loudly", r.returncode == 1,
              f"(rc={r.returncode})")
        check(f"{label}: clear error message",
              "[ERROR] Could not create the models folder" in r.stdout)
        check(f"{label}: the blocker file was not touched",
              (sim / "blocker").is_file())
    finally:
        shutil.rmtree(sim, ignore_errors=True)


def case_lsp_bad_token():
    label = "install_lsp_packs bad token"
    sim = Path(tempfile.mkdtemp(prefix="hmsim_lsp_"))
    try:
        (sim / "deploy").mkdir()
        shutil.copy2(ROOT / "deploy" / "install_lsp_packs.bat",
                     sim / "deploy" / "install_lsp_packs.bat")
        r = run_cmd(sim, "deploy\\install_lsp_packs.bat",
                    args=["1", "9"], stdin="")
        check(f"{label}: unknown pack warned in plain text",
              '[WARN] Unknown pack "9"' in r.stdout)
        check(f"{label}: valid pack still processed",
              "[SKIP] npm not found" in r.stdout)
        check(f"{label}: verification step still reached",
              "Checking which language servers actually answer" in r.stdout)
        assert_no_cmd_crash(r.stdout, r.stderr, label)
    finally:
        shutil.rmtree(sim, ignore_errors=True)


def _compile_fake_uvx(fakebin: Path) -> bool:
    """Build a uvx.exe that ignores its args and exits 0.

    A .cmd/.bat fake would trigger cmd's no-call chaining - invoking a batch
    from a batch without `call` never returns - and swallow the rest of
    setup_voice.bat. The real uvx is uvx.exe, so an exe fake is required; the
    .NET-Framework compiler ships with every Windows and compiles the stub.
    """
    csc = (Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET"
           / "Framework64" / "v4.0.30319" / "csc.exe")
    if not csc.exists():
        return False
    src = fakebin / "_stub.cs"
    src.write_text('class X { static int Main(string[] a) { return 0; } }\n',
                   encoding="ascii")
    r = subprocess.run([str(csc), "/nologo", "/out:uvx.exe", str(src.name)],
                       cwd=str(fakebin), capture_output=True, text=True)
    src.unlink(missing_ok=True)
    return r.returncode == 0


def _voice_sandbox(sim, uvx_ok=True):
    (sim / "deploy").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "deploy" / "setup_voice.bat",
                 sim / "deploy" / "setup_voice.bat")
    (sim / "deploy" / "fetch_whisper.py").write_text(
        "import sys\nprint('fake whisper ok')\nsys.exit(0)\n",
        encoding="utf-8")
    fakebin = sim / "fakebin"
    fakebin.mkdir(exist_ok=True)
    if uvx_ok and not _compile_fake_uvx(fakebin):
        print("  SKIP uvx.exe stub (no .NET csc on this machine)")
    return fakebin


def case_voice_stt_default():
    label = "setup_voice default (STT only)"
    sim = Path(tempfile.mkdtemp(prefix="hmsim_voice1_"))
    try:
        fakebin = _voice_sandbox(sim, uvx_ok=False)
        r = run_cmd(sim, "deploy\\setup_voice.bat", stdin="",
                    env=build_env(fakebin, {"HIVEMIND_NO_PAUSE": "1"}))
        check(f"{label}: speech to text ran with the default model",
              "[1/2] Speech to text - engine + model: parakeet-q4k"
              in r.stdout and "fake whisper ok" in r.stdout)
        check(f"{label}: TTS skipped", "[2/2] Text to speech - skipped"
              in r.stdout)
        check(f"{label}: exit 0", r.returncode == 0, f"(rc={r.returncode})")
        check(f"{label}: uvx never invoked",
              not (fakebin / "uvx.exe").exists())
    finally:
        shutil.rmtree(sim, ignore_errors=True)


def case_voice_tts_happy():
    label = "setup_voice --tts happy path"
    sim = Path(tempfile.mkdtemp(prefix="hmsim_voice2_"))
    try:
        fakebin = _voice_sandbox(sim, uvx_ok=True)
        r = run_cmd(sim, "deploy\\setup_voice.bat", args=["--tts"], stdin="",
                    env=build_env(fakebin, {"HIVEMIND_NO_PAUSE": "1"}))
        check(f"{label}: STT skipped", "[1/2] Speech to text - skipped"
              in r.stdout)
        if (fakebin / "uvx.exe").exists():
            check(f"{label}: env ready + model ready reported",
                  "[OK] Environment ready" in r.stdout
                  and "[OK] Model ready" in r.stdout)
        check(f"{label}: exit 0", r.returncode == 0, f"(rc={r.returncode})")
    finally:
        shutil.rmtree(sim, ignore_errors=True)


def case_voice_tts_env_fail():
    label = "setup_voice --tts without uvx"
    sim = Path(tempfile.mkdtemp(prefix="hmsim_voice3_"))
    try:
        _voice_sandbox(sim, uvx_ok=False)
        r = run_cmd(sim, "deploy\\setup_voice.bat", args=["--tts"], stdin="",
                    env=build_env(None, {"HIVEMIND_NO_PAUSE": "1"}))
        check(f"{label}: graceful warning",
              "[WARN] Could not prepare the Pocket TTS environment"
              in r.stdout)
        check(f"{label}: still exits 0 (optional step)", r.returncode == 0,
              f"(rc={r.returncode})")
    finally:
        shutil.rmtree(sim, ignore_errors=True)


def case_install_abort_paren_path():
    label = "install.bat abort on 'HiveMind (2)' path"
    sim = Path(tempfile.mkdtemp(prefix="hmsim_inst_")) / "HiveMind (2)"
    sim.mkdir(parents=True)
    try:
        shutil.copy2(ROOT / "install.bat", sim / "install.bat")
        r = run_cmd(sim, "install.bat", stdin="N\n")
        check(f"{label}: clean abort", r.returncode == 0
              and "Aborted - nothing was installed." in r.stdout,
              f"(rc={r.returncode})")
        assert_no_cmd_crash(r.stdout, r.stderr, label)
    finally:
        shutil.rmtree(sim.parent, ignore_errors=True)


def case_install_optionals_garbage_voice_pick():
    label = "install.bat optionals-only with voice pick 9"
    sim = Path(tempfile.mkdtemp(prefix="hmsim_opt_")) / "HiveMind (3)"
    sim.mkdir(parents=True)
    try:
        shutil.copy2(ROOT / "install.bat", sim / "install.bat")
        r = run_cmd(sim, "install.bat", stdin="O\n9\nN\nX\n")
        check(f"{label}: optionals path entered",
              "Optional parts only" in r.stdout)
        check(f"{label}: docker absent handled",
              "Docker not found" in r.stdout)
        check(f"{label}: garbage voice pick shows the skip hint",
              "Skipped - add it later with no reinstall" in r.stdout)
        check(f"{label}: summary reached with default port",
              "Installation finished!" in r.stdout
              and "http://localhost:8001" in r.stdout)
        check(f"{label}: exit 0", r.returncode == 0, f"(rc={r.returncode})")
        assert_no_cmd_crash(r.stdout, r.stderr, label)
    finally:
        shutil.rmtree(sim.parent, ignore_errors=True)


def main() -> int:
    case_setup_models_paren_path()
    case_setup_models_mkdir_failure()
    case_lsp_bad_token()
    case_voice_stt_default()
    case_voice_tts_happy()
    case_voice_tts_env_fail()
    case_install_abort_paren_path()
    case_install_optionals_garbage_voice_pick()
    return h.finish("installer_sim")


if __name__ == "__main__":
    sys.exit(main())

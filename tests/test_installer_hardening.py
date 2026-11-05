"""Installer hardening (audit r4): install_linux.sh + fetch_llamacpp.py.

Covers the crash / data-loss class found in the audit:
  1. rsync --delete semantics: every runtime/user-data path is excluded, so a
     re-run cannot wipe downloaded models (30 GB), llama.cpp builds, uv/HF
     caches or gateway/memory state. The tar fallback mirrors the same list.
  2. model_configs is RELEASE SOURCE (models_registry.py + per-model
     profiles) and must be installed; only the runtime-extended tag map is
     protected.
  3. error propagation: no silent `|| true`, package-manager/uv/curl failures
     warn and fall back instead of aborting a half-done install.
  4. temp collisions: no fixed /tmp names (TTS check wav via mktemp).
  5. non-systemd hosts: systemctl guarded, unit path reported.
  6. fetch_llamacpp: verify-before-delete staging swap, .staging dirs are
     not "installed builds", safe tar extraction (path traversal), chunked
     sha256.
  7. fetch_decider: `--dir` outside the repo root must not crash on the
     success path (target.relative_to raised ValueError).

Run: python tests/test_installer_hardening.py   (pure source guards + temp
dir simulations - no network, no install)
"""
from __future__ import annotations

import io
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def ok(name, extra=""):
    global passed
    passed += 1
    print(f"  PASS {name} {extra}")


def fail(name, extra=""):
    global failed
    failed += 1
    print(f"  FAIL {name} {extra}")


def check(name, cond, extra=""):
    (ok if cond else fail)(name, extra)


SH = (ROOT / "deploy" / "install_linux.sh").read_text(encoding="utf-8", errors="replace")
FL_SRC = (ROOT / "deploy" / "fetch_llamacpp.py").read_text(encoding="utf-8")

# ── 1. shell syntax gate ────────────────────────────────────────────────────
_bash = shutil.which("bash")
if _bash:
    r = subprocess.run([_bash, "-n", str(ROOT / "deploy" / "install_linux.sh")],
                       capture_output=True, text=True)
    check("bash -n install_linux.sh", r.returncode == 0, (r.stderr or "").strip()[:120])
else:
    check("bash -n install_linux.sh (bash unavailable - skipped)", True, "(no bash on PATH)")

LSP = (ROOT / "deploy" / "install_lsp_packs.bat").read_text(
    encoding="utf-8", errors="replace")
check("LSP check refreshes the PATH after winget/rustup installs",
      "PATH-REFRESH" in LSP
      and "GetEnvironmentVariable('Path','Machine')" in LSP
      and r"%ProgramFiles%\LLVM\bin" in LSP)

# ── 2. rsync --delete protection set (+ tar fallback parity) ───────────────
_rsync_block = SH[SH.index("rsync -a --delete"):SH.index('"$SCRIPT_DIR"/ "$INSTALL_DIR"/')]
_tar_block = SH[SH.index("tar -C \"$SCRIPT_DIR\""):SH.index('-cf - . | tar')]
_rsync_ex = set(re.findall(r"--exclude '([^']+)'", _rsync_block))
_tar_ex = {"./" + x if not x.startswith("./") else x
           for x in re.findall(r"--exclude='([^']+)'", _tar_block)}

# runtime data the update contract (update_prune.ps1 $protDirs) protects
for d in ("/models", "/llama", "/.cache", "/custom_prompts", "/.hive_uploads",
          "/learning_logs"):
    check(f"rsync protects {d}/ from --delete", d in _rsync_ex)
    check(f"tar fallback excludes .{d}", ("." + d) in _tar_ex)
# runtime state files (update_prune.ps1 $protFiles, not shipped by the repo)
for f in ("/models.json", "/memory.json", "/vision_model.json",
          "/routing_weights.json", "/gateway_state.json",
          "/gateway_audit.jsonl", "/gateway.disabled"):
    check(f"rsync protects {f}", f in _rsync_ex)
    check(f"tar fallback excludes .{f}", ("." + f) in _tar_ex)
# the runtime-extended tag map stays protected ...
check("runtime tag map protected",
      "/model_configs/gguf_filename_tags.json" in _rsync_ex
      and "./model_configs/gguf_filename_tags.json" in _tar_ex)
# ... while the REQUIRED SOURCE package is no longer skipped entirely
check("model_configs is installed (was wrongly excluded as user state)",
      "--exclude 'model_configs'" not in _rsync_block
      and "--exclude='./model_configs'" not in _tar_block)
# anchored: an unanchored `models` would also match model_configs/models/
check("models exclude is path-anchored", "/models" in _rsync_ex
      and "'models'" not in _rsync_block.replace("--exclude '/models'", ""))
check("every rsync exclude has a tar fallback twin",
      {"./" + x.lstrip("/") for x in _rsync_ex} ==
      {x for x in _tar_ex})

# ── 3. error propagation ────────────────────────────────────────────────────
check("no silent || true left", "|| true" not in SH.replace(
    "# ERROR-PROPAGATION (audit r4): this was `... || true`, which hid the", ""))
check("voice pip failure warns instead of dying silently",
      "if ! \"$INSTALL_DIR/.venv/bin/pip\" install -q -r" in SH
      and "requirements-voice.txt could not be installed" in SH)
check("package-manager failures are non-fatal warnings",
      "apt-get update -qq || echo" in SH
      and "dnf install -y curl python3 python3-pip || echo" in SH)
check("uv install failure falls through to the pip fallback",
      "if ! curl -LsSf https://astral.sh/uv/install.sh" in SH
      and "uv could not be installed (offline?)" in SH)
check("uv is installed where the script looks for it",
      'UV_INSTALL_DIR="/usr/local/bin"' in SH)
check("the script resolves the repo root from deploy/",
      'SCRIPT_DIR/../pyproject.toml' in SH)
check("Linux sync keeps optional packages (--inexact)",
      "--frozen --inexact" in SH)
check("the uv-managed .local runtime is excluded from re-sync",
      "--exclude '/.local'" in SH and "--exclude='./.local'" in SH)
check("uv sync failure falls back instead of aborting",
      "uv sync --all-extras --frozen --inexact; then" in SH
      and "falling back to python3+pip" in SH)
check("pip fallback still present", "python3 -m venv \"$INSTALL_DIR/.venv\"" in SH)

# ── 4. temp collisions / concurrency ────────────────────────────────────────
check("TTS check wav uses mktemp (no fixed /tmp name)",
      "mktemp \"${TMPDIR:-/tmp}/hivemind_tts_check.XXXXXXXX.wav\"" in SH
      and "/tmp/hivemind_tts_check.wav" not in SH)
check("fetch_llamacpp downloads into a per-process temp dir",
      "tempfile.mkdtemp(prefix=\"hivemind-llamacpp-\")" in FL_SRC
      and 'Path(tempfile.gettempdir()) / asset["name"]' not in FL_SRC)

# ── 5. systemd / ownership ──────────────────────────────────────────────────
check("systemctl guarded on non-systemd hosts",
      '[ -d /run/systemd/system ]' in SH
      and SH.index("[ -d /run/systemd/system ]") < SH.index("systemctl daemon-reload"))
check("non-systemd path reports the unit + manual start",
      "systemd is not running (container?)" in SH)
check("destination chowned to the service user",
      'chown -R hivemind:hivemind "$INSTALL_DIR" "$LOG_DIR"' in SH)
check("uv python provisioned under the install dir (not root's 0700 home)",
      "UV_PYTHON_INSTALL_DIR=\"$INSTALL_DIR/.cache/uv/python\"" in SH)

# quoting spot checks (paths with spaces must stay quoted)
check("rsync src/dst quoted", '"$SCRIPT_DIR"/ "$INSTALL_DIR"/' in SH)
check("service copy quoted", 'cp "$INSTALL_DIR/deploy/hivemind.service" "$SERVICE_FILE"' in SH)

# ── 6. fetch_llamacpp: verify-before-delete + staging ──────────────────────
check("extraction+swap factored (_extract_and_swap)", "def _extract_and_swap(" in FL_SRC)
check("old build removed only after verification",
      FL_SRC.index("def _extract_and_swap(") <
      FL_SRC.index('print(f"    Removing old version:'))
check("staging dirs are not 'installed builds'",
      'if ".staging" in exe.parent.name:' in FL_SRC)
check("safe tar extraction present", "filter=\"data\"" in FL_SRC)
check("digest hashed in chunks (no read_bytes RAM spike)",
      "def _sha256_file(" in FL_SRC and "tmp_zip.read_bytes()" not in FL_SRC)
check("cudart digest mismatch fails hard (not a warning)",
      "except RuntimeError as _cud_dig:" in FL_SRC)


def _import_llamacpp():
    import deploy.fetch_llamacpp as fl
    return fl


def test_existing_build_skips_staging():
    fl = _import_llamacpp()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        staging = root / "llama-b1200-bin-win-vulkan-x64.staging"
        (staging / "lib").mkdir(parents=True)
        (staging / fl.SERVER_NAME).write_text("half-extracted")
        good = root / "llama-b1100-bin-win-vulkan-x64"
        good.mkdir()
        (good / fl.SERVER_NAME).write_text("installed")
        old = fl.LLAMA_DIR
        fl.LLAMA_DIR = root
        try:
            have = fl.existing_build()
        finally:
            fl.LLAMA_DIR = old
        check("existing_build ignores .staging dirs",
              have is not None and have[0] == 1100, f"got {have}")


def test_extract_and_swap_preserves_old_build():
    fl = _import_llamacpp()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        target = root / "llama-b1000-bin-win-vulkan-x64"
        target.mkdir()
        (target / "keep.txt").write_text("working install")
        # corrupt archive: the old build must survive untouched
        bad = root / "llama-b1000-bin-win-vulkan-x64.zip"
        bad.write_bytes(b"PK\x03\x04 this is not a real zip")
        raised = False
        try:
            fl._extract_and_swap(bad, target, None)
        except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError, RuntimeError):
            raised = True
        check("corrupt archive raises", raised)
        check("corrupt archive keeps the old build", (target / "keep.txt").exists())
        check("no .staging leftovers after failure",
              not list(root.glob("*.staging")))
        # digest mismatch: same guarantee
        good_zip = root / "good.zip"
        with zipfile.ZipFile(good_zip, "w") as zf:
            zf.writestr("llama-server", "MZ fake binary")
        raised = False
        try:
            fl._extract_and_swap(good_zip, target, "sha256:" + "0" * 64)
        except RuntimeError:
            raised = True
        check("digest mismatch raises RuntimeError", raised)
        check("digest mismatch keeps the old build", (target / "keep.txt").exists())
        # valid archive + matching digest: atomic swap
        import hashlib
        digest = hashlib.sha256(good_zip.read_bytes()).hexdigest()
        fl._extract_and_swap(good_zip, target, "sha256:" + digest)
        check("valid archive replaces the old build",
              target.is_dir() and (target / "llama-server").exists()
              and not (target / "keep.txt").exists())
        check("swap leaves no staging dir", not list(root.glob("*.staging")))


def test_safe_tar_extract_rejects_traversal():
    fl = _import_llamacpp()
    # ".." members must be REJECTED outright
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        dest = root / "out"
        dest.mkdir()
        bad = root / "bad.tar.gz"
        with tarfile.open(bad, "w:gz") as tf:
            data = b"evil"
            ti = tarfile.TarInfo("../evil.txt")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        raised = False
        try:
            with tarfile.open(bad) as tf:
                fl._safe_tar_extractall(tf, dest)
        except tarfile.TarError:
            raised = True
        check("traversal rejected: ../evil.txt", raised)
        check("nothing written outside dest: ../evil.txt",
              not (root / "evil.txt").exists() and not (dest / "evil.txt").exists())
    # absolute members: either rejected (old-Python fallback) or sanitized
    # INTO the destination by tarfile's data filter - never outside.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        dest = root / "out"
        dest.mkdir()
        bad = root / "abs.tar.gz"
        with tarfile.open(bad, "w:gz") as tf:
            data = b"evil"
            ti = tarfile.TarInfo("/abs-evil.txt")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        raised = False
        try:
            with tarfile.open(bad) as tf:
                fl._safe_tar_extractall(tf, dest)
        except tarfile.TarError:
            raised = True
        contained = raised or ((dest / "abs-evil.txt").exists()
                               and not (root / "abs-evil.txt").exists())
        check("absolute member cannot escape dest", contained)


def test_sha256_file_chunked_matches():
    fl = _import_llamacpp()
    import hashlib
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "blob.bin"
        blob = bytes(range(256)) * 70000  # ~17.9 MB > one 1 MB chunk
        p.write_bytes(blob)
        check("_sha256_file matches hashlib",
              fl._sha256_file(p) == hashlib.sha256(blob).hexdigest())


def test_decider_outside_root_no_crash():
    import deploy.fetch_decider as fd
    with tempfile.TemporaryDirectory() as td:
        outside = Path(td) / "elsewhere" / "decider"
        with mock.patch.object(shutil, "which", return_value=str(Path(td) / "hf")), \
                mock.patch.object(fd.subprocess, "call", return_value=0):
            rc = fd.main(["--dir", str(outside)])
        check("fetch_decider --dir outside ROOT does not crash", rc == 0)
        check("decider target parent created", outside.parent.exists())


if __name__ == "__main__":
    test_existing_build_skips_staging()
    test_extract_and_swap_preserves_old_build()
    test_safe_tar_extract_rejects_traversal()
    test_sha256_file_chunked_matches()
    test_decider_outside_root_no_crash()
    print()
    print(f"SUMMARY installer_hardening: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)

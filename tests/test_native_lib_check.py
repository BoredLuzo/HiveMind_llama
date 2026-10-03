"""Native-lib check must accept the Linux .so layout (2026-09-13).

Live (Ubuntu, commilitone): the installer hung forever in "DLL check pending
(ggml-vulkan.so)" and the backend probe would report "DLLs missing" — both
globs looked for a FLAT "ggml-vulkan.so" next to llama-server, but the
ubuntu tarballs ship lib/libggml-vulkan.so (lib prefix + lib/ subdir).
Fix: rglob "*ggml-vulkan.so" covers both layouts, Windows .dll behavior
unchanged.

Run: python tests/test_native_lib_check.py
Exit 0 = all pass, Exit 1 = failures.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import platform

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


def test_fetch_check_windows_layout():
    import deploy.fetch_llamacpp as fm
    tmp = Path(tempfile.mkdtemp(prefix="hvm_dll_w_"))
    (tmp / "llama-server.exe").write_bytes(b"x")
    (tmp / "ggml-vulkan.dll").write_bytes(b"x")
    missing = fm._verify_backend_dlls(tmp / "llama-server.exe", "vulkan")
    if not missing:
        ok("Installer-Check: flaches Windows-Layout (ggml-vulkan.dll) komplett")
    else:
        fail("fetch_win", f"missing={missing}")


def test_fetch_check_linux_lib_layout():
    import deploy.fetch_llamacpp as fm
    tmp = Path(tempfile.mkdtemp(prefix="hvm_dll_l_"))
    (tmp / "llama-server").write_bytes(b"x")
    libdir = tmp / "lib"
    libdir.mkdir()
    (libdir / "libggml-vulkan.so").write_bytes(b"x")
    old = fm._IS_WIN
    try:
        fm._IS_WIN = False
        missing = fm._verify_backend_dlls(tmp / "llama-server", "vulkan")
    finally:
        fm._IS_WIN = old
    if not missing:
        ok("Installer-Check: Linux-Layout lib/libggml-vulkan.so wird erkannt (Live-Bug)")
    else:
        fail("fetch_linux", f"missing={missing}")


def test_probe_linux_lib_layout():
    import backend.llama_manager_utils as lmu
    tmp = Path(tempfile.mkdtemp(prefix="hvm_dll_p_"))
    (tmp / "llama-server").write_bytes(b"x")
    libdir = tmp / "lib"
    libdir.mkdir()
    (libdir / "libggml-vulkan.so").write_bytes(b"x")
    real_system = platform.system
    try:
        platform.system = lambda: "Linux"
        r_ok = lmu._probe_backend_dlls(str(tmp / "llama-server"), "vulkan")
        (libdir / "libggml-vulkan.so").unlink()
        r_missing = lmu._probe_backend_dlls(str(tmp / "llama-server"), "vulkan")
    finally:
        platform.system = real_system
    if r_ok is True and r_missing is False:
        ok("Backend-Probe: Linux-Layout erkannt, fehlende Lib korrekt gemeldet")
    else:
        fail("probe_linux", f"ok={r_ok} missing={r_missing}")


def test_probe_windows_layout_unchanged():
    import backend.llama_manager_utils as lmu
    tmp = Path(tempfile.mkdtemp(prefix="hvm_dll_pw_"))
    (tmp / "llama-server.exe").write_bytes(b"x")
    (tmp / "ggml-vulkan.dll").write_bytes(b"x")
    r = lmu._probe_backend_dlls(str(tmp / "llama-server.exe"), "vulkan")
    if r is True:
        ok("Backend-Probe: Windows-Layout unverändert ok")
    else:
        fail("probe_win", f"r={r}")


def test_binary_discovery_nested_layout():
    """ubuntu tarball = double nesting: <root>/<target>/llama-bXXXX/llama-server.
    Runtime autodiscovery (flat "*/llama-server" glob) missed those entirely."""
    import tempfile
    import backend.llama_config as lc
    tmp = Path(tempfile.mkdtemp(prefix="hvm_disc_"))
    nested = tmp / "llama-b10941-bin-ubuntu-vulkan-x64" / "llama-b10941"
    nested.mkdir(parents=True)
    (nested / "llama-server.exe").write_bytes(b"x")
    real_root = lc._LLAMA_ROOT
    try:
        lc._LLAMA_ROOT = tmp
        found = lc._find_llama_server()
    finally:
        lc._LLAMA_ROOT = real_root
    if found and found.name == "llama-server.exe" and "llama-b10941-bin-ubuntu-vulkan-x64" in str(found):
        ok("Autodiscovery findet llama-server im doppelt verschachtelten Layout")
    else:
        fail("discovery_nested", f"found={found}")


def test_source_guards():
    root = Path(__file__).parent.parent
    checks = [
        ("backend/llama_config.py", "NESTED-LAYOUT (2026-09-13)"),
        ("backend/manager_load.py", "POSIX-LIB-PATH (2026-09-13)"),
        ("backend/manager_load.py", "LD_LIBRARY_PATH"),
        ("deploy/fetch_llamacpp.py", "_stem.lower() in Path(n).name.lower()"),
    ]
    bad = [f"{rel}:{needle}" for rel, needle in checks
           if needle not in (root / rel).read_text(encoding="utf-8")]
    if not bad:
        ok("source_guards (Nested-Discovery, LD_LIBRARY_PATH, AV-stem-match)")
    else:
        fail("source_guards", f"fehlt: {bad}")

if __name__ == "__main__":
    test_fetch_check_windows_layout()
    test_fetch_check_linux_lib_layout()
    test_probe_linux_lib_layout()
    test_probe_windows_layout_unchanged()
    test_binary_discovery_nested_layout()
    test_source_guards()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)

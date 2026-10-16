"""Linux/POSIX support paths (2026-09-08): gpu_backend cpu, POSIX binary
discovery, /proc/meminfo parser, /proc port-kill helpers, CPU gating guards.
Pure functions + source guards — no GPU/llama-server needed."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

passed = failed = 0


def ok(m):
    global passed
    passed += 1
    print(f"  PASS  {m}")


def fail(m):
    global failed
    failed += 1
    print(f"  FAIL  {m}")


def test_gpu_backend_cpu():
    import importlib
    os.environ["HIVEMIND_GPU_BACKEND"] = "cpu"
    import backend.llama_config as lc
    importlib.reload(lc)
    if lc.GPU_BACKEND == "cpu":
        ok("HIVEMIND_GPU_BACKEND=cpu accepted")
    else:
        fail(f"GPU_BACKEND={lc.GPU_BACKEND}")
    os.environ.pop("HIVEMIND_GPU_BACKEND", None)
    importlib.reload(lc)


def test_binary_discovery_posix():
    from unittest.mock import patch
    import tempfile
    import backend.llama_config as lc
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "llama-b9999-bin-ubuntu-vulkan-x64").mkdir()
        (root / "llama-b9999-bin-ubuntu-vulkan-x64" / "llama-server").write_text("x")
        (root / "llama-b8888-bin-ubuntu-x64").mkdir()
        (root / "llama-b8888-bin-ubuntu-x64" / "llama-server").write_text("x")
        old_root = lc._LLAMA_ROOT
        old_env = os.environ.get("HIVEMIND_GPU_BACKEND")
        try:
            lc._LLAMA_ROOT = root
            with patch("platform.system", return_value="Linux"):
                p = lc._find_llama_server()
            if p.name == "llama-server" and "b9999" in str(p):
                ok("POSIX discovery: newest build, no .exe suffix")
            else:
                fail(f"POSIX discovery returned {p}")
            # cpu backend: prefers the plain ubuntu build over vulkan/cuda/rocm
            os.environ["HIVEMIND_GPU_BACKEND"] = "cpu"
            import importlib
            importlib.reload(lc)
            lc._LLAMA_ROOT = root  # reload re-derives _LLAMA_ROOT — re-patch
            with patch("platform.system", return_value="Linux"):
                p2 = lc._find_llama_server()
            if p2.name == "llama-server" and "b8888" in str(p2):
                ok("cpu backend prefers the non-vulkan/cuda build")
            else:
                fail(f"cpu discovery returned {p2}")
        finally:
            lc._LLAMA_ROOT = old_root
            if old_env is None:
                os.environ.pop("HIVEMIND_GPU_BACKEND", None)
            else:
                os.environ["HIVEMIND_GPU_BACKEND"] = old_env
            import importlib
            importlib.reload(lc)


def test_meminfo_parser():
    from backend.llama_manager_utils import _parse_proc_meminfo
    sample = "MemTotal:       16384000 kB\nMemFree:        1024 kB\nMemAvailable:    8388608 kB\n"
    v = _parse_proc_meminfo(sample)
    (ok if abs(v - 8.0) < 0.01 else fail)(f"meminfo MemAvailable 8388608 kB -> {v:.2f} GB")
    (ok if _parse_proc_meminfo("no useful line") == -1.0 else fail)("meminfo missing key -> -1.0")


def test_proc_net_parser():
    from backend.llama_manager_utils import _inodes_for_port
    sample = (
        "  sl local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:1FA9 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 12345\n"
        "   1: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 99999\n"
    )
    inodes = _inodes_for_port(sample, 8105)  # 0x1FA9 = 8105
    (ok if inodes == {"12345"} else fail)(f"/proc/net/tcp port->inode {inodes}")
    (ok if _inodes_for_port(sample, 9999) == set() else fail)("non-listening/wrong port -> empty")


def test_source_guards():
    root = Path(__file__).parent.parent
    guards = [
        ("backend/manager_load.py", 'GPU_BACKEND != "cpu" and type(self)._device_flag_supported'),
        ("backend/manager_load.py", '_gpu_layers = 0'),
        ("backend/manager_load.py", 'GPU_BACKEND != "cpu" and type(self)._backend_devices_ok'),
        ("backend/manager_load.py", 'if _moe_count > 0 and GPU_BACKEND != "cpu":'),
        ("backend/manager_evict.py", '"cpu-backend"'),
        ("backend/llama_vram_table.py", "no VRAM to reclaim"),
        ("deploy/fetch_llamacpp.py", '"cpu"'),
        ("deploy/fetch_llamacpp.py", "tarfile.open"),
        ("hive_functions/language_config.py", "head -30"),
        ("backend/llama_manager_utils.py", "_kill_port_via_proc"),
        ("backend/llama_config.py", '"llama-server"'),
        ("server.py", '"vulkan", "cuda", "cpu"'),
    ]
    bad = []
    for rel, needle in guards:
        try:
            txt = (root / rel).read_text(encoding="utf-8")
            if needle not in txt:
                bad.append(f"{rel}::{needle[:40]}")
        except Exception as e:
            bad.append(f"{rel} (read fail {e})")
    (ok if not bad else fail)(f"source guards {'' if not bad else str(bad)}")


def test_installer_is_executable():
    """README says `sudo deploy/install_linux.sh` - that only works when the
    file carries the executable bit in git (a fresh clone has no local chmod)."""
    import subprocess
    root = Path(__file__).parent.parent
    r = subprocess.run(["git", "ls-files", "-s", "deploy/install_linux.sh"],
                       cwd=str(root), capture_output=True, text=True)
    out = (r.stdout or "").strip()
    (ok if out.startswith("100755") else fail)(f"install_linux.sh mode {out[:6] or '?'} (want 100755)")
    mode = os.stat(root / "deploy" / "install_linux.sh").st_mode
    (ok if mode & 0o111 or os.name == "nt" else fail)("install_linux.sh executable on disk (or Windows)")


def test_shell_mode_is_posix_on_unix():
    """A bash-less distro (Alpine: /bin/sh only) must not be labelled
    'powershell' - execution already falls back to /bin/sh, the label lied."""
    from unittest.mock import patch
    from tools.handlers.exec_tools import _run_bash_shell_mode, shell_os_hint
    with patch("sys.platform", "linux"):
        m = _run_bash_shell_mode()
    (ok if m == "bash" else fail)(f"shell mode on linux = {m!r} (want 'bash')")
    hint = shell_os_hint("bash", "linux")
    # the unix branch says "bash/sh on Unix ... not PowerShell" - it must not
    # carry the Windows CRITICAL block
    (ok if "RUNTIME SHELL: bash" in hint and "CRITICAL - Windows PowerShell" not in hint
     else fail)("linux hint names bash and carries no Windows block")
    src = (Path(__file__).parent.parent / "tools" / "handlers" / "exec_tools.py"
           ).read_text(encoding="utf-8")
    (ok if 'if sys.platform != "win32":\n        return "bash"' in src
     else fail)("POSIX short-circuit present in _run_bash_shell_mode")


def test_fetch_llamacpp_documents_linux():
    src = (Path(__file__).parent.parent / "deploy" / "fetch_llamacpp.py"
           ).read_text(encoding="utf-8")
    head = src[:900]
    (ok if "Linux" in head else fail)("fetch_llamacpp docstring mentions Linux")
    (ok if "ubuntu" in src and "tarfile.open" in src else fail)("ubuntu assets + tarfile handling present")


if __name__ == "__main__":
    test_gpu_backend_cpu()
    test_binary_discovery_posix()
    test_meminfo_parser()
    test_proc_net_parser()
    test_source_guards()
    test_installer_is_executable()
    test_shell_mode_is_posix_on_unix()
    test_fetch_llamacpp_documents_linux()
    print(f"\n=== {passed} passed, {failed} failed ===")
    sys.exit(1 if failed else 0)

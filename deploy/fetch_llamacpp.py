"""
fetch_llamacpp.py
=================
Downloads the newest llama.cpp nightly release (Windows and Linux) from GitHub
and extracts it to <HiveMind>/llama/.

Assets are picked per platform: win-*-x64 on Windows, ubuntu-*-x64 on Linux
(.zip there, .tar.gz on Linux) - see ASSET_REGEX / _OS_TAG below.

Usage (Windows):
  python deploy\\fetch_llamacpp.py --backend vulkan            # AMD/Intel (newest nightly)
  python deploy\\fetch_llamacpp.py --backend cuda              # NVIDIA (newest nightly)
  python deploy\\fetch_llamacpp.py --backend vulkan --upgrade  # upgrade if a newer build exists
  python deploy\\fetch_llamacpp.py --backend vulkan --pinned   # use deploy/llama_build_pin.txt
  python deploy\\fetch_llamacpp.py --backend vulkan --force    # re-download even if a build exists

Usage (Linux, also run by deploy/install_linux.sh):
  ./.venv/bin/python deploy/fetch_llamacpp.py --backend vulkan|cuda|rocm|cpu [--upgrade]

Stdlib only (urllib/zipfile/tarfile) — no extra dependencies needed.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import platform
import zipfile
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    _reconf = getattr(_s, "reconfigure", None)
    if callable(_reconf):
        try:
            _reconf(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

ROOT = Path(__file__).resolve().parent.parent
LLAMA_DIR = ROOT / "llama"
PIN_FILE = Path(__file__).resolve().parent / "llama_build_pin.txt"

API_LATEST = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"


def _read_pinned_tag(pin_file: Path | None = None) -> str:
    """Return the pinned llama.cpp release tag from `deploy/llama_build_pin.txt`, or ''."""
    pf = pin_file or PIN_FILE
    if not pf.is_file():
        return ""
    try:
        for line in pf.read_text(encoding="utf-8").splitlines():
            clean = line.strip()
            if clean and not clean.startswith("#"):
                return clean
    except OSError:
        return ""
    return ""


def _configured_update_channel(settings_file: Path | None = None) -> str:
    """Return 'pinned' when settings.json sets llama_update_channel='pinned', else 'latest'."""
    sf = settings_file or (ROOT / "settings.json")
    if not sf.is_file():
        return "latest"
    try:
        data = json.loads(sf.read_text(encoding="utf-8", errors="replace"))
        if isinstance(data, dict):
            ch = str(data.get("llama_update_channel") or "").strip().lower()
            if ch in ("latest", "pinned"):
                return ch
    except (OSError, ValueError, TypeError):
        return "latest"
    return "latest"


def _resolve_releases(
    backend: str,
    rx,
    *,
    tag: str = "",
    use_pinned: bool = False,
    use_latest: bool = False,
    cuda_version: str = "",
    max_cuda_version: str = "",
) -> list[dict]:
    """Resolve release metadata: newest nightly by default, or pinned/explicit tag."""
    want = _parse_cuda_tuple(cuda_version)
    max_want = _parse_cuda_tuple(max_cuda_version)

    def _has_matching_asset(rel: dict, *, ignore_cuda: bool = False) -> bool:
        assets = rel.get("assets") or []
        for a in assets:
            name = str(a.get("name", "") or "")
            if not rx.match(name):
                continue
            if ignore_cuda or (want is None and max_want is None):
                return True
            cv = _cuda_version_of(name, rx)
            if want is not None and cv[:2] != want:
                continue
            if max_want is not None and cv != (0,) and cv[:2] > max_want:
                continue
            return True
        return False

    explicit_tag = tag.strip()
    if not explicit_tag and not use_latest:
        explicit_tag = os.environ.get("HIVEMIND_LLAMA_TAG", "").strip()
    pin_tag = _read_pinned_tag()
    want_tag = explicit_tag or (pin_tag if (use_pinned and not use_latest) else "")
    if want_tag:
        try:
            pin_rel = http_json(f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{want_tag}")
            allow_unfiltered = bool(explicit_tag and not cuda_version)
            if _has_matching_asset(pin_rel, ignore_cuda=allow_unfiltered):
                label = "pinned" if (use_pinned and not explicit_tag) else "requested"
                print(f"       Using {label} llama.cpp release: {want_tag} (default is newest nightly)")
                return [pin_rel]
            print(f"       [INFO] Release '{want_tag}' has no compatible {backend} asset for {_OS_TAG} - falling back to newest nightly")
        except (OSError, ValueError, http.client.HTTPException) as e:
            print(f"       [INFO] Release '{want_tag}' unavailable ({e}) - falling back to newest nightly")

    releases: list[dict] = []
    raw_matches: list[dict] = []
    try:
        _rels = http_json("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=100")
        if isinstance(_rels, list):
            for _rel in _rels:
                if not isinstance(_rel, dict):
                    continue
                _tag = str(_rel.get("tag_name") or "")
                if not re.fullmatch(r"b\d{4,}", _tag):
                    continue
                if _has_matching_asset(_rel):
                    releases.append(_rel)
                    if len(releases) >= 5:
                        break
                elif (want is not None or max_want is not None) and len(raw_matches) < 5 and _has_matching_asset(_rel, ignore_cuda=True):
                    raw_matches.append(_rel)
        if not releases and raw_matches:
            releases = raw_matches
        if releases:
            _tags_preview = ", ".join(str(r.get("tag_name") or "?") for r in releases[:3])
            print(f"       Newest nightly candidate(s) with {backend} asset: {_tags_preview}")
    except Exception as e:
        print(f"       [WARNING] Nightly list could not be fetched: {e}")

    main_rel: dict | None = None
    if not releases:
        try:
            main_rel = http_json(API_LATEST)
            if isinstance(main_rel, dict):
                nt = next((a for a in main_rel.get("assets", []) if a.get("name") == "nightly-tag.txt"), None)
                if nt:
                    try:
                        _tag = http_text(nt["browser_download_url"]).strip()
                        if _tag:
                            _nt_rel = http_json(f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{_tag}")
                            if _has_matching_asset(_nt_rel) or _has_matching_asset(_nt_rel, ignore_cuda=(not cuda_version)):
                                releases = [_nt_rel]
                                print(f"       Nightly release resolved (stable pointer): {_tag}")
                    except Exception as e:
                        print(f"       [WARNING] Nightly tag could not be resolved: {e}")
                if not releases and (_has_matching_asset(main_rel) or _has_matching_asset(main_rel, ignore_cuda=(not cuda_version))):
                    releases = [main_rel]
                    print(f"       Latest release resolved: {main_rel.get('tag_name', '?')}")
        except (OSError, ValueError, http.client.HTTPException) as e:
            print(f"       [WARNING] Latest release could not be fetched: {e}")

    if not releases and not use_latest and pin_tag and pin_tag != want_tag:
        try:
            pin_rel = http_json(f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{pin_tag}")
            if _has_matching_asset(pin_rel) or _has_matching_asset(pin_rel, ignore_cuda=(not cuda_version)):
                print(f"       [INFO] GitHub latest unavailable - falling back to pinned release: {pin_tag}")
                return [pin_rel]
        except (OSError, ValueError, http.client.HTTPException) as e:
            print(f"       [WARNING] Pinned fallback '{pin_tag}' unavailable: {e}")

    if not releases and isinstance(main_rel, dict):
        releases = [main_rel]
    return releases

_IS_WIN = platform.system() == "Windows"
_OS_TAG = "win" if _IS_WIN else "ubuntu"
_ASSET_EXT = r"\.zip" if _IS_WIN else r"\.(?:zip|tar\.gz)"
SERVER_NAME = "llama-server.exe" if _IS_WIN else "llama-server"
ASSET_REGEX = {
    "vulkan": re.compile(rf"^llama-b(\d+)-bin-{_OS_TAG}-vulkan-x64{_ASSET_EXT}$", re.IGNORECASE),
    # CUDA assets are named e.g. llama-b1234-bin-win-cuda-12.4-x64.zip
    # or llama-b1234-bin-win-cuda-13.3-x64.zip — the version is captured,
    # so that with equal build numbers the NEWER CUDA runtime wins
    # (CUDA-VERSION-FIX 2026-08-27: previously cuda-12.4 was always pulled,
    # even when the driver supports 13.x — that left --list-devices empty).
    "cuda": re.compile(rf"^llama-b(\d+)-bin-{_OS_TAG}-cuda-([\d.]+)-x64{_ASSET_EXT}$", re.IGNORECASE),
    # CPU build (no backend tag in the name) + ROCm (Linux):
    "cpu": re.compile(rf"^llama-b(\d+)-bin-{_OS_TAG}-x64{_ASSET_EXT}$", re.IGNORECASE),
    "rocm": re.compile(rf"^llama-b(\d+)-bin-{_OS_TAG}-rocm(?:-[\d.]+)?-x64{_ASSET_EXT}$", re.IGNORECASE),
}


def _cuda_version_of(name: str, rx) -> tuple[int, ...]:
    """CUDA version from the asset name (e.g. 13.3 -> (13, 3)); (0,) otherwise."""
    m = rx.match(name or "")
    if not m or len(m.groups()) < 2:
        return (0,)
    try:
        return tuple(int(x) for x in m.group(2).split("."))
    except Exception:
        return (0,)


def _parse_cuda_tuple(cuda_version: str) -> tuple[int, ...] | None:
    if not cuda_version:
        return None
    try:
        parts = tuple(int(x) for x in cuda_version.split(".")[:2])
        return parts if len(parts) >= 1 else None
    except ValueError:
        return None


def _pick_asset(
    releases: list[dict],
    rx,
    cuda_version: str = "",
    max_cuda_version: str = "",
) -> tuple[dict | None, str]:
    """Highest build wins; with equal build the higher CUDA version.

    cuda_version: optional "major.minor" — filters assets exactly to this
    version (for old drivers without 13.x support).
    max_cuda_version: optional "major.minor" ceiling — keeps assets whose
    CUDA runtime <= max_cuda_version when an exact runtime build is absent.
    """
    want = _parse_cuda_tuple(cuda_version)
    max_want = _parse_cuda_tuple(max_cuda_version)
    best = None
    best_key = (0, (0,))
    best_tag = ""
    for rel in releases:
        for a in rel.get("assets", []):
            m = rx.match(a.get("name", ""))
            if not m:
                continue
            build = int(m.group(1))
            cv = _cuda_version_of(a.get("name", ""), rx)
            if want is not None and cv[:2] != want:
                continue
            if max_want is not None and cv != (0,) and cv[:2] > max_want:
                continue
            key = (build, cv)
            if key > best_key:
                best, best_key, best_tag = a, key, rel.get("tag_name", "?")
    return best, best_tag


def _find_nvidia_smi() -> str | None:
    """Locate nvidia-smi on PATH or standard Windows driver locations."""
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if _IS_WIN:
        sys_root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
        prog_files = Path(os.environ.get("ProgramFiles") or r"C:\Program Files")
        for cand in (
            sys_root / "System32" / "nvidia-smi.exe",
            prog_files / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
        ):
            if cand.is_file():
                return str(cand)
    return None


def _detect_cuda_version_from_driver() -> str:
    """Read 'CUDA Version: 13.1' from nvidia-smi ('' if not detectable)."""
    smi = _find_nvidia_smi()
    if not smi:
        return ""
    for args in ([smi], [smi, "-q"]):
        try:
            out = subprocess.run(
                args, capture_output=True, text=True, timeout=30,
                encoding="utf-8", errors="replace",
            )
            m = re.search(r"CUDA Version\s*:\s*(\d+\.\d+)", out.stdout)
            if m:
                return m.group(1)
        except Exception:
            continue
    return ""


def http_json(url: str):
    headers = {
        "User-Agent": "HiveMind-Installer",
        "Accept": "application/vnd.github+json",
    }
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def http_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "HiveMind-Installer"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def download(url: str, dest: Path, max_attempts: int = 6) -> None:
    """Download a file with resume + retries.

    Flaky connections used to abort the whole install with a traceback
    mid-download. Now:
      - a partial file is RESUMED via HTTP Range instead of restarted,
      - transient errors (reset / timeout / EOF / IncompleteRead) retry
        with backoff,
      - the byte count is verified against Content-Length; a short body
        raises instead of silently producing a corrupt ZIP.
    """
    chunk = 256 * 1024
    for attempt in range(1, max_attempts + 1):
        try:
            # Already-complete file: a Range request from EOF answers 416.
            # Probe Content-Length once; if the local size matches, we're done
            # (the caller CRC-checks archives before extracting anyway).
            if dest.exists():
                resume_from = dest.stat().st_size
                if resume_from:
                    _probe = urllib.request.Request(url, headers={
                        "User-Agent": "HiveMind-Installer", "Range": f"bytes={resume_from - 1}-{resume_from - 1}"})
                    try:
                        with urllib.request.urlopen(_probe, timeout=60) as pr:
                            _cr = pr.headers.get("Content-Range") or ""
                        _m = re.match(r"^bytes \d+-(\d+)/(\d+)$", _cr)
                        if _m and resume_from == int(_m.group(2)):
                            return  # fully downloaded already
                        if _m and resume_from > int(_m.group(2)):
                            dest.unlink(missing_ok=True)
                    except urllib.error.HTTPError as pe:
                        if pe.code == 416:
                            dest.unlink(missing_ok=True)
                        else:
                            raise
            resume_from = dest.stat().st_size if dest.exists() else 0
            headers = {"User-Agent": "HiveMind-Installer"}
            if resume_from:
                headers["Range"] = f"bytes={resume_from}-"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=120) as r:
                _cr_main = r.headers.get("Content-Range") or ""
                _cr_start = None
                if r.getcode() == 206 and _cr_main:
                    _cm = re.match(r"^bytes\s+(\d+)-", _cr_main)
                    if _cm:
                        _cr_start = int(_cm.group(1))
                if r.getcode() == 206 and resume_from and (_cr_start is None or _cr_start == resume_from):
                    total = resume_from + int(r.headers.get("Content-Length") or 0)
                    mode = "ab"
                else:
                    # Server ignored Range or resumed from wrong offset -> restart cleanly.
                    resume_from = 0
                    total = int(r.headers.get("Content-Length") or 0)
                    mode = "wb"
                done = resume_from
                with open(dest, mode) as f:
                    while True:
                        data = r.read(chunk)
                        if not data:
                            break
                        f.write(data)
                        done += len(data)
                        if total:
                            pct = min(100, done * 100 // total)
                            sys.stdout.write(f"\r    {done / (1024*1024):.0f} / {total / (1024*1024):.0f} MB ({pct}%)")
                            sys.stdout.flush()
                sys.stdout.write("\n")
            if total and done < total:
                raise IOError(f"incomplete download ({done}/{total} bytes)")
            return
        except Exception as e:
            sys.stdout.write("\n")
            if attempt >= max_attempts:
                print(f"    [ERROR] Download failed after {max_attempts} attempts: {e}")
                raise
            backoff = min(60, 2 ** attempt)
            print(f"    [WARNING] Download error ({e}) - retry {attempt + 1}/{max_attempts} in {backoff}s")
            time.sleep(backoff)


def _sha256_file(path: Path) -> str:
    """SHA-256 of a file, read in 1 MB chunks.

    The digest check used to slurp the WHOLE archive into RAM in one go
    (the CUDA tarballs are ~1.5 GB) - on a small install box that is an
    avoidable MemoryError.
    """
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_tar_extractall(tf: tarfile.TarFile, dest: Path) -> None:
    """extractall hardened against path traversal (audit r4).

    An archive member like `../../x` or an absolute path would write OUTSIDE
    the install dir. Python 3.12+ (and the security backports) supports
    filter="data", which rejects those, device nodes and links pointing out
    of the destination. Older interpreters get an explicit per-member check
    instead of an unsafe fallback.
    """
    try:
        tf.extractall(dest, filter="data")
        return
    except TypeError:
        pass  # Python without filter= support: validate members ourselves
    dest_r = os.path.realpath(dest)
    for m in tf.getmembers():
        name = m.name.replace("\\", "/")
        if name.startswith("/") or name.startswith("../"):
            raise tarfile.ExtractError(f"unsafe archive member: {m.name!r}")
        final = os.path.normpath(os.path.join(dest_r, name))
        if final != dest_r and not final.startswith(dest_r + os.sep):
            raise tarfile.ExtractError(f"unsafe archive member: {m.name!r}")
        if not (m.isreg() or m.isdir() or m.issym() or m.islnk()):
            raise tarfile.ExtractError(f"unsafe member type: {m.name!r}")
        tf.extract(m, dest, set_attrs=False)


def _extract_and_swap(tmp_zip: Path, target: Path, digest: str | None) -> None:
    """Verify + extract the archive, then atomically replace `target`.

    The previous build is only removed AFTER the new archive passed the
    digest check and the CRC/extraction: the old code deleted the existing
    build first, so one corrupt/interrupted download destroyed the working
    install. The `.staging` dir is skipped by existing_build(), so an
    interrupted run cannot make a half-extracted folder look "installed".
    """
    staging = target.with_name(target.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        if isinstance(digest, str) and digest.lower().startswith("sha256:"):
            _h = _sha256_file(tmp_zip)
            if _h != digest.split(":", 1)[1].strip().lower():
                raise RuntimeError(
                    f"llama.cpp asset digest mismatch ({_h[:16]}...) - "
                    "refusing to install a tampered/incomplete binary")
        _is_targz = tmp_zip.name.lower().endswith(".tar.gz")
        if _is_targz:
            with tarfile.open(tmp_zip) as tf:
                _safe_tar_extractall(tf, staging)
        else:
            with zipfile.ZipFile(tmp_zip) as zf:
                # CRC-check every member before extracting: a ZIP whose download
                # was interrupted mid-body would otherwise unpack into broken
                # binaries that "find no devices" or crash at startup.
                bad = zf.testzip()
                if bad is not None:
                    raise zipfile.BadZipFile(f"corrupt member: {bad}")
                zf.extractall(staging)
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError, RuntimeError):
        # any failure leaves the OLD build in place (staging goes, target stays)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    backup = target.with_name(target.name + ".old")
    shutil.rmtree(backup, ignore_errors=True)
    if target.exists():
        print(f"    Removing old version: {target.name}")
        try:
            target.rename(backup)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise OSError(
                f"Cannot replace {target} because files are locked (stop llama-server first): {exc}"
            ) from exc
    try:
        staging.rename(target)
    except OSError:
        if backup.exists() and not target.exists():
            try:
                backup.rename(target)
            except OSError:
                pass
        raise
    shutil.rmtree(backup, ignore_errors=True)


def _matches_backend(path_str: str, backend: str | None) -> bool:
    if not backend:
        return True
    low = path_str.lower()
    if backend == "cpu":
        return not any(tok in low for tok in ("vulkan", "cuda", "rocm"))
    return backend.lower() in low


def existing_build(backend: str | None = None) -> tuple[int, Path] | None:
    """Find the highest already installed build (optionally matching backend)."""
    best: tuple[int, Path] | None = None
    if LLAMA_DIR.is_dir():
        for exe in LLAMA_DIR.rglob(SERVER_NAME):
            # STAGING / OLD (audit r4): half-extracted `.staging` or `.old`
            # backups are NOT installed builds.
            if ".staging" in exe.parent.name:
                continue
            if any(p.endswith((".staging", ".old")) for p in exe.parts):
                continue
            if not _matches_backend(str(exe.parent), backend):
                continue
            m = re.search(r"b(\d{4,})", str(exe.parent))
            b = int(m.group(1)) if m else 0
            if best is None or b > best[0]:
                best = (b, exe.parent)
    return best


def _verify_backend_dlls(exe: Path, backend: str) -> list[str]:
    """Check that the backend runtime DLLs sit next to llama-server.exe.

    CUDA builds need ggml-cuda.dll + the bundled CUDA runtime DLLs
    (cudart64_*, cublas64_*, cublasLt64_*); Vulkan builds need ggml-vulkan.dll.
    The official llama.cpp Windows ZIPs bundle these — if they are missing,
    llama-server finds no devices ("--device CUDA0" needs them). Returns the
    list of missing DLL names (empty = complete).
    """
    if backend in ("cpu", "rocm"):
        return []  # CPU/ROCm: keine gesonderten Runtime-DLLs neben dem Binary erwartet
    dll_dir = Path(exe).parent
    _so = ".dll" if _IS_WIN else ".so"
    missing: list[str] = []
    if backend == "cuda":
        # SO-LAYOUT (2026-09-13): ubuntu tarballs ship lib/libggml-cuda.so —
        # rglob covers lib prefix + lib/ subdir; Windows keeps flat .dll.
        if not list(dll_dir.rglob(f"*ggml-cuda{_so}")):
            missing.append(f"*ggml-cuda{_so}")
        if _IS_WIN:
            for base in ("cudart64", "cublas64", "cublasLt64"):
                if not list(dll_dir.glob(f"{base}*.dll")):
                    missing.append(f"{base}*.dll")
    else:
        # Directory enumeration, not exists()/stat: while antivirus is actively
        # scanning a freshly extracted DLL, stat() can fail (ACCESS_DENIED)
        # while the name is still listed — a healthy build must not look broken.
        # SO-LAYOUT (2026-09-13): ubuntu tarballs ship lib/libggml-vulkan.so —
        # a flat glob for "ggml-vulkan.so" next to the binary never matched and
        # the installer hung in the "DLL check pending" loop forever.
        if not list(dll_dir.rglob(f"*ggml-vulkan{_so}")):
            missing.append(f"*ggml-vulkan{_so}")
    return missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["vulkan", "cuda", "cpu", "rocm"], default="vulkan")
    ap.add_argument("--cuda-version", default="",
                    help="Pick the CUDA runtime exactly (e.g. 12.4 or 13.3). Default: "
                         "driver version via nvidia-smi, otherwise the newest available.")
    ap.add_argument("--force", action="store_true", help="Re-download even if a build already exists")
    ap.add_argument("--upgrade", action="store_true",
                    help="Check target release and download only if a newer build is available")
    ap.add_argument("--tag", default="", help="Override release tag (default: newest nightly)")
    ap.add_argument("--latest", action="store_true",
                    help="Install newest nightly (default; kept for backward compatibility)")
    ap.add_argument("--pinned", action="store_true",
                    help="Use the pinned build tag from deploy/llama_build_pin.txt")
    args = ap.parse_args(argv)

    have = existing_build(args.backend)
    if have and not args.force and not args.upgrade:
        print(f"[OK] llama.cpp b{have[0]} ({args.backend}) already installed: {have[1]}")
        print("     (use --upgrade to check for a newer build, or --force to re-download)")
        return 0

    print(f"[1/3] Fetching llama.cpp release info ({args.backend})...")
    rx = ASSET_REGEX[args.backend]

    _det = ""
    if args.backend == "cuda" and not args.cuda_version:
        _det = _detect_cuda_version_from_driver()
        if _det:
            print(f"       Driver CUDA version: {_det}")
        else:
            print("       nvidia-smi not available — taking the newest CUDA version.")

    _use_pinned = bool(
        args.pinned or (not args.latest and not args.tag and _configured_update_channel() == "pinned")
    )
    releases = _resolve_releases(
        args.backend,
        rx,
        tag=args.tag,
        use_pinned=_use_pinned,
        use_latest=args.latest,
        cuda_version=args.cuda_version or "",
        max_cuda_version=_det,
    )

    if not releases:
        if have and args.upgrade and not args.force:
            print(f"[WARNING] Could not check latest llama.cpp release; keeping existing b{have[0]} ({args.backend}): {have[1]}")
            return 0
        print(f"[ERROR] Could not resolve any llama.cpp release for '{args.backend}'.")
        print("         Manual: https://github.com/ggml-org/llama.cpp/releases")
        return 1

    # CUDA-VERSION-SELECTION (2026-08-27, FIX): prefer the driver version
    # (nvidia-smi) or highest compatible <= driver runtime on the newest build.
    # Previously cuda-12.4 was always chosen even though cuda-13.3 exists.
    if args.backend == "cuda" and not args.cuda_version and _det:
        asset, asset_tag = _pick_asset(releases, rx, max_cuda_version=_det)
        if asset is not None:
            _picked_cv = _cuda_version_of(asset["name"], rx)
            _det_tup = _parse_cuda_tuple(_det)
            if _det_tup and _picked_cv[:2] != _det_tup:
                print(f"       No exact CUDA {_det} build — using compatible <= {_det} runtime ({asset['name']}).")
        else:
            print(f"       No asset <= CUDA {_det} — taking the newest version.")
            asset, asset_tag = _pick_asset(releases, rx)
    else:
        asset, asset_tag = _pick_asset(releases, rx, args.cuda_version or "")

    if asset is None:
        print(f"[ERROR] No asset for '{args.backend}' found in release {releases[0].get('tag_name', '?')}.")
        print(f"         Manual: {releases[0].get('html_url', 'https://github.com/ggml-org/llama.cpp/releases')}")
        return 1

    tag = asset_tag
    build_num = int(rx.match(asset["name"]).group(1))
    if have and args.upgrade and not args.force and build_num <= have[0]:
        print(f"[OK] llama.cpp b{have[0]} ({args.backend}) is already up to date")
        print(f"     Installed: {have[1]} (target: b{build_num})")
        return 0
    _cv = _cuda_version_of(asset["name"], rx)
    _cv_txt = f" (CUDA {'.'.join(str(x) for x in _cv)})" if _cv != (0,) else ""
    print(f"[2/3] Downloading {asset['name']}{_cv_txt} (release {tag}, build b{build_num})...")
    print(f"       Install target: {LLAMA_DIR}")
    LLAMA_DIR.mkdir(parents=True, exist_ok=True)

    # TEMP-COLLISION (audit r4): the archive used to go to a PREDICTABLE name
    # in the shared temp dir - two parallel installs corrupted each other's
    # download and a planted symlink made us overwrite a foreign file. A
    # per-process mkdtemp is unique; in-run resume still works (same file).
    tmp_dir = Path(tempfile.mkdtemp(prefix="hivemind-llamacpp-"))
    tmp_zip = tmp_dir / asset["name"]
    url = asset.get("browser_download_url")
    if not url:
        print("[ERROR] Asset without download URL.")
        return 1
    download(url, tmp_zip)

    _base_name = re.sub(r"\.(zip|tar\.gz)$", "", asset["name"], flags=re.IGNORECASE)
    target = LLAMA_DIR / _base_name
    _is_targz = tmp_zip.name.lower().endswith(".tar.gz")
    print(f"[3/3] Extracting to {target}...")
    try:
        # I2 (audit r2): verify the asset digest from the GitHub API when
        # present - CRC32 (testzip) catches transport corruption but
        # proves nothing about authenticity of the binary we execute.
        # VERIFY-BEFORE-DELETE (audit r4): the old build is only replaced
        # once the new archive passed digest + CRC + extraction (see
        # _extract_and_swap) - a bad download no longer destroys it.
        _digest = asset.get("digest") if isinstance(asset, dict) else None
        _extract_and_swap(tmp_zip, target, _digest)
    except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print()
        print(f"[ERROR] The downloaded archive is corrupt or incomplete ({e}).")
        print("        Delete the temp ZIP and run again - it will download")
        print("        from scratch. If it keeps failing, download the asset")
        print("        manually from: https://github.com/ggml-org/llama.cpp/releases")
        return 1
    except RuntimeError:
        # digest mismatch (verify-before-delete: the old build stays intact)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    exe = target / SERVER_NAME
    if not exe.exists():
        # Some archives extract a subfolder
        nested = list(target.glob(f"*/{SERVER_NAME}"))
        exe = nested[0] if nested else exe
    if not _IS_WIN and exe.exists():
        exe.chmod(exe.stat().st_mode | 0o755)

    # CUDA-RUNTIME-SPLIT (2026-09-02): recent llama.cpp nightlies (b10760+)
    # stopped bundling the CUDA runtime libs in the main archive on both
    # Windows (cudart-llama-bin-win-cuda-<ver>-x64.zip) and Linux
    # (cudart-llama-bNNNN-bin-ubuntu-cuda-<ver>-x64.tar.gz). Download the
    # matching asset and extract next to llama-server.
    if args.backend == "cuda" and releases:
        _cuda_ver = ""
        _m_ver = re.match(
            rf"^llama-b\d+-bin-{_OS_TAG}-cuda-([\d.]+)-x64{_ASSET_EXT}$",
            str(asset.get("name") or ""),
            re.IGNORECASE,
        )
        if _m_ver:
            _cuda_ver = _m_ver.group(1)
        _cudart = None
        _cu_rx = re.compile(
            rf"^cudart-llama(?:-b\d+)?-bin-{_OS_TAG}-cuda-([\d.]+)-x64{_ASSET_EXT}$",
            re.IGNORECASE,
        )
        picked_rel = next(
            (r for r in releases if r.get("tag_name") == asset_tag),
            releases[0],
        )
        for _a in (picked_rel.get("assets") or []):
            _an = str(_a.get("name") or "")
            _mm = _cu_rx.match(_an)
            if _mm and (not _cuda_ver or _mm.group(1) == _cuda_ver):
                _cudart = _a
                break
        if _cudart and _cudart.get("browser_download_url"):
            _cu_zip = tmp_dir / str(_cudart["name"])
            print(f"       CUDA runtime split detected - downloading {_cudart['name']} ...")
            try:
                download(str(_cudart["browser_download_url"]), _cu_zip)
                _cu_digest = str(_cudart.get("digest") or "")
                if _cu_digest.lower().startswith("sha256:"):
                    _cu_h = _sha256_file(_cu_zip)
                    if _cu_h != _cu_digest.split(":", 1)[1].strip().lower():
                        _cu_zip.unlink(missing_ok=True)
                        raise RuntimeError(
                            f"cudart asset digest mismatch ({_cu_h[:16]}...) - "
                            "refusing to install; do NOT just retry, verify "
                            "the download source")
                if _cu_zip.name.lower().endswith(".tar.gz"):
                    with tarfile.open(_cu_zip) as _tf:
                        _safe_tar_extractall(_tf, exe.parent)
                else:
                    with zipfile.ZipFile(_cu_zip) as _zf:
                        _bad = _zf.testzip()
                        if _bad is not None:
                            raise zipfile.BadZipFile(f"corrupt member: {_bad}")
                        _zf.extractall(exe.parent)
                print(f"       CUDA runtime libraries installed next to {SERVER_NAME}")
            except RuntimeError as _cud_dig:
                # DIGEST (audit r4): fail hard, do not install unverified code
                shutil.rmtree(tmp_dir, ignore_errors=True)
                shutil.rmtree(exe.parent, ignore_errors=True)
                print(f"       [ERROR] {_cud_dig}")
                return 1
            except Exception as _cud_err:
                print(f"       [WARNING] CUDA runtime download/extract failed: {_cud_err}")
            finally:
                _cu_zip.unlink(missing_ok=True)
        elif not _cudart:
            print("       (no separate cudart archive on this release - runtime libs expected in the main build)")

    # DLL-VERIFY (2026-08-27): `--device CUDA0` needs the bundled CUDA runtime
    # DLLs next to the exe. A ZIP without them would install a build that
    # "finds no devices" — fail here with a clear message instead.
    # AV-RETRY (2026-09-08): Windows Defender real-time scanning can make large
    # native DLLs (observed: ggml-vulkan.dll) vanish for several seconds right
    # after extraction (quarantine-then-release or slow scan), so a complete
    # ZIP looks broken. Retry with a growing wait (~90s total window); after
    # the final wait re-check once more and treat a now-complete build as
    # success. If it is still incomplete, cross-check the SOURCE archive:
    # DLL in the ZIP but not on disk => antivirus removed it — keep the build,
    # tell the user to restore/allow it, and don't force a re-download that
    # hits the same scanner. Only a ZIP that genuinely lacks the DLL is deleted.
    _missing = []
    _t_verify0 = time.time()
    for _attempt in range(10):
        _missing = _verify_backend_dlls(exe, args.backend)
        if not _missing:
            break
        if _attempt < 9:
            print(f"       DLL check pending ({_m if (_m := _missing[0]) else ''}) — "
                  f"waiting (antivirus scanning, {int(time.time() - _t_verify0)}s elapsed)...",
                  flush=True)
            time.sleep(10.0)
    if not _missing:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print()
        print(f"[OK] llama.cpp b{build_num} installed.")
        print(f"     llama-server: {exe}")
        if have and build_num < have[0]:
            print(f"     NOTE: b{have[0]} is newer and stays in place (auto-discovery picks the highest build).")
        return 0
    _in_archive: dict[str, bool] = {}
    if tmp_zip.exists():
        try:
            if _is_targz:
                with tarfile.open(tmp_zip) as tf:
                    _arc_names = set(tf.getnames())
            else:
                with zipfile.ZipFile(tmp_zip) as zf:
                    _arc_names = set(zf.namelist())
            for _m in _missing:
                # SO-LAYOUT (2026-09-13): missing entries can be "*ggml-vulkan.so"
                # (rglob pattern) — strip the leading "*" and match on the
                # archive member's FILE NAME (lib/ paths, versioned suffixes).
                _stem = _m.lstrip("*").replace("*.dll", "")
                _in_archive[_m] = any(
                    _stem.lower() in Path(n).name.lower() for n in _arc_names
                )
        except (zipfile.BadZipFile, tarfile.TarError, OSError):
            pass
    _av_suspected = any(_in_archive.get(_m, False) for _m in _missing)
    print()
    print(f"[ERROR] {args.backend} runtime DLLs missing next to the exe:")
    for _m in _missing:
        if _av_suspected and _in_archive.get(_m):
            print(f"    - {_m}  [in the archive, but absent on disk (antivirus?)]")
        else:
            print(f"    - {_m}")
    print(f"  Expected next to: {exe}")
    _ggml_present = sorted(p.name for p in exe.parent.rglob("ggml*.dll")) if exe.parent.exists() else []
    _ggml_present += sorted(p.name for p in exe.parent.rglob("libggml*.so")) if exe.parent.exists() else []
    if _ggml_present:
        print(f"  ggml DLLs actually on disk: {', '.join(_ggml_present) or '(none)'}")
    if _av_suspected:
        print()
        print("  The downloaded archive is COMPLETE - antivirus/Defender most likely")
        print("  quarantined the DLL right after extraction. Fix:")
        print("    1. Windows Security -> Protection history -> restore/allow the DLL")
        print("       (or add an exclusion for the llama\\ folder)")
        print("    2. Re-extract manually from the temp ZIP, or run again with --force")
        print(f"       (archive kept at: {tmp_zip})")
    else:
        print("  The ZIP seems incomplete. Try again (--force) or download")
        print(f"  the {args.backend} asset manually from:")
        print("  https://github.com/ggml-org/llama.cpp/releases")
        shutil.rmtree(target, ignore_errors=True)
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAborted by user.")
        sys.exit(1)
    except Exception as e:
        # Never die with a raw traceback in the installer — print a clear,
        # actionable message and suggest the manual path.
        print()
        print(f"[ERROR] llama.cpp downloader crashed: {type(e).__name__}: {e}")
        print("  This is usually transient (network/API) — just run the command again:")
        print("    python deploy\\fetch_llamacpp.py --backend vulkan --force")
        print("  Or download the matching build manually from:")
        print("    https://github.com/ggml-org/llama.cpp/releases")
        print("  (pick the newest `llama-bXXXX-bin-win-vulkan-x64.zip`, extract it into")
        print("   a folder under `llama\\` — auto-discovery picks the highest build).")
        sys.exit(1)

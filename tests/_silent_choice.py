# -*- coding: utf-8 -*-
"""Silent drop-in replacement for Windows ``choice.exe`` in test suites.

Windows ``C:\\Windows\\System32\\choice.exe`` calls ``kernel32!Beep`` whenever it
reads an unexpected character (including ``\\r``/``\\n`` left in piped stdin or
rejected menu keys), blasting full-volume beeps into headsets even when
stdout/stderr are captured.

This helper compiles a tiny, 100% silent ``choice.exe`` shim once per content
hash into the user temp directory using the built-in .NET Framework C#
compiler (``csc.exe``). The shim:

- supports ``/c``, ``/n``, ``/cs``, ``/t``, ``/d``, and ``/m`` switches;
- reads stdin strictly 1 byte at a time via Win32 ``ReadFile`` (unbuffered, so it
  never steals bytes meant for subsequent ``choice``, ``set /p``, or ``pause``
  commands on pipes or redirected files);
- emits zero audio (no ``Beep``, ``MessageBeep``, ``PlaySound``, or BEL).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

_COMPILE_LOCK = threading.Lock()

_CHOICE_CS = r'''using System;
using System.Runtime.InteropServices;
using System.Text;

class SilentChoice {
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr GetStdHandle(int nStdHandle);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool ReadFile(
        IntPtr hFile,
        byte[] lpBuffer,
        uint nNumberOfBytesToRead,
        out uint lpNumberOfBytesRead,
        IntPtr lpOverlapped
    );

    static int FindChoice(char ch, string choices, bool caseSensitive) {
        if (!caseSensitive) {
            ch = char.ToUpperInvariant(ch);
            choices = choices.ToUpperInvariant();
        }
        return choices.IndexOf(ch);
    }

    static int Main(string[] args) {
        string choices = "YN";
        bool hideChoices = false;
        bool caseSensitive = false;
        string defaultChoice = null;
        string message = null;

        for (int i = 0; i < args.Length; i++) {
            string a = args[i];
            if (a.Length < 2 || (a[0] != '/' && a[0] != '-')) {
                continue;
            }
            int colon = a.IndexOf(':');
            string flag = (colon > 1 ? a.Substring(1, colon - 1) : a.Substring(1)).ToUpperInvariant();
            string inlineVal = colon > 1 ? a.Substring(colon + 1) : null;

            if (flag == "N") {
                hideChoices = true;
            } else if (flag == "CS") {
                caseSensitive = true;
            } else if (flag == "C") {
                if (inlineVal != null) {
                    choices = inlineVal;
                } else if (i + 1 < args.Length) {
                    choices = args[++i];
                }
            } else if (flag == "T") {
                if (inlineVal == null && i + 1 < args.Length) {
                    i++;
                }
            } else if (flag == "D") {
                if (inlineVal != null) {
                    defaultChoice = inlineVal;
                } else if (i + 1 < args.Length) {
                    defaultChoice = args[++i];
                }
            } else if (flag == "M") {
                if (inlineVal != null) {
                    message = inlineVal;
                } else if (i + 1 < args.Length) {
                    message = args[++i];
                }
            }
        }

        if (string.IsNullOrEmpty(choices)) {
            return 255;
        }

        if (message != null) {
            Console.Write(message);
            if (!hideChoices && message.Length > 0 && !char.IsWhiteSpace(message[message.Length - 1])) {
                Console.Write(' ');
            }
        }
        if (!hideChoices) {
            StringBuilder sb = new StringBuilder();
            sb.Append('[');
            for (int i = 0; i < choices.Length; i++) {
                if (i > 0) {
                    sb.Append(',');
                }
                sb.Append(choices[i]);
            }
            sb.Append("]?");
            Console.Write(sb.ToString());
        }
        Console.Out.Flush();

        IntPtr hIn = GetStdHandle(-10);
        byte[] buf = new byte[1];
        uint nRead = 0;
        while (ReadFile(hIn, buf, 1, out nRead, IntPtr.Zero) && nRead == 1) {
            char ch = (char)buf[0];
            int idx = FindChoice(ch, choices, caseSensitive);
            if (idx >= 0) {
                Console.WriteLine(choices[idx]);
                Console.Out.Flush();
                return idx + 1;
            }
        }

        if (!string.IsNullOrEmpty(defaultChoice)) {
            int defIdx = FindChoice(defaultChoice[0], choices, caseSensitive);
            if (defIdx >= 0) {
                Console.WriteLine(choices[defIdx]);
                Console.Out.Flush();
                return defIdx + 1;
            }
        }
        return 255;
    }
}
'''


def _find_csc() -> Path | None:
    """Locate the built-in .NET Framework C# compiler on Windows."""
    windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for arch in ("Framework64", "Framework"):
        cand = windir / "Microsoft.NET" / arch / "v4.0.30319" / "csc.exe"
        if cand.is_file():
            return cand
    return None


def _is_valid_exe(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def ensure_silent_choice_dir() -> str | None:
    """Return a directory containing a silent ``choice.exe`` shim on Windows.

    Returns ``None`` on non-Windows platforms or if compilation fails.
    """
    if os.name != "nt":
        return None
    digest = hashlib.sha256(_CHOICE_CS.encode("utf-8")).hexdigest()[:12]
    cache_dir = Path(tempfile.gettempdir()) / f"hivemind_silent_choice_{digest}"
    target_exe = cache_dir / "choice.exe"
    if _is_valid_exe(target_exe):
        return str(cache_dir)

    with _COMPILE_LOCK:
        if _is_valid_exe(target_exe):
            return str(cache_dir)
        csc = _find_csc()
        if csc is None:
            return None
        cache_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{os.getpid()}_{threading.get_ident()}_{time.time_ns()}"
        tmp_cs = cache_dir / f"_choice_{stamp}.cs"
        tmp_exe = cache_dir / f"_choice_{stamp}.exe"
        try:
            tmp_cs.write_text(_CHOICE_CS, encoding="ascii")
            res = subprocess.run(
                [str(csc), "/nologo", "/optimize+", f"/out:{tmp_exe.name}", tmp_cs.name],
                cwd=str(cache_dir),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if res.returncode != 0 or not _is_valid_exe(tmp_exe):
                return str(cache_dir) if _is_valid_exe(target_exe) else None
            try:
                os.replace(tmp_exe, target_exe)
            except OSError:
                time.sleep(0.05)
                if not _is_valid_exe(target_exe):
                    return None
        except (OSError, subprocess.SubprocessError):
            return str(cache_dir) if _is_valid_exe(target_exe) else None
        finally:
            tmp_cs.unlink(missing_ok=True)
            tmp_exe.unlink(missing_ok=True)
    return str(cache_dir) if _is_valid_exe(target_exe) else None


def prepend_silent_choice_to_path(path_value: str, silent_dir: str | None = None) -> str:
    """Return ``PATH`` with the silent ``choice.exe`` directory placed first."""
    sdir = silent_dir if silent_dir is not None else ensure_silent_choice_dir()
    if not sdir:
        return path_value
    norm_sdir = os.path.normcase(os.path.normpath(sdir))
    entries = [p for p in (path_value or "").split(os.pathsep) if p]
    filtered = [
        p for p in entries
        if os.path.normcase(os.path.normpath(p)) != norm_sdir
    ]
    return os.pathsep.join([sdir, *filtered])

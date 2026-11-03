# -*- coding: utf-8 -*-
"""B7: a resumed download must not accept a hybrid file.

The catalog stores only a size. If a partial file comes from an older catalog
generation and the upstream file changed (same size), a Range resume appends
new bytes to old ones — same size, wrong content, accepted. The server's
ETag (Hugging Face: the LFS sha256) is the available truth.
"""
import contextlib
import hashlib
import http.server
import io
import json
import re
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import deploy.fetch_models as F                   # noqa: E402

NEW = b"B" * 4096
OLD_PARTIAL = b"A" * 2048                         # older generation, same file
ETAG = hashlib.sha256(NEW).hexdigest()

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(NEW)))
        self.send_header("ETag", f'"{ETAG}"')
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/api/models/"):
            body = json.dumps([
                {"path": name, "lfs": {"oid": ETAG}}
                for name in ("model.gguf", "fresh.gguf", "redirected.gguf")
            ]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        start = 0
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d+)-", rng)
            if m:
                start = int(m.group(1))
        body = NEW[start:]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", f'"{ETAG}"')
        if rng:
            self.send_header("Content-Range",
                             f"bytes {start}-{len(NEW) - 1}/{len(NEW)}")
        self.end_headers()
        self.wfile.write(body)


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
F.HF_RESOLVE = f"http://127.0.0.1:{port}/{{repo}}/{{path}}"

F.HF_API_TREE = (f"http://127.0.0.1:{port}/api/models/"
                 "{repo}/tree/main?recursive=1")

dest_dir = Path(tempfile.mkdtemp())
dest = dest_dir / "model.gguf"
dest.write_bytes(OLD_PARTIAL)                    # stale partial, 2 KB

ok = F.download_file("test/repo", {"path": "model.gguf", "size": len(NEW)},
                     dest_dir, auto_yes=True)
got = dest.read_bytes() if dest.exists() else b""
check("1 the download reports success", ok is True)
check("2 the file matches the upstream bytes", got == NEW,
      f"({len(got)} bytes, starts {got[:4]!r})")
check("3 its sha256 matches the server ETag",
      hashlib.sha256(got).hexdigest() == ETAG)

# A clean download still works and keeps the content intact.
dest2 = dest_dir / "fresh.gguf"
ok2 = F.download_file("test/repo", {"path": "fresh.gguf", "size": len(NEW)},
                      dest_dir, auto_yes=True)
check("4 a fresh download works", ok2 is True and dest2.read_bytes() == NEW)

# Hugging Face serves X-Linked-Etag on the 302 and redirects to a CDN that
# does not repeat it. The checksum must be captured from the redirect.
_PORT = 0


class RedirectHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/api/models/"):
            body = json.dumps([
                {"path": name, "lfs": {"oid": ETAG}}
                for name in ("model.gguf", "fresh.gguf", "redirected.gguf")
            ]).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.path.startswith("/cdn/"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{_PORT}/cdn/file")
            self.send_header("X-Linked-Etag", f'"{ETAG}"')
            self.end_headers()
            return
        start = 0
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d+)-", rng)
            if m:
                start = int(m.group(1))
        body = NEW[start:]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(body)))
        if rng:
            self.send_header("Content-Range",
                             f"bytes {start}-{len(NEW) - 1}/{len(NEW)}")
        self.end_headers()
        self.wfile.write(body)


srv2 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
_PORT = srv2.server_address[1]
threading.Thread(target=srv2.serve_forever, daemon=True).start()
F.HF_RESOLVE = f"http://127.0.0.1:{_PORT}/{{repo}}/{{path}}"
F.HF_API_TREE = (f"http://127.0.0.1:{_PORT}/api/models/"
                 "{repo}/tree/main?recursive=1")

dest3 = dest_dir / "redirected.gguf"
dest3.write_bytes(OLD_PARTIAL)
_buf = io.StringIO()
with contextlib.redirect_stdout(_buf):
    ok3 = F.download_file("test/repo",
                          {"path": "redirected.gguf", "size": len(NEW)},
                          dest_dir, auto_yes=True)
_stdout = _buf.getvalue()
got3 = dest3.read_bytes() if dest3.exists() else b""
check("5 a redirect download uses the redirect ETag", ok3 is True and got3 == NEW,
      f"({len(got3)} bytes)")
check("6 the log shows the sha256 check was active",
      "sha256 available" in _stdout, _stdout.strip().splitlines()[-1:])

srv.shutdown()
srv2.shutdown()

# P2 (deep audit 2026-11-03): the HF token from the environment must reach the
# API/file requests, or gated repos silently lose hash verification.
import os                                            # noqa: E402
_old_tok = os.environ.get("HF_TOKEN")
os.environ["HF_TOKEN"] = "test-token"
try:
    check("7 the HF token is sent as a bearer header",
          F._hf_auth_headers() == {"Authorization": "Bearer test-token"})
finally:
    if _old_tok is None:
        os.environ.pop("HF_TOKEN", None)
    else:
        os.environ["HF_TOKEN"] = _old_tok

# hf auth login writes the token to $HF_HOME/token (or ~/.cache/huggingface).
_home = Path(tempfile.mkdtemp())
(_home / "token").write_text("file-token", encoding="utf-8")
_old_hf_home = os.environ.get("HF_HOME")
_old_env_tok = os.environ.get("HF_TOKEN")
os.environ.pop("HF_TOKEN", None)
os.environ["HF_HOME"] = str(_home)
try:
    check("8 the token written by hf auth login is used",
          F._hf_token() == "file-token"
          and F._hf_auth_headers() == {"Authorization": "Bearer file-token"})
finally:
    os.environ.pop("HF_HOME", None)
    if _old_hf_home is not None:
        os.environ["HF_HOME"] = _old_hf_home
    if _old_env_tok is not None:
        os.environ["HF_TOKEN"] = _old_env_tok


class DeniedHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(401)
        self.end_headers()


srv3 = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DeniedHandler)
threading.Thread(target=srv3.serve_forever, daemon=True).start()
F.HF_RESOLVE = (f"http://127.0.0.1:{srv3.server_address[1]}"
                "/{repo}/{path}")
_buf401 = io.StringIO()
with contextlib.redirect_stdout(_buf401):
    ok401 = F.download_file("test/denied", {"path": "x.gguf", "size": 10},
                            dest_dir, auto_yes=True)
check("9 a gated 401 fails cleanly with a login hint",
      ok401 is False and "hf auth login" in _buf401.getvalue(),
      _buf401.getvalue().strip().splitlines()[-1:])
srv3.shutdown()

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)

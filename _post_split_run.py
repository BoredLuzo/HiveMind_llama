"""Runtime proof after the preflight split: one small agentic run, verified here."""
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = "http://127.0.0.1:8001"
WS = Path(tempfile.mkdtemp(prefix="hm_post_split_"))
PY = sys.executable


def get(p):
    with urllib.request.urlopen(f"{BASE}{p}", timeout=25) as r:
        return json.loads(r.read())


def post(p, body=None, timeout=120):
    req = urllib.request.Request(f"{BASE}{p}", data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


saved = get("/settings")
print("workspace:", WS)
post("/settings", {"workspace": str(WS), "duo_action_approval_enabled": False})
try:
    body = json.dumps({
        "q": "Create hello.py with a main() that prints 'hi from hivemind', then run "
             "it with run_bash to prove it works. Keep it to 5 lines.",
        "flush_ok": True, "mode": "code_duo", "duo_agentic": True}).encode()
    req = urllib.request.Request(f"{BASE}/stream", data=body,
                                headers={"Content-Type": "application/json"})
    tools, done, t0 = [], None, time.time()
    with urllib.request.urlopen(req, timeout=600) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            pl = line[5:].strip()
            if not pl or pl == "[DONE]":
                continue
            try:
                ev = json.loads(pl)
            except ValueError:
                continue
            if ev.get("type") == "tool_call":
                tools.append(str(ev.get("name") or ev.get("tool")))
            elif ev.get("type") == "done":
                done = ev
                break
    elapsed = round(time.time() - t0, 1)
    r = subprocess.run([PY, "hello.py"], cwd=str(WS), capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    print(f"\nrun: {elapsed}s | stop={(done or {}).get('stop_reason')} "
          f"| tools={tools}")
    print("tool_rounds:", (done or {}).get("metrics_tool_rounds"))
    print("file exists:", (WS / "hello.py").exists())
    print("hello.py output:", out[:120] or "(nothing)")
    print("\nRUNTIME PROOF:", "OK" if r.returncode == 0 and "hi from hivemind" in out
          else "FAILED")
finally:
    post("/settings", {"workspace": saved.get("workspace"),
                       "duo_action_approval_enabled": saved.get("duo_action_approval_enabled")})
    shutil.rmtree(WS, ignore_errors=True)

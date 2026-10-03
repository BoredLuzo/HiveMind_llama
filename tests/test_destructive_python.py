"""Eval: Destructive-Python-Gate (tools/runner._is_destructive_python).

Audit-Punkt: run_python nicht ausreichend gesandboxt. Verifiziert die
classification is deterministic (incl. no false positives).
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.runner import _is_destructive_python

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


def det(label, code, expected):
    r = _is_destructive_python(code)
    check(label, (r is not None) == expected, f" (matched: {r})" if r else "")


# ── Destruktiv (True) ──
det("os.system", "import os; os.system('rm -rf /tmp')", True)
det("os.popen", "os.popen('dir')", True)
det("subprocess.run", "import subprocess; subprocess.run(['x'])", True)
det("shutil.rmtree", "shutil.rmtree('/tmp/x')", True)
det("os.remove", "os.remove('f.txt')", True)
det("eval", "eval('1+1')", True)
det("exec", "exec(code)", True)
det("compile", "compile(src, 'x', 'exec')", True)
det("__import__", "__import__('os')", True)
det("socket.connect", "import socket; socket.connect(('h', 1))", True)
det("requests.delete", "requests.delete('http://x')", True)
det("open absolute path", "open('/etc/passwd')", True)
det("winreg.DeleteKey", "winreg.DeleteKey(k, 'x')", True)
det("ctypes.windll", "ctypes.windll.user32", True)

# ── Harmlos (False, keine False Positives) ──
det("print", "print('hello')", False)
det("sum", "sum([1,2,3])", False)
det("len", "len(items)", False)
det("open relative", "open('data.txt', 'r')", False)
det("open('data.txt')", "open('data.txt')", False)
det("re.sub", "re.sub(r'a', 'b', s)", False)
det("requests.get", "requests.get('http://x')", False)
det("os.getcwd", "os.getcwd()", False)
det("os.path.join", "os.path.join('a', 'b')", False)
det("pathlib.Path", "Path('a.txt').read_text()", False)
det("json.loads", "json.loads(s)", False)
det("socket.gethostname", "socket.gethostname()", False)

print()
print(f"{'='*50}")
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print(f"{'='*50}")
sys.exit(0 if failed == 0 else 1)

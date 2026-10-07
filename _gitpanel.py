"""Git panel honesty: real GitHub remote check in /git/validate, remote
repo creation in /git/init, precise messages for token/permission issues."""
import ast

src = open('routers/git.py', encoding='utf-8').read()

# 1) helper: github repo status + creation
a = '@router.post("/validate")'
b = '''def _gh_owner_name(repo_url: str):
    """Parse (owner, name) from a github.com https URL, else (None, None)."""
    import re as _re
    _m = _re.match(r"https://github\\.com/([^/]+)/([^/]+?)(?:\\.git)?/?$", str(repo_url or "").strip())
    if not _m:
        return None, None
    return _m.group(1), _m.group(2)


async def _github_repo_exists(repo_url: str, token: str):
    """True/false/None (None = cannot judge, e.g. non-github URL) plus a
    plain-language detail for the panel. GITHUB-PANEL FIX (2026-10-07):
    'Test connection' only checked the LOCAL repo and 'Create repo' only
    ran a local git init - both reported green while GitHub had no repo
    and the token had no write access."""
    import httpx as _hx
    _owner, _name = _gh_owner_name(repo_url)
    if not _owner:
        return None, "remote is not a github.com URL"
    _headers = {"Accept": "application/vnd.github+json"}
    if token:
        _headers["Authorization"] = "Bearer " + token
    try:
        async with _hx.AsyncClient(timeout=15.0) as _c:
            _r = await _c.get("https://api.github.com/repos/" + _owner + "/" + _name,
                              headers=_headers)
    except Exception as _e:
        return False, "github unreachable: " + str(_e)[:120]
    if _r.status_code == 200:
        return True, "repo exists and the token can see it"
    if _r.status_code == 404:
        return False, ("repo does not exist - or your token cannot see it "
                       "(fine-grained tokens must list this repo)")
    if _r.status_code == 401:
        return False, "token is invalid or expired (401)"
    if _r.status_code == 403:
        return False, "token is valid but lacks access to this repo (403)"
    return False, "github returned HTTP " + str(_r.status_code)


@router.post("/validate")'''
assert src.count(a) == 1, "validate anchor"
src = src.replace(a, b, 1)

# 2) validate response: remote check attached
a = '''    valid, branch, branches = await asyncio.to_thread(_validate_git_repo, _ws)
    has_credentials = bool(settings.get("git_username") or settings.get("git_token"))
    fully_valid = valid and has_credentials
    return {
        "valid": fully_valid,
        "branch": branch,
        "branches": branches,'''
b = '''    valid, branch, branches = await asyncio.to_thread(_validate_git_repo, _ws)
    has_credentials = bool(settings.get("git_username") or settings.get("git_token"))
    fully_valid = valid and has_credentials
    # GITHUB-PANEL FIX (2026-10-07): also answer what the panel button
    # actually promises - can this token SEE the configured remote repo?
    _remote_ok, _remote_msg = None, ""
    _repo_url = str(settings.get("git_repo_url", "") or "")
    if valid and _repo_url:
        _remote_ok, _remote_msg = await _github_repo_exists(
            _repo_url, str(settings.get("git_token", "") or ""))
    return {
        "valid": fully_valid,
        "branch": branch,
        "branches": branches,
        "remote_ok": _remote_ok,
        "remote_msg": _remote_msg,'''
assert src.count(a) == 1, "validate response"
src = src.replace(a, b, 1)

# 3) init endpoint: create the github repo when missing (best effort)
a = '''        if _init.returncode != 0:
            return {"ok": False, "error": f"git init fehlgeschlagen: {_init.stderr.strip()[:200]}"}'''
b = '''        if _init.returncode != 0:
            return {"ok": False, "error": f"git init fehlgeschlagen: {_init.stderr.strip()[:200]}"}
        # GITHUB CREATE (2026-10-07, owner): the button says "Create repo" -
        # actually create it on GitHub when it does not exist yet. Errors
        # come back in plain language instead of a green "created" that
        # only meant a local git init.
        _gh_created, _gh_msg = False, ""
        _gh_owner, _gh_name = _gh_owner_name(remote_url)
        if _gh_owner and str(settings.get("git_token", "") or ""):
            import httpx as _hx
            _headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + str(settings.get("git_token", "") or ""),
            }
            try:
                async with _hx.AsyncClient(timeout=20.0) as _c:
                    _chk = await _c.get("https://api.github.com/repos/" + _gh_owner + "/" + _gh_name,
                                        headers=_headers)
                    if _chk.status_code == 404:
                        _cr = await _c.post("https://api.github.com/user/repos",
                                            headers=_headers,
                                            json={"name": _gh_name, "private": False,
                                                  "auto_init": False})
                        if _cr.status_code == 201:
                            _gh_created = True
                            _gh_msg = "github repo created"
                        else:
                            _gh_msg = ("github repo NOT created (HTTP "
                                       + str(_cr.status_code) + ": "
                                       + str((_cr.json() or {}).get("message", ""))[:120]
                                       + ") - check token permissions")
                    elif _chk.status_code == 200:
                        _gh_msg = "github repo already exists"
                    else:
                        _gh_msg = ("github check failed (HTTP " + str(_chk.status_code)
                                   + ") - token permissions?")'''.rstrip() + "\n"
# careful: _gh_msg default for no-owner path
b2 = b + '''        else:
            _gh_created, _gh_msg = False, ""'''
assert src.count(a) == 1, "init create anchor"
src = src.replace(a, b2, 1)

open('routers/git.py', 'w', encoding='utf-8').write(src)
ast.parse(src)
print("git router patched")

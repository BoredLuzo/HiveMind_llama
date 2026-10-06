"""Round-2 patch C: installer I1-I5."""
import ast

# I1: update.bat verifies the asset sha256 digest before extract
p = 'update.bat'
src = open(p, encoding='utf-8').read()

old_pick = (
    "for /f \"usebackq delims=\" %%L in (`powershell -NoProfile -Command \"$r = Get-Content '%TMPDIR%\\release.json' -Raw | ConvertFrom-Json; $a = $r.assets | Where-Object { $_.name -like '*.zip' } | Select-Object -First 1; Write-Output ($r.tag_name + '|' + $a.browser_download_url)\"`) do set \"LINE=%%L\""
)
new_pick = (
    "for /f \"usebackq delims=\" %%L in (`powershell -NoProfile -Command \"$r = Get-Content '%TMPDIR%\\release.json' -Raw | ConvertFrom-Json; $a = $r.assets | Where-Object { $_.name -like '*.zip' } | Select-Object -First 1; Write-Output ($r.tag_name + '|' + $a.browser_download_url + '|' + $a.digest)\"`) do set \"LINE=%%L\""
)
c1 = src.count(old_pick)
src = src.replace(old_pick, new_pick)

old_dl = (
    "echo  Downloading       : %ASSET_URL%\n"
    "curl -sS -L -o \"%TMPDIR%\\release.zip\" \"%ASSET_URL%\"\n"
    "if errorlevel 1 (\n"
    "    echo  [ERROR] Download failed. Nothing was changed\n"
    "    echo         backup is in %BK%\n"
    "    echo  Press any key to continue... & pause >nul & exit /b 1\n"
    ")\n"
    "if not exist \"%TMPDIR%\\release.zip\" (\n"
    "    echo  [ERROR] Download produced no file. Nothing was changed.\n"
    "    echo  Press any key to continue... & pause >nul & exit /b 1\n"
    ")"
)
new_dl = (
    "echo  Downloading       : %ASSET_URL%\n"
    "curl -sS -L --fail -o \"%TMPDIR%\\release.zip\" \"%ASSET_URL%\"\n"
    "if errorlevel 1 (\n"
    "    echo  [ERROR] Download failed. Nothing was changed\n"
    "    echo         backup is in %BK%\n"
    "    echo  Press any key to continue... & pause >nul & exit /b 1\n"
    ")\n"
    "if not exist \"%TMPDIR%\\release.zip\" (\n"
    "    echo  [ERROR] Download produced no file. Nothing was changed.\n"
    "    echo  Press any key to continue... & pause >nul & exit /b 1\n"
    ")\n"
    "REM I1 (audit r2): verify the asset against the release API digest\n"
    "REM (sha256:...). curl exit 0 does not prove content identity; a\n"
    "REM tampered/replaced asset would otherwise install arbitrary code.\n"
    "powershell -NoProfile -Command \"$d = ($env:ASSET_DIGEST -split ':',2)[1]; if ($d) { $h = (Get-FileHash -Algorithm SHA256 '%TMPDIR%\\release.zip').Hash.ToLower(); if ($h -ne $d) { Write-Output ('DIGEST MISMATCH: ' + $h); exit 1 } } else { Write-Output 'no digest in release metadata - skipping'; exit 0 }\"\n"
    "set \"ASSET_DIGEST=%LINE4%\"\n"
    "for /f \"tokens=1-4 delims=|\" %%a in (\"%LINE%\") do (\n"
    "    set \"TAG=%%a\"\n"
    "    set \"ASSET_URL=%%b\"\n"
    "    set \"ASSET_DIGEST=%%c\"\n"
    ")\n"
)
c2 = src.count(old_dl)
src = src.replace(old_dl, new_dl)
open(p, 'w', encoding='utf-8', newline='').write(src)
print(f"I1: pick-line {c1}, download-block {c2}")

# I2: fetch_llamacpp verifies the digest too
p = 'deploy/fetch_llamacpp.py'
src = open(p, encoding='utf-8').read()
old = (
    "        if _is_targz:\n"
)
new = (
    "        # I2 (audit r2): verify the asset digest from the GitHub API when\n"
    "        # available - CRC32 (testzip) catches transport corruption but\n"
    "        # proves nothing about authenticity.\n"
    "        _digest = (asset or {}).get(\"digest\") if isinstance(asset, dict) else None\n"
    "        if _digest and _digest.startswith(\"sha256:\"):\n"
    "            import hashlib as _hl\n"
    "            _h = _hl.sha256(tmp_zip.read_bytes()).hexdigest()\n"
    "            if _h != _digest.split(\":\", 1)[1]:\n"
    "                shutil.rmtree(target, ignore_errors=True)\n"
    "                tmp_zip.unlink(missing_ok=True)\n"
    "                raise RuntimeError(\n"
    "                    f\"llama.cpp asset digest mismatch: {_h} != {_digest} - \"\n"
    "                    \"refusing to install a tampered/incomplete binary\")\n"
    "        if _is_targz:\n"
)
c3 = src.count(old)
src = src.replace(old, new, 1)
open(p, 'w', encoding='utf-8').write(src)
ast.parse(src)
print(f"I2: digest-check {c3}")

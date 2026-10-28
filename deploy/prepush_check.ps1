# HiveMind pre-push gate
#
# Run before every commit that goes to dev or main. Everything here is
# offline and takes about two minutes.
#
#   powershell -NoProfile -File deploy\prepush_check.ps1
#
# Exits 0 when every check passes, 1 otherwise. ASCII only.

param(
    [string]$Repo = ""
)

$ErrorActionPreference = "Continue"

if (-not $Repo) {
    $candidate = Split-Path -Parent $PSScriptRoot
    if (Test-Path (Join-Path $candidate ".git")) { $Repo = $candidate }
    elseif (Get-Command git -ErrorAction SilentlyContinue) {
        # PRIVATE-PATH (2026-10-28, release audit): the fallback hardcoded a
        # C:\Users\<name>\... worktree and shipped in the 1.4.10/1.4.11 zips.
        # Resolve the worktree from git instead of a personal path.
        $top = git rev-parse --show-toplevel 2>$null
        if ($top -and (Test-Path (Join-Path $top ".git"))) { $Repo = $top }
    }
    if (-not $Repo) {
        Write-Host "[FATAL] repo not found" -ForegroundColor Red
        exit 1
    }
}
Set-Location $Repo

$py = Join-Path $Repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$script:pass = 0
$script:fail = 0

function Check {
    param([string]$Name, [bool]$Ok, [string]$Info = "")
    if ($Ok) {
        $script:pass++
        Write-Host ("OK    {0}{1}" -f $Name, $(if ($Info) { "  -> $Info" } else { "" })) -ForegroundColor Green
    } else {
        $script:fail++
        Write-Host ("FAIL  {0}{1}" -f $Name, $(if ($Info) { "  -> $Info" } else { "" })) -ForegroundColor Red
    }
}

Write-Host "=========================================================="
Write-Host " HiveMind pre-push gate"
Write-Host " repo: $Repo"
Write-Host "=========================================================="

# -- a) working tree clean ---------------------------------------------------
$dirty = (git status --porcelain)
Check "a) git status clean" ([string]::IsNullOrWhiteSpace($dirty)) `
    ($(if ($dirty) { ($dirty -split "`n" | Select-Object -First 3) -join " | " } else { "" }))

# -- b) version consistent across server.py, pyproject, CHANGELOG -------------
$srv = (Select-String -Path server.py -Pattern 'HIVEMIND_VERSION = "([^"]+)"').Matches[0].Groups[1].Value
$pyp = (Select-String -Path pyproject.toml -Pattern '^version = "([^"]+)"').Matches[0].Groups[1].Value
$chal = (Select-String -Path CHANGELOG.md -Pattern '^## \[([0-9]+\.[0-9]+\.[0-9]+)\]' |
         Select-Object -First 1).Matches[0].Groups[1].Value
Check "b) version server.py == pyproject == CHANGELOG" `
    ($srv -eq $pyp -and $srv -eq $chal) "server=$srv pyproject=$pyp changelog=$chal"

# -- c) changelog has a block for the current version ------------------------
Check "c) CHANGELOG block exists for $srv" `
    ((Select-String -Path CHANGELOG.md -Pattern "^## \[$([regex]::Escape($srv))\]" -Quiet) -eq $true)

# -- d) ruff -----------------------------------------------------------------
& $py -m ruff check . *> $null
Check "d) ruff clean" ($LASTEXITCODE -eq 0)

# -- e) settings docs current ------------------------------------------------
& $py deploy\gen_settings_docs.py --check *> $null
Check "e) settings docs current" ($LASTEXITCODE -eq 0)

# -- f) .bat files ASCII + CRLF ----------------------------------------------
& $py tests\test_bat_encoding.py *> $null
Check "f) .bat files ascii+crlf" ($LASTEXITCODE -eq 0)

# -- g) full regression suite ------------------------------------------------
$suiteOut = & $py tests\run_regressions.py 2>&1
$suiteLine = ($suiteOut | Select-String -Pattern "Suites:" | Select-Object -Last 1)
Check "g) regression suite" ($LASTEXITCODE -eq 0) $(if ($suiteLine) { $suiteLine.ToString().Trim() } else { "" })

# -- h) no scratch files in the root -----------------------------------------
$scratch = @(Get-ChildItem -File -Filter "_*.py" | ForEach-Object { $_.Name })
Check "h) no scratch scripts in the repo root" ($scratch.Count -eq 0) ($scratch -join ", ")

# -- i) no build artifacts tracked -------------------------------------------
$tracked = @(git ls-files | Select-String -Pattern "\.(zip|sse|bundle|log)$|^dist/")
Check "i) no build artifacts tracked" ($tracked.Count -eq 0) (($tracked | Select-Object -First 3) -join ", ")

# -- j) every test file is registered ----------------------------------------
$regOut = & $py -c "import io,re,pathlib;reg=set(re.findall(r'tests/(test_[a-z0-9_]+\.py)',io.open('tests/run_regressions.py',encoding='utf-8').read()));files={p.name for p in pathlib.Path('tests').glob('test_*.py')};print('missing',sorted(files-reg));print('extra',sorted(reg-files))" 2>&1
$missing = ($regOut | Select-String -Pattern "missing \[\]" -Quiet)
Check "j) test files and registrations match" ($missing -eq $true) (($regOut | Select-Object -First 2) -join " ")

# -- k) branch model: main must be contained in dev --------------------------
git merge-base --is-ancestor main dev 2>$null
$mainOk = ($LASTEXITCODE -eq 0)
$mainAhead = (git rev-list --count dev..main 2>$null)
Check "k) main is an ancestor of dev" $mainOk "main ahead of dev: $mainAhead"

# -- l) .gitignore covers the scratch patterns -------------------------------
$gi = Get-Content .gitignore -Raw
$need = @("_*.py", "*.zip", "*.sse", "dist/")
$missingGi = @($need | Where-Object { -not $gi.Contains($_) })
Check "l) .gitignore covers scratch patterns" ($missingGi.Count -eq 0) ($missingGi -join ", ")

# -- m) release notes for the current version, and they name it -----------------
# A notes file is optional (a release may rely on the changelog only), but if it
# exists it must carry the version and must not be empty.
$notesFile = "RELEASE_NOTES_$($srv).md"
if (Test-Path $notesFile) {
    $notesBody = Get-Content $notesFile -Raw
    Check "m) release notes match $srv" `
        ($notesBody.Length -gt 200 -and $notesBody.Contains($srv)) `
        ("$notesFile, {0} chars" -f $notesBody.Length)
} else {
    Check "m) release notes for $srv (optional)" $true "none, changelog only"
}

Write-Host "=========================================================="
if ($script:fail -eq 0) {
    Write-Host (" RESULT: {0} OK, 0 FAIL - safe to push" -f $script:pass) -ForegroundColor Green
    exit 0
} else {
    Write-Host (" RESULT: {0} OK, {1} FAIL - do NOT push" -f $script:pass, $script:fail) -ForegroundColor Red
    exit 1
}

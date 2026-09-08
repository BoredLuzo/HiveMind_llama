# A/B Phase-1 Helper (Legacy vs. Neu vs. Partial)
# Schaltet die duo-Cache-Settings um (bevorzugt am laufenden Server via
# POST /settings; Fallback: settings.json direkt editieren) und zeigt den
# effektiven Stand.
#
# WICHTIG - Aufruf (im HiveMind-Installationsordner, NICHT per Doppelklick):
#   powershell -ExecutionPolicy Bypass -File .\deploy\ab_phase1.ps1 -Mode legacy
#   powershell -ExecutionPolicy Bypass -File .\deploy\ab_phase1.ps1 -Mode new
#   powershell -ExecutionPolicy Bypass -File .\deploy\ab_phase1.ps1 -Status
param(
    [ValidateSet("legacy", "new", "partial", "status")]
    [string]$Mode = "status",
    [int]$Port = 8001
)

$ErrorActionPreference = "Continue"
$hadError = $false
$base = "http://localhost:$Port/settings"

function Show-Stand {
    Write-Host ""
    Write-Host "  Effective state:"
    Write-Host ("    duo_cache_friendly_ctx  = {0}" -f $script:cf)
    Write-Host ("    duo_partial_compression = {0}" -f $script:pc)
    Write-Host ("    duo_compress_threshold  = {0}   (0 = Auto)" -f $script:thr)
    Write-Host ("    duo_compress_auto_floor = {0}" -f $script:flr)
    Write-Host ("    duo_max_compressions    = {0}" -f $script:mxc)
}

function Read-ServerStand {
    try {
        $s = Invoke-RestMethod -Method Get -Uri $base -ContentType "application/json" -TimeoutSec 5
        $script:cf = $s.duo_cache_friendly_ctx
        $script:pc = $s.duo_partial_compression
        $script:thr = $s.duo_compress_threshold
        $script:flr = $s.duo_compress_auto_floor
        $script:mxc = $s.duo_max_compressions
        $script:serverOk = $true
    } catch {
        $script:serverOk = $false
    }
}

function Read-JsonStand {
    $p = Join-Path (Get-Location) "settings.json"
    if (-not (Test-Path -LiteralPath $p)) {
        Write-Host "  [WARN] settings.json not found: $p"
        return
    }
    try {
        $raw = Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json
        $script:cf = $raw.duo_cache_friendly_ctx
        $script:pc = $raw.duo_partial_compression
        $script:thr = $raw.duo_compress_threshold
        $script:flr = $raw.duo_compress_auto_floor
        $script:mxc = $raw.duo_max_compressions
    } catch {
        Write-Host "  [WARN] settings.json not readable: $($_.Exception.Message)"
    }
}

function Write-JsonStand {
    $p = Join-Path (Get-Location) "settings.json"
    if (-not (Test-Path -LiteralPath $p)) {
        Write-Host "  [ERROR] settings.json not found - fallback possible."
        $script:hadError = $true
        return
    }
    try {
        $raw = Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json
        $raw.duo_cache_friendly_ctx = [bool]$script:newCf
        $raw.duo_partial_compression = [bool]$script:newPc
        $raw | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $p -Encoding UTF8
        Write-Host "  -> settings.json updated directly (server restart required to take effect)."
    } catch {
        Write-Host "  [ERROR] failed to write settings.json: $($_.Exception.Message)"
        $script:hadError = $true
    }
}

# ── Stand initial ermitteln ──
Read-ServerStand
if (-not $serverOk) { Read-JsonStand }

if ($Mode -eq "status") {
    Show-Stand
    exit 0
}

switch ($Mode) {
    "legacy"  { $script:newCf = $false; $script:newPc = $false }
    "new"     { $script:newCf = $true;  $script:newPc = $false }
    "partial" { $script:newCf = $true;  $script:newPc = $true }
}

if ($serverOk) {
    $json = @{ duo_cache_friendly_ctx = [bool]$script:newCf; duo_partial_compression = [bool]$script:newPc } | ConvertTo-Json -Compress
    try {
        Invoke-RestMethod -Method Post -Uri $base -Body $json -ContentType "application/json" -TimeoutSec 5 | Out-Null
        Write-Host "  -> Mode '$Mode' set on the server."
    } catch {
        Write-Host "  [ERROR] POST /settings failed: $($_.Exception.Message)"
        $script:hadError = $true
    }
} else {
    Write-Host "  [WARN] server unreachable at $base - trying settings.json directly..."
    Write-JsonStand
}

# Log wegsichern (nur wenn ein hivemind.log existiert)
if (Test-Path -LiteralPath "logs\hivemind.log") {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $dst = "logs\ab_${stamp}_$Mode.log"
    Copy-Item -LiteralPath "logs\hivemind.log" -Destination $dst -ErrorAction SilentlyContinue
    Write-Host "  -> Previous log saved to: $dst"
}

# Stand nach dem Schalten neu anzeigen
if ($serverOk) { Read-ServerStand } else { Read-JsonStand }
Show-Stand

Write-Host ""
Write-Host "  Phase 1 procedure:"
Write-Host "    1) Mode legacy: run the same task (log is saved automatically)"
Write-Host "    2) Mode new:    run the SAME task again"
Write-Host "    3) Evaluation:  python deploy\analyze_cache_log.py logs\ab_*_legacy.log logs\hivemind.log"
Write-Host ""
if ($hadError) {
    Write-Host "  There were errors (see above)."
    Read-Host "  Press Enter to close..."
    exit 1
}

# Prune step of update.bat (extracted 2026-10-16 from a giant inline
# powershell -Command that broke in cmd quoting: a PowerShell parse error
# silently skipped the prune AND the manifest rewrite).
#
# Contract: files on the PREVIOUS manifest that the new zip no longer ships
# are release files by construction - they move to the trash folder
# (reversible). User-data files are protected by name/directory even if a
# future zip accidentally ships them. Afterwards the manifest is rewritten
# from the extracted zip (the file list this update actually installed).
# Output: the number of pruned files (update.bat reads it as PRUNED).
param(
  [Parameter(Mandatory=$true)][string]$Base,
  [Parameter(Mandatory=$true)][string]$Ex,
  [Parameter(Mandatory=$true)][string]$Trash,
  [Parameter(Mandatory=$true)][string]$Manifest
)

$protFiles = 'settings.json','models.json','vision_model.json','presets.json',
             'memory.json','routing_weights.json','gateway.toml',
             'gateway_state.json','gateway_state.json.corrupt','gateway.disabled',
             'gateway.lock','gateway_audit.jsonl','gateway_state.tmp'
$protDirs  = 'sessions','context','logs','custom_prompts','.hive_uploads',
             'learning','learning_logs','.venv','llama','models'

$old = Get-Content -LiteralPath $Manifest -ErrorAction SilentlyContinue |
  Where-Object {
    $_ -and ($protFiles -notcontains $_) -and
    -not ($_.StartsWith('gateway_audit.jsonl.')) -and
    ($protDirs -notcontains $_.Split('\')[0]) -and
    ((Get-Item -LiteralPath (Join-Path $Base $_) -ErrorAction SilentlyContinue) -is [System.IO.FileInfo])
  }

$n = 0
foreach ($f in $old) {
  if (-not (Test-Path -LiteralPath (Join-Path $Ex $f))) {
    $src = Join-Path $Base $f
    $dst = Join-Path $Trash $f
    $d = Split-Path $dst -Parent
    if (-not (Test-Path -LiteralPath $d)) {
      New-Item -ItemType Directory -Path $d -Force | Out-Null
    }
    Move-Item -Force -LiteralPath $src -Destination $dst
    $n++
  }
}
if (-not (Test-Path -LiteralPath $Trash)) {
  New-Item -ItemType Directory -Path $Trash -Force | Out-Null
}
Get-ChildItem -LiteralPath $Ex -Recurse -File |
  ForEach-Object { $_.FullName.Substring($Ex.Length + 1) } |
  Set-Content -LiteralPath $Manifest -Encoding UTF8
Write-Output $n

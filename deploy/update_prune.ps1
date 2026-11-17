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

$Ex = $Ex.TrimEnd('\', '/')
$Base = $Base.TrimEnd('\', '/')

$protFiles = 'settings.json','models.json','vision_model.json','presets.json',
             'memory.json','routing_weights.json','soul.json','automations.json',
             'memories_db.json','runtime_models.json','token_stats.json',
             'run_counter.json','gateway.toml','gateway.pid',
             'gateway_state.json','gateway_state.json.corrupt','gateway.disabled',
             'gateway.lock','gateway_audit.jsonl','gateway_state.tmp',
             'model_configs\gguf_filename_tags.json',
             'context\last_workspace.json','context\decider_cache.json',
             'searxng-config\settings.yml','searxng-config\docker-compose.yml'
$protDirs  = 'sessions','logs','custom_prompts','.hive_uploads',
             'learning_logs','.venv','llama','models','voice_models',
             'stt','tts-venv'
$protSub   = 'context\projects','learning\sessions','model_configs\learned'

# NEVER-PRUNE update.bat (audit 2026-11-04): this script runs while
# update.bat is still executing. If a future release ever dropped
# update.bat from the zip, moving the file would kill the RUNNING script
# at its next line read (mid-prune, before the manifest rewrite and the
# final self-swap). The updater must never move itself.
$old = Get-Content -LiteralPath $Manifest -ErrorAction SilentlyContinue |
  ForEach-Object { ($_ -replace '/', '\').Trim() } |
  Where-Object {
    $rel = $_
    $rel -and ($_ -ne 'update.bat') -and ($protFiles -notcontains $rel) -and
    -not ($rel.StartsWith('gateway_audit.jsonl.')) -and
    ($protDirs -notcontains $rel.Split('\')[0]) -and
    -not ($protSub | Where-Object { ($rel -eq $_) -or ($rel.StartsWith($_ + '\')) }) -and
    ((Get-Item -LiteralPath (Join-Path $Base $rel) -ErrorAction SilentlyContinue) -is [System.IO.FileInfo])
  }

$n = 0
$prunedDirs = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
foreach ($f in $old) {
  if (-not (Test-Path -LiteralPath (Join-Path $Ex $f))) {
    $src = Join-Path $Base $f
    $dst = Join-Path $Trash $f
    $d = Split-Path $dst -Parent
    if (-not (Test-Path -LiteralPath $d)) {
      New-Item -ItemType Directory -Path $d -Force | Out-Null
    }
    try {
      Move-Item -Force -LiteralPath $src -Destination $dst -ErrorAction Stop
      $n++
      $parent = Split-Path $src -Parent
      if ($parent -and $parent.Length -gt $Base.Length) {
        [void]$prunedDirs.Add($parent)
      }
    } catch {
      Write-Warning ("Could not move {0}: {1}" -f $f, $_.Exception.Message)
    }
  }
}
foreach ($dir in ($prunedDirs | Sort-Object Length -Descending)) {
  $cur = $dir
  while ($cur -and $cur.Length -gt $Base.Length) {
    if (-not (Test-Path -LiteralPath $cur)) { break }
    $items = Get-ChildItem -LiteralPath $cur -Force -ErrorAction SilentlyContinue
    if ($items.Count -eq 0) {
      Remove-Item -LiteralPath $cur -Force -ErrorAction SilentlyContinue
      $cur = Split-Path $cur -Parent
    } else {
      break
    }
  }
}
Get-ChildItem -LiteralPath $Ex -Recurse -File |
  ForEach-Object { $_.FullName.Substring($Ex.Length + 1) } |
  Set-Content -LiteralPath $Manifest -Encoding UTF8
Write-Output $n

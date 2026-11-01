# Render GitHub release notes for the console (installer/updater audit
# 2026-11-01). Replaces the broken inline render in update.bat:
#   - reads release.json as UTF-8 (PS 5.1 defaults to ANSI without a BOM,
#     which double-mangled umlauts/dashes through cmd's OEM codepage),
#   - converts markdown headings/bullets/bold/inline-code/links,
#   - marks code blocks, word-wraps to -Width columns,
#   - writes Unicode via the console API (no intermediate file, no `type`).
# Probe-verified on PS 5.1 against a realistic release body.
param(
    [Parameter(Mandatory = $true)][string]$JsonPath,
    [int]$Width = 61,
    [string]$Title = ""
)
$body = ''
try {
    $r = Get-Content -LiteralPath $JsonPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $body = [string]$r.body
} catch { }
if (-not $body.Trim()) { $body = '(no release notes in the release metadata)' }
$body = $body -replace "`r", ''
function Convert-Line([string]$l) {
    $t = $l.TrimEnd()
    $t = $t -replace '\*\*([^*]+)\*\*', '$1'
    $t = $t -replace '(?<!\*)\*([^*]+)\*(?!\*)', '$1'
    $t = $t -replace '`([^`]+)`', '$1'
    $t = $t -replace '!?\[([^\]]*)\]\(([^)]+)\)', '$1 ($2)'
    if ($t -match '^\s*(```|~~~)') { return 'CODE_MARK' }
    if ($t -match '^#{1,6}\s*(.*)$') { return ('== ' + $Matches[1]) }
    if ($t -match '^\s*[-*+]\s+(.*)$') { return (' - ' + $Matches[1]) }
    return $t
}
$lines = @()
$inCode = $false
foreach ($l in ($body -split "`n")) {
    $t = Convert-Line $l
    if ($t -eq 'CODE_MARK') {
        $inCode = -not $inCode
        if ($inCode) { ' --- code ---' } else { ' --------------' }
        continue
    }
    if ($inCode) { $t = ' | ' + $t }
    while ($t.Length -gt $Width) {
        $cut = $t.Substring(0, $Width)
        $sp = $cut.LastIndexOf(' ')
        if ($sp -lt 20) { $sp = $Width }
        $lines += $t.Substring(0, $sp).TrimEnd()
        $t = '   ' + $t.Substring($sp).TrimStart()
    }
    $lines += $t
}
# The box is drawn here (EOD-UI audit P1-1): update.bat used to pipe the
# body into a second powershell that never read stdin — cmd dropped the
# whole body and only the 3-line header printed.
$line = '+' + ('-' * $Width) + '+'
if ($Title) {
    $inner = ' ' + $Title + ' '
    $pad = [Math]::Max(0, $Width - $inner.Length)
    $lines = @($line, ('| ' + $inner + (' ' * $pad) + '|'), $line) + $lines
}
$lines += $line
$lines | ForEach-Object { Write-Output $_ }

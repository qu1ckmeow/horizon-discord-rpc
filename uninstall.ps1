# Removes Horizon Discord Presence:
#   * the Startup shortcut
#   * the running watcher
#   * the discordPresence:false line it added to Horizon's settings (the game's
#     own presence works again)
#
#   powershell -ExecutionPolicy Bypass -File .\uninstall.ps1

$ErrorActionPreference = 'Stop'

$startup = [Environment]::GetFolderPath('Startup')
if (-not $startup) {
    $startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
}
$shortcutPath = Join-Path $startup 'Horizon Discord Presence.lnk'
if (Test-Path $shortcutPath) {
    Remove-Item $shortcutPath -Force
    Write-Host "Removed $shortcutPath"
}

Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*horizon_presence.py*' } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "Stopped process $($_.ProcessId)"
    }

$homes = New-Object System.Collections.Generic.List[string]
$homes.Add((Join-Path $env:APPDATA 'horizon'))
foreach ($kind in @('instances', 'ports', 'horizon-packs')) {
    $root = Join-Path (Join-Path $env:APPDATA 'horizon') $kind
    if (-not (Test-Path $root)) { continue }
    foreach ($dir in (Get-ChildItem $root -Directory -ErrorAction SilentlyContinue)) {
        $gameHome = if ($kind -eq 'instances') { $dir.FullName } else { Join-Path $dir.FullName 'home' }
        if (Test-Path $gameHome) { $homes.Add($gameHome) }
    }
}

foreach ($gameHome in ($homes | Select-Object -Unique)) {
    $extras = Join-Path $gameHome 'horizon-extras.txt'
    if (-not (Test-Path $extras)) { continue }
    $text = [IO.File]::ReadAllText($extras, [Text.Encoding]::UTF8)
    $clean = [regex]::Replace($text, '(?im)^\s*discordPresence\s*:\s*false\s*\r?\n?', '')
    if ($clean -ne $text) {
        [IO.File]::WriteAllText($extras, $clean, (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "Horizon's own Discord presence is on again ($extras)."
    }
}

Write-Host 'Horizon Discord Presence is uninstalled. The files in this folder stay; delete them if you want.' -ForegroundColor Green

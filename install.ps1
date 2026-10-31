# Installs Horizon Discord Presence:
#   * puts a shortcut to the invisible watcher in the Start Menu's Startup folder
#   * turns off Horizon's own in-game Discord presence so the two do not fight
#   * starts the watcher now
#
# Run it from this folder:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'horizon_presence.py'

if (-not (Test-Path $script)) {
    throw "horizon_presence.py was not found next to install.ps1."
}

# --- find Python and its windowless launcher --------------------------------
$python = $null
foreach ($name in @('python.exe', 'python3.exe')) {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if ($found) {
        $python = $found.Source
        break
    }
}
if (-not $python) {
    throw "Python 3 was not found. Install it from https://www.python.org/downloads/ and run this again."
}

$pythonw = (& $python -c "import os, sys; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))").Trim()
if (-not (Test-Path $pythonw)) {
    $pythonw = $python
    Write-Host "pythonw.exe was not found; the shortcut will use python.exe instead." -ForegroundColor Yellow
}

# --- startup shortcut --------------------------------------------------------
$startup = [Environment]::GetFolderPath('Startup')
if (-not $startup) {
    $startup = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Startup'
}
New-Item -ItemType Directory -Force -Path $startup | Out-Null
$shortcutPath = Join-Path $startup 'Horizon Discord Presence.lnk'

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + $script + '"'
$shortcut.WorkingDirectory = $here
$shortcut.Description = 'Shows Discord Rich Presence for Horizon'
$shortcut.WindowStyle = 1
$shortcut.Save()
Write-Host "Startup shortcut: $shortcutPath"

# --- stop a copy that may already be running ---------------------------------
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*horizon_presence.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# --- turn off the launcher's built-in in-game presence -----------------------
# The game reads horizon-extras.txt from its own game folder, so every instance
# (and every port/pack home) needs the line, not only the main one.
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

$line = 'discordPresence:false'
foreach ($gameHome in ($homes | Select-Object -Unique)) {
    New-Item -ItemType Directory -Force -Path $gameHome | Out-Null
    $extras = Join-Path $gameHome 'horizon-extras.txt'

    $text = ''
    if (Test-Path $extras) {
        $text = [IO.File]::ReadAllText($extras, [Text.Encoding]::UTF8)
        $backup = $extras + '.before-presence'
        if (-not (Test-Path $backup) -and $text -notmatch '(?im)^\s*discordPresence\s*:\s*false\s*$') {
            Copy-Item $extras $backup
            Write-Host "Backup of the old settings: $backup"
        }
    }

    if ($text -match '(?im)^\s*discordPresence\s*:.*$') {
        $text = [regex]::Replace($text, '(?im)^\s*discordPresence\s*:.*$', $line)
    } else {
        if ($text.Length -gt 0 -and -not $text.EndsWith("`n")) {
            $text += "`n"
        }
        $text += $line + "`n"
    }
    # UTF-8 without a BOM: a BOM would become part of the first key for the game.
    [IO.File]::WriteAllText($extras, $text, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Horizon's own presence is off ($extras)."
}

# --- start it now ------------------------------------------------------------
Start-Process -FilePath $pythonw -ArgumentList ('"' + $script + '"') -WorkingDirectory $here -WindowStyle Hidden
Start-Sleep -Seconds 2

Write-Host ''
Write-Host 'Current state:' -ForegroundColor Cyan
& $python $script --status
Write-Host ''
Write-Host 'Done. Open Horizon and the activity will appear in Discord.' -ForegroundColor Green
Write-Host "Log file: $(Join-Path $here 'presence.log')"

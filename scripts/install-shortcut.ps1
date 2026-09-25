<#
    Puts Cohandle on the Desktop and in the Start Menu.

        powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1

    The shortcut runs pythonw, not python, so there is no console window.
    That is also why the app has a Quit button: with no console there is
    nothing to Ctrl+C.

    Pass -Remove to take the shortcuts away again.
#>
param([switch]$Remove)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "Cohandle.lnk"),
    (Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\Cohandle.lnk")
)

if ($Remove) {
    foreach ($path in $targets) {
        if (Test-Path $path) { Remove-Item $path -Force; Write-Host "removed $path" }
    }
    exit 0
}

# pythonw is the windowless twin of python.exe and sits beside it.
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "Python is not on PATH. Install it from python.org, ticking 'Add python.exe to PATH'." }
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { throw "pythonw.exe was not found next to $python" }

$script = Join-Path $root "scripts\desktop.py"
$icon   = Join-Path $root "app\static\cohandle.ico"
if (-not (Test-Path $script)) { throw "missing $script" }

$shell = New-Object -ComObject WScript.Shell
foreach ($path in $targets) {
    $parent = Split-Path -Parent $path
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }

    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath       = $pythonw
    $lnk.Arguments        = '"{0}" --windowed' -f $script
    $lnk.WorkingDirectory = $root
    $lnk.Description      = "Personalised investor outreach, run on this machine"
    if (Test-Path $icon) { $lnk.IconLocation = $icon }
    $lnk.Save()
    Write-Host "created $path"
}

Write-Host ""
Write-Host "Double-click Cohandle on your Desktop, or search for it in the Start Menu."
Write-Host "Stop it with the Quit button in the app's own header."

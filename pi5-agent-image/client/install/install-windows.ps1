<#
Install the PiAgent companion client on Windows as a Scheduled Task that
starts at logon and runs as the current user (no administrator rights).

    powershell -ExecutionPolicy Bypass -File install-windows.ps1 -EnrollFile .\laptop.json
    powershell -ExecutionPolicy Bypass -File install-windows.ps1            # update, keep config

The enroll file comes from `piagent devices add laptop -o laptop.json` on the Pi.
Requires Python 3.8+ from python.org (tick "Add python.exe to PATH").
#>
param([string]$EnrollFile)
$ErrorActionPreference = "Stop"

function Find-Exe([string[]]$names) {
    foreach ($n in $names) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
    }
    return $null
}

$python = Find-Exe @("python.exe", "py.exe")
$pythonw = Find-Exe @("pythonw.exe", "pyw.exe")
if (-not $python -or -not $pythonw) { throw "Python 3 is required (python.org installer, 'Add python.exe to PATH')." }

$dir = Join-Path $env:APPDATA "piagent-client"
$config = Join-Path $dir "config.json"
$log = Join-Path $env:LOCALAPPDATA "piagent-client\client.log"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
Copy-Item (Join-Path $PSScriptRoot "..\piagent_client.py") (Join-Path $dir "piagent_client.py") -Force

if (-not (Test-Path $config)) {
    if (-not $EnrollFile) { throw "First install needs -EnrollFile (from 'piagent devices add NAME -o NAME.json' on the Pi)." }
    & $python (Join-Path $dir "piagent_client.py") --config $config init --from-json $EnrollFile
    if ($LASTEXITCODE -ne 0) { throw "init failed" }
}
# Only the current user may read the config (it holds the device secret).
icacls $config /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null

$user = "$env:USERDOMAIN\$env:USERNAME"
$arguments = "`"$dir\piagent_client.py`" --config `"$config`" --log `"$log`" run"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName "PiAgent Client" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force | Out-Null
Stop-ScheduledTask -TaskName "PiAgent Client" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "PiAgent Client"

Write-Host ""
Write-Host "Installed. Review the allowlist in $config"
Write-Host "Check:  python `"$dir\piagent_client.py`" --config `"$config`" test"
Write-Host "Logs:   Get-Content -Wait `"$log`""
Write-Host "Remove: Unregister-ScheduledTask -TaskName 'PiAgent Client' -Confirm:`$false"

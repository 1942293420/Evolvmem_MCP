param(
    [switch]$Uninstall,
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'EvolvMem\Codex\config.json'),
    [string]$DataRoot = ''
)
$ErrorActionPreference = 'Stop'
$taskName = 'EvolvMem DSH Sync'
if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    Write-Output 'DSH sync task removed; source logs, queue, receipts and program retained.'
    exit 0
}
$config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
$pythonConsole = [string]$config.decoder_python
$python = Join-Path (Split-Path -Parent $pythonConsole) 'pythonw.exe'
$stateDir = $DataRoot
if (-not $stateDir) {
    $stateDir = (& $pythonConsole -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).parent.resolve().parent / "DSH")' $ConfigPath).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $stateDir) { throw 'Cannot resolve DSH data directory' }
}
$installDir = Join-Path $stateDir 'program'
$sourceScript = Join-Path $PSScriptRoot 'dsh_archive.py'
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $sourceScript)) { throw 'Required DSH archiver runtime is missing' }
& $pythonConsole -c 'import zstandard'
if ($LASTEXITCODE -ne 0) { throw 'Zstandard runtime check failed' }
New-Item -ItemType Directory -Path $installDir -Force | Out-Null
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls $stateDir /inheritance:r /grant:r "*${sid}:(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Private directory ACL check failed' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = Join-Path $stateDir "backups\$stamp"
New-Item -ItemType Directory -Path $backup -Force | Out-Null
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Export-ScheduledTask -TaskName $taskName | Set-Content -LiteralPath (Join-Path $backup 'task.xml') -Encoding UTF8
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
}
$targetScript=Join-Path $installDir 'dsh_archive.py'
if (Test-Path -LiteralPath $targetScript) { Copy-Item -LiteralPath $targetScript -Destination (Join-Path $backup 'dsh_archive.py') }
Copy-Item -LiteralPath $sourceScript -Destination $targetScript -Force
$action=New-ScheduledTaskAction -Execute $python -Argument "`"$targetScript`" once --root `"$stateDir`" --config `"$ConfigPath`"" -WorkingDirectory $installDir
$periodic=New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 1)
$login=New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal=New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($periodic,$login) -Settings $settings -Principal $principal `
    -Description 'Archive complete persisted native DSH transcripts to personal EvolvMem every minute. DPAPI queue; no AI extraction.' -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Output "Installed $taskName (60-second scan and logon recovery). State: $stateDir"

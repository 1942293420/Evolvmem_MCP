[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$SourcePath)

# Native integration coverage for the hidden WindowsPowerShell launcher.  This
# runs the compiled executable beside a synthetic adjacent worker; it never
# uses the installed client or network.
$ErrorActionPreference = 'Stop'
$utf8 = New-Object Text.UTF8Encoding($false)
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('evolvmem-sync-launcher-' + [Guid]::NewGuid().ToString('N'))
$script:TestProcesses = New-Object 'System.Collections.Generic.List[System.Diagnostics.Process]'

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Write-Utf8([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, $utf8)
}

function Read-Status([string]$Directory) {
    $statusPath = Join-Path $Directory 'launcher-status.json'
    Assert-True ([IO.File]::Exists($statusPath)) 'launcher status was not written.'
    Assert-True (@(Get-ChildItem -LiteralPath $Directory -Filter 'launcher-status.json.*.tmp' -ErrorAction SilentlyContinue).Count -eq 0) 'launcher left an atomic-write temporary file.'
    return ([IO.File]::ReadAllText($statusPath, [Text.Encoding]::UTF8) | ConvertFrom-Json)
}

function Invoke-Launcher([string]$LauncherPath) {
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $LauncherPath
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        $process.Dispose()
        throw 'The synthetic launcher process could not start.'
    }
    [void]$script:TestProcesses.Add($process)
    if (-not $process.WaitForExit(15000)) {
        throw 'The synthetic launcher process did not exit within 15 seconds.'
    }
    $exitCode = $process.ExitCode
    [void]$script:TestProcesses.Remove($process)
    $process.Dispose()
    return $exitCode
}

function Write-SyntheticWorker([string]$Path) {
    $worker = @'
param([string]$Action)
$ErrorActionPreference = 'Stop'
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class EvolvMemConsoleProbe {
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
}
"@
$observation = [ordered]@{
    action = $Action
    console_window = [Int64][EvolvMemConsoleProbe]::GetConsoleWindow().ToInt64()
}
$observation | ConvertTo-Json -Compress | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'worker-observation.json') -Encoding UTF8
switch ($env:EVOLVMEM_TEST_WORKER_MODE) {
    'failed-result' {
        '{"worker_status":"failed","error":"synthetic worker failure: top-secret-token"}'
        exit 0
    }
    'nonzero-exit' {
        [Console]::Error.Write((New-Object string ('x', 1048576)))
        exit 23
    }
    'large-stderr' {
        [Console]::Error.Write((New-Object string ('x', 1048576)))
        '{"worker_status":"idle","captured_versions":4,"acknowledged_versions":3,"pending_versions":0,"discovered_sessions":4,"capture_errors":0,"discovery_errors":0}'
        exit 0
    }
    'retry-pending' {
        '{"worker_status":"retry_pending","captured_versions":2,"acknowledged_versions":1,"pending_versions":1,"discovered_sessions":7,"capture_errors":1,"discovery_errors":2,"last_started_utc":"2026-09-17T00:00:00.0000000Z","last_finished_utc":"2026-09-17T00:00:01.0000000Z"}'
        exit 0
    }
    'malformed-result' {
        'unexpected prefix {"worker_status":"idle","captured_versions":4,"acknowledged_versions":3,"pending_versions":0,"discovered_sessions":4,"capture_errors":0,"discovery_errors":0}'
        exit 0
    }
    'missing-count' {
        '{"worker_status":"idle","captured_versions":4,"acknowledged_versions":3,"pending_versions":0,"capture_errors":0,"discovery_errors":0}'
        exit 0
    }
    default {
        '{"worker_status":"idle","captured_versions":4,"acknowledged_versions":3,"pending_versions":0,"discovered_sessions":4,"capture_errors":0,"discovery_errors":0}'
        exit 0
    }
}
'@
    Write-Utf8 $Path $worker
}

try {
    if ($env:OS -ne 'Windows_NT') { throw 'This test must run on Windows.' }
    if (-not [IO.File]::Exists($SourcePath)) { throw 'The launcher C# source does not exist.' }
    [void][IO.Directory]::CreateDirectory($testRoot)
    $compiler = Join-Path ([Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()) 'csc.exe'
    Assert-True ([IO.File]::Exists($compiler)) 'The built-in .NET Framework C# compiler is unavailable.'
    $compiledLauncher = Join-Path $testRoot 'compiled-launcher.exe'
    & $compiler /nologo /target:winexe /r:System.Web.Extensions.dll ('/out:' + $compiledLauncher) $SourcePath
    Assert-True ($LASTEXITCODE -eq 0 -and [IO.File]::Exists($compiledLauncher)) 'The launcher source did not compile with the built-in .NET Framework compiler.'

    # A Chinese and space-containing directory catches argument quoting and
    # adjacent-file resolution together.
    $caseRoot = Join-Path $testRoot '同步 launcher 中文 path'
    [void][IO.Directory]::CreateDirectory($caseRoot)
    $launcher = Join-Path $caseRoot 'evolvmem-sync.exe'
    $worker = Join-Path $caseRoot 'evolvmem-codex.ps1'
    Copy-Item -LiteralPath $compiledLauncher -Destination $launcher
    Write-SyntheticWorker $worker

    $env:EVOLVMEM_TEST_WORKER_MODE = 'success'
    $exitCode = Invoke-Launcher $launcher
    Assert-True ($exitCode -eq 0) 'A healthy worker must make the launcher succeed.'
    $observation = ([IO.File]::ReadAllText((Join-Path $caseRoot 'worker-observation.json'), [Text.Encoding]::UTF8) | ConvertFrom-Json)
    Assert-True ($observation.action -eq 'worker') 'The launcher did not pass -Action worker.'
    Assert-True ([Int64]$observation.console_window -eq 0) 'The worker received a visible console window.'
    $status = Read-Status $caseRoot
    Assert-True ($status.run_state -eq 'completed' -and [int]$status.process_exit_code -eq 0) 'Healthy run status is incomplete.'
    Assert-True ($null -ne $status.started_utc -and $null -ne $status.finished_utc) 'Status must include UTC start and finish timestamps.'
    Assert-True ([int]$status.captured_versions -eq 4 -and [int]$status.acknowledged_versions -eq 3 -and [int]$status.pending_versions -eq 0) 'Worker counts were not retained in launcher status.'

    $env:EVOLVMEM_TEST_WORKER_MODE = 'failed-result'
    $exitCode = Invoke-Launcher $launcher
    Assert-True ($exitCode -ne 0) 'An error worker result must fail the launcher.'
    $status = Read-Status $caseRoot
    Assert-True ($status.run_state -eq 'failed' -and [int]$status.process_exit_code -eq 0) 'Error result status must retain child exit code and fail the run.'
    Assert-True ([string]$status.error -notmatch 'top-secret-token|synthetic worker failure') 'Launcher status exposed worker output.'

    $env:EVOLVMEM_TEST_WORKER_MODE = 'nonzero-exit'
    $exitCode = Invoke-Launcher $launcher
    Assert-True ($exitCode -eq 23) 'The launcher must return a nonzero child exit code.'
    $status = Read-Status $caseRoot
    Assert-True ($status.run_state -eq 'failed' -and [int]$status.process_exit_code -eq 23) 'Nonzero child exit status is incorrect.'

    $env:EVOLVMEM_TEST_WORKER_MODE = 'large-stderr'
    $exitCode = Invoke-Launcher $launcher
    Assert-True ($exitCode -eq 0) 'Large redirected stderr must not deadlock a valid worker run.'
    $status = Read-Status $caseRoot
    Assert-True ($status.run_state -eq 'completed') 'Large stderr worker status is incorrect.'

    $env:EVOLVMEM_TEST_WORKER_MODE = 'malformed-result'
    $exitCode = Invoke-Launcher $launcher
    Assert-True ($exitCode -ne 0) 'Malformed worker JSON must fail the launcher.'
    $status = Read-Status $caseRoot
    Assert-True ($status.run_state -eq 'failed') 'Malformed worker JSON status is not failed.'

    $env:EVOLVMEM_TEST_WORKER_MODE = 'missing-count'
    $exitCode = Invoke-Launcher $launcher
    Assert-True ($exitCode -ne 0) 'Worker JSON missing a required count must fail the launcher.'
    $status = Read-Status $caseRoot
    Assert-True ($status.run_state -eq 'failed') 'Missing-count worker status is not failed.'

    $env:EVOLVMEM_TEST_WORKER_MODE = 'retry-pending'
    $exitCode = Invoke-Launcher $launcher
    Assert-True ($exitCode -ne 0) 'Retry-pending worker status must request another scheduled run.'
    $status = Read-Status $caseRoot
    Assert-True ($status.run_state -eq 'failed' -and $status.worker_status -eq 'retry_pending') 'Retry-pending worker status was not retained.'
    Assert-True ([int]$status.discovered_sessions -eq 7 -and [int]$status.capture_errors -eq 1 -and [int]$status.discovery_errors -eq 2) 'Retry-pending worker counts were not retained.'
    Assert-True ($status.worker_last_started_utc -eq '2026-09-17T00:00:00.0000000Z' -and $status.worker_last_finished_utc -eq '2026-09-17T00:00:01.0000000Z') 'Retry-pending worker timestamps were not retained.'

    Remove-Item -LiteralPath $worker -Force
    $exitCode = Invoke-Launcher $launcher
    Assert-True ($exitCode -ne 0) 'A missing adjacent client must fail the launcher.'
    $status = Read-Status $caseRoot
    Assert-True ($status.run_state -eq 'failed') 'Missing client status is not failed.'

    [ordered]@{ passed=$true; compiler=$compiler; unicode_path=$caseRoot } | ConvertTo-Json -Compress
}
finally {
    foreach ($process in @($script:TestProcesses)) {
        try {
            if (-not $process.HasExited) {
                $process.Kill()
                [void]$process.WaitForExit(5000)
            }
        }
        catch { }
        finally { try { $process.Dispose() } catch { } }
    }
    try { Remove-Item Env:EVOLVMEM_TEST_WORKER_MODE -ErrorAction SilentlyContinue } catch { }
    try { if ([IO.Directory]::Exists($testRoot)) { [IO.Directory]::Delete($testRoot, $true) } } catch { }
}

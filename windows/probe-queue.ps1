# Read-only queue/worker sampler for the EvolvMem Codex Windows client.
# Prints only: session_id, sha256, sizes, offsets, timestamps and worker counters.
# Never reads config.json, the credential, or any queue payload (.bin) content.
$ErrorActionPreference = 'Stop'
$clientHome = [IO.Path]::Combine($env:LOCALAPPDATA, 'EvolvMem', 'Codex')
$queueDir = [IO.Path]::Combine($clientHome, 'queue')
$result = [ordered]@{
    sampled_utc = [DateTime]::UtcNow.ToString('o')
    client_home = $clientHome
    queue_dir_exists = [IO.Directory]::Exists($queueDir)
    manifests = @()
}
if ($result.queue_dir_exists) {
    foreach ($path in @([IO.Directory]::GetFiles($queueDir, '*.json'))) {
        $m = $null
        try { $m = ([IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)) | ConvertFrom-Json } catch { }
        if ($null -eq $m) { continue }
        $binPath = [IO.Path]::Combine($queueDir, [IO.Path]::GetFileNameWithoutExtension($path) + '.bin')
        $result.manifests += [ordered]@{
            session_id = [string]$m.session_id
            sha256 = [string]$m.sha256
            total_bytes = [int64]$m.total_bytes
            next_offset = [int64]$m.next_offset
            remaining_bytes = [int64]$m.total_bytes - [int64]$m.next_offset
            project_len = ([string]$m.project).Length
            extract = [bool]$m.extract
            created_utc = [string]$m.created_utc
            manifest_mtime_utc = ([IO.File]::GetLastWriteTimeUtc($path)).ToString('o')
            bin_bytes = if ([IO.File]::Exists($binPath)) { ([IO.FileInfo]$binPath).Length } else { -1 }
            bin_mtime_utc = if ([IO.File]::Exists($binPath)) { ([IO.FileInfo]$binPath).LastWriteTimeUtc.ToString('o') } else { '' }
        }
    }
}
$workerPath = [IO.Path]::Combine($clientHome, 'worker-status.json')
if ([IO.File]::Exists($workerPath)) {
    $w = ([IO.File]::ReadAllText($workerPath, [Text.Encoding]::UTF8)) | ConvertFrom-Json
    $result.worker = [ordered]@{
        worker_status = [string]$w.worker_status
        last_started_utc = [string]$w.last_started_utc
        last_finished_utc = [string]$w.last_finished_utc
        discovered_sessions = $w.discovered_sessions
        discovery_errors = $w.discovery_errors
        capture_errors = $w.capture_errors
        captured_versions = $w.captured_versions
        acknowledged_versions = $w.acknowledged_versions
        pending_versions = $w.pending_versions
        capture_failures = @($w.capture_failures | ForEach-Object { [ordered]@{ error_type = [string]$_.error_type; line = $_.line; category = [string]$_.category } })
    }
}
try {
    $task = Get-ScheduledTask -TaskName 'EvolvMem Codex Sync' -ErrorAction Stop
    $info = Get-ScheduledTaskInfo -InputObject $task -ErrorAction Stop
    $result.task = [ordered]@{
        state = [string]$task.State
        enabled = [bool]$task.Settings.Enabled
        last_run_utc = $info.LastRunTime.ToUniversalTime().ToString('o')
        next_run_utc = $info.NextRunTime.ToUniversalTime().ToString('o')
        last_result = [int64]$info.LastTaskResult
    }
} catch { $result.task = @{ error = 'task_unavailable' } }
[Console]::Out.WriteLine(($result | ConvertTo-Json -Depth 8 -Compress))

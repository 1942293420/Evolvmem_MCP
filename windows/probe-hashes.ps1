# Read-only: hash the installed client files so the local source reading can be
# tied to what is actually running on Windows. No file contents are printed.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$clientHome = [IO.Path]::Combine($env:LOCALAPPDATA, 'EvolvMem', 'Codex')
$out = [ordered]@{}
foreach ($name in @('evolvmem-codex.ps1', 'evolvmem-sync.exe', 'config.json')) {
    $path = [IO.Path]::Combine($clientHome, $name)
    if (-not [IO.File]::Exists($path)) { $out[$name] = 'missing'; continue }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = ([BitConverter]::ToString($sha.ComputeHash([IO.File]::ReadAllBytes($path)))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
    $out[$name] = [ordered]@{ sha256 = $hash; bytes = ([IO.FileInfo]$path).Length
                              mtime_utc = ([IO.FileInfo]$path).LastWriteTimeUtc.ToString('o') }
}
$out.powershell = $PSVersionTable.PSVersion.ToString()
$out.queue_dir_files = @([IO.Directory]::GetFiles([IO.Path]::Combine($clientHome, 'queue'))).Count
$out.archive_status_files = @([IO.Directory]::GetFiles([IO.Path]::Combine($clientHome, 'archive-status'))).Count
$out.session_registrations = @([IO.Directory]::GetFiles([IO.Path]::Combine($clientHome, 'sessions'), '*.json')).Count
$out.session_cache_files = @([IO.Directory]::GetFiles([IO.Path]::Combine($clientHome, 'session-cache'))).Count
[Console]::Out.WriteLine(($out | ConvertTo-Json -Depth 5 -Compress))

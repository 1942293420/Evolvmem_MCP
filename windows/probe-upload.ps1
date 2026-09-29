# EvolvMem Codex upload diagnostic (read-only + ONE idempotent replay).
# Loads ONLY the function definitions from the installed client via the PowerShell
# AST, so the client's main dispatch logic never runs. The DPAPI blob is decrypted
# in this process only; no transcript bytes, project name, URL or credential are
# printed. Output is limited to: session_id, sizes, offsets, hashes, timing and the
# remote tool's own error/status keywords.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)

$clientHome = [IO.Path]::Combine($env:LOCALAPPDATA, 'EvolvMem', 'Codex')
$installed = [IO.Path]::Combine($clientHome, 'evolvmem-codex.ps1')
$tokens = $null; $parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($installed, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw 'installed client did not parse' }
$definitions = $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)
$module = New-Module -Name EvolvMemDiag -ScriptBlock ([scriptblock]::Create((($definitions | ForEach-Object { $_.Extent.Text }) -join "`r`n")))

& $module {
    $script:RunningOnWindows = $true
    $script:ClientHome = [IO.Path]::Combine($env:LOCALAPPDATA, 'EvolvMem', 'Codex')
    $script:ConfigPath = [IO.Path]::Combine($script:ClientHome, 'config.json')
    $script:ChunkBytes = 262144
    $script:RpcTimeoutSeconds = 30
    $script:ActionToken = ''
    $script:IdentityValidated = $false
    $script:PreflightStatus = $null
    $script:MaxContextChars = 8000
    $script:ConnectionMetadataReserveChars = 1600

    $report = [ordered]@{ replays = @(); status_probes = @() }
    $config = Get-Config

    # ---- read-only probes: what the server already recorded for two sessions ----
    foreach ($probe in @('01a0bd06-0390-7363-b1c2-cf00d3130116', '01a0b354-4257-7c31-8c9e-12093d8e42dc')) {
        $entry = [ordered]@{ session_id = $probe }
        try {
            $st = Invoke-McpTool $config 'session_archive_status' ([ordered]@{
                    device_id = [string]$config.device_id; session_id = $probe })
            foreach ($k in @('status', 'next_offset', 'total_bytes', 'source_sha256', 'extraction_status',
                             'archive_id', 'processing_error', 'attribution_reason', 'backfill_status', 'payload_state', 'error')) {
                if ($null -ne (Get-Value $st $k)) { $entry[$k] = (Get-Value $st $k) }
            }
        }
        catch { $entry.probe_error = $_.Exception.Message }
        $report.status_probes += $entry
    }

    # ---- pick the smallest queued version (its only chunk is the final one) ----
    $queueDir = [IO.Path]::Combine($script:ClientHome, 'queue')
    $manifests = @([IO.Directory]::GetFiles($queueDir, '*.json') | Sort-Object {
            try { [int64](Get-Value (Read-JsonFile $_) 'total_bytes' ([int64]::MaxValue)) } catch { [int64]::MaxValue } })
    if ($manifests.Count -lt 1) { throw 'queue is empty' }
    $manifestPath = $manifests[0]
    $manifest = Read-JsonFile $manifestPath
    $stem = [IO.Path]::GetFileNameWithoutExtension($manifestPath)
    $dataPath = [IO.Path]::Combine($queueDir, $stem + '.bin')
    $plain = Unprotect-Bytes ([IO.File]::ReadAllBytes($dataPath))
    $offset = [int64]$manifest.next_offset
    $length = [int64][Math]::Min($script:ChunkBytes, $plain.Length - $offset)
    $chunk = New-Object byte[] ([int]$length)
    [Array]::Copy($plain, $offset, $chunk, 0, $length)
    $sessionHash = Get-TextSha256 ([string]$manifest.session_id)
    # Identical idempotency parameters to the ones the real client derives.
    $requestId = 'archive-' + $sessionHash.Substring(0, 12) + '-' +
        ([string]$manifest.sha256).Substring(0, 24) + '-' + $offset
    $arguments = [ordered]@{
        device_id = [string]$config.device_id; session_id = [string]$manifest.session_id
        project = [string]$manifest.project; sha256 = [string]$manifest.sha256
        total_bytes = [int64]$manifest.total_bytes; offset = $offset
        content_b64 = [Convert]::ToBase64String($chunk); extract = [bool]$manifest.extract
        request_id = $requestId
    }
    $replay = [ordered]@{
        session_id = [string]$manifest.session_id
        sha256 = [string]$manifest.sha256
        total_bytes = [int64]$manifest.total_bytes
        offset = $offset
        chunk_bytes = $length
        local_blob_length_matches = ($plain.Length -eq [int64]$manifest.total_bytes)
        local_blob_hash_matches = ((Get-Sha256Hex $plain) -ceq [string]$manifest.sha256)
        queue_entries = $manifests.Count
    }
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $result = Invoke-Rpc $config 'tools/call' @{ name = 'session_archive_upload'; arguments = $arguments } $requestId
        $watch.Stop()
        $replay.elapsed_ms = [int64]$watch.ElapsedMilliseconds
        $replay.is_error = [bool](Get-Value $result 'isError' $false)
        $content = @(Get-Value $result 'content' @())
        if ($content.Count -ge 1 -and (Get-Value $content[0] 'type' '') -eq 'text') {
            $payload = ([string](Get-Value $content[0] 'text' '')) | ConvertFrom-Json
            foreach ($k in @('error', 'status', 'next_offset', 'total_bytes', 'source_sha256', 'extraction_status',
                             'archive_id', 'processing_error', 'attribution_reason', 'backfill_status',
                             'submitted_sha256', 'submitted_total_bytes', 'authenticated_user')) {
                $v = Get-Value $payload $k
                if ($null -ne $v) {
                    if ($k -eq 'authenticated_user') { $replay.user_ok = ($v -ceq [string]$config.expected_user) }
                    else { $replay[$k] = $v }
                }
            }
            $replay.returned_keys = @($payload.PSObject.Properties.Name | Sort-Object)
        }
        else { $replay.content_shape = 'unexpected' }
    }
    catch {
        $watch.Stop()
        $replay.elapsed_ms = [int64]$watch.ElapsedMilliseconds
        $replay.transport_error = $_.Exception.GetType().FullName
        $replay.transport_message = [string]$_.Exception.Message
        if ($_.Exception.InnerException) { $replay.transport_inner = $_.Exception.InnerException.GetType().FullName }
    }
    $report.replays += $replay
    [Console]::Out.WriteLine(($report | ConvertTo-Json -Depth 6 -Compress))
}

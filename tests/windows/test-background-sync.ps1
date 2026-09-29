[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ClientPath,
    [string]$TestRoot,
    [switch]$WorkerProbe
)

# Real PowerShell 5.1, DPAPI, files and fresh worker processes. Only the MCP
# boundary is replaced; synthetic conversation records never leave this test.
$ErrorActionPreference = 'Stop'
$utf8 = New-Object Text.UTF8Encoding($false)
if ($WorkerProbe) {
    $env:EVOLVMEM_CLIENT_HOME = Join-Path $TestRoot 'client'
    # Load functions with an empty, local-only snapshot event.
    [Console]::SetIn((New-Object IO.StringReader('{}')))
    . $ClientPath -Action snapshot
    function Invoke-McpTool($Config, [string]$Name, $Arguments, [string]$RpcId = '') {
        if ($Name -ne 'session_archive_upload') { throw 'Unexpected MCP call in isolated test.' }
        $attempt = Join-Path $TestRoot 'attempt.json'
        [IO.File]::WriteAllText($attempt, ($Arguments | ConvertTo-Json -Depth 10 -Compress), $utf8)
        if ([IO.File]::Exists((Join-Path $TestRoot 'offline'))) { throw 'Simulated offline transport.' }
        $bytes = [Convert]::FromBase64String($Arguments.content_b64)
        if ($Arguments.offset -ne 0 -or $Arguments.total_bytes -ne $bytes.Length) { throw 'Unexpected small-test chunk.' }
        [IO.File]::WriteAllBytes((Join-Path $TestRoot ($Arguments.sha256 + '.received')), $bytes)
        return [pscustomobject]@{ status='archived'; next_offset=$bytes.Length; source_sha256=$Arguments.sha256; archive_id='isolated'; extraction_status='pending' }
    }
    Invoke-Worker (Get-Config)
    exit 0
}

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}

function Run-Worker {
    $text = @(& powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $PSCommandPath -ClientPath $ClientPath -TestRoot $TestRoot -WorkerProbe)
    Assert-True ($LASTEXITCODE -eq 0) 'The worker probe process failed.'
    return ($text[-1] | ConvertFrom-Json)
}

function Hash-Bytes([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

if ($env:OS -ne 'Windows_NT') { throw 'Run this test on Windows PowerShell 5.1.' }
$TestRoot = Join-Path ([IO.Path]::GetTempPath()) ('evolvmem-background-test-' + [Guid]::NewGuid().ToString('N'))
try {
    $client = Join-Path $TestRoot 'client'
    $codex = Join-Path $TestRoot 'codex'
    $sessions = Join-Path $codex 'sessions'
    [void][IO.Directory]::CreateDirectory($client)
    [void][IO.Directory]::CreateDirectory($sessions)
    $chinese = ([string][char]0x4e2d) + [char]0x6587
    $workspace = Join-Path $TestRoot ($chinese + ' workspace')
    $projects = @{}; $projects[$workspace] = 'isolated-project'
    $config = @{ version=1; url='http://127.0.0.1:1/mcp'; token_env_var='EVOLVMEM_UNUSED'; expected_user='isolated'; device_id='isolated'; projects=$projects; codex_root=$codex; capture_since_utc=[DateTime]::UtcNow.AddMinutes(-1).ToString('o') }
    [IO.File]::WriteAllText((Join-Path $client 'config.json'), ($config | ConvertTo-Json -Depth 5), $utf8)
    $header = @{type='session_meta'; payload=@{id='isolated-session'; cwd=$workspace}} | ConvertTo-Json -Depth 5 -Compress
    $message = @{type='event_msg'; payload=@{type='user_message'; message=($chinese + [char]0x2028 + 'separator')}} | ConvertTo-Json -Depth 5 -Compress
    $first = $header + "`n" + $message + "`n"
    $tail = '{"type":"event_msg","payload":{"type":"agent_message","message":"complete"}}'
    $transcript = Join-Path $sessions 'rollout-isolated-session.jsonl'
    [IO.File]::WriteAllText($transcript, $first + $tail, $utf8)
    $old = Join-Path $sessions 'rollout-dormant-session.jsonl'
    [IO.File]::WriteAllText($old, ($header.Replace('isolated-session', 'dormant-session') + "`n"), $utf8)
    [IO.File]::SetLastWriteTimeUtc($old, [DateTime]::UtcNow.AddDays(-10))
    $offline = Join-Path $TestRoot 'offline'
    [IO.File]::WriteAllText($offline, 'offline', $utf8)
    $one = Run-Worker
    Assert-True ($one.discovered_sessions -eq 1 -and $one.pending_versions -eq 1 -and $one.worker_status -eq 'retry_pending') 'New session discovery or durable offline queue failed.'
    $attemptOne = [IO.File]::ReadAllText((Join-Path $TestRoot 'attempt.json'), $utf8) | ConvertFrom-Json
    Assert-True ($attemptOne.project -eq 'isolated-project') 'Project mapping failed.'
    $queue = @(Get-ChildItem -LiteralPath (Join-Path $client 'queue') -Filter '*.bin')
    Add-Type -AssemblyName System.Security
    $encrypted = [IO.File]::ReadAllBytes($queue[0].FullName)
    $plain = [Security.Cryptography.ProtectedData]::Unprotect($encrypted, $utf8.GetBytes('evolvmem-codex-archive-v1'), [Security.Cryptography.DataProtectionScope]::CurrentUser)
    Assert-True ($utf8.GetString($plain) -ceq $first) 'Capture changed bytes or included an incomplete record.'
    Assert-True ((Hash-Bytes $encrypted) -cne (Hash-Bytes $plain)) 'Queued data was plaintext.'
    [IO.File]::Delete($offline)
    $two = Run-Worker
    $attemptTwo = [IO.File]::ReadAllText((Join-Path $TestRoot 'attempt.json'), $utf8) | ConvertFrom-Json
    Assert-True ($two.acknowledged_versions -eq 1 -and $two.pending_versions -eq 0) 'Fresh-process retry did not acknowledge and clear the queued version.'
    Assert-True ($attemptOne.request_id -ceq $attemptTwo.request_id) 'Restart changed the retry identity.'
    [IO.File]::AppendAllText($transcript, "`n", $utf8)
    $three = Run-Worker
    $complete = $utf8.GetBytes($first + $tail + "`n")
    $received = Join-Path $TestRoot ((Hash-Bytes $complete) + '.received')
    Assert-True ($three.acknowledged_versions -eq 1 -and [IO.File]::Exists($received)) 'Completed record was not uploaded.'
    Assert-True ((Hash-Bytes ([IO.File]::ReadAllBytes($received))) -ceq (Hash-Bytes $complete)) 'Uploaded bytes differ from the complete transcript.'
    [IO.File]::SetLastWriteTimeUtc($old, [DateTime]::UtcNow)
    $four = Run-Worker
    Assert-True ($four.discovered_sessions -eq 1 -and $four.acknowledged_versions -eq 1) 'A resumed historical session was not discovered.'
    $receipt = [IO.File]::ReadAllText((Join-Path $client 'worker-status.json'), $utf8) | ConvertFrom-Json
    Assert-True ($receipt.last_finished_utc -and $receipt.pending_versions -eq 0) 'Worker completion receipt is missing.'
    [ordered]@{passed=$true; powershell=$PSVersionTable.PSVersion.ToString(); independent_discovery=$true; dpapi=$true; partial_line_wait=$true; restart_retry=$true; exact_bytes=$true; resumed_history=$true} | ConvertTo-Json -Compress
}
finally {
    if ([IO.Directory]::Exists($TestRoot)) { [IO.Directory]::Delete($TestRoot, $true) }
}

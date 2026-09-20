[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$ClientPath)

# Synthetic Windows PowerShell 5.1 regression test for client error visibility.
# It dot-sources the real client, replaces only the RPC transport and the DPAPI
# helpers, and drives a synthetic queue inside a temporary client home. It never
# reads the real EvolvMem client home, transcripts, queue payloads or credentials.

$ErrorActionPreference = 'Stop'
$utf8 = New-Object Text.UTF8Encoding($false)
$failures = New-Object Collections.Generic.List[string]
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('evolvmem-client-error-test-' + [Guid]::NewGuid().ToString('N'))
$previousClientHome = $env:EVOLVMEM_CLIENT_HOME

function Assert-Equal($Expected, $Actual, [string]$Message) {
    if ([string]$Expected -cne [string]$Actual) {
        $script:failures.Add($Message + ' | expected=[' + [string]$Expected + '] actual=[' + [string]$Actual + ']')
    }
}
function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { $script:failures.Add($Message) }
}
function Read-SyntheticJson([string]$Path) {
    return ([IO.File]::ReadAllText($Path, $script:utf8) | ConvertFrom-Json)
}
function Get-Field($Object, [string]$Name, $Default = $null) {
    # Status objects are ordered dictionaries; manifests are PSCustomObjects.
    if ($Object -is [Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
        return $Default
    }
    return Get-Value $Object $Name $Default
}
function New-McpToolResult([string]$Text, [bool]$IsError) {
    return [pscustomobject]@{
        content = @([pscustomobject]@{ type = 'text'; text = $Text })
        isError = $IsError
    }
}
function Get-ClassifiedCode($ErrorRecord) {
    if (-not (Get-Command Get-ClientErrorCode -ErrorAction SilentlyContinue)) { return '' }
    return Get-ClientErrorCode $ErrorRecord
}
function Add-SyntheticVersion([string]$ClientHome, [string]$Stem, [string]$Session, [string]$Content) {
    $queue = Join-Path $ClientHome 'queue'
    [void][IO.Directory]::CreateDirectory($queue)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Content)
    $sha = Get-Sha256Hex $bytes
    $bin = Join-Path $queue ($Stem + '.bin')
    $manifestPath = Join-Path $queue ($Stem + '.json')
    [IO.File]::WriteAllBytes($bin, $bytes)
    # Legacy on-disk shape: plain JSON object without any error field.
    $manifest = [ordered]@{
        version = 1; session_id = $Session; project = 'synthetic'
        sha256 = $sha; total_bytes = $bytes.Length; next_offset = 0; extract = $true
    }
    [IO.File]::WriteAllText($manifestPath, ($manifest | ConvertTo-Json -Compress), $script:utf8)
    return [pscustomobject]@{ Sha = $sha; Bin = $bin; Manifest = $manifestPath; Total = [int64]$bytes.Length }
}

try {
    [void][IO.Directory]::CreateDirectory($testRoot)
    $env:EVOLVMEM_CLIENT_HOME = Join-Path $testRoot 'client'
    [void][IO.Directory]::CreateDirectory($env:EVOLVMEM_CLIENT_HOME)
    $configSeed = [ordered]@{
        version = 1; url = 'http://127.0.0.1:1/mcp'; token_env_var = 'EVOLVMEM_SYNTHETIC_UNUSED_TOKEN'
        expected_user = 'synthetic-user'; device_id = 'synthetic-device'
    }
    [IO.File]::WriteAllText((Join-Path $env:EVOLVMEM_CLIENT_HOME 'config.json'),
        ($configSeed | ConvertTo-Json -Depth 4), $utf8)

    # Empty worker mode loads the real client without stdin, network or queue access.
    . $ClientPath -Action worker

    # Replace only DPAPI and the RPC transport. Error classification, queue
    # persistence and receipts stay on the real implementation.
    function Protect-Bytes([byte[]]$Bytes) { return ,$Bytes }
    function Unprotect-Bytes([byte[]]$Bytes) { return ,$Bytes }
    $script:SyntheticRpcResults = New-Object Collections.Generic.Queue[object]
    function Invoke-Rpc($Config, [string]$Method, $Parameters, [string]$RpcId) {
        if ($script:SyntheticRpcResults.Count -lt 1) { throw 'synthetic RPC result queue is empty' }
        return $script:SyntheticRpcResults.Dequeue()
    }
    $script:IdentityValidated = $true
    $config = Get-Config

    # 1. An error response carries no authenticated_user but must keep its code.
    $classified = '<unexpected-success>'
    try { [void](Convert-McpToolResult $config (New-McpToolResult '{"error":"transcript_fork"}' $true)) }
    catch { $classified = Get-ClassifiedCode $_ }
    Assert-Equal 'transcript_fork' $classified 'isError without authenticated_user must surface transcript_fork'

    $fork = Add-SyntheticVersion $env:EVOLVMEM_CLIENT_HOME 'version-fork' 'session-fork' ('x' * 300000)
    $script:SyntheticRpcResults.Enqueue((New-McpToolResult '{"error":"transcript_fork"}' $true))
    [void](Invoke-UploadQueue $config)
    $forkJson = [IO.File]::ReadAllText($fork.Manifest, $utf8)
    $forkSaved = $forkJson | ConvertFrom-Json
    Assert-Equal 'transcript_fork' (Get-Value $forkSaved 'last_error' '') 'failed upload must persist the stable code'
    Assert-True ([bool](Get-Value $forkSaved 'last_error_utc' '')) 'failed upload must persist the failure time'
    Assert-Equal $fork.Sha (Get-Value $forkSaved 'sha256' '') 'sha256 must stay unchanged'
    Assert-Equal '0' ([string](Get-Value $forkSaved 'next_offset' 'missing')) 'next_offset must stay unchanged'
    Assert-Equal ([string]$fork.Total) ([string](Get-Value $forkSaved 'total_bytes' 'missing')) 'total_bytes must stay unchanged'
    Assert-True ([IO.File]::Exists($fork.Bin)) 'payload must stay queued after a failed upload'
    Assert-True ([IO.File]::Exists($fork.Manifest)) 'manifest must stay queued after a failed upload'
    Assert-True ($forkJson -notmatch 'https?://|EVOLVMEM_SYNTHETIC_UNUSED_TOKEN') 'manifest must not persist URLs or credentials'

    # 2. Progress is retained and clears the stale failure; an unclassified
    # failure keeps a bounded generic code; deletion needs a terminal ack.
    $script:SyntheticRpcResults.Enqueue((New-McpToolResult `
        '{"authenticated_user":"synthetic-user","status":"receiving","next_offset":262144}' $false))
    [void](Invoke-UploadQueue $config)
    $forkSaved = Read-SyntheticJson $fork.Manifest
    Assert-Equal '262144' ([string](Get-Value $forkSaved 'next_offset' 'missing')) 'acknowledged offset must persist'
    Assert-Equal 'upload_failed' (Get-Value $forkSaved 'last_error' '') 'unclassified failure must use a bounded generic code'
    Assert-True ([IO.File]::Exists($fork.Bin)) 'payload must remain until terminal acknowledgement'
    $script:SyntheticRpcResults.Enqueue((New-McpToolResult `
        '{"authenticated_user":"synthetic-user","status":"receiving","next_offset":300000}' $false))
    [void](Invoke-UploadQueue $config)
    $forkSaved = Read-SyntheticJson $fork.Manifest
    Assert-Equal '' (Get-Value $forkSaved 'last_error' '') 'acknowledged progress must clear the stale failure code'
    Assert-Equal '300000' ([string](Get-Value $forkSaved 'next_offset' 'missing')) 'final progress offset must persist'
    Assert-True ([IO.File]::Exists($fork.Bin)) 'payload must remain until terminal acknowledgement'
    # Synthetic bookkeeping: retire the progressed version to isolate later cases.
    Remove-Item -Force $fork.Bin, $fork.Manifest
    $terminal = Add-SyntheticVersion $env:EVOLVMEM_CLIENT_HOME 'version-terminal' 'session-terminal' `
        '{"type":"assistant","text":"synthetic-complete"}'
    $script:SyntheticRpcResults.Enqueue((New-McpToolResult `
        ('{"authenticated_user":"synthetic-user","status":"archived","next_offset":' + $terminal.Total +
         ',"archive_id":"synthetic-archive","source_sha256":"' + $terminal.Sha + '","extraction_status":"pending"}') $false))
    [void](Invoke-UploadQueue $config)
    Assert-True (-not [IO.File]::Exists($terminal.Bin)) 'payload may be deleted only after terminal acknowledgement'
    Assert-True (-not [IO.File]::Exists($terminal.Manifest)) 'manifest may be deleted only after terminal acknowledgement'
    Assert-True ([IO.File]::Exists([IO.Path]::Combine($env:EVOLVMEM_CLIENT_HOME, 'archive-status', $terminal.Sha + '.json'))) `
        'terminal acknowledgement must write an archive receipt'

    # 3. Unrecognized server text must collapse into a bounded generic code.
    $unknown = Add-SyntheticVersion $env:EVOLVMEM_CLIENT_HOME 'version-unknown' 'session-unknown' 'synthetic-unknown'
    $script:SyntheticRpcResults.Enqueue((New-McpToolResult '{"error":"internal detail https://example.invalid/x?token=leak"}' $true))
    [void](Invoke-UploadQueue $config)
    $unknownJson = [IO.File]::ReadAllText($unknown.Manifest, $utf8)
    $unknownSaved = $unknownJson | ConvertFrom-Json
    Assert-Equal 'server_error' (Get-Value $unknownSaved 'last_error' '') 'unrecognized server text must not be stored verbatim'
    Assert-True ($unknownJson -notmatch 'internal detail|example\.invalid|leak') 'server body text must never reach the manifest'

    # 4. A success response with a missing or mismatched identity is still refused.
    $missingCode = '<unexpected-success>'
    try { [void](Convert-McpToolResult $config (New-McpToolResult '{"status":"receiving","next_offset":0}' $false)) }
    catch { $missingCode = Get-ClassifiedCode $_ }
    Assert-True ($missingCode -cne '<unexpected-success>') 'success without authenticated_user must be rejected'
    $malloryCode = '<unexpected-success>'
    try { [void](Convert-McpToolResult $config (New-McpToolResult '{"authenticated_user":"mallory","status":"receiving"}' $false)) }
    catch { $malloryCode = Get-ClassifiedCode $_ }
    Assert-True ($malloryCode -cne '<unexpected-success>') 'mismatched authenticated_user must be rejected'

    # Synthetic bookkeeping: hide the other queued manifest so this case is isolated.
    $unknownStash = $unknown.Manifest + '.stash'
    Move-Item $unknown.Manifest $unknownStash
    try {
        $identity = Add-SyntheticVersion $env:EVOLVMEM_CLIENT_HOME 'version-identity' 'session-identity' 'synthetic-identity'
        $script:SyntheticRpcResults.Enqueue((New-McpToolResult '{"authenticated_user":"mallory","status":"receiving","next_offset":0}' $false))
        [void](Invoke-UploadQueue $config)
        $identitySaved = Read-SyntheticJson $identity.Manifest
        Assert-Equal 'identity_mismatch' (Get-Value $identitySaved 'last_error' '') 'identity refusal must persist a bounded stable code'
        Assert-True ([IO.File]::Exists($identity.Bin)) 'version must stay queued when identity is refused'
    }
    finally { Move-Item $unknownStash $unknown.Manifest }

    # 5. Status reports bounded failure counts and codes, never bodies.
    $local = Get-LocalStatus $config
    Assert-Equal '2' ([string](Get-Field $local 'pending_failed_versions' 'missing')) 'status must report the pending failure count'
    $codes = @(Get-Field $local 'pending_error_codes' @())
    Assert-True ($codes -contains 'identity_mismatch' -and $codes -contains 'server_error') `
        ('status must report the pending stable error codes | actual=[' + ($codes -join ',') + ']')
    $localJson = $local | ConvertTo-Json -Depth 6 -Compress
    Assert-True ($localJson -notmatch 'internal detail|example\.invalid|leak|EVOLVMEM_SYNTHETIC_UNUSED_TOKEN') `
        'status must not expose error bodies or credentials'
}
finally {
    $env:EVOLVMEM_CLIENT_HOME = $previousClientHome
    Remove-Item -Recurse -Force $testRoot -ErrorAction SilentlyContinue
}

if ($failures.Count -gt 0) {
    [ordered]@{
        passed = $false; powershell = $PSVersionTable.PSVersion.ToString()
        failure_count = $failures.Count; failures = $failures.ToArray()
    } | ConvertTo-Json -Depth 5 -Compress
    exit 1
}
[ordered]@{ passed = $true; powershell = $PSVersionTable.PSVersion.ToString(); checks = 5 } |
    ConvertTo-Json -Compress

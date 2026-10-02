[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$ClientPath)

# Synthetic Windows PowerShell 5.1 test for local source ordering and current
# snapshot confirmation. It dot-sources the real client, replaces only DPAPI and
# the MCP transport, and drives synthetic transcripts inside a temporary client
# home. It never reads the real EvolvMem client home, transcripts, queue payloads
# or credentials, and it never contacts a real server.

$ErrorActionPreference = 'Stop'
$utf8 = New-Object Text.UTF8Encoding($false)
$failures = New-Object Collections.Generic.List[string]
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('evolvmem-version-order-test-' + [Guid]::NewGuid().ToString('N'))
$previousClientHome = $env:EVOLVMEM_CLIENT_HOME

function Assert-True($Condition, [string]$Message) {
    if (-not $Condition) { $script:failures.Add($Message) }
}
function Assert-Equal($Expected, $Actual, [string]$Message) {
    if ([string]$Expected -cne [string]$Actual) {
        $script:failures.Add($Message + ' | expected=[' + [string]$Expected + '] actual=[' + [string]$Actual + ']')
    }
}
function Assert-Order($Value, [string]$Message) {
    # source_order must stay a positive integer literal through JSON.
    if ($null -eq $Value) { $script:failures.Add($Message + ' | missing'); return }
    if ($Value -is [double] -or $Value -is [decimal] -or $Value -is [single]) {
        $script:failures.Add($Message + ' | became a floating point value: ' + [string]$Value); return
    }
    if ($Value -isnot [ValueType]) { $script:failures.Add($Message + ' | not numeric'); return }
    if ([int64]$Value -le 0) { $script:failures.Add($Message + ' | not positive: ' + [string]$Value) }
}
function Get-Field($Object, [string]$Name, $Default = $null) {
    if ($null -eq $Object) { return $Default }
    if ($Object -is [Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
        return $Default
    }
    return Get-Value $Object $Name $Default
}
function Read-Json([string]$Path) {
    return ([IO.File]::ReadAllText($Path, $script:utf8) | ConvertFrom-Json)
}
function Write-Utf8([string]$Path, [string]$Text) {
    [IO.File]::WriteAllText($Path, $Text, $script:utf8)
}
function Get-NowMs {
    $epoch = New-Object DateTime(1970, 1, 1, 0, 0, 0, [DateTimeKind]::Utc)
    return [int64][Math]::Floor(([DateTime]::UtcNow - $epoch).TotalMilliseconds)
}
function Convert-ToRecord($Object) {
    $copy = [ordered]@{}
    if ($null -ne $Object) {
        foreach ($property in $Object.PSObject.Properties) { $copy[$property.Name] = $property.Value }
    }
    return $copy
}

function Invoke-Capture([string]$SessionId, [string]$TranscriptPath, [string]$Workspace) {
    $event = [pscustomobject]@{ session_id = $SessionId; transcript_path = $TranscriptPath; cwd = $Workspace }
    return [bool](Invoke-WithClientLock { Capture-Transcript $Config $event } 5000 (Get-SessionLockScope $SessionId))
}
function Invoke-UploadRound {
    return [void](Invoke-WithClientLock { Invoke-UploadQueue $Config } 100 'upload')
}
function Get-SessionManifests([string]$SessionId) {
    $queue = Join-Path $env:EVOLVMEM_CLIENT_HOME 'queue'
    if (-not [IO.Directory]::Exists($queue)) { return @() }
    $result = New-Object Collections.Generic.List[object]
    foreach ($path in @([IO.Directory]::GetFiles($queue, '*.json'))) {
        try { $manifest = Read-Json $path } catch { continue }
        if ([string](Get-Field $manifest 'session_id' '') -cne $SessionId) { continue }
        $result.Add([pscustomobject]@{
            Path = $path; Manifest = $manifest; Text = [IO.File]::ReadAllText($path, $script:utf8)
        })
    }
    return @($result.ToArray())
}
function Get-ManifestForSha([string]$SessionId, [string]$Sha) {
    $matches = @(Get-SessionManifests $SessionId | Where-Object {
        [string](Get-Field $_.Manifest 'sha256' '') -ceq $Sha
    })
    if ($matches.Count -lt 1) { return $null }
    return $matches[0]
}
function Get-SessionRecord([string]$SessionId) { return Read-Json (Get-SessionPath $SessionId) }
function Get-ShaOf([string]$Text) { return Get-Sha256Hex ($script:utf8.GetBytes($Text)) }
function Add-QueueVersion([string]$SessionId, [string]$Content, [string]$CreatedUtc, [string]$Stem) {
    $queue = Join-Path $env:EVOLVMEM_CLIENT_HOME 'queue'
    [void][IO.Directory]::CreateDirectory($queue)
    $bytes = $script:utf8.GetBytes($Content)
    $sha = Get-Sha256Hex $bytes
    $binPath = Join-Path $queue ($Stem + '.bin')
    [IO.File]::WriteAllBytes($binPath, $bytes)
    $manifest = [ordered]@{
        version = 1; session_id = $SessionId; project = 'synthetic'
        sha256 = $sha; total_bytes = $bytes.Length; next_offset = 0; extract = $true
    }
    if ($CreatedUtc) { $manifest['created_utc'] = $CreatedUtc }
    $manifestPath = Join-Path $queue ($Stem + '.json')
    Write-Utf8 $manifestPath ($manifest | ConvertTo-Json -Depth 5 -Compress)
    return [pscustomobject]@{ Sha = $sha; Bytes = $bytes.Length; Bin = $binPath; Manifest = $manifestPath }
}
function Set-LegacySession([string]$SessionId, [string]$TranscriptPath, [string]$Workspace, [string]$CacheContent, [bool]$WriteCache = $true) {
    # Legacy registration shape: no source_order and no current_sha256.
    $sessionDir = Join-Path $env:EVOLVMEM_CLIENT_HOME 'sessions'
    [void][IO.Directory]::CreateDirectory($sessionDir)
    $cacheName = (Get-TextSha256 $SessionId) + '.bin'
    $cacheDir = Join-Path $env:EVOLVMEM_CLIENT_HOME 'session-cache'
    [void][IO.Directory]::CreateDirectory($cacheDir)
    if ($WriteCache) { [IO.File]::WriteAllBytes((Join-Path $cacheDir $cacheName), $script:utf8.GetBytes($CacheContent)) }
    $record = [ordered]@{
        version = 1; session_id = $SessionId; transcript_path = $TranscriptPath
        workspace_path = $Workspace; project = 'synthetic'
        source_bytes = $script:utf8.GetBytes($CacheContent).Length
        source_last_write_utc = [IO.File]::GetLastWriteTimeUtc($TranscriptPath).ToString('o')
        cache_file = $cacheName; last_seen_utc = [DateTime]::UtcNow.ToString('o')
    }
    Write-Utf8 (Get-SessionPath $SessionId) ($record | ConvertTo-Json -Depth 5 -Compress)
}

try {
    [void][IO.Directory]::CreateDirectory($testRoot)
    $env:EVOLVMEM_CLIENT_HOME = Join-Path $testRoot 'client'
    [void][IO.Directory]::CreateDirectory($env:EVOLVMEM_CLIENT_HOME)
    # No codex_root and no capture_since_utc: loading the client in worker mode
    # must not discover or read any real user transcript.
    $configSeed = [ordered]@{
        version = 1; url = 'http://127.0.0.1:1/mcp'; token_env_var = 'EVOLVMEM_SYNTHETIC_UNUSED_TOKEN'
        expected_user = 'synthetic-user'; device_id = 'synthetic-device'; projects = @{}
    }
    Write-Utf8 (Join-Path $env:EVOLVMEM_CLIENT_HOME 'config.json') ($configSeed | ConvertTo-Json -Depth 5)

    # Load the real client without stdin, network or queue access.
    . $ClientPath -Action worker

    # Replace only DPAPI and the MCP boundary; ordering, persistence, receipts
    # and retry stay on the real implementation. The stubs must be installed
    # after the dot-source so they are not overwritten by the client.
    function Protect-Bytes([byte[]]$Bytes) { return ,$Bytes }
    function Unprotect-Bytes([byte[]]$Bytes) { return ,$Bytes }
    $script:UploadCalls = New-Object Collections.Generic.List[object]
    $script:Offline = $false
    # Minimal fixture of the documented service contract: a version's
    # source_order is fixed during retries; a new observation of an old sha
    # can raise its order above the current head. An empty head is anchored on first
    # contact with the version protocol, the head only moves on an explicit
    # current declaration with a strictly greater order, and every terminal
    # receipt carries archive_id, current and the stored source_order. The
    # fixture never stores transcript text.
    $script:VersionOrders = @{}   # '<session>|<sha>' -> latest observation order
    $script:SessionHeads = @{}    # '<session>' -> @{ Sha; Order }
    $script:ForceReceipt = $null  # optional field override for archived receipts
    function Invoke-McpTool($Config, [string]$Name, $Arguments, [string]$RpcId = '') {
        if ($Name -ne 'session_archive_upload') { throw ('Unexpected synthetic MCP call: ' + $Name) }
        $copy = [ordered]@{}
        foreach ($key in @($Arguments.Keys)) { $copy[$key] = $Arguments[$key] }
        $script:UploadCalls.Add([pscustomobject]@{ Arguments = $copy; RpcId = $RpcId })
        if ($script:Offline) { throw 'synthetic offline transport' }
        $sessionId = [string]$Arguments.session_id
        $sha = [string]$Arguments.sha256
        $order = if ($Arguments.Contains('source_order')) { [int64]$Arguments.source_order } else { [int64]0 }
        $declared = if ($Arguments.Contains('current_sha256')) { [string]$Arguments.current_sha256 } else { '' }
        $versionKey = $sessionId + '|' + $sha
        if ($order -gt 0 -and $script:VersionOrders.ContainsKey($versionKey) -and
            [int64]$script:VersionOrders[$versionKey] -ne $order) {
            $floor = if ($script:SessionHeads.ContainsKey($sessionId)) { [int64]$script:SessionHeads[$sessionId].Order } else { [int64]0 }
            if ($declared -cne $sha -or $order -le [int64]$script:VersionOrders[$versionKey] -or $order -le $floor) {
                throw (New-ClientError 'upload_metadata_conflict' 'synthetic metadata conflict')
            }
        }
        $bytes = [Convert]::FromBase64String([string]$Arguments.content_b64)
        $next = [int64]$Arguments.offset + $bytes.Length
        if ($next -lt [int64]$Arguments.total_bytes) {
            return [pscustomobject]@{ authenticated_user = 'synthetic'; status = 'receiving'; next_offset = $next }
        }
        $alreadyStored = $script:VersionOrders.ContainsKey($versionKey)
        if ($order -gt 0) {
            $script:VersionOrders[$versionKey] = [int64]$order
        }
        $storedOrder = if ($order -gt 0) { [int64]$order } else { $null }
        if ($order -gt 0 -and -not $script:SessionHeads.ContainsKey($sessionId)) {
            # First contact with the version protocol anchors an empty head, so a
            # history-only upload can never become current by accident.
            $script:SessionHeads[$sessionId] = @{ Sha = ''; Order = [int64]0 }
        }
        $isCurrent = $false
        if ($order -gt 0) {
            $head = $script:SessionHeads[$sessionId]
            $knownOrder = [int64]$head['Order']
            $sameVersion = [string]$head['Sha'] -ceq $sha
            if (-not $sameVersion -and $knownOrder -gt 0 -and $order -eq $knownOrder) {
                throw (New-ClientError 'upload_metadata_conflict' 'synthetic metadata conflict')
            }
            if ($sameVersion) { $isCurrent = $true }
            elseif ($declared -ceq $sha -and ($knownOrder -le 0 -or $order -gt $knownOrder)) { $isCurrent = $true }
            if ($isCurrent) {
                $head['Sha'] = $sha
                if ($order -gt [int64]$head['Order']) { $head['Order'] = [int64]$order }
            }
        }
        $receipt = [ordered]@{
            authenticated_user = 'synthetic'; status = 'archived'; next_offset = [int64]$Arguments.total_bytes
            archive_id = 'synthetic-archive'; source_sha256 = $sha
            source_order = $storedOrder; current = $isCurrent; extraction_status = 'pending'
        }
        if ($null -ne $script:ForceReceipt) {
            foreach ($key in @($script:ForceReceipt.Keys)) { $receipt[$key] = $script:ForceReceipt[$key] }
        }
        return [pscustomobject]$receipt
    }
    function Clear-UploadCalls { $script:UploadCalls.Clear() }
    function Get-UploadCalls { return @($script:UploadCalls.ToArray()) }
    function Get-SessionUploadCalls([string]$SessionId) {
        return @(Get-UploadCalls | Where-Object { [string](Get-Field $_.Arguments 'session_id' '') -ceq $SessionId })
    }
    function Test-Argument($Call, [string]$Name) { return $Call.Arguments.Contains($Name) }
    $Config = Get-Config

    # ------------------------------------------------------------------
    # 1. A new session advances source_order; a repeated identical snapshot
    #    keeps the manifest's fixed order.
    # ------------------------------------------------------------------
    $transcriptA = Join-Path $testRoot 'case-a.jsonl'
    $lineA1 = '{"type":"event_msg","payload":{"message":"alpha"}}' + "`n"
    $lineA2 = '{"type":"event_msg","payload":{"message":"beta"}}' + "`n"
    Write-Utf8 $transcriptA $lineA1
    Assert-True (Invoke-Capture 'session-order-a' $transcriptA $testRoot) 'A: first capture must create a version.'
    $manifestsA = @(Get-SessionManifests 'session-order-a')
    Assert-Equal 1 $manifestsA.Count 'A: one capture must create exactly one manifest.'
    if ($manifestsA.Count -ge 1) {
        $shaA1 = [string](Get-Field $manifestsA[0].Manifest 'sha256' '')
        Assert-Order (Get-Field $manifestsA[0].Manifest 'source_order' $null) 'A: a new capture must persist an integer source_order.'
        $orderA1 = [int64](Get-Field $manifestsA[0].Manifest 'source_order' 0)
        Assert-True ($manifestsA[0].Text -match '"source_order":\d+') 'A: source_order must be an integer literal on disk.'
        $sessionA = Get-SessionRecord 'session-order-a'
        Assert-Equal $shaA1 (Get-Field $sessionA 'current_sha256' '') 'A: the registration must persist the captured current sha.'
        Assert-Equal $orderA1 (Get-Field $sessionA 'source_order' 0) 'A: the registration must persist the current order.'
        Assert-True (-not [string](Get-Field $sessionA 'anchor_error' '')) 'A: a fresh capture must not record a migration diagnostic.'

        Write-Utf8 $transcriptA ($lineA1 + $lineA2)
        Assert-True (Invoke-Capture 'session-order-a' $transcriptA $testRoot) 'A: an appended snapshot must create a version.'
        $manifestsA = @(Get-SessionManifests 'session-order-a')
        Assert-Equal 2 $manifestsA.Count 'A: an append must add one more manifest.'
        $appendedA = @($manifestsA | Where-Object { [string](Get-Field $_.Manifest 'sha256' '') -cne $shaA1 })
        Assert-Equal 1 $appendedA.Count 'A: the appended manifest must have a new sha.'
        $orderA2 = [int64](Get-Field $appendedA[0].Manifest 'source_order' 0)
        Assert-True ($orderA2 -gt $orderA1) 'A: the appended snapshot must receive a strictly greater order.'
        Assert-Equal $orderA2 ([int64](Get-Field (Get-SessionRecord 'session-order-a') 'source_order' 0)) 'A: the registration must follow the newest order.'

        # Rewrite identical bytes with a fresh write time: same sha, no renumber.
        Write-Utf8 $transcriptA ($lineA1 + $lineA2)
        [IO.File]::SetLastWriteTimeUtc($transcriptA, [DateTime]::UtcNow.AddSeconds(3))
        Assert-True (-not (Invoke-Capture 'session-order-a' $transcriptA $testRoot)) 'A: unchanged content must not create another version.'
        Assert-Equal 2 (@(Get-SessionManifests 'session-order-a').Count) 'A: a repeated snapshot must not duplicate the manifest.'
        $repeatedA = Get-ManifestForSha 'session-order-a' $shaA1
        Assert-True ($null -ne $repeatedA) 'A: the first version must still be queued.'
        if ($null -ne $repeatedA) {
            Assert-Equal $orderA1 ([int64](Get-Field $repeatedA.Manifest 'source_order' 0)) 'A: an older version must keep its fixed order.'
        }
        $repeatedCurrentA = Get-ManifestForSha 'session-order-a' ([string](Get-Field $appendedA[0].Manifest 'sha256' ''))
        Assert-True ($null -ne $repeatedCurrentA) 'A: the repeated current version must still be queued.'
        if ($null -ne $repeatedCurrentA) {
            Assert-Equal $orderA2 ([int64](Get-Field $repeatedCurrentA.Manifest 'source_order' 0)) 'A: a repeated sha must keep its fixed order.'
        }
    }

    # ------------------------------------------------------------------
    # 2. A rewritten, shorter snapshot receives a new order and becomes the
    #    current sha; only it may declare current_sha256.
    # ------------------------------------------------------------------
    $transcriptB = Join-Path $testRoot 'case-b.jsonl'
    $longB = ('{"type":"user","payload":"' + ('L' * 4096) + '"}' + "`n") + ('{"type":"agent","payload":"done"}' + "`n")
    $shortB = '{"type":"user","payload":"rewritten short"}' + "`n"
    Write-Utf8 $transcriptB $longB
    Assert-True (Invoke-Capture 'session-rewrite-b' $transcriptB $testRoot) 'B: the long snapshot must be captured.'
    $longManifestB = @(Get-SessionManifests 'session-rewrite-b')[0]
    $longShaB = [string](Get-Field $longManifestB.Manifest 'sha256' '')
    $longOrderB = [int64](Get-Field $longManifestB.Manifest 'source_order' 0)
    Write-Utf8 $transcriptB $shortB
    Assert-True (Invoke-Capture 'session-rewrite-b' $transcriptB $testRoot) 'B: the shorter rewrite must be captured.'
    $shortShaB = Get-ShaOf $shortB
    $shortManifestB = Get-ManifestForSha 'session-rewrite-b' $shortShaB
    Assert-True ($null -ne $shortManifestB) 'B: the rewritten shorter snapshot needs its own manifest.'
    if ($null -ne $shortManifestB) {
        $shortOrderB = [int64](Get-Field $shortManifestB.Manifest 'source_order' 0)
        Assert-True ($shortOrderB -gt $longOrderB) 'B: a smaller snapshot must still receive a greater order.'
        Assert-True ([int64](Get-Field $shortManifestB.Manifest 'total_bytes' 0) -lt [int64](Get-Field $longManifestB.Manifest 'total_bytes' 0)) 'B: the rewritten snapshot must actually be shorter.'
        $sessionB = Get-SessionRecord 'session-rewrite-b'
        Assert-Equal $shortShaB (Get-Field $sessionB 'current_sha256' '') 'B: the registration must follow the rewrite.'
        Assert-Equal $shortOrderB ([int64](Get-Field $sessionB 'source_order' 0)) 'B: the registration must follow the rewrite order.'
        Clear-UploadCalls
        $script:Offline = $false
        Invoke-UploadRound
        $callsB = @(Get-SessionUploadCalls 'session-rewrite-b')
        Assert-Equal 2 $callsB.Count 'B: both versions must terminate.'
        if ($callsB.Count -ge 2) {
            Assert-Equal $shortShaB ([string](Get-Field $callsB[0].Arguments 'sha256' '')) 'B: the current rewrite must upload first.'
            Assert-Equal $shortShaB ([string](Get-Field $callsB[0].Arguments 'current_sha256' '')) 'B: the current rewrite must declare current_sha256.'
            Assert-Equal $shortOrderB ([int64](Get-Field $callsB[0].Arguments 'source_order' 0)) 'B: the current rewrite must carry its own order.'
            $oldCallB = @($callsB | Where-Object { [string](Get-Field $_.Arguments 'sha256' '') -ceq $longShaB })
            Assert-Equal 1 $oldCallB.Count 'B: the superseded long version must still archive.'
            if ($oldCallB.Count -ge 1) {
                Assert-True (-not (Test-Argument $oldCallB[0] 'current_sha256')) 'B: a superseded version must not declare current_sha256.'
                Assert-Equal $longOrderB ([int64](Get-Field $oldCallB[0].Arguments 'source_order' 0)) 'B: a superseded version keeps its fixed order.'
            }
        }
    }

    # ------------------------------------------------------------------
    # 3. Legacy queue plus an already-acknowledged current cache: build the
    #    migration anchor first, keep legacy payloads, and never let a legacy
    #    fork declare current.
    # ------------------------------------------------------------------
    $transcriptC = Join-Path $testRoot 'case-c.jsonl'
    $currentC = ('{"type":"user","payload":"current snapshot"}' + "`n") + ('{"type":"agent","payload":"tail"}' + "`n")
    Write-Utf8 $transcriptC $currentC
    $legacyOneC = Add-QueueVersion 'session-migrate-c' '{"type":"user","payload":"fork one"}' '2026-09-01T08:00:00.0000000Z' 'legacy-c-one'
    $legacyTwoC = Add-QueueVersion 'session-migrate-c' '{"type":"agent","payload":"fork two"}' '2026-09-02T09:30:00.0000000Z' 'legacy-c-two'
    Set-LegacySession 'session-migrate-c' $transcriptC $testRoot $currentC
    $currentShaC = Get-ShaOf $currentC

    $script:Offline = $true
    Clear-UploadCalls
    Invoke-UploadRound
    $offlineCallsC = @(Get-SessionUploadCalls 'session-migrate-c')
    Assert-Equal 1 $offlineCallsC.Count 'C: an offline migration must attempt the anchor before the old queue.'
    if ($offlineCallsC.Count -ge 1) {
        Assert-Equal $currentShaC ([string](Get-Field $offlineCallsC[0].Arguments 'sha256' '')) 'C: the migration anchor must be the local current cache.'
        Assert-Equal $currentShaC ([string](Get-Field $offlineCallsC[0].Arguments 'current_sha256' '')) 'C: the migration anchor must declare current_sha256.'
    }
    Assert-True ([IO.File]::Exists($legacyOneC.Bin) -and [IO.File]::Exists($legacyOneC.Manifest)) 'C: an offline anchor must keep the legacy payload.'
    Assert-True ([IO.File]::Exists($legacyTwoC.Bin) -and [IO.File]::Exists($legacyTwoC.Manifest)) 'C: an offline anchor must keep every legacy payload.'
    $sessionC = Get-SessionRecord 'session-migrate-c'
    Assert-Equal $currentShaC (Get-Field $sessionC 'current_sha256' '') 'C: migration must persist the local current sha.'
    $anchorOrderC = [int64](Get-Field $sessionC 'source_order' 0)
    Assert-Order $anchorOrderC 'C: migration must persist the anchor order.'
    $legacyOneSaved = Read-Json $legacyOneC.Manifest
    $legacyTwoSaved = Read-Json $legacyTwoC.Manifest
    Assert-Order (Get-Field $legacyOneSaved 'source_order' $null) 'C: a legacy manifest must persist a fixed order.'
    Assert-Order (Get-Field $legacyTwoSaved 'source_order' $null) 'C: every legacy manifest must persist a fixed order.'
    $legacyOneOrder = [int64](Get-Field $legacyOneSaved 'source_order' 0)
    $legacyTwoOrder = [int64](Get-Field $legacyTwoSaved 'source_order' 0)
    Assert-True ($legacyOneOrder -lt $anchorOrderC -and $legacyTwoOrder -lt $anchorOrderC) 'C: the anchor order must be strictly greater than every legacy order.'
    Assert-True ($legacyOneOrder -ne $legacyTwoOrder) 'C: distinct legacy versions must not share one order.'
    $anchorManifestC = Get-ManifestForSha 'session-migrate-c' $currentShaC
    Assert-True ($null -ne $anchorManifestC) 'C: migration must create an idempotent anchor manifest.'
    if ($null -ne $anchorManifestC) {
        $anchorBinC = $anchorManifestC.Path -replace '\.json$', '.bin'
        Assert-True ([IO.File]::Exists($anchorBinC)) 'C: the anchor payload must be queued.'
        Assert-Equal $script:utf8.GetBytes($currentC).Length ([int64](Get-Field $anchorManifestC.Manifest 'total_bytes' 0)) 'C: the anchor must carry the current cache bytes.'
    }

    $script:Offline = $false
    Clear-UploadCalls
    Invoke-UploadRound
    $callsC = @(Get-SessionUploadCalls 'session-migrate-c')
    Assert-Equal 3 $callsC.Count 'C: the anchor and both legacy versions must terminate.'
    if ($callsC.Count -ge 1) {
        Assert-Equal $currentShaC ([string](Get-Field $callsC[0].Arguments 'sha256' '')) 'C: the anchor must be uploaded before the legacy queue.'
        foreach ($legacyCallC in @($callsC | Select-Object -Skip 1)) {
            Assert-True (-not (Test-Argument $legacyCallC 'current_sha256')) 'C: an archived legacy version must not declare current.'
            $order = [int64](Get-Field $legacyCallC.Arguments 'source_order' 0)
            Assert-Order $order 'C: a legacy upload must carry its persisted order.'
            Assert-True ($order -lt $anchorOrderC) 'C: a legacy upload order must stay below the anchor.'
        }
    }
    Assert-Equal $currentShaC ([string](Get-Field (Get-SessionRecord 'session-migrate-c') 'anchor_confirmed_sha256' '')) 'C: a terminal anchor receipt must confirm the migration.'
    Clear-UploadCalls
    Invoke-UploadRound
    Assert-Equal 0 (@(Get-SessionUploadCalls 'session-migrate-c').Count) 'C: a confirmed anchor must not be re-uploaded every round.'

    # ------------------------------------------------------------------
    # 4. An offline retry keeps the same order, hash and request identity.
    # ------------------------------------------------------------------
    $transcriptD = Join-Path $testRoot 'case-d.jsonl'
    $contentD = '{"type":"user","payload":"retry stable"}' + "`n"
    Write-Utf8 $transcriptD $contentD
    Assert-True (Invoke-Capture 'session-retry-d' $transcriptD $testRoot) 'D: the snapshot must be captured.'
    $manifestD = @(Get-SessionManifests 'session-retry-d')[0]
    $shaD = [string](Get-Field $manifestD.Manifest 'sha256' '')
    $orderD = [int64](Get-Field $manifestD.Manifest 'source_order' 0)
    Assert-Order $orderD 'D: the captured version must persist an order.'
    $script:Offline = $true
    Clear-UploadCalls
    Invoke-UploadRound
    $offlineCallsD = @(Get-SessionUploadCalls 'session-retry-d')
    Assert-Equal 1 $offlineCallsD.Count 'D: the offline attempt must reach the transport.'
    $offlineRequestD = if ($offlineCallsD.Count -ge 1) { [string]$offlineCallsD[0].RpcId } else { '' }
    $savedD = Read-Json $manifestD.Path
    Assert-Equal $shaD (Get-Field $savedD 'sha256' '') 'D: an offline retry must not change the hash.'
    Assert-Equal $orderD ([int64](Get-Field $savedD 'source_order' 0)) 'D: an offline retry must not renumber the order.'
    Assert-Equal 0 ([int64](Get-Field $savedD 'next_offset' 0)) 'D: an offline retry must not advance the offset.'
    Assert-True ([IO.File]::Exists($manifestD.Path)) 'D: an offline retry must keep the manifest queued.'
    Assert-Equal $shaD ([string](Get-Field (Get-SessionRecord 'session-retry-d') 'current_sha256' '')) 'D: an offline retry must not drop the local current sha.'
    $script:Offline = $false
    Clear-UploadCalls
    Invoke-UploadRound
    $onlineCallsD = @(Get-SessionUploadCalls 'session-retry-d')
    Assert-Equal 1 $onlineCallsD.Count 'D: the retry must terminate once online.'
    if ($onlineCallsD.Count -ge 1) {
        Assert-Equal $shaD ([string](Get-Field $onlineCallsD[0].Arguments 'sha256' '')) 'D: the retry must keep the same hash.'
        Assert-Equal $orderD ([int64](Get-Field $onlineCallsD[0].Arguments 'source_order' 0)) 'D: the retry must reuse the same order.'
        Assert-Equal $shaD ([string](Get-Field $onlineCallsD[0].Arguments 'current_sha256' '')) 'D: the retried current snapshot must still declare current_sha256.'
        Assert-Equal $offlineRequestD ([string]$onlineCallsD[0].RpcId) 'D: the retry must reuse the same request id.'
    }

    # ------------------------------------------------------------------
    # 5. A rolled-back clock still advances max(previous order + 1, now).
    # ------------------------------------------------------------------
    $transcriptE = Join-Path $testRoot 'case-e.jsonl'
    $contentE = '{"type":"user","payload":"clock base"}' + "`n"
    Write-Utf8 $transcriptE $contentE
    Assert-True (Invoke-Capture 'session-clock-e' $transcriptE $testRoot) 'E: the base snapshot must be captured.'
    $futureE = (Get-NowMs) + 31536000000
    $recordE = Convert-ToRecord (Get-SessionRecord 'session-clock-e')
    $recordE['source_order'] = [int64]$futureE
    $recordE['max_source_order'] = [int64]$futureE
    Write-Utf8 (Get-SessionPath 'session-clock-e') ($recordE | ConvertTo-Json -Depth 5 -Compress)
    Write-Utf8 $transcriptE ($contentE + '{"type":"agent","payload":"after rollback"}' + "`n")
    Assert-True (Invoke-Capture 'session-clock-e' $transcriptE $testRoot) 'E: the snapshot after the rollback must be captured.'
    $sessionE = Get-SessionRecord 'session-clock-e'
    Assert-Equal ($futureE + 1) ([int64](Get-Field $sessionE 'source_order' 0)) 'E: a rolled-back clock must still advance the previous order by one.'
    $newManifestE = Get-ManifestForSha 'session-clock-e' (Get-ShaOf ($contentE + '{"type":"agent","payload":"after rollback"}' + "`n"))
    Assert-True ($null -ne $newManifestE) 'E: the new snapshot needs its own manifest.'
    if ($null -ne $newManifestE) {
        Assert-Equal ($futureE + 1) ([int64](Get-Field $newManifestE.Manifest 'source_order' 0)) 'E: the manifest must carry the max+1 order.'
    }

    # ------------------------------------------------------------------
    # 6. A missing cache must never invent a current snapshot; the legacy
    #    versions stay queued as history with a stable diagnostic code.
    # ------------------------------------------------------------------
    $transcriptF = Join-Path $testRoot 'case-f.jsonl'
    $contentF = '{"type":"user","payload":"cache missing"}' + "`n"
    Write-Utf8 $transcriptF $contentF
    $legacyF = Add-QueueVersion 'session-nocache-f' '{"type":"user","payload":"legacy fork"}' '2026-09-03T10:00:00.0000000Z' 'legacy-f-one'
    Set-LegacySession 'session-nocache-f' $transcriptF $testRoot $contentF -WriteCache $false
    $script:Offline = $false
    Clear-UploadCalls
    Invoke-UploadRound
    $callsF = @(Get-SessionUploadCalls 'session-nocache-f')
    Assert-True ($callsF.Count -ge 1) 'F: an unanchored legacy version must still be attempted as history.'
    foreach ($callF in $callsF) {
        Assert-True (-not (Test-Argument $callF 'current_sha256')) 'F: a missing cache must never declare current.'
    }
    $sessionF = Get-SessionRecord 'session-nocache-f'
    Assert-True (-not [string](Get-Field $sessionF 'current_sha256' '')) 'F: a missing cache must not invent a current sha.'
    Assert-Equal 'current_snapshot_unavailable' ([string](Get-Field $sessionF 'anchor_error' '')) 'F: a missing cache must persist a stable diagnostic code.'
    $localF = Get-LocalStatus $Config
    Assert-True ([int](Get-Field $localF 'migration_unavailable_sessions' 0) -ge 1) 'F: local status must count sessions without a current anchor.'
    $codesF = @(Get-Field $localF 'migration_error_codes' @())
    Assert-True ($codesF -contains 'current_snapshot_unavailable') 'F: local status must expose the stable migration diagnostic code.'

    # ------------------------------------------------------------------
    # 7. Legacy manifests can be assigned a fixed order from created_utc and
    #    persist it; the same order is reused on the next retry.
    # ------------------------------------------------------------------
    $legacyG1 = Add-QueueVersion 'session-legacy-g' 'legacy g one' '2026-09-05T12:00:00.0000000Z' 'legacy-g-one'
    $legacyG2 = Add-QueueVersion 'session-legacy-g' 'legacy g two' '2026-09-05T12:00:00.0000000Z' 'legacy-g-two'
    $script:Offline = $true
    Clear-UploadCalls
    Invoke-UploadRound
    $savedG1 = Read-Json $legacyG1.Manifest
    $savedG2 = Read-Json $legacyG2.Manifest
    $orderG1 = [int64](Get-Field $savedG1 'source_order' 0)
    $orderG2 = [int64](Get-Field $savedG2 'source_order' 0)
    Assert-Order (Get-Field $savedG1 'source_order' $null) 'G: a valid created_utc must become a persisted integer order.'
    Assert-Order (Get-Field $savedG2 'source_order' $null) 'G: every valid created_utc must become a persisted integer order.'
    Assert-True ($orderG1 -ne $orderG2) 'G: equal created_utc values must still receive distinct orders.'
    $expectedG1 = 1788609600000
    Assert-True ([Math]::Abs($orderG1 - $expectedG1) -le 1000 -or [Math]::Abs($orderG2 - $expectedG1) -le 1000) 'G: the assigned order must derive from created_utc milliseconds.'
    $textG1 = [IO.File]::ReadAllText($legacyG1.Manifest, $utf8)
    Assert-True ($textG1 -match '"source_order":\d+') 'G: source_order must persist as an integer literal.'
    Assert-Equal $legacyG1.Sha (Get-Field $savedG1 'sha256' '') 'G: a legacy manifest must keep its hash.'
    Assert-Equal ([string]$legacyG1.Bytes) ([string](Get-Field $savedG1 'total_bytes' '')) 'G: a legacy manifest must keep its byte count.'
    Assert-Equal '0' ([string](Get-Field $savedG1 'next_offset' '')) 'G: a legacy manifest must keep its offset.'
    Assert-True ([bool](Get-Field $savedG1 'extract' $false)) 'G: a legacy manifest must keep its extract flag.'
    Assert-Equal 'synthetic' ([string](Get-Field $savedG1 'project' '')) 'G: a legacy manifest must keep its project.'
    $script:Offline = $false
    Clear-UploadCalls
    Invoke-UploadRound
    $reused = @{}
    foreach ($callG in @(Get-SessionUploadCalls 'session-legacy-g')) {
        $reused[[string](Get-Field $callG.Arguments 'sha256' '')] = [int64](Get-Field $callG.Arguments 'source_order' 0)
        Assert-True (-not (Test-Argument $callG 'current_sha256')) 'G: an unregistered legacy session must not declare current.'
    }
    Assert-Equal $orderG1 ([int64]$reused[$legacyG1.Sha]) 'G: the persisted order must be reused on retry.'
    Assert-Equal $orderG2 ([int64]$reused[$legacyG2.Sha]) 'G: every persisted order must be reused on retry.'

    # ------------------------------------------------------------------
    # 8. A terminal receipt that archived the local current snapshot as
    #    history (current=false), with another order, or without an archive id
    #    must keep the queue, refuse the anchor and never release older items.
    # ------------------------------------------------------------------
    function Invoke-RefusedAnchor([string]$SessionId, [string]$Tag, $Override, [string]$ExpectedCode) {
        $transcript = Join-Path $testRoot ('case-' + $Tag + '.jsonl')
        $current = '{"type":"user","payload":"current ' + $Tag + '"}' + "`n"
        Write-Utf8 $transcript $current
        Assert-True (Invoke-Capture $SessionId $transcript $testRoot) ($Tag + ': the current snapshot must be captured.')
        $older = Add-QueueVersion $SessionId ('{"type":"user","payload":"older ' + $Tag + '"}') '2026-09-01T08:00:00.0000000Z' ('older-' + $Tag)
        $script:ForceReceipt = $Override
        Clear-UploadCalls
        Invoke-UploadRound
        $script:ForceReceipt = $null
        $calls = @(Get-SessionUploadCalls $SessionId)
        Assert-Equal 1 $calls.Count ($Tag + ': only the current snapshot may be attempted while the anchor is unconfirmed.')
        if ($calls.Count -ge 1) {
            Assert-Equal (Get-ShaOf $current) ([string](Get-Field $calls[0].Arguments 'sha256' '')) ($Tag + ': the refused upload must be the local current snapshot.')
        }
        Assert-True ([IO.File]::Exists($older.Bin) -and [IO.File]::Exists($older.Manifest)) ($Tag + ': an unconfirmed anchor must keep the older queued version.')
        $currentManifest = Get-ManifestForSha $SessionId (Get-ShaOf $current)
        Assert-True ($null -ne $currentManifest) ($Tag + ': an unconfirmed anchor must keep the current payload queued.')
        if ($null -ne $currentManifest) {
            Assert-Equal $ExpectedCode ([string](Get-Field $currentManifest.Manifest 'last_error' '')) ($Tag + ': the retained anchor must carry the stable diagnostic code.')
        }
        $session = Get-SessionRecord $SessionId
        Assert-True (-not [string](Get-Field $session 'anchor_confirmed_sha256' '')) ($Tag + ': a non-confirming receipt must never confirm the anchor.')
        Assert-Equal (Get-ShaOf $current) ([string](Get-Field $session 'current_sha256' '')) ($Tag + ': the local current sha must survive the refusal.')
        $local = Get-LocalStatus $Config
        $codes = @(Get-Field $local 'pending_error_codes' @())
        Assert-True ($codes -contains $ExpectedCode) ($Tag + ': local status must expose the stable anchor diagnostic code.')
    }
    Invoke-RefusedAnchor 'session-history-h' 'h-history' @{ current = $false } 'anchor_not_current'
    Invoke-RefusedAnchor 'session-order-h' 'h-order' @{ current = $true; source_order = 987654321 } 'anchor_not_current'
    Invoke-RefusedAnchor 'session-archive-h' 'h-archive' @{ current = $true; archive_id = '' } 'anchor_not_current'

    # ------------------------------------------------------------------
    # 9. Rewinding to content that the service already archived (A -> B -> A)
    #    reuses its immutable archive with a strictly higher observation order.
    # ------------------------------------------------------------------
    $transcriptI = Join-Path $testRoot 'case-i.jsonl'
    $contentA = '{"type":"user","payload":"aba version A"}' + "`n"
    $contentB = '{"type":"assistant","payload":"aba version B"}' + "`n"
    Write-Utf8 $transcriptI $contentA
    Assert-True (Invoke-Capture 'session-aba-i' $transcriptI $testRoot) 'I: the first A version must be captured.'
    Clear-UploadCalls
    Invoke-UploadRound
    $firstA = @(Get-SessionUploadCalls 'session-aba-i')
    Assert-Equal 1 $firstA.Count 'I: the first A version must archive once.'
    $firstAOrder = if ($firstA.Count -ge 1) { [int64](Get-Field $firstA[0].Arguments 'source_order' 0) } else { [int64]0 }
    Write-Utf8 $transcriptI ($contentA + $contentB)
    Assert-True (Invoke-Capture 'session-aba-i' $transcriptI $testRoot) 'I: the appended B version must be captured.'
    Clear-UploadCalls
    Invoke-UploadRound
    Assert-Equal 1 (@(Get-SessionUploadCalls 'session-aba-i').Count) 'I: the appended B version must archive once.'
    Write-Utf8 $transcriptI $contentA
    Assert-True (Invoke-Capture 'session-aba-i' $transcriptI $testRoot) 'I: rewinding to A must be captured as the current snapshot.'
    $shaA = Get-ShaOf $contentA
    $sessionI = Get-SessionRecord 'session-aba-i'
    Assert-Equal $shaA ([string](Get-Field $sessionI 'current_sha256' '')) 'I: the rewound A version must be the local current sha.'
    Clear-UploadCalls
    Invoke-UploadRound
    $callsI = @(Get-SessionUploadCalls 'session-aba-i')
    Assert-Equal 1 $callsI.Count 'I: only the rewound A version may be attempted while its head is unconfirmed.'
    if ($callsI.Count -ge 1) {
        Assert-Equal $shaA ([string](Get-Field $callsI[0].Arguments 'sha256' '')) 'I: the rewound A version must be attempted first.'
        Assert-Equal $shaA ([string](Get-Field $callsI[0].Arguments 'current_sha256' '')) 'I: the rewound A version must declare current.'
        Assert-True ([int64](Get-Field $callsI[0].Arguments 'source_order' 0) -ne $firstAOrder) 'I: the rewound A version cannot reuse the fixed order of the archived A version.'
    }
    $manifestA = Get-ManifestForSha 'session-aba-i' $shaA
    Assert-True ($null -eq $manifestA) 'I: the confirmed recaptured version must leave the queue.'
    $sessionI = Get-SessionRecord 'session-aba-i'
    Assert-Equal $shaA ([string](Get-Field $sessionI 'anchor_confirmed_sha256' '')) 'I: the server must confirm the recaptured A.'
    Assert-Equal $shaA ([string]$script:SessionHeads['session-aba-i'].Sha) 'I: the remote current must be A.'

    # Revisit A while its unacknowledged manifest still exists. A real capture
    # must advance the order; a transport retry must keep that new order.
    $transcriptJ = Join-Path $testRoot 'case-j.jsonl'
    Write-Utf8 $transcriptJ $contentA
    [void](Invoke-Capture 'session-aba-j' $transcriptJ $testRoot)
    $oldOrderJ = [int64](Get-Field (Get-SessionRecord 'session-aba-j') 'source_order' 0)
    Write-Utf8 $transcriptJ ($contentA + $contentB)
    [void](Invoke-Capture 'session-aba-j' $transcriptJ $testRoot)
    $middleOrderJ = [int64](Get-Field (Get-SessionRecord 'session-aba-j') 'source_order' 0)
    Write-Utf8 $transcriptJ $contentA
    [void](Invoke-Capture 'session-aba-j' $transcriptJ $testRoot)
    $newOrderJ = [int64](Get-Field (Get-SessionRecord 'session-aba-j') 'source_order' 0)
    Assert-True ($newOrderJ -gt $middleOrderJ -and $middleOrderJ -gt $oldOrderJ) 'J: revisiting queued A must advance beyond B.'
    $script:Offline = $true
    Invoke-UploadRound
    $script:Offline = $false
    Assert-Equal $newOrderJ ([int64](Get-Field (Get-SessionRecord 'session-aba-j') 'source_order' 0)) 'J: network failure must keep the recapture order.'
    Invoke-UploadRound
    Assert-Equal (Get-ShaOf $contentA) ([string]$script:SessionHeads['session-aba-j'].Sha) 'J: late B must not overwrite recaptured A.'
    Assert-Equal 0 (@(Get-SessionManifests 'session-aba-j').Count) 'J: both immutable versions must be acknowledged.'

    # A parent hook must never register or capture its child's transcript.
    $transcriptK = Join-Path $testRoot 'case-k.jsonl'
    Write-Utf8 $transcriptK ('{"type":"session_meta","payload":{"id":"child-k","parent_thread_id":"parent-k","cwd":"C:\\synthetic"}}' + "`n")
    $mismatchCode = ''
    try { [void](Invoke-Capture 'parent-k' $transcriptK $testRoot) }
    catch { $mismatchCode = Get-ClientErrorCode $_ }
    Assert-Equal 'session_id_mismatch' $mismatchCode 'K: a child transcript cannot be captured as its parent.'
    Assert-True (-not [IO.File]::Exists((Get-SessionPath 'parent-k'))) 'K: mismatch must not create a parent registration.'
    Assert-Equal 0 (@(Get-SessionManifests 'parent-k').Count) 'K: mismatch must not create a misattributed queue item.'
    Assert-True (Invoke-Capture 'child-k' $transcriptK $testRoot) 'K: the same transcript must be accepted for its own session.'

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
[ordered]@{ passed = $true; powershell = $PSVersionTable.PSVersion.ToString(); checks = 11 } |
    ConvertTo-Json -Compress

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('session-start', 'prompt-submit', 'snapshot', 'worker', 'upload', 'status', 'self-test')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$script:MaxContextChars = 8000
$script:ConnectionMetadataReserveChars = 1600
# Explicit project mentions get their own bounded slice of the prompt context.
# The prompt path adds the project block before related experience, so a large
# experience block can never push the project details out of the budget.
$script:ProjectRecallMaxChars = 4000
$script:ChunkBytes = 262144
$script:RpcTimeoutSeconds = 3
$script:RunningOnWindows = $env:OS -eq 'Windows_NT'
$script:Utf8NoBom = New-Object Text.UTF8Encoding($false)
[Console]::InputEncoding = $script:Utf8NoBom
[Console]::OutputEncoding = $script:Utf8NoBom
$OutputEncoding = $script:Utf8NoBom
$script:ActionToken = ''
$script:IdentityValidated = $false
$script:PreflightStatus = $null

function Get-ClientHome {
    if ($env:EVOLVMEM_CLIENT_HOME) {
        return [IO.Path]::GetFullPath($env:EVOLVMEM_CLIENT_HOME)
    }
    if (-not $env:LOCALAPPDATA) {
        throw 'client home unavailable'
    }
    return [IO.Path]::Combine($env:LOCALAPPDATA, 'EvolvMem', 'Codex')
}

$script:ClientHome = Get-ClientHome
$script:ConfigPath = [IO.Path]::Combine($script:ClientHome, 'config.json')

function Ensure-Directory([string]$Path) {
    if (-not [IO.Directory]::Exists($Path)) {
        [void][IO.Directory]::CreateDirectory($Path)
    }
}

function Get-Value($Object, [string]$Name, $Default = $null) {
    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Read-JsonFile([string]$Path) {
    if (-not [IO.File]::Exists($Path)) { return $null }
    $text = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
    if (-not $text.Trim()) { return $null }
    return $text | ConvertFrom-Json
}

function Write-JsonAtomic([string]$Path, $Value) {
    Ensure-Directory ([IO.Path]::GetDirectoryName($Path))
    $temp = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    $json = $Value | ConvertTo-Json -Depth 20 -Compress
    [IO.File]::WriteAllText($temp, $json, (New-Object Text.UTF8Encoding($false)))
    try {
        if ([IO.File]::Exists($Path)) {
            $backup = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.replace.bak'
            try { [IO.File]::Replace($temp, $Path, $backup) }
            finally { if ([IO.File]::Exists($backup)) { [IO.File]::Delete($backup) } }
        }
        else { [IO.File]::Move($temp, $Path) }
    }
    finally {
        if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) }
    }
}

function Write-OutputJson($Value) {
    [Console]::Out.WriteLine(($Value | ConvertTo-Json -Depth 20 -Compress))
}

function Get-Sha256Hex([byte[]]$Bytes) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally { $sha.Dispose() }
}

function Get-TextSha256([string]$Text) {
    return Get-Sha256Hex ([Text.Encoding]::UTF8.GetBytes($Text))
}

function Invoke-WithClientLock(
    [scriptblock]$Body,
    [int]$TimeoutMilliseconds = 5000,
    [string]$Scope = 'client'
) {
    $name = 'EvolvMemCodex-' + (Get-TextSha256 ($script:ClientHome + '|' + $Scope)).Substring(0, 20)
    if ($script:RunningOnWindows) { $name = 'Local\' + $name }
    $mutex = New-Object Threading.Mutex($false, $name)
    $acquired = $false
    try {
        $acquired = $mutex.WaitOne($TimeoutMilliseconds)
        if (-not $acquired) { throw 'EvolvMem local queue is busy' }
        return & $Body
    }
    finally {
        if ($acquired) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

function Get-SessionLockScope([string]$SessionId) {
    return 'session-' + (Get-TextSha256 $SessionId).Substring(0, 20)
}

function Get-Config {
    $config = Read-JsonFile $script:ConfigPath
    if ($null -eq $config) { throw 'EvolvMem client is not configured' }
    foreach ($field in @('url', 'token_env_var', 'expected_user', 'device_id')) {
        if (-not [string](Get-Value $config $field '')) { throw 'EvolvMem client configuration is incomplete' }
    }
    return $config
}

function Get-UserToken($Config) {
    $name = [string](Get-Value $Config 'token_env_var' '')
    if ($script:RunningOnWindows) {
        $token = [Environment]::GetEnvironmentVariable($name, [EnvironmentVariableTarget]::User)
    }
    else {
        # Portable PowerShell protocol tests run on Linux. Production Windows
        # always reads the named CurrentUser environment variable above.
        $token = [Environment]::GetEnvironmentVariable($name, [EnvironmentVariableTarget]::Process)
    }
    if (-not $token) { throw 'EvolvMem credential is unavailable' }
    return $token
}

function Get-ActionToken($Config) {
    if (-not $script:ActionToken) { $script:ActionToken = Get-UserToken $Config }
    return $script:ActionToken
}

# Stable error codes the archive service documents for upload failures. Any
# other server text collapses into a bounded generic code, so receipts and
# manifests never persist remote messages, URLs or credentials.
$script:StableUploadErrorCodes = @(
    'transcript_fork', 'previous_archive_unavailable', 'upload_metadata_conflict'
    'invalid_transcript_hash', 'transcript_hash_mismatch', 'invalid_chunk', 'invalid_arguments'
    'archive_not_found', 'archive_unavailable', 'archive_payload_unavailable'
    'archive_project_unassigned', 'archive_project_conflict', 'project_not_registered'
    'upload_staging_unavailable', 'archive_encryption_unavailable'
    'invalid_transcript', 'session_id_mismatch', 'invalid_source_order', 'invalid_current_sha256'
    'invalid_source_order', 'invalid_current_sha256'
)

function New-ClientError([string]$Code, [string]$Message) {
    # Exception.Data survives the catch boundary without carrying server text.
    $exception = New-Object System.Exception $Message
    $exception.Data['EvolvMemErrorCode'] = $Code
    return $exception
}

function Get-ClientErrorCode($ErrorRecord, [string]$Default = 'upload_failed') {
    $exception = Get-Value $ErrorRecord 'Exception'
    foreach ($candidate in @($exception, (Get-Value $exception 'InnerException'))) {
        $data = Get-Value $candidate 'Data'
        if ($null -ne $data -and $data.Contains('EvolvMemErrorCode')) {
            return [string]$data['EvolvMemErrorCode']
        }
    }
    return $Default
}

function Get-StableErrorCode($Value) {
    $candidate = [string](Get-Value $Value 'error' '')
    if ($candidate -and $script:StableUploadErrorCodes -ccontains $candidate) { return $candidate }
    return 'server_error'
}

function Test-ArchiveCurrentConfirmation($Response, [int64]$Order, [string]$Sha) {
    # A terminal receipt only confirms the local current snapshot when the
    # service reports that this exact version and order became the session
    # head and that the payload is durably archived. A history archive
    # (current=false) or a missing/other order must never confirm an anchor.
    if (-not [bool](Get-Value $Response 'current' $false)) { return $false }
    if ([int64](Get-Value $Response 'source_order' -1) -ne $Order) { return $false }
    if (-not [string](Get-Value $Response 'archive_id' '')) { return $false }
    if ([string](Get-Value $Response 'source_sha256' '') -cne $Sha) { return $false }
    return $true
}

function Invoke-Rpc($Config, [string]$Method, $Parameters, [string]$RpcId) {
    $token = Get-ActionToken $Config
    $headers = @{
        Authorization = 'Bearer ' + $token
        'MCP-Protocol-Version' = '2025-11-25'
        'X-EvolvMem-Expected-User' = [string]$Config.expected_user
    }
    $body = @{ jsonrpc = '2.0'; id = $RpcId; method = $Method; params = $Parameters } |
        ConvertTo-Json -Depth 20 -Compress
    $bodyBytes = [Text.Encoding]::UTF8.GetBytes($body)
    $response = Invoke-WebRequest -UseBasicParsing -Uri ([string]$Config.url) -Method Post `
        -ContentType 'application/json; charset=utf-8' -Headers $headers -Body $bodyBytes `
        -TimeoutSec $script:RpcTimeoutSeconds
    # PS 5.1 decodes application/json without charset as Latin-1. MCP JSON
    # is UTF-8; read the response bytes so injected Chinese stays intact.
    $responseText = [Text.Encoding]::UTF8.GetString($response.RawContentStream.ToArray())
    $rpc = $responseText | ConvertFrom-Json
    if ($null -ne (Get-Value $rpc 'error')) { throw (New-ClientError 'remote_request_failed' 'remote MCP request failed') }
    return Get-Value $rpc 'result'
}

function Convert-McpToolResult($Config, $Result) {
    $result = $Result
    $content = @(Get-Value $result 'content' @())
    # Error responses carry a stable code and deliberately omit
    # authenticated_user. Classify them before the identity check so the real
    # cause survives instead of a generic transport message.
    if ([bool](Get-Value $result 'isError' $false)) {
        $value = $null
        if ($content.Count -ge 1 -and (Get-Value $content[0] 'type' '') -eq 'text') {
            try { $value = ([string](Get-Value $content[0] 'text' '')) | ConvertFrom-Json }
            catch { $value = $null }
        }
        throw (New-ClientError (Get-StableErrorCode $value) 'remote MCP tool returned an error')
    }
    if ($content.Count -lt 1 -or (Get-Value $content[0] 'type' '') -ne 'text') {
        throw (New-ClientError 'invalid_response' 'remote MCP returned an invalid result')
    }
    try { $value = ([string](Get-Value $content[0] 'text' '')) | ConvertFrom-Json }
    catch { throw (New-ClientError 'invalid_response' 'remote MCP returned an invalid result') }
    $actual = [string](Get-Value $value 'authenticated_user' '')
    if (-not $actual -or $actual -cne [string]$Config.expected_user) {
        throw (New-ClientError 'identity_mismatch' 'EvolvMem authenticated user does not match this installation')
    }
    if (Get-Value $value 'error') { throw (New-ClientError (Get-StableErrorCode $value) 'remote MCP tool failed') }
    return $value
}

function Confirm-RemoteIdentity($Config) {
    if ($script:IdentityValidated) { return }
    $result = Invoke-Rpc $Config 'tools/call' @{ name = 'memory_status'; arguments = @{} } `
        ([Guid]::NewGuid().ToString('N'))
    $script:PreflightStatus = Convert-McpToolResult $Config $result
    $script:IdentityValidated = $true
}

function Invoke-McpTool($Config, [string]$Name, $Arguments, [string]$RpcId = '') {
    Confirm-RemoteIdentity $Config
    if ($Name -eq 'memory_status') { return $script:PreflightStatus }
    if (-not $RpcId) { $RpcId = [Guid]::NewGuid().ToString('N') }
    $result = Invoke-Rpc $Config 'tools/call' @{ name = $Name; arguments = $Arguments } $RpcId
    return Convert-McpToolResult $Config $result
}

function Normalize-WorkspacePath([string]$Path) {
    if (-not $Path) { return '' }
    try { $full = [IO.Path]::GetFullPath($Path) }
    catch { return $Path }
    return $full.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
}

function Resolve-Project($Config, [string]$WorkspacePath) {
    $workspace = Normalize-WorkspacePath $WorkspacePath
    $bestProject = ''
    $bestLength = -1
    $projects = Get-Value $Config 'projects'
    if ($null -eq $projects) { return $bestProject }
    foreach ($mapping in $projects.PSObject.Properties) {
        $prefix = Normalize-WorkspacePath ([string]$mapping.Name)
        if (-not $prefix) { continue }
        $matches = $workspace.Equals($prefix, [StringComparison]::OrdinalIgnoreCase)
        if (-not $matches -and $workspace.Length -gt $prefix.Length -and
            $workspace.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
            $next = $workspace[$prefix.Length]
            $matches = $next -eq [IO.Path]::DirectorySeparatorChar -or
                $next -eq [IO.Path]::AltDirectorySeparatorChar
        }
        if ($matches -and $prefix.Length -gt $bestLength) {
            $bestLength = $prefix.Length
            $bestProject = [string]$mapping.Value
        }
    }
    return $bestProject
}

function Get-RepoSnapshot([string]$WorkspacePath) {
    $unknown = [ordered]@{ kind = 'non_git'; branch = ''; root_commit = ''; head_commit = '' }
    if (-not $WorkspacePath -or -not (Get-Command git -ErrorAction SilentlyContinue)) { return $unknown }
    try {
        $inside = @(& git -C $WorkspacePath rev-parse --is-inside-work-tree 2>$null)
        if ($LASTEXITCODE -ne 0 -or $inside.Count -lt 1 -or $inside[-1].Trim() -ne 'true') { return $unknown }
        $head = (@(& git -C $WorkspacePath rev-parse HEAD 2>$null))[-1].Trim().ToLowerInvariant()
        $roots = @(& git -C $WorkspacePath rev-list --max-parents=0 HEAD 2>$null)
        if ($LASTEXITCODE -ne 0 -or $roots.Count -lt 1) { return $unknown }
        $root = $roots[0].Trim().ToLowerInvariant()
        $branchLines = @(& git -C $WorkspacePath symbolic-ref --short -q HEAD 2>$null)
        $branch = if ($branchLines.Count -gt 0) { $branchLines[-1].Trim() } else { '' }
        if ($head -notmatch '^[0-9a-f]{40}$|^[0-9a-f]{64}$' -or
            $root -notmatch '^[0-9a-f]{40}$|^[0-9a-f]{64}$') { return $unknown }
        return [ordered]@{ kind = 'git'; branch = $branch; root_commit = $root; head_commit = $head }
    }
    catch { return $unknown }
}

function Get-EventInput {
    $raw = [Console]::In.ReadToEnd()
    if (-not $raw.Trim()) { return [pscustomobject]@{} }
    return $raw | ConvertFrom-Json
}

function Get-ReceiptPath([string]$SessionId) {
    return [IO.Path]::Combine($script:ClientHome, 'receipts', (Get-TextSha256 $SessionId) + '.json')
}

function Get-SessionPath([string]$SessionId) {
    return [IO.Path]::Combine($script:ClientHome, 'sessions', (Get-TextSha256 $SessionId) + '.json')
}

function Limit-Context([string]$Text) {
    if ($null -eq $Text) { return '' }
    if ($Text.Length -le $script:MaxContextChars) { return $Text }
    return $Text.Substring(0, $script:MaxContextChars)
}

function Join-Context([object[]]$Parts) {
    $usable = @($Parts | ForEach-Object { if ($null -ne $_ -and ([string]$_).Trim()) { ([string]$_).Trim() } })
    return Limit-Context ($usable -join "`n`n")
}

function Get-InjectionArguments($Config, $Event, [string]$Query, [int]$ReserveChars = 0) {
    $cwd = [string](Get-Value $Event 'cwd' '')
    return [ordered]@{
        project = Resolve-Project $Config $cwd
        query = $Query
        workspace_path = $cwd
        device_id = [string]$Config.device_id
        repo_snapshot = Get-RepoSnapshot $cwd
        max_chars = [Math]::Max(1, $script:MaxContextChars - $script:ConnectionMetadataReserveChars - $ReserveChars)
    }
}

function Invoke-Injection($Config, $Event, [string]$Query, [int]$ReserveChars = 0) {
    $arguments = Get-InjectionArguments $Config $Event $Query $ReserveChars
    $response = Invoke-McpTool $Config 'context_session_start' $arguments
    $metadata = [ordered]@{
        device_id = [string]$Config.device_id
        workspace_path = [string]$arguments.workspace_path
        project = [string]$arguments.project
        repo_snapshot = $arguments.repo_snapshot
        session_id = [string](Get-Value $Event 'session_id' '')
    }
    return [pscustomobject]@{ Arguments = $arguments; Response = $response; Metadata = $metadata }
}

function Get-StartQuery([string]$Source) {
    switch ($Source) {
        'resume' { return 'Resume the current Codex work with relevant memory and task continuity.' }
        'clear' { return 'Start a cleared Codex conversation with relevant workspace memory.' }
        'compact' { return 'Continue the current work after context compaction.' }
        default { return 'Start work in this Codex workspace with relevant memory.' }
    }
}

function Format-InjectionContext($Injection) {
    $prefix = ''
    if (-not [string]$Injection.Arguments.project) {
        $prefix = '[EvolvMem: this workspace is not bound to a project; only general memory was loaded.]'
    }
    $metadata = $Injection.Metadata | ConvertTo-Json -Depth 10 -Compress
    $connection = "[EvolvMem connection metadata]`n" + $metadata + "`n" +
        'Use these exact values for later continuity MCP calls in this session; do not replace device_id with a hostname.'
    return Join-Context @($connection, $prefix, [string](Get-Value $Injection.Response 'block' ''))
}

function Save-SessionRegistration($Config, $Event) {
    $sessionId = [string](Get-Value $Event 'session_id' '')
    if (-not $sessionId) { return }
    $path = Get-SessionPath $sessionId
    $prior = Read-JsonFile $path
    $transcript = [string](Get-Value $Event 'transcript_path' (Get-Value $prior 'transcript_path' ''))
    if ($transcript -and [IO.File]::Exists($transcript)) {
        $stream = New-Object IO.FileStream($transcript, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        try {
            $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8, $true)
            try { $line = $reader.ReadLine() } finally { $reader.Dispose() }
        }
        finally { $stream.Dispose() }
        $header = $null
        try { $header = $line | ConvertFrom-Json } catch { }
        if ([string](Get-Value $header 'type' '') -eq 'session_meta') {
            $actual = [string](Get-Value (Get-Value $header 'payload') 'id' '')
            if ($actual -and $actual -cne $sessionId) {
                throw (New-ClientError 'session_id_mismatch' 'transcript identity does not match this session')
            }
        }
    }
    $record = [ordered]@{
        version = 1
        session_id = $sessionId
        transcript_path = [string](Get-Value $Event 'transcript_path' (Get-Value $prior 'transcript_path' ''))
        workspace_path = [string](Get-Value $Event 'cwd' (Get-Value $prior 'workspace_path' ''))
        project = Resolve-Project $Config ([string](Get-Value $Event 'cwd' (Get-Value $prior 'workspace_path' '')))
        source_bytes = [int64](Get-Value $prior 'source_bytes' 0)
        source_last_write_utc = [string](Get-Value $prior 'source_last_write_utc' '')
        cache_file = [string](Get-Value $prior 'cache_file' '')
        source_order = [int64](Get-Value $prior 'source_order' 0)
        max_source_order = [int64](Get-Value $prior 'max_source_order' 0)
        current_sha256 = [string](Get-Value $prior 'current_sha256' '')
        anchor_confirmed_sha256 = [string](Get-Value $prior 'anchor_confirmed_sha256' '')
        anchor_error = [string](Get-Value $prior 'anchor_error' '')
        last_seen_utc = [DateTime]::UtcNow.ToString('o')
    }
    Write-JsonAtomic $path $record
}

function Invoke-SessionStart($Config, $Event) {
    Ensure-Directory ([IO.Path]::Combine($script:ClientHome, 'receipts'))
    $sessionId = [string](Get-Value $Event 'session_id' '')
    $sessionScope = Get-SessionLockScope $sessionId
    [void](Invoke-WithClientLock { Save-SessionRegistration $Config $Event } 1000 $sessionScope)
    $source = [string](Get-Value $Event 'source' 'startup')
    $receiptPath = Get-ReceiptPath $sessionId
    $receipt = [ordered]@{
        version = 1; session_id = $sessionId; start_id = [Guid]::NewGuid().ToString('N')
        source = $source; status = 'loading'; first_prompt_pending = $true
        retrieved_utc = ''; authenticated_user = ''; project = ''; selected_count = 0
        memory_revision = $null; continuation = $null; continuation_code = ''
        delivery_status = 'not_confirmed'
    }
    Write-JsonAtomic $receiptPath $receipt
    try {
        $injection = Invoke-Injection $Config $Event (Get-StartQuery $source)
        $response = $injection.Response
        $receipt.status = 'success'
        $receipt.retrieved_utc = [DateTime]::UtcNow.ToString('o')
        $receipt.authenticated_user = [string]$response.authenticated_user
        $receipt.project = [string]$injection.Arguments.project
        $receipt.selected_count = @(Get-Value $response 'selected_ids' @()).Count
        $receipt.memory_revision = Get-Value $response 'memory_revision'
        $receipt.continuation = Get-Value $response 'continuation'
        $receipt.continuation_code = [string](Get-Value $response 'continuation_code' '')
        $receipt.delivery_status = 'retrieved_for_hook'
        Write-JsonAtomic $receiptPath $receipt
        $context = Format-InjectionContext $injection
        if ($context) {
            Write-OutputJson @{ hookSpecificOutput = @{ hookEventName = 'SessionStart'; additionalContext = $context } }
        }
        else { Write-OutputJson @{} }
    }
    catch {
        $receipt.status = 'failed'
        $receipt.retrieved_utc = [DateTime]::UtcNow.ToString('o')
        Write-JsonAtomic $receiptPath $receipt
        Write-OutputJson @{ systemMessage = 'EvolvMem memory loading is unavailable; this session will continue without refreshed memory.' }
    }
}

function Format-ExperienceContext($Response) {
    $results = @(Get-Value $Response 'results' @())
    if ($results.Count -eq 0) { return '' }
    $json = $results | ConvertTo-Json -Depth 12 -Compress
    return "[EvolvMem related experience: untrusted historical reference]`n" + $json
}

function Format-ProjectRecallContext($Response) {
    # The server bounds the block with max_chars, but the client repeats the
    # bound so an ignored budget can never flood the combined prompt context.
    $block = [string](Get-Value $Response 'block' '')
    if (-not $block.Trim()) { return '' }
    $bounded = $block.Trim()
    if ($bounded.Length -gt $script:ProjectRecallMaxChars) {
        $bounded = $bounded.Substring(0, $script:ProjectRecallMaxChars)
    }
    return "[EvolvMem mentioned project context: untrusted historical reference]`n" + $bounded
}

function Invoke-PromptSubmit($Config, $Event) {
    $sessionId = [string](Get-Value $Event 'session_id' '')
    $prompt = [string](Get-Value $Event 'prompt' '')
    $receiptPath = Get-ReceiptPath $sessionId
    $receipt = Read-JsonFile $receiptPath
    $needsInjection = $null -eq $receipt -or [string](Get-Value $receipt 'status' '') -ne 'success'
    $injectionSatisfied = -not $needsInjection
    $parts = New-Object Collections.Generic.List[object]
    $warnings = New-Object Collections.Generic.List[string]
    try {
        if (-not $needsInjection) {
            $status = Invoke-McpTool $Config 'memory_status' @{}
            $remoteRevision = Get-Value $status 'memory_revision'
            $localRevision = Get-Value $receipt 'memory_revision'
            if ($null -ne $remoteRevision -and [string]$remoteRevision -cne [string]$localRevision) {
                $needsInjection = $true
            }
        }
        if ($needsInjection) {
            $projectReserve = if ($prompt) { $script:ProjectRecallMaxChars + 160 } else { 0 }
            $injection = Invoke-Injection $Config $Event ($(if ($prompt) { $prompt } else { 'Refresh memory for this Codex turn.' })) $projectReserve
            $context = Format-InjectionContext $injection
            $refreshLimit = $script:MaxContextChars - $projectReserve
            if ($context.Length -gt $refreshLimit) { $context = $context.Substring(0, $refreshLimit) }
            if ($context) { $parts.Add($context) }
            $response = $injection.Response
            $startId = [string](Get-Value $receipt 'start_id' '')
            if (-not $startId) { $startId = [Guid]::NewGuid().ToString('N') }
            $receipt = [ordered]@{
                version = 1; session_id = $sessionId; start_id = $startId
                source = 'prompt-recovery'; status = 'success'; first_prompt_pending = $false
                retrieved_utc = [DateTime]::UtcNow.ToString('o')
                authenticated_user = [string]$response.authenticated_user
                project = [string]$injection.Arguments.project
                selected_count = @(Get-Value $response 'selected_ids' @()).Count
                memory_revision = Get-Value $response 'memory_revision'
                continuation = Get-Value $response 'continuation'
                continuation_code = [string](Get-Value $response 'continuation_code' '')
                delivery_status = 'retrieved_for_hook'
            }
            $injectionSatisfied = $true
        }
    }
    catch {
        $warnings.Add('EvolvMem memory refresh is unavailable for this turn; continuing with the last available context.')
        $strict = [bool](Get-Value $Config 'strict_injection' $false)
        if ($strict -and -not $injectionSatisfied) {
            Write-OutputJson @{ decision = 'block'; reason = 'EvolvMem memory injection is unavailable for this session start. Retry the prompt after connectivity is restored.' }
            return
        }
    }
    if ($prompt -and $injectionSatisfied) {
        # Explicit project mentions are recalled first. A dedicated call and a
        # dedicated catch keep this path isolated: an unavailable project tool
        # only adds a short warning and never removes the memory or experience
        # context already collected for this turn.
        try {
            $projectRecallArgs = [ordered]@{
                query = $prompt
                max_chars = $script:ProjectRecallMaxChars
            }
            $projectRecall = Invoke-McpTool $Config 'context_project_recall' $projectRecallArgs
            $projectContext = Format-ProjectRecallContext $projectRecall
            if ($projectContext) { $parts.Add($projectContext) }
        }
        catch {
            $warnings.Add('EvolvMem project mention recall is unavailable for this turn.')
        }
        try {
            $experienceArgs = [ordered]@{
                query = $prompt
                project = Resolve-Project $Config ([string](Get-Value $Event 'cwd' ''))
            }
            $experience = Invoke-McpTool $Config 'experience_recall' $experienceArgs
            $experienceContext = Format-ExperienceContext $experience
            if ($experienceContext) { $parts.Add($experienceContext) }
        }
        catch {
            $warnings.Add('EvolvMem related experience lookup is unavailable for this turn.')
        }
    }
    if ($injectionSatisfied -and $null -ne $receipt) {
        try {
            if ($receipt -isnot [Collections.IDictionary]) {
                $copy = [ordered]@{}
                foreach ($property in $receipt.PSObject.Properties) { $copy[$property.Name] = $property.Value }
                $receipt = $copy
            }
            $receipt['first_prompt_pending'] = $false
            Write-JsonAtomic $receiptPath $receipt
        }
        catch {
            $warnings.Add('EvolvMem could not update its local injection receipt.')
        }
    }
    $context = Join-Context $parts.ToArray()
    $output = [ordered]@{}
    if ($context) {
        $output.hookSpecificOutput = @{ hookEventName = 'UserPromptSubmit'; additionalContext = $context }
    }
    if ($warnings.Count -gt 0) { $output.systemMessage = $warnings -join ' ' }
    Write-OutputJson $output
}

function Initialize-DataProtection {
    if (-not $script:RunningOnWindows) { throw 'DPAPI CurrentUser is only available on Windows' }
    # Windows PowerShell 5.1 does not load System.Security in a fresh hook or
    # scheduled-task process. PowerShell 7 can already resolve this type.
    if ($null -eq ('Security.Cryptography.ProtectedData' -as [type])) {
        Add-Type -AssemblyName System.Security
    }
}

function Protect-Bytes([byte[]]$Bytes) {
    Initialize-DataProtection
    $entropy = [Text.Encoding]::UTF8.GetBytes('evolvmem-codex-archive-v1')
    return ,([Security.Cryptography.ProtectedData]::Protect(
        $Bytes, $entropy, [Security.Cryptography.DataProtectionScope]::CurrentUser))
}

function Unprotect-Bytes([byte[]]$Bytes) {
    Initialize-DataProtection
    $entropy = [Text.Encoding]::UTF8.GetBytes('evolvmem-codex-archive-v1')
    return ,([Security.Cryptography.ProtectedData]::Unprotect(
        $Bytes, $entropy, [Security.Cryptography.DataProtectionScope]::CurrentUser))
}

function Read-CompleteTail([string]$Path, [int64]$Offset) {
    $stream = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        if ($Offset -lt 0 -or $Offset -gt $stream.Length) { $Offset = 0 }
        [void]$stream.Seek($Offset, [IO.SeekOrigin]::Begin)
        $remaining = [int64]($stream.Length - $Offset)
        if ($remaining -le 0) { return [pscustomobject]@{ Bytes = [byte[]]@(); EndOffset = $Offset } }
        if ($remaining -gt [int]::MaxValue) { throw 'transcript tail is too large' }
        $bytes = New-Object byte[] ([int]$remaining)
        $read = 0
        while ($read -lt $bytes.Length) {
            $count = $stream.Read($bytes, $read, $bytes.Length - $read)
            if ($count -le 0) { break }
            $read += $count
        }
        $lastNewline = -1
        for ($index = $read - 1; $index -ge 0; $index--) {
            if ($bytes[$index] -eq 10) { $lastNewline = $index; break }
        }
        if ($lastNewline -lt 0) { return [pscustomobject]@{ Bytes = [byte[]]@(); EndOffset = $Offset } }
        $complete = New-Object byte[] ($lastNewline + 1)
        [Array]::Copy($bytes, 0, $complete, 0, $complete.Length)
        return [pscustomobject]@{ Bytes = $complete; EndOffset = $Offset + $complete.Length }
    }
    finally { $stream.Dispose() }
}

function Test-FilePrefix([string]$Path, [byte[]]$Expected) {
    if ($Expected.Length -eq 0) { return $true }
    $stream = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try {
        if ($stream.Length -lt $Expected.Length) { return $false }
        $buffer = New-Object byte[] 65536
        $position = 0
        while ($position -lt $Expected.Length) {
            $wanted = [Math]::Min($buffer.Length, $Expected.Length - $position)
            $read = $stream.Read($buffer, 0, $wanted)
            if ($read -ne $wanted) { return $false }
            # Compare in .NET rather than creating one PowerShell operation per
            # byte of every historical transcript on every scheduler tick.
            if ([Convert]::ToBase64String($buffer, 0, $read) -cne
                [Convert]::ToBase64String($Expected, $position, $read)) { return $false }
            $position += $read
        }
        return $true
    }
    finally { $stream.Dispose() }
}

function Save-ProtectedBytes([string]$Path, [byte[]]$Plaintext) {
    Ensure-Directory ([IO.Path]::GetDirectoryName($Path))
    $temp = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllBytes($temp, (Protect-Bytes $Plaintext))
    try {
        if ([IO.File]::Exists($Path)) {
            $backup = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.replace.bak'
            try { [IO.File]::Replace($temp, $Path, $backup) }
            finally { if ([IO.File]::Exists($backup)) { [IO.File]::Delete($backup) } }
        }
        else { [IO.File]::Move($temp, $Path) }
    }
    finally { if ([IO.File]::Exists($temp)) { [IO.File]::Delete($temp) } }
}

function Capture-Transcript($Config, $Event) {
    $sessionId = [string](Get-Value $Event 'session_id' '')
    $transcriptPath = [string](Get-Value $Event 'transcript_path' '')
    if (-not $sessionId -or -not $transcriptPath -or -not [IO.File]::Exists($transcriptPath)) { return $false }
    Save-SessionRegistration $Config $Event
    $sessionPath = Get-SessionPath $sessionId
    $session = ConvertTo-OrderedRecord (Read-JsonFile $sessionPath)
    # A legacy session without a source order or current anchor is migrated
    # once before its transcript is read, so the local current cache keeps a
    # strictly greater order than every old pending version.
    $state = $null
    if (-not [string](Get-RecordValue $session 'current_sha256' '') -or
        [int64](Get-RecordValue $session 'source_order' 0) -le 0) {
        $state = Initialize-SessionSourceState $Config $sessionId
        $session = $state.Record
    }
    $sourceBytes = [int64](Get-RecordValue $session 'source_bytes' 0)
    $cacheName = (Get-TextSha256 $sessionId) + '.bin'
    $cachePath = [IO.Path]::Combine($script:ClientHome, 'session-cache', $cacheName)
    $file = New-Object IO.FileInfo($transcriptPath)
    if ($sourceBytes -gt 0 -and $file.Length -eq $sourceBytes -and [IO.File]::Exists($cachePath) -and
        $file.LastWriteTimeUtc.ToString('o') -ceq [string](Get-RecordValue $session 'source_last_write_utc' '')) {
        return $false
    }
    $prior = [byte[]]@()
    if ($sourceBytes -gt 0 -and [IO.File]::Exists($cachePath)) {
        $prior = Unprotect-Bytes ([IO.File]::ReadAllBytes($cachePath))
        if ($prior.Length -ne $sourceBytes) { $sourceBytes = 0; $prior = [byte[]]@() }
    }
    elseif ($sourceBytes -gt 0) { $sourceBytes = 0 }
    if ($file.Length -lt $sourceBytes) { $sourceBytes = 0; $prior = [byte[]]@() }
    elseif ($sourceBytes -gt 0 -and -not (Test-FilePrefix $transcriptPath $prior)) {
        # A rewritten or divergent transcript begins a new immutable content
        # version. Older unacknowledged queue entries remain untouched.
        $sourceBytes = 0
        $prior = [byte[]]@()
    }
    $tail = Read-CompleteTail $transcriptPath $sourceBytes
    if ($tail.Bytes.Length -eq 0) { return $false }
    $combined = New-Object byte[] ($prior.Length + $tail.Bytes.Length)
    if ($prior.Length -gt 0) { [Array]::Copy($prior, 0, $combined, 0, $prior.Length) }
    [Array]::Copy($tail.Bytes, 0, $combined, $prior.Length, $tail.Bytes.Length)
    $sha = Get-Sha256Hex $combined
    $sessionHash = Get-TextSha256 $sessionId
    $stem = $sessionHash.Substring(0, 16) + '-' + $sha
    $queuePath = [IO.Path]::Combine($script:ClientHome, 'queue', $stem + '.bin')
    $manifestPath = [IO.Path]::Combine($script:ClientHome, 'queue', $stem + '.json')
    if ($null -eq $state) {
        # Steady state: the next order only has to clear this session's own
        # high-water mark, so an unchanged large log is never re-scanned.
        $state = [ordered]@{
            MaxOrder = [int64](Get-RecordValue $session 'max_source_order' 0)
            SourceOrder = [int64](Get-RecordValue $session 'source_order' 0)
        }
    }
    $existingManifest = Read-JsonFile $manifestPath
    if (-not [IO.File]::Exists($queuePath)) { Save-ProtectedBytes $queuePath $combined }
    if ($null -eq $existingManifest) {
        # Each distinct complete snapshot receives one order computed as
        # max(previous order + 1, current UTC milliseconds).
        $order = Get-NextSourceOrder $state
        Write-JsonAtomic $manifestPath ([ordered]@{
            version = 1; session_id = $sessionId; project = Resolve-Project $Config ([string](Get-Value $Event 'cwd' ''))
            sha256 = $sha; total_bytes = $combined.Length; next_offset = 0; extract = $true
            created_utc = [DateTime]::UtcNow.ToString('o'); source_order = [int64]$order
        })
    }
    else {
        # Retries keep their order. A real B -> A capture can reuse A's
        # immutable payload while receiving a newer observation order.
        $order = [int64](Get-Value $existingManifest 'source_order' 0)
        if ($order -le 0 -or [string](Get-RecordValue $session 'current_sha256' '') -cne $sha) {
            $order = Get-NextSourceOrder $state
            $record = ConvertTo-OrderedRecord $existingManifest
            $record['source_order'] = [int64]$order
            Write-JsonAtomic $manifestPath $record
        }
    }
    Save-ProtectedBytes $cachePath $combined
    $updated = ConvertTo-OrderedRecord $session
    $updated['source_bytes'] = [int64]$tail.EndOffset
    $updated['source_last_write_utc'] = $file.LastWriteTimeUtc.ToString('o')
    $updated['cache_file'] = $cacheName
    $updated['last_seen_utc'] = [DateTime]::UtcNow.ToString('o')
    # The captured cache is the local current snapshot; only a version equal to
    # this persisted sha may later declare current_sha256.
    $updated['current_sha256'] = $sha
    $updated['source_order'] = [int64]$order
    if ($order -gt [int64](Get-RecordValue $state 'MaxOrder' 0)) {
        $state['MaxOrder'] = [int64]$order
    }
    $updated['max_source_order'] = [int64](Get-RecordValue $state 'MaxOrder' 0)
    if ([string](Get-RecordValue $session 'current_sha256' '') -cne $sha -or
        [int64](Get-RecordValue $session 'source_order' 0) -ne $order) {
        $updated['anchor_confirmed_sha256'] = ''
        $updated['anchor_confirmed_utc'] = ''
    }
    $updated['anchor_error'] = ''
    Write-JsonAtomic $sessionPath $updated
    return $true
}

function Invoke-Snapshot($Config, $Event) {
    try {
        $scope = Get-SessionLockScope ([string](Get-Value $Event 'session_id' ''))
        [void](Invoke-WithClientLock { Capture-Transcript $Config $Event } 100 $scope)
        Write-OutputJson @{}
    }
    catch {
        Write-OutputJson @{ systemMessage = 'EvolvMem could not queue the latest local transcript snapshot.' }
    }
}

function ConvertTo-QueueManifest($Manifest) {
    # ConvertFrom-Json yields a PSCustomObject that cannot accept new
    # properties. Copy it into an ordered dictionary that preserves every
    # existing value and can carry the bounded failure fields. Stale failure
    # fields are dropped so acknowledged progress clears them.
    $data = [ordered]@{}
    if ($null -ne $Manifest) {
        foreach ($property in $Manifest.PSObject.Properties) {
            if ($property.Name -in @('last_error', 'last_error_utc', 'last_error_stage', 'last_error_type')) { continue }
            $data[$property.Name] = $property.Value
        }
    }
    return $data
}

function Get-QueueFailureSummary([string]$QueueDir) {
    $failed = 0
    $codes = New-Object Collections.Generic.List[string]
    if ([IO.Directory]::Exists($QueueDir)) {
        foreach ($path in @([IO.Directory]::GetFiles($QueueDir, '*.json'))) {
            try {
                $code = [string](Get-Value (Read-JsonFile $path) 'last_error' '')
                if (-not $code) { continue }
                $failed += 1
                if (-not $codes.Contains($code)) { $codes.Add($code) }
            }
            catch { }
        }
    }
    return [ordered]@{
        pending_failed_versions = $failed
        pending_error_codes = @($codes | Sort-Object)
    }
}

function ConvertTo-OrderedRecord($Object) {
    # ConvertFrom-Json yields a PSCustomObject that cannot accept new
    # properties. Copy every existing value into an ordered dictionary before
    # adding source ordering or anchor fields.
    $data = [ordered]@{}
    if ($null -ne $Object) {
        foreach ($property in $Object.PSObject.Properties) { $data[$property.Name] = $property.Value }
    }
    return $data
}

function Get-RecordValue($Record, [string]$Name, $Default = $null) {
    if ($null -eq $Record) { return $Default }
    if ($Record -is [Collections.IDictionary]) {
        if ($Record.Contains($Name)) { return $Record[$Name] }
        return $Default
    }
    $property = $Record.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Get-UtcMilliseconds([DateTime]$Value) {
    $epoch = New-Object DateTime(1970, 1, 1, 0, 0, 0, [DateTimeKind]::Utc)
    return [int64][Math]::Floor(($Value.ToUniversalTime() - $epoch).TotalMilliseconds)
}

function Get-CreatedOrderMilliseconds($Manifest) {
    # Only a valid created_utc may seed a legacy order. Arrival time or file
    # size are never used to guess which snapshot is current.
    $text = [string](Get-Value $Manifest 'created_utc' '')
    if (-not $text) { return [int64]0 }
    try {
        $parsed = [DateTimeOffset]::Parse($text, [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::AssumeUniversal)
        $order = Get-UtcMilliseconds $parsed.UtcDateTime
        if ($order -gt 0) { return [int64]$order }
    }
    catch { }
    return [int64]0
}

function Get-NextSourceOrder($State) {
    # max(previous order + 1, current UTC milliseconds). The previous order is
    # the session high-water mark, so a rolled-back clock still advances.
    $previous = [int64](Get-RecordValue $State 'MaxOrder' 0)
    $sourceOrder = [int64](Get-RecordValue $State 'SourceOrder' 0)
    if ($sourceOrder -gt $previous) { $previous = $sourceOrder }
    $order = $previous + 1
    $now = Get-UtcMilliseconds ([DateTime]::UtcNow)
    if ($now -gt $order) { $order = $now }
    return [int64]$order
}

function Get-SessionQueueEntries([string]$SessionId) {
    $queueDir = [IO.Path]::Combine($script:ClientHome, 'queue')
    $entries = New-Object Collections.Generic.List[object]
    if (-not [IO.Directory]::Exists($queueDir)) { return @() }
    foreach ($manifestPath in @([IO.Directory]::GetFiles($queueDir, '*.json'))) {
        try {
            $manifest = Read-JsonFile $manifestPath
            if ($null -eq $manifest) { continue }
            if ([string](Get-Value $manifest 'session_id' '') -cne $SessionId) { continue }
            $entries.Add([ordered]@{
                Path = $manifestPath
                Manifest = ConvertTo-OrderedRecord $manifest
                Order = [int64](Get-Value $manifest 'source_order' 0)
                CreatedOrder = Get-CreatedOrderMilliseconds $manifest
                TotalBytes = [int64](Get-Value $manifest 'total_bytes' 0)
            })
        }
        catch { }
    }
    return @($entries.ToArray())
}

function New-QueueAnchorFromCache($Config, $State, [string]$Sha = '', [byte[]]$Bytes = $null, [int64]$Order = 0) {
    # Idempotent one-time migration anchor: when the local current snapshot was
    # already acknowledged and its queue entry is gone, rebuild the identical
    # queue entry so the server learns which local snapshot is current.
    $sessionId = [string](Get-RecordValue $State.Record 'session_id' '')
    if (-not $sessionId) { return $null }
    if ($null -eq $Bytes -or -not $Sha) {
        $cacheName = [string](Get-RecordValue $State.Record 'cache_file' '')
        if (-not $cacheName) { return $null }
        $cachePath = [IO.Path]::Combine($script:ClientHome, 'session-cache', $cacheName)
        if (-not [IO.File]::Exists($cachePath)) { return $null }
        try { $Bytes = Unprotect-Bytes ([IO.File]::ReadAllBytes($cachePath)) }
        catch { return $null }
        if ($null -eq $Bytes -or $Bytes.Length -le 0) { return $null }
        $Sha = Get-Sha256Hex $Bytes
    }
    if ($Order -le 0) { $Order = Get-NextSourceOrder $State }
    $queueDir = [IO.Path]::Combine($script:ClientHome, 'queue')
    Ensure-Directory $queueDir
    $stem = (Get-TextSha256 $sessionId).Substring(0, 16) + '-' + $Sha
    $dataPath = [IO.Path]::Combine($queueDir, $stem + '.bin')
    $manifestPath = [IO.Path]::Combine($queueDir, $stem + '.json')
    if (-not [IO.File]::Exists($dataPath)) { Save-ProtectedBytes $dataPath $Bytes }
    $existing = Read-JsonFile $manifestPath
    if ($null -ne $existing) {
        $existingOrder = [int64](Get-Value $existing 'source_order' 0)
        if ($existingOrder -le 0) {
            $record = ConvertTo-OrderedRecord $existing
            $record['source_order'] = [int64]$Order
            Write-JsonAtomic $manifestPath $record
        }
        else { $Order = $existingOrder }
    }
    else {
        $project = Resolve-Project $Config ([string](Get-RecordValue $State.Record 'workspace_path' ''))
        Write-JsonAtomic $manifestPath ([ordered]@{
            version = 1; session_id = $sessionId; project = $project
            sha256 = $Sha; total_bytes = [int64]$Bytes.Length; next_offset = 0; extract = $true
            created_utc = [DateTime]::UtcNow.ToString('o'); source_order = [int64]$Order
        })
    }
    return [ordered]@{ Path = $manifestPath; Order = [int64]$Order; Sha256 = $Sha }
}

function Initialize-SessionSourceState($Config, [string]$SessionId) {
    # Prepares the local source ordering and the migration anchor before any
    # upload. Callers hold the session lock. A current snapshot is only ever
    # declared from a readable local cache or a fresh capture; a missing cache
    # keeps every pending version queued with a stable diagnostic code.
    $sessionPath = Get-SessionPath $SessionId
    $exists = [IO.File]::Exists($sessionPath)
    $session = ConvertTo-OrderedRecord (Read-JsonFile $sessionPath)
    $state = [ordered]@{
        Path = $sessionPath; Exists = $exists; Record = $session
        CurrentSha256 = [string](Get-RecordValue $session 'current_sha256' '')
        SourceOrder = [int64](Get-RecordValue $session 'source_order' 0)
        MaxOrder = [int64](Get-RecordValue $session 'max_source_order' 0)
        AnchorConfirmed = [string](Get-RecordValue $session 'anchor_confirmed_sha256' '')
        AnchorError = [string](Get-RecordValue $session 'anchor_error' '')
        AnchorPath = ''; AnchorOrder = [int64]0
        Entries = @(); Dirty = $false
    }
    if ($state.SourceOrder -gt $state.MaxOrder) { $state.MaxOrder = $state.SourceOrder }
    $entries = @(Get-SessionQueueEntries $SessionId)
    $state.Entries = $entries
    $floor = [int64]0
    foreach ($entry in $entries) { if ($entry.Order -gt $floor) { $floor = $entry.Order } }
    # Legacy manifests without source_order receive one fixed millisecond order
    # from their valid created_utc. Equal timestamps never share an order.
    $legacy = @($entries | Where-Object { $_.Order -le 0 -and $_.CreatedOrder -gt 0 } |
        Sort-Object { [int64]$_.CreatedOrder }, { [string]$_.Path })
    foreach ($entry in $legacy) {
        $order = [int64]$entry.CreatedOrder
        if ($order -le $floor) { $order = $floor + 1 }
        $entry.Manifest['source_order'] = [int64]$order
        Write-JsonAtomic $entry.Path $entry.Manifest
        $entry.Order = $order
        $floor = $order
    }
    if ($floor -gt $state.MaxOrder) { $state.MaxOrder = $floor; $state.Dirty = $true }
    if ($state.CurrentSha256 -and $state.SourceOrder -gt 0) {
        # A readable current anchor clears any stale missing-cache diagnostic.
        if ($state.AnchorError) { $state.AnchorError = ''; $state.Dirty = $true }
        if ($state.AnchorConfirmed -cne $state.CurrentSha256) {
            $anchor = @($entries | Where-Object {
                $_.Order -gt 0 -and
                [string](Get-RecordValue $_.Manifest 'sha256' '') -ceq $state.CurrentSha256 -and
                [IO.File]::Exists(($_.Path -replace '\.json$', '.bin'))
            })
            if ($anchor.Count -ge 1) {
                $anchorEntry = $anchor[0]
                # The migration anchor must stay strictly above every other
                # pending version of this session.
                $otherMax = [int64]0
                foreach ($entry in $entries) {
                    if ([string]$entry.Path -ceq [string]$anchorEntry.Path) { continue }
                    if ([int64]$entry.Order -gt $otherMax) { $otherMax = [int64]$entry.Order }
                }
                if ([int64]$anchorEntry.Order -le $otherMax) {
                    $fixed = $otherMax + 1
                    $anchorEntry.Manifest['source_order'] = [int64]$fixed
                    Write-JsonAtomic $anchorEntry.Path $anchorEntry.Manifest
                    $anchorEntry.Order = $fixed
                }
                if ([int64]$anchorEntry.Order -gt $state.MaxOrder) {
                    $state.MaxOrder = [int64]$anchorEntry.Order
                    $state.Dirty = $true
                }
                $state.AnchorPath = [string]$anchorEntry.Path
                $state.AnchorOrder = [int64]$anchorEntry.Order
            }
            else {
                $created = New-QueueAnchorFromCache $Config $state
                if ($null -ne $created) {
                    $state.AnchorPath = [string]$created.Path
                    $state.AnchorOrder = [int64]$created.Order
                }
                else {
                    $state.AnchorError = 'anchor_payload_unavailable'
                    $state.Dirty = $true
                }
            }
        }
    }
    else {
        $cacheName = [string](Get-RecordValue $session 'cache_file' '')
        $declaredBytes = [int64](Get-RecordValue $session 'source_bytes' 0)
        $cacheBytes = $null
        if ($exists -and $cacheName) {
            $cachePath = [IO.Path]::Combine($script:ClientHome, 'session-cache', $cacheName)
            if ([IO.File]::Exists($cachePath)) {
                try { $cacheBytes = Unprotect-Bytes ([IO.File]::ReadAllBytes($cachePath)) }
                catch { $cacheBytes = $null }
            }
        }
        if ($null -ne $cacheBytes -and $cacheBytes.Length -gt 0 -and
            ($declaredBytes -le 0 -or $cacheBytes.Length -eq $declaredBytes)) {
            $sha = Get-Sha256Hex $cacheBytes
            $order = Get-NextSourceOrder $state
            $existing = @($entries | Where-Object {
                [string](Get-RecordValue $_.Manifest 'sha256' '') -ceq $sha
            })
            if ($existing.Count -ge 1) {
                $anchorEntry = $existing[0]
                if ([int64]$anchorEntry.Order -ne $order) {
                    $anchorEntry.Manifest['source_order'] = [int64]$order
                    Write-JsonAtomic $anchorEntry.Path $anchorEntry.Manifest
                    $anchorEntry.Order = $order
                }
                $state.AnchorPath = [string]$anchorEntry.Path
            }
            else {
                $created = New-QueueAnchorFromCache $Config $state $sha $cacheBytes $order
                if ($null -ne $created) { $state.AnchorPath = [string]$created.Path }
            }
            $state.CurrentSha256 = $sha
            $state.SourceOrder = $order
            $state.AnchorOrder = $order
            if ($order -gt $state.MaxOrder) { $state.MaxOrder = $order }
            $state.AnchorError = ''
            $state.Dirty = $true
        }
        elseif ($exists -and ($cacheName -or $entries.Count -gt 0)) {
            $state.AnchorError = 'current_snapshot_unavailable'
            $state.Dirty = $true
        }
    }
    if ($state.Dirty -and $state.Exists) {
        $record = ConvertTo-OrderedRecord $session
        $record['source_order'] = [int64]$state.SourceOrder
        $record['max_source_order'] = [int64]$state.MaxOrder
        $record['current_sha256'] = [string]$state.CurrentSha256
        $record['anchor_error'] = [string]$state.AnchorError
        Write-JsonAtomic $sessionPath $record
        $state.Record = $record
    }
    return $state
}

function Get-SessionMigrationSummary([string]$SessionDir) {
    $unavailable = 0
    $codes = New-Object Collections.Generic.List[string]
    if ([IO.Directory]::Exists($SessionDir)) {
        foreach ($path in @([IO.Directory]::GetFiles($SessionDir, '*.json'))) {
            try {
                $code = [string](Get-Value (Read-JsonFile $path) 'anchor_error' '')
                if (-not $code) { continue }
                $unavailable += 1
                if (-not $codes.Contains($code)) { $codes.Add($code) }
            }
            catch { }
        }
    }
    return [ordered]@{
        migration_unavailable_sessions = $unavailable
        migration_error_codes = @($codes | Sort-Object)
    }
}

function Invoke-UploadQueue($Config) {
    $queueDir = [IO.Path]::Combine($script:ClientHome, 'queue')
    $statusDir = [IO.Path]::Combine($script:ClientHome, 'archive-status')
    Ensure-Directory $queueDir
    Ensure-Directory $statusDir
    $uploaded = 0
    # Prepare every queued session's local source ordering and migration anchor
    # before the first upload, so an old fork can never reach the server ahead
    # of the local current snapshot.
    $sessionIds = New-Object Collections.Generic.List[string]
    foreach ($manifestPath in @([IO.Directory]::GetFiles($queueDir, '*.json'))) {
        try {
            $sessionId = [string](Get-Value (Read-JsonFile $manifestPath) 'session_id' '')
            if ($sessionId -and -not $sessionIds.Contains($sessionId)) { $sessionIds.Add($sessionId) }
        }
        catch { }
    }
    foreach ($sessionId in $sessionIds) {
        try {
            [void](Invoke-WithClientLock {
                Initialize-SessionSourceState $Config $sessionId
            } 5000 (Get-SessionLockScope $sessionId))
        }
        catch { }
    }
    # Reload the prepared queue. A session's anchor uploads before its older
    # versions; an unacknowledged anchor defers those older versions.
    $entries = New-Object Collections.Generic.List[object]
    foreach ($manifestPath in @([IO.Directory]::GetFiles($queueDir, '*.json'))) {
        try {
            $manifest = ConvertTo-QueueManifest (Read-JsonFile $manifestPath)
            $sessionId = [string](Get-RecordValue $manifest 'session_id' '')
            if (-not $sessionId) { continue }
            $stem = [IO.Path]::GetFileNameWithoutExtension($manifestPath)
            $dataPath = [IO.Path]::Combine($queueDir, $stem + '.bin')
            if (-not [IO.File]::Exists($dataPath)) { continue }
            $session = ConvertTo-OrderedRecord (Read-JsonFile (Get-SessionPath $sessionId))
            $currentSha = [string](Get-RecordValue $session 'current_sha256' '')
            $order = [int64](Get-RecordValue $manifest 'source_order' 0)
            $confirmed = [string](Get-RecordValue $session 'anchor_confirmed_sha256' '')
            $entries.Add([ordered]@{
                Path = $manifestPath; Manifest = $manifest; DataPath = $dataPath
                SessionId = $sessionId; SessionScope = (Get-SessionLockScope $sessionId)
                TotalBytes = [int64](Get-RecordValue $manifest 'total_bytes' 0)
                IsAnchor = ($order -gt 0 -and $currentSha -and
                    ($currentSha -ceq [string](Get-RecordValue $manifest 'sha256' '')) -and
                    ($confirmed -cne $currentSha))
            })
        }
        catch { }
    }
    $ordered = @($entries.ToArray() | Sort-Object `
        @{ Expression = { if ($_.IsAnchor) { 0 } else { 1 } } }, `
        @{ Expression = { [int64]$_.TotalBytes } })
    $blocked = @{}
    foreach ($entry in $ordered) {
        $sessionId = [string]$entry.SessionId
        if ($blocked.ContainsKey($sessionId)) { continue }
        $manifest = $entry.Manifest
        $manifestPath = [string]$entry.Path
        $dataPath = [string]$entry.DataPath
        $sessionPath = Get-SessionPath $sessionId
        $sessionScope = [string]$entry.SessionScope
        $isAnchor = [bool]$entry.IsAnchor
        $acknowledged = $false
        $failureStage = 'decrypt'
        try {
            $plain = Unprotect-Bytes ([IO.File]::ReadAllBytes($dataPath))
            $failureStage = 'checksum'
            $sha = [string](Get-RecordValue $manifest 'sha256' '')
            if ($plain.Length -ne [int64](Get-RecordValue $manifest 'total_bytes' 0) -or
                (Get-Sha256Hex $plain) -cne $sha) {
                # The local payload is unreadable: keep it queued and never let
                # older versions of this session overtake it.
                if ($isAnchor) { $blocked[$sessionId] = $true }
                continue
            }
            # The current declaration comes from the locally persisted current
            # sha at upload time, never from the server or a queued version.
            $session = ConvertTo-OrderedRecord (Read-JsonFile $sessionPath)
            $currentSha = [string](Get-RecordValue $session 'current_sha256' '')
            $order = [int64](Get-RecordValue $manifest 'source_order' 0)
            $claimsCurrent = $order -gt 0 -and $currentSha -and $currentSha -ceq $sha
            $offset = [int64](Get-RecordValue $manifest 'next_offset' 0)
            $sentChunks = 0
            # A large historical version must not monopolize the whole worker.
            # Its confirmed offset is durable; the next tick continues it.
            while ($offset -lt $plain.Length -and $sentChunks -lt 16) {
                $length = [Math]::Min($script:ChunkBytes, $plain.Length - $offset)
                $chunk = New-Object byte[] ([int]$length)
                [Array]::Copy($plain, $offset, $chunk, 0, $length)
                $sessionHash = Get-TextSha256 $sessionId
                $requestId = 'archive-' + $sessionHash.Substring(0, 12) + '-' +
                    $sha.Substring(0, 24) + '-' + $offset
                $arguments = [ordered]@{
                    device_id = [string]$Config.device_id; session_id = $sessionId
                    project = [string](Get-RecordValue $manifest 'project' ''); sha256 = $sha
                    total_bytes = [int64](Get-RecordValue $manifest 'total_bytes' 0); offset = $offset
                    content_b64 = [Convert]::ToBase64String($chunk)
                    extract = [bool](Get-RecordValue $manifest 'extract' $false)
                    request_id = $requestId
                }
                if ($order -gt 0) { $arguments['source_order'] = [int64]$order }
                if ($claimsCurrent) { $arguments['current_sha256'] = $sha }
                $failureStage = 'upload'
                $response = Invoke-McpTool $Config 'session_archive_upload' $arguments $requestId
                $failureStage = 'receipt'
                $sentChunks += 1
                $remoteStatus = [string](Get-Value $response 'status' '')
                if ($remoteStatus -eq 'archived' -or $remoteStatus -eq 'stale') {
                    if ($remoteStatus -eq 'archived') {
                        $next = [int64](Get-Value $response 'next_offset' -1)
                        if ($next -ne $plain.Length) { throw 'archive terminal acknowledgement is incomplete' }
                        if ([string](Get-Value $response 'source_sha256' '') -cne $sha) {
                            throw 'archive checksum acknowledgement mismatch'
                        }
                        if ($claimsCurrent -and -not (Test-ArchiveCurrentConfirmation $response $order $sha)) {
                            # The service stored this payload as history, or an
                            # older head kept its order, or no archive id came
                            # back. Keep the queue and the unconfirmed anchor
                            # instead of silently declaring the local current
                            # snapshot synchronized.
                            throw (New-ClientError 'anchor_not_current' 'archive did not confirm this snapshot as current')
                        }
                    }
                    else {
                        if ([int64](Get-Value $response 'submitted_total_bytes' -1) -ne $plain.Length -or
                            [string](Get-Value $response 'submitted_sha256' '') -cne $sha) {
                            throw 'stale archive acknowledgement mismatch'
                        }
                        if ($claimsCurrent) {
                            # A stale receipt belongs to another version, so the
                            # local current snapshot was not accepted as head.
                            throw (New-ClientError 'anchor_not_current' 'archive kept a different current snapshot')
                        }
                    }
                    $receipt = [ordered]@{
                        session_id = $sessionId; sha256 = $sha
                        archive_status = $remoteStatus; archive_id = [string](Get-Value $response 'archive_id' '')
                        extraction_status = [string](Get-Value $response 'extraction_status' 'pending')
                        acknowledged_utc = [DateTime]::UtcNow.ToString('o')
                    }
                    $failureStage = 'local_commit'
                    Invoke-WithClientLock {
                        Write-JsonAtomic ([IO.Path]::Combine($statusDir, ($sha + '.json'))) $receipt
                        if ($claimsCurrent) {
                            # The new protocol archived this version with its
                            # source order, so the one-time anchor is complete.
                            $latest = ConvertTo-OrderedRecord (Read-JsonFile $sessionPath)
                            if ([string](Get-RecordValue $latest 'current_sha256' '') -ceq $sha) {
                                $latest['anchor_confirmed_sha256'] = $sha
                                $latest['anchor_confirmed_utc'] = [DateTime]::UtcNow.ToString('o')
                                $latest['anchor_error'] = ''
                                Write-JsonAtomic $sessionPath $latest
                            }
                        }
                        [IO.File]::Delete($dataPath)
                        [IO.File]::Delete($manifestPath)
                    } 5000 $sessionScope
                    $uploaded += 1
                    $acknowledged = $true
                    break
                }
                if ($remoteStatus -ne 'receiving') { throw 'archive acknowledgement status is invalid' }
                $next = [int64](Get-Value $response 'next_offset' -1)
                if ($next -ne $offset + $length) { throw 'archive acknowledgement offset mismatch' }
                $offset = $next
                $manifest['next_offset'] = $offset
                Invoke-WithClientLock { Write-JsonAtomic $manifestPath $manifest } 5000 $sessionScope
            }
        }
        catch {
            # Unacknowledged data and its stable request position remain queued.
            # Record only a bounded stable code and the failure time so the
            # stuck version stays diagnosable without persisting remote text.
            try {
                if ($null -ne $manifest) {
                    $manifest['last_error'] = Get-ClientErrorCode $_
                    $manifest['last_error_utc'] = [DateTime]::UtcNow.ToString('o')
                    $manifest['last_error_stage'] = $failureStage
                    $manifest['last_error_type'] = $_.Exception.GetType().Name
                    Invoke-WithClientLock { Write-JsonAtomic $manifestPath $manifest } 5000 $sessionScope
                }
            }
            catch { }
        }
        if (-not $acknowledged -and $isAnchor) { $blocked[$sessionId] = $true }
    }
    return $uploaded
}

function Get-CodexRoot($Config) {
    $configured = [string](Get-Value $Config 'codex_root' '')
    if ($configured) { return $configured }
    if ($env:CODEX_HOME) { return [IO.Path]::GetFullPath($env:CODEX_HOME) }
    if ($env:USERPROFILE) { return [IO.Path]::Combine($env:USERPROFILE, '.codex') }
    return ''
}

function Find-LocalSessions($Config) {
    $found = 0; $errors = 0
    $root = Get-CodexRoot $Config
    $sinceText = [string](Get-Value $Config 'capture_since_utc' '')
    # Upgrades set the cutoff explicitly. Older clients still capture their
    # registered sessions without silently importing unrelated history.
    if (-not $root -or -not $sinceText) { return @{ Found = 0; Errors = 0 } }
    $since = [DateTimeOffset]::Parse($sinceText).UtcDateTime
    foreach ($folder in @('sessions', 'archived_sessions')) {
        $directory = [IO.Path]::Combine($root, $folder)
        if (-not [IO.Directory]::Exists($directory)) { continue }
        try { $paths = [IO.Directory]::GetFiles($directory, '*.jsonl', [IO.SearchOption]::AllDirectories) }
        catch { $errors += 1; continue }
        foreach ($path in $paths) {
            try {
                $stream = New-Object IO.FileStream($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
                try {
                    $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8, $true)
                    try { $line = $reader.ReadLine() }
                    finally { $reader.Dispose() }
                }
                finally { $stream.Dispose() }
                if (-not $line) { continue }
                $header = $line | ConvertFrom-Json
                if ([string](Get-Value $header 'type' '') -ne 'session_meta') { continue }
                $payload = Get-Value $header 'payload'
                $sessionId = [string](Get-Value $payload 'id' '')
                $cwd = [string](Get-Value $payload 'cwd' '')
                if (-not $sessionId -or -not $cwd) { continue }
                $registered = [IO.File]::Exists((Get-SessionPath $sessionId))
                if (-not $registered -and [IO.File]::GetLastWriteTimeUtc($path) -lt $since) { continue }
                $event = [pscustomobject]@{ session_id = $sessionId; transcript_path = $path; cwd = $cwd }
                [void](Invoke-WithClientLock { Save-SessionRegistration $Config $event } 1000 (Get-SessionLockScope $sessionId))
                if (-not $registered) { $found += 1 }
            }
            catch { $errors += 1 }
        }
    }
    return @{ Found = $found; Errors = $errors }
}

function Invoke-Worker($Config) {
    $started = [DateTime]::UtcNow.ToString('o')
    $scanned = 0
    $captureErrors = 0
    $captureFailures = New-Object Collections.Generic.List[object]
    $sessionDir = [IO.Path]::Combine($script:ClientHome, 'sessions')
    Ensure-Directory $sessionDir
    $discovery = Find-LocalSessions $Config
    foreach ($path in @([IO.Directory]::GetFiles($sessionDir, '*.json'))) {
        $session = $null
        try {
            $session = Read-JsonFile $path
            $event = [pscustomobject]@{
                session_id = [string]$session.session_id; transcript_path = [string]$session.transcript_path
                cwd = [string]$session.workspace_path
            }
            $scope = Get-SessionLockScope ([string]$session.session_id)
            if (Invoke-WithClientLock { Capture-Transcript $Config $event } 5000 $scope) { $scanned += 1 }
        }
        catch {
            $captureErrors += 1
            if ($captureFailures.Count -lt 10) {
                $captureFailures.Add([ordered]@{
                    session_id = [string](Get-Value $session 'session_id' '')
                    error_type = $_.Exception.GetType().FullName
                    inner_type = if ($_.Exception.InnerException) { $_.Exception.InnerException.GetType().FullName } else { '' }
                    line = $_.InvocationInfo.ScriptLineNumber
                    category = [string]$_.CategoryInfo.Category
                })
            }
        }
    }
    # Background work can wait for a large encrypted staging update; hooks
    # retain their short request timeout.
    $script:RpcTimeoutSeconds = 30
    try { $acknowledged = Invoke-WithClientLock { Invoke-UploadQueue $Config } 100 'upload' }
    catch { $acknowledged = 0 }
    $pending = @([IO.Directory]::GetFiles([IO.Path]::Combine($script:ClientHome, 'queue'), '*.json')).Count
    $queueFailures = Get-QueueFailureSummary ([IO.Path]::Combine($script:ClientHome, 'queue'))
    $migration = Get-SessionMigrationSummary $sessionDir
    $status = [ordered]@{
        worker_status = if ($pending -gt 0 -or $captureErrors -gt 0 -or $discovery.Errors -gt 0) { 'retry_pending' } else { 'idle' }
        last_started_utc = $started; last_finished_utc = [DateTime]::UtcNow.ToString('o')
        discovered_sessions = $discovery.Found; discovery_errors = $discovery.Errors; capture_errors = $captureErrors
        capture_failures = $captureFailures.ToArray()
        captured_versions = $scanned; acknowledged_versions = $acknowledged; pending_versions = $pending
        pending_failed_versions = $queueFailures.pending_failed_versions
        pending_error_codes = $queueFailures.pending_error_codes
        migration_unavailable_sessions = $migration.migration_unavailable_sessions
        migration_error_codes = $migration.migration_error_codes
    }
    Write-JsonAtomic ([IO.Path]::Combine($script:ClientHome, 'worker-status.json')) $status
    Write-OutputJson $status
}

function Get-WorkerTaskStatus {
    $launcher = [IO.Path]::Combine($script:ClientHome, 'evolvmem-sync.exe')
    $result = [ordered]@{
        worker_task_configured = $null; worker_task_enabled = $null; worker_task_state = 'not_windows'
        worker_task_last_result = $null; worker_task_last_run = $null; worker_task_next_run = $null
        windowless_launcher_available = [IO.File]::Exists($launcher)
    }
    if (-not $script:RunningOnWindows) { return $result }
    $result.worker_task_configured = $false; $result.worker_task_enabled = $false
    $result.worker_task_state = 'missing'
    try {
        $task = Get-ScheduledTask -TaskName 'EvolvMem Codex Sync' -ErrorAction Stop
        $actions = @($task.Actions)
        $result.worker_task_configured = $actions.Count -eq 1 -and
            ([string]$actions[0].Execute).Trim('"') -ieq $launcher -and
            -not [string]$actions[0].Arguments -and $result.windowless_launcher_available
        $result.worker_task_enabled = [bool]$task.Settings.Enabled
        $result.worker_task_state = [string]$task.State
        $info = Get-ScheduledTaskInfo -InputObject $task -ErrorAction Stop
        $result.worker_task_last_result = [int64]$info.LastTaskResult
        $result.worker_task_last_run = $info.LastRunTime.ToUniversalTime().ToString('o')
        $result.worker_task_next_run = $info.NextRunTime.ToUniversalTime().ToString('o')
    }
    catch { }
    return $result
}

function Get-LocalStatus($Config) {
    $queueDir = [IO.Path]::Combine($script:ClientHome, 'queue')
    $archiveDir = [IO.Path]::Combine($script:ClientHome, 'archive-status')
    $sessionDir = [IO.Path]::Combine($script:ClientHome, 'sessions')
    Ensure-Directory $queueDir; Ensure-Directory $archiveDir; Ensure-Directory $sessionDir
    $pending = @([IO.Directory]::GetFiles($queueDir, '*.json')).Count
    $failures = Get-QueueFailureSummary $queueDir
    $migration = Get-SessionMigrationSummary $sessionDir
    $archived = 0; $pendingExtraction = 0; $extracted = 0
    foreach ($path in @([IO.Directory]::GetFiles($archiveDir, '*.json'))) {
        $receipt = Read-JsonFile $path
        if ([string](Get-Value $receipt 'archive_status' '') -in @('archived', 'stale')) { $archived += 1 }
        $state = [string](Get-Value $receipt 'extraction_status' '')
        if ($state -in @('pending', 'queued', 'processing')) { $pendingExtraction += 1 }
        elseif ($state -in @('completed', 'extracted')) { $extracted += 1 }
    }
    $result = [ordered]@{
        pending_archive_versions = $pending; acknowledged_archive_versions = $archived
        pending_extractions = $pendingExtraction; completed_extractions = $extracted
        pending_failed_versions = $failures.pending_failed_versions
        pending_error_codes = $failures.pending_error_codes
        migration_unavailable_sessions = $migration.migration_unavailable_sessions
        migration_error_codes = $migration.migration_error_codes
        registered_sessions = @([IO.Directory]::GetFiles($sessionDir, '*.json')).Count
    }
    $taskStatus = Get-WorkerTaskStatus
    foreach ($key in $taskStatus.Keys) { $result[$key] = $taskStatus[$key] }
    foreach ($name in @('worker', 'launcher')) {
        try { $result[$name] = Read-JsonFile ([IO.Path]::Combine($script:ClientHome, $name + '-status.json')) }
        catch { $result[$name] = @{ error = 'status_receipt_unreadable' } }
    }
    return $result
}

function Invoke-Status($Config) {
    $local = Get-LocalStatus $Config
    try {
        $remote = Invoke-McpTool $Config 'memory_status' @{}
        $active = Get-Value $remote 'active_memories'
        $memoryState = if ($null -ne $active -and [int64]$active -eq 0) { 'empty' } else { 'available' }
        $output = [ordered]@{
            connected = $true; authenticated_user = [string]$remote.authenticated_user
            active_memories = $active; memory_state = $memoryState
            memory_revision = Get-Value $remote 'memory_revision'
            device_id = [string]$Config.device_id
        }
        $archiveSessions = New-Object Collections.Generic.List[object]
        $sessionDir = [IO.Path]::Combine($script:ClientHome, 'sessions')
        foreach ($path in @([IO.Directory]::GetFiles($sessionDir, '*.json'))) {
            $session = Read-JsonFile $path
            try {
                $archive = Invoke-McpTool $Config 'session_archive_status' ([ordered]@{
                    device_id = [string]$Config.device_id
                    session_id = [string]$session.session_id
                })
                $archiveSessions.Add([ordered]@{
                    session_id = [string]$session.session_id
                    archive_status = [string](Get-Value $archive 'status' '')
                    archive_id = [string](Get-Value $archive 'archive_id' '')
                    source_sha256 = [string](Get-Value $archive 'source_sha256' '')
                    extraction_status = [string](Get-Value $archive 'extraction_status' '')
                    processing_error = [string](Get-Value $archive 'processing_error' '')
                })
            }
            catch {
                $archiveSessions.Add([ordered]@{ session_id = [string]$session.session_id; archive_status = 'unavailable' })
            }
        }
        $output.archive_sessions = $archiveSessions.ToArray()
        foreach ($key in $local.Keys) { $output[$key] = $local[$key] }
        Write-OutputJson $output
    }
    catch {
        $output = [ordered]@{ connected = $false; authenticated_user = ''; memory_state = 'unavailable'; device_id = [string]$Config.device_id }
        foreach ($key in $local.Keys) { $output[$key] = $local[$key] }
        Write-OutputJson $output
    }
}

function Invoke-SelfTest($Config) {
    $required = @(
        'memory_search', 'memory_status', 'memory_add', 'memory_replace', 'memory_remove', 'memory_consolidate',
        'memory_publish', 'memory_update_public', 'memory_unpublish',
        'context_session_start', 'context_search', 'context_read', 'context_status', 'context_confirm',
        'context_record_outcome', 'context_archive_project', 'context_sweep', 'experience_recall',
        'experience_record', 'continuity_begin', 'continuity_resume', 'continuity_find', 'continuity_bind',
        'continuity_checkpoint', 'continuity_list', 'project_board_sync', 'project_board_status',
        'session_archive_upload', 'session_archive_status', 'session_archive_retry', 'session_archive_assign'
    )
    # Newer capabilities are reported separately. A server that has not
    # deployed them yet must not turn the existing required-tool health contract
    # unhealthy, but self-test must still make the gap visible.
    $optional = @('context_project_recall')
    $mcpConfigured = $false
    $hooksConfigured = $false
    $hooksFeatureEnabled = $true
    $invalidHooks = New-Object Collections.Generic.List[string]
    $taskStatus = Get-WorkerTaskStatus
    $workerTaskConfigured = $taskStatus.worker_task_configured
    $codexRoot = Get-CodexRoot $Config
    try {
        if ($codexRoot) {
            $tomlPath = [IO.Path]::Combine($codexRoot, 'config.toml')
            if ([IO.File]::Exists($tomlPath)) {
                $toml = [IO.File]::ReadAllText($tomlPath, [Text.Encoding]::UTF8)
                $features = [regex]::Match(
                    $toml, '(?ms)^[ \t]*\[features\][ \t]*\r?\n(?<body>.*?)(?=^[ \t]*\[|\z)')
                if ($features.Success) {
                    $featureBody = $features.Groups['body'].Value
                    $hooksFlag = [regex]::Match(
                        $featureBody,
                        '(?mi)^[ \t]*hooks[ \t]*=[ \t]*(?<value>true|false)[ \t]*(?:#.*)?\r?$')
                    if (-not $hooksFlag.Success) {
                        $hooksFlag = [regex]::Match(
                            $featureBody,
                            '(?mi)^[ \t]*codex_hooks[ \t]*=[ \t]*(?<value>true|false)[ \t]*(?:#.*)?\r?$')
                    }
                    if ($hooksFlag.Success) {
                        $hooksFeatureEnabled = $hooksFlag.Groups['value'].Value -ine 'false'
                    }
                }
                $sections = [regex]::Matches(
                    $toml,
                    '(?ms)^[ \t]*\[mcp_servers\.evolvmem\][ \t]*(?:#[^\r\n]*)?\r?\n' +
                    '(?<body>.*?)(?=^[ \t]*\[|\z)')
                if ($sections.Count -eq 1) {
                    $body = $sections[0].Groups['body'].Value
                    $url = [regex]::Match($body, '(?m)^\s*url\s*=\s*"(?<value>(?:\\.|[^"])*)"\s*$')
                    $token = [regex]::Match(
                        $body, '(?m)^\s*bearer_token_env_var\s*=\s*"(?<value>(?:\\.|[^"])*)"\s*$')
                    $expected = [regex]::Match(
                        $body,
                        '(?m)^[ \t]*http_headers[ \t]*=[ \t]*\{[^\r\n}]*' +
                        '(?:"X-EvolvMem-Expected-User"|X-EvolvMem-Expected-User)' +
                        '[ \t]*=[ \t]*"(?<value>(?:\\.|[^"])*)"[^\r\n}]*\}' +
                        '[ \t]*(?:#[^\r\n]*)?\r?$')
                    if (-not $expected.Success) {
                        $headerSection = [regex]::Match(
                            $toml,
                            '(?ms)^[ \t]*\[mcp_servers\.evolvmem\.http_headers\][ \t]*' +
                            '(?:#[^\r\n]*)?\r?\n(?<body>.*?)(?=^[ \t]*\[|\z)')
                        if ($headerSection.Success) {
                            $expected = [regex]::Match(
                                $headerSection.Groups['body'].Value,
                                '(?mi)^[ \t]*(?:"X-EvolvMem-Expected-User"|' +
                                'X-EvolvMem-Expected-User)[ \t]*=[ \t]*' +
                                '"(?<value>(?:\\.|[^"])*)"[ \t]*(?:#[^\r\n]*)?\r?$')
                        }
                    }
                    $mcpConfigured = $url.Success -and $token.Success -and $expected.Success -and
                        $url.Groups['value'].Value -ceq [string]$Config.url -and
                        $token.Groups['value'].Value -ceq [string]$Config.token_env_var -and
                        $expected.Groups['value'].Value -ceq [string]$Config.expected_user
                }
            }
        }
    }
    catch { $mcpConfigured = $false }
    try {
        if ($codexRoot) {
            $hooksPath = [IO.Path]::Combine($codexRoot, 'hooks.json')
            $hooksDocument = Read-JsonFile $hooksPath
            $hookRoot = Get-Value $hooksDocument 'hooks'
            $requirements = [ordered]@{
                SessionStart = 'session-start'; UserPromptSubmit = 'prompt-submit'
                Stop = 'snapshot'; PreCompact = 'snapshot'; SessionEnd = 'snapshot'; Interrupt = 'snapshot'
            }
            $scriptPattern = [regex]::Escape([string]$PSCommandPath)
            foreach ($entry in $requirements.GetEnumerator()) {
                $matching = New-Object Collections.Generic.List[object]
                $eventProperty = if ($null -ne $hookRoot) { $hookRoot.PSObject.Properties[$entry.Key] } else { $null }
                if ($null -ne $eventProperty) {
                    foreach ($group in @($eventProperty.Value)) {
                        foreach ($handler in @(Get-Value $group 'hooks' @())) {
                            $command = [string](Get-Value $handler 'command' '')
                            if ([string](Get-Value $handler 'type' '') -eq 'command' -and
                                $command -match $scriptPattern -and
                                $command -match ('(?i)(?:^|\s)-Action\s+' + [regex]::Escape($entry.Value) + '(?:\s|$)')) {
                                $matching.Add([pscustomobject]@{ Group = $group; Handler = $handler })
                            }
                        }
                    }
                }
                $valid = $matching.Count -eq 1
                if ($valid -and $entry.Key -eq 'SessionStart') {
                    $matcher = [string](Get-Value $matching[0].Group 'matcher' '')
                    foreach ($source in @('startup', 'resume', 'clear', 'compact')) {
                        if ($matcher -and $source -notmatch $matcher) { $valid = $false }
                    }
                }
                if (-not $valid) { $invalidHooks.Add($entry.Key + ':' + $entry.Value) }
            }
            $hooksConfigured = $invalidHooks.Count -eq 0
        }
        else {
            $invalidHooks.Add('user-profile-unavailable')
        }
    }
    catch {
        $invalidHooks.Clear()
        $invalidHooks.Add('hooks-config-unreadable')
        $hooksConfigured = $false
    }

    $remoteConnected = $false
    $authenticatedUser = ''
    $names = @()
    $missing = $required
    $missingOptional = $optional
    try {
        $status = Invoke-McpTool $Config 'memory_status' @{}
        $listed = Invoke-Rpc $Config 'tools/list' @{} ([Guid]::NewGuid().ToString('N'))
        $names = @(@(Get-Value $listed 'tools' @()) | ForEach-Object { [string](Get-Value $_ 'name' '') })
        $missing = @($required | Where-Object { $_ -notin $names })
        $missingOptional = @($optional | Where-Object { $_ -notin $names })
        $authenticatedUser = [string]$status.authenticated_user
        $remoteConnected = $true
    }
    catch { }
    $healthy = $remoteConnected -and $missing.Count -eq 0 -and $mcpConfigured -and
        $hooksConfigured -and $hooksFeatureEnabled
    if ($script:RunningOnWindows) { $healthy = $healthy -and [bool]$workerTaskConfigured -and $taskStatus.worker_task_enabled }
    $note = if ($remoteConnected) {
        'Self-test checks configuration and remote MCP only. Review/trust hooks in Codex CLI /hooks; Windows desktop may not show a review prompt. Native hook delivery and DPAPI/task execution require acceptance testing.'
    } else {
        'EvolvMem self-test could not reach the authenticated MCP.'
    }
    Write-OutputJson ([ordered]@{
        healthy = $healthy; remote_connected = $remoteConnected; authenticated_user = $authenticatedUser
        device_id = [string]$Config.device_id; missing_tools = $missing; tool_count = $names.Count
        missing_optional_tools = $missingOptional
        mcp_configured = $mcpConfigured; hooks_configured = $hooksConfigured
        hooks_feature_enabled = $hooksFeatureEnabled
        invalid_hooks = $invalidHooks.ToArray(); worker_task_configured = $workerTaskConfigured
        worker_task_enabled = $taskStatus.worker_task_enabled; worker_task_state = $taskStatus.worker_task_state
        windowless_launcher_available = $taskStatus.windowless_launcher_available
        self_test_scope = 'configuration_and_remote_mcp'; hook_trust_checked = $false
        native_event_test_required = $true; note = $note
    })
}

try {
    Ensure-Directory $script:ClientHome
    $config = Get-Config
    switch ($Action) {
        'session-start' { Invoke-SessionStart $config (Get-EventInput) }
        'prompt-submit' { Invoke-PromptSubmit $config (Get-EventInput) }
        'snapshot' { Invoke-Snapshot $config (Get-EventInput) }
        'worker' { Invoke-Worker $config }
        'upload' { Write-OutputJson @{ acknowledged_versions = (Invoke-WithClientLock { Invoke-UploadQueue $config } 100 'upload') } }
        'status' { Invoke-Status $config }
        'self-test' { Invoke-SelfTest $config }
    }
}
catch {
    if ($Action -eq 'session-start' -or $Action -eq 'prompt-submit') {
        Write-OutputJson @{ systemMessage = 'EvolvMem is unavailable; Codex will continue without refreshed memory.' }
    }
    elseif ($Action -eq 'snapshot') {
        Write-OutputJson @{ systemMessage = 'EvolvMem could not queue the latest local transcript snapshot.' }
    }
    else {
        Write-OutputJson @{ healthy = $false; error = 'EvolvMem client action failed.' }
    }
}

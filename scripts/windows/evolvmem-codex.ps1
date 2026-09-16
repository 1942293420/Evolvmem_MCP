[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('session-start', 'prompt-submit', 'snapshot', 'worker', 'upload', 'status', 'self-test')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'
$script:MaxContextChars = 8000
$script:ConnectionMetadataReserveChars = 1600
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
    if ($null -ne (Get-Value $rpc 'error')) { throw 'remote MCP request failed' }
    return Get-Value $rpc 'result'
}

function Convert-McpToolResult($Config, $Result) {
    $result = $Result
    if ([bool](Get-Value $result 'isError' $false)) { throw 'remote MCP tool failed' }
    $content = @(Get-Value $result 'content' @())
    if ($content.Count -lt 1 -or (Get-Value $content[0] 'type' '') -ne 'text') {
        throw 'remote MCP returned an invalid result'
    }
    $value = ([string](Get-Value $content[0] 'text' '')) | ConvertFrom-Json
    $actual = [string](Get-Value $value 'authenticated_user' '')
    if (-not $actual -or $actual -cne [string]$Config.expected_user) {
        throw 'EvolvMem authenticated user does not match this installation'
    }
    if (Get-Value $value 'error') { throw 'remote MCP tool failed' }
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

function Get-InjectionArguments($Config, $Event, [string]$Query) {
    $cwd = [string](Get-Value $Event 'cwd' '')
    return [ordered]@{
        project = Resolve-Project $Config $cwd
        query = $Query
        workspace_path = $cwd
        device_id = [string]$Config.device_id
        repo_snapshot = Get-RepoSnapshot $cwd
        max_chars = $script:MaxContextChars - $script:ConnectionMetadataReserveChars
    }
}

function Invoke-Injection($Config, $Event, [string]$Query) {
    $arguments = Get-InjectionArguments $Config $Event $Query
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
    $record = [ordered]@{
        version = 1
        session_id = $sessionId
        transcript_path = [string](Get-Value $Event 'transcript_path' (Get-Value $prior 'transcript_path' ''))
        workspace_path = [string](Get-Value $Event 'cwd' (Get-Value $prior 'workspace_path' ''))
        project = Resolve-Project $Config ([string](Get-Value $Event 'cwd' (Get-Value $prior 'workspace_path' '')))
        source_bytes = [int64](Get-Value $prior 'source_bytes' 0)
        source_last_write_utc = [string](Get-Value $prior 'source_last_write_utc' '')
        cache_file = [string](Get-Value $prior 'cache_file' '')
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
            $injection = Invoke-Injection $Config $Event ($(if ($prompt) { $prompt } else { 'Refresh memory for this Codex turn.' }))
            $context = Format-InjectionContext $injection
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
    return [Security.Cryptography.ProtectedData]::Protect(
        $Bytes, $entropy, [Security.Cryptography.DataProtectionScope]::CurrentUser)
}

function Unprotect-Bytes([byte[]]$Bytes) {
    Initialize-DataProtection
    $entropy = [Text.Encoding]::UTF8.GetBytes('evolvmem-codex-archive-v1')
    return [Security.Cryptography.ProtectedData]::Unprotect(
        $Bytes, $entropy, [Security.Cryptography.DataProtectionScope]::CurrentUser)
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
            for ($index = 0; $index -lt $read; $index++) {
                if ($buffer[$index] -ne $Expected[$position + $index]) { return $false }
            }
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
    $session = Read-JsonFile $sessionPath
    $sourceBytes = [int64](Get-Value $session 'source_bytes' 0)
    $cacheName = (Get-TextSha256 $sessionId) + '.bin'
    $cachePath = [IO.Path]::Combine($script:ClientHome, 'session-cache', $cacheName)
    $prior = [byte[]]@()
    if ($sourceBytes -gt 0 -and [IO.File]::Exists($cachePath)) {
        $prior = Unprotect-Bytes ([IO.File]::ReadAllBytes($cachePath))
        if ($prior.Length -ne $sourceBytes) { $sourceBytes = 0; $prior = [byte[]]@() }
    }
    elseif ($sourceBytes -gt 0) { $sourceBytes = 0 }
    $file = New-Object IO.FileInfo($transcriptPath)
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
    if (-not [IO.File]::Exists($queuePath)) { Save-ProtectedBytes $queuePath $combined }
    if (-not [IO.File]::Exists($manifestPath)) {
        Write-JsonAtomic $manifestPath ([ordered]@{
            version = 1; session_id = $sessionId; project = Resolve-Project $Config ([string](Get-Value $Event 'cwd' ''))
            sha256 = $sha; total_bytes = $combined.Length; next_offset = 0; extract = $true
            created_utc = [DateTime]::UtcNow.ToString('o')
        })
    }
    Save-ProtectedBytes $cachePath $combined
    $updated = [ordered]@{}
    foreach ($property in $session.PSObject.Properties) { $updated[$property.Name] = $property.Value }
    $updated.source_bytes = [int64]$tail.EndOffset
    $updated.source_last_write_utc = $file.LastWriteTimeUtc.ToString('o')
    $updated.cache_file = $cacheName
    $updated.last_seen_utc = [DateTime]::UtcNow.ToString('o')
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

function Invoke-UploadQueue($Config) {
    $queueDir = [IO.Path]::Combine($script:ClientHome, 'queue')
    $statusDir = [IO.Path]::Combine($script:ClientHome, 'archive-status')
    Ensure-Directory $queueDir
    Ensure-Directory $statusDir
    $uploaded = 0
    foreach ($manifestPath in @([IO.Directory]::GetFiles($queueDir, '*.json'))) {
        try {
            $manifest = Read-JsonFile $manifestPath
            $sessionScope = Get-SessionLockScope ([string]$manifest.session_id)
            $stem = [IO.Path]::GetFileNameWithoutExtension($manifestPath)
            $dataPath = [IO.Path]::Combine($queueDir, $stem + '.bin')
            if (-not [IO.File]::Exists($dataPath)) { continue }
            $plain = Unprotect-Bytes ([IO.File]::ReadAllBytes($dataPath))
            if ($plain.Length -ne [int64]$manifest.total_bytes -or (Get-Sha256Hex $plain) -cne [string]$manifest.sha256) {
                continue
            }
            $offset = [int64]$manifest.next_offset
            while ($offset -lt $plain.Length) {
                $length = [Math]::Min($script:ChunkBytes, $plain.Length - $offset)
                $chunk = New-Object byte[] ([int]$length)
                [Array]::Copy($plain, $offset, $chunk, 0, $length)
                $sessionHash = Get-TextSha256 ([string]$manifest.session_id)
                $requestId = 'archive-' + $sessionHash.Substring(0, 12) + '-' +
                    ([string]$manifest.sha256).Substring(0, 24) + '-' + $offset
                $arguments = [ordered]@{
                    device_id = [string]$Config.device_id; session_id = [string]$manifest.session_id
                    project = [string]$manifest.project; sha256 = [string]$manifest.sha256
                    total_bytes = [int64]$manifest.total_bytes; offset = $offset
                    content_b64 = [Convert]::ToBase64String($chunk); extract = [bool]$manifest.extract
                    request_id = $requestId
                }
                $response = Invoke-McpTool $Config 'session_archive_upload' $arguments $requestId
                $remoteStatus = [string](Get-Value $response 'status' '')
                if ($remoteStatus -eq 'archived' -or $remoteStatus -eq 'stale') {
                    if ($remoteStatus -eq 'archived') {
                        $next = [int64](Get-Value $response 'next_offset' -1)
                        if ($next -ne $plain.Length) { throw 'archive terminal acknowledgement is incomplete' }
                        if ([string](Get-Value $response 'source_sha256' '') -cne [string]$manifest.sha256) {
                            throw 'archive checksum acknowledgement mismatch'
                        }
                    }
                    else {
                        if ([int64](Get-Value $response 'submitted_total_bytes' -1) -ne $plain.Length -or
                            [string](Get-Value $response 'submitted_sha256' '') -cne [string]$manifest.sha256) {
                            throw 'stale archive acknowledgement mismatch'
                        }
                    }
                    $receipt = [ordered]@{
                        session_id = [string]$manifest.session_id; sha256 = [string]$manifest.sha256
                        archive_status = $remoteStatus; archive_id = [string](Get-Value $response 'archive_id' '')
                        extraction_status = [string](Get-Value $response 'extraction_status' 'pending')
                        acknowledged_utc = [DateTime]::UtcNow.ToString('o')
                    }
                    Invoke-WithClientLock {
                        Write-JsonAtomic ([IO.Path]::Combine($statusDir, ([string]$manifest.sha256) + '.json')) $receipt
                        [IO.File]::Delete($dataPath)
                        [IO.File]::Delete($manifestPath)
                    } 5000 $sessionScope
                    $uploaded += 1
                    break
                }
                if ($remoteStatus -ne 'receiving') { throw 'archive acknowledgement status is invalid' }
                $next = [int64](Get-Value $response 'next_offset' -1)
                if ($next -ne $offset + $length) { throw 'archive acknowledgement offset mismatch' }
                $offset = $next
                $manifest.next_offset = $offset
                Invoke-WithClientLock { Write-JsonAtomic $manifestPath $manifest } 5000 $sessionScope
            }
        }
        catch {
            # Unacknowledged data and its stable request position remain queued.
            continue
        }
    }
    return $uploaded
}

function Invoke-Worker($Config) {
    $scanned = 0
    $sessionDir = [IO.Path]::Combine($script:ClientHome, 'sessions')
    Ensure-Directory $sessionDir
    foreach ($path in @([IO.Directory]::GetFiles($sessionDir, '*.json'))) {
        try {
            $session = Read-JsonFile $path
            $event = [pscustomobject]@{
                session_id = [string]$session.session_id; transcript_path = [string]$session.transcript_path
                cwd = [string]$session.workspace_path
            }
            $scope = Get-SessionLockScope ([string]$session.session_id)
            if (Invoke-WithClientLock { Capture-Transcript $Config $event } 5000 $scope) { $scanned += 1 }
        }
        catch { continue }
    }
    try { $acknowledged = Invoke-WithClientLock { Invoke-UploadQueue $Config } 100 'upload' }
    catch { $acknowledged = 0 }
    $pending = @([IO.Directory]::GetFiles([IO.Path]::Combine($script:ClientHome, 'queue'), '*.json')).Count
    Write-OutputJson @{ worker_status = 'idle'; captured_versions = $scanned; acknowledged_versions = $acknowledged; pending_versions = $pending }
}

function Get-LocalStatus($Config) {
    $queueDir = [IO.Path]::Combine($script:ClientHome, 'queue')
    $archiveDir = [IO.Path]::Combine($script:ClientHome, 'archive-status')
    $sessionDir = [IO.Path]::Combine($script:ClientHome, 'sessions')
    Ensure-Directory $queueDir; Ensure-Directory $archiveDir; Ensure-Directory $sessionDir
    $pending = @([IO.Directory]::GetFiles($queueDir, '*.json')).Count
    $archived = 0; $pendingExtraction = 0; $extracted = 0
    foreach ($path in @([IO.Directory]::GetFiles($archiveDir, '*.json'))) {
        $receipt = Read-JsonFile $path
        if ([string](Get-Value $receipt 'archive_status' '') -in @('archived', 'stale')) { $archived += 1 }
        $state = [string](Get-Value $receipt 'extraction_status' '')
        if ($state -in @('pending', 'queued', 'processing')) { $pendingExtraction += 1 }
        elseif ($state -in @('completed', 'extracted')) { $extracted += 1 }
    }
    return [ordered]@{
        pending_archive_versions = $pending; acknowledged_archive_versions = $archived
        pending_extractions = $pendingExtraction; completed_extractions = $extracted
        registered_sessions = @([IO.Directory]::GetFiles($sessionDir, '*.json')).Count
    }
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
    $mcpConfigured = $false
    $hooksConfigured = $false
    $hooksFeatureEnabled = $true
    $invalidHooks = New-Object Collections.Generic.List[string]
    $workerTaskConfigured = $null
    $codexRoot = if ($env:CODEX_HOME) {
        [IO.Path]::GetFullPath($env:CODEX_HOME)
    } elseif ($env:USERPROFILE) {
        [IO.Path]::Combine($env:USERPROFILE, '.codex')
    } else { '' }
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

    try {
        if ($script:RunningOnWindows) {
            $taskXml = @(& schtasks.exe /Query /TN 'EvolvMem Codex Sync' /XML 2>$null) -join "`n"
            if ($LASTEXITCODE -eq 0) {
                $parsedTask = [xml]$taskXml
                $exec = $parsedTask.Task.Actions.Exec
                $taskCommand = ([string]$exec.Command) + ' ' + ([string]$exec.Arguments)
                $workerTaskConfigured = $taskCommand -match [regex]::Escape([string]$PSCommandPath) -and
                    $taskCommand -match '(?i)(?:^|\s)-Action\s+worker(?:\s|$)'
            }
            else { $workerTaskConfigured = $false }
        }
    }
    catch {
        if ($script:RunningOnWindows) { $workerTaskConfigured = $false }
    }

    $remoteConnected = $false
    $authenticatedUser = ''
    $names = @()
    $missing = $required
    try {
        $status = Invoke-McpTool $Config 'memory_status' @{}
        $listed = Invoke-Rpc $Config 'tools/list' @{} ([Guid]::NewGuid().ToString('N'))
        $names = @(@(Get-Value $listed 'tools' @()) | ForEach-Object { [string](Get-Value $_ 'name' '') })
        $missing = @($required | Where-Object { $_ -notin $names })
        $authenticatedUser = [string]$status.authenticated_user
        $remoteConnected = $true
    }
    catch { }
    $healthy = $remoteConnected -and $missing.Count -eq 0 -and $mcpConfigured -and
        $hooksConfigured -and $hooksFeatureEnabled
    if ($script:RunningOnWindows) { $healthy = $healthy -and [bool]$workerTaskConfigured }
    $note = if ($remoteConnected) {
        'Self-test checks configuration and remote MCP only. Review/trust hooks in Codex CLI /hooks; Windows desktop may not show a review prompt. Native hook delivery and DPAPI/task execution require acceptance testing.'
    } else {
        'EvolvMem self-test could not reach the authenticated MCP.'
    }
    Write-OutputJson ([ordered]@{
        healthy = $healthy; remote_connected = $remoteConnected; authenticated_user = $authenticatedUser
        device_id = [string]$Config.device_id; missing_tools = $missing; tool_count = $names.Count
        mcp_configured = $mcpConfigured; hooks_configured = $hooksConfigured
        hooks_feature_enabled = $hooksFeatureEnabled
        invalid_hooks = $invalidHooks.ToArray(); worker_task_configured = $workerTaskConfigured
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

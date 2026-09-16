[CmdletBinding()]
param(
    [string]$Url,
    [string]$ExpectedUser,
    [string]$TokenEnvVar = 'EVOLVMEM_TOKEN',
    [string[]]$ProjectMapping = @(),
    [switch]$StrictInjection,
    [switch]$ForceIdentity,
    [switch]$RunSelfTest,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$script:RunningOnWindows = $env:OS -eq 'Windows_NT'
$sourceClient = [IO.Path]::Combine($PSScriptRoot, 'evolvmem-codex.ps1')
if (-not $env:LOCALAPPDATA) { throw 'The local application data directory is unavailable.' }
$installRoot = [IO.Path]::Combine($env:LOCALAPPDATA, 'EvolvMem', 'Codex')
$installedClient = [IO.Path]::Combine($installRoot, 'evolvmem-codex.ps1')
$clientConfigPath = [IO.Path]::Combine($installRoot, 'config.json')
$codexRoot = if ($env:CODEX_HOME) {
    [IO.Path]::GetFullPath($env:CODEX_HOME)
} elseif ($env:USERPROFILE) {
    [IO.Path]::Combine($env:USERPROFILE, '.codex')
} else {
    throw 'CODEX_HOME and the user profile directory are unavailable.'
}
$codexConfigPath = [IO.Path]::Combine($codexRoot, 'config.toml')
$hooksPath = [IO.Path]::Combine($codexRoot, 'hooks.json')
$beginMarker = '# BEGIN EVOLVMEM WINDOWS CLIENT'
$endMarker = '# END EVOLVMEM WINDOWS CLIENT'

function Ensure-Directory([string]$Path) {
    if (-not [IO.Directory]::Exists($Path)) { [void][IO.Directory]::CreateDirectory($Path) }
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

function Write-TextAtomic([string]$Path, [string]$Text) {
    Ensure-Directory ([IO.Path]::GetDirectoryName($Path))
    $temp = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    [IO.File]::WriteAllText($temp, $Text, (New-Object Text.UTF8Encoding($false)))
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

function Write-JsonAtomic([string]$Path, $Value) {
    Write-TextAtomic $Path (($Value | ConvertTo-Json -Depth 30) + "`r`n")
}

function Backup-File([string]$Path) {
    if (-not [IO.File]::Exists($Path)) { return }
    $backup = $Path + '.evolvmem.' + [DateTime]::UtcNow.ToString('yyyyMMddHHmmssfff') + '.bak'
    [IO.File]::Copy($Path, $backup, $false)
}

function Escape-Toml([string]$Value) {
    return $Value.Replace('\', '\\').Replace('"', '\"')
}

function Get-UnmanagedMcpHeaderLayout([string]$Text) {
    $root = [regex]::Match(
        $Text,
        '(?ms)^[ \t]*\[mcp_servers\.evolvmem\][ \t]*(?:#[^\r\n]*)?\r?\n(?<body>.*?)(?=^[ \t]*\[|\z)')
    if (-not $root.Success) { return $null }
    $body = $root.Groups['body'].Value
    $inlineMatches = [regex]::Matches(
        $body,
        '(?m)^[ \t]*http_headers[ \t]*=[ \t]*\{(?<entries>[^\r\n{}]*)(?<close>\})(?<tail>[ \t]*(?:#[^\r\n]*)?\r?)$')
    $headerAssignments = [regex]::Matches(
        $body,
        '(?m)^[ \t]*(?:http_headers|"http_headers"|''http_headers'')[ \t]*(?:=|\.)')
    $subtableMatches = [regex]::Matches(
        $Text,
        '(?ms)^[ \t]*\[mcp_servers\.evolvmem\.http_headers\][ \t]*(?:#[^\r\n]*)?\r?\n(?<body>.*?)(?=^[ \t]*\[|\z)')
    $headerTables = [regex]::Matches(
        $Text,
        '(?mi)^[ \t]*\[[^\]\r\n]*mcp_servers[^\]\r\n]*evolvmem[^\]\r\n]*http_headers[^\]\r\n]*\][ \t]*(?:#[^\r\n]*)?\r?$')

    $supported = $inlineMatches.Count -le 1 -and $subtableMatches.Count -le 1 -and
        -not ($inlineMatches.Count -eq 1 -and $subtableMatches.Count -eq 1) -and
        $headerAssignments.Count -eq $inlineMatches.Count -and
        $headerTables.Count -eq $subtableMatches.Count
    $mode = 'none'
    $containerText = ''
    if ($supported -and $inlineMatches.Count -eq 1) {
        $mode = 'inline'
        $containerText = $inlineMatches[0].Groups['entries'].Value
        $key = '(?:"X-EvolvMem-Expected-User"|X-EvolvMem-Expected-User)'
        $pair = '(?:"(?:\\.|[^"\\])*"|[A-Za-z0-9_-]+)[ \t]*=[ \t]*"(?:\\.|[^"\\])*"'
        if ($containerText -notmatch ('^[ \t]*(?:' + $pair + '(?:[ \t]*,[ \t]*' + $pair + ')*[ \t]*,?)?[ \t]*$')) {
            $supported = $false
        }
        $expectedMatches = [regex]::Matches(
            $containerText,
            '(?i)(?:^|,)[ \t]*' + $key + '[ \t]*=[ \t]*"(?<value>(?:\\.|[^"\\])*)"[ \t]*(?=,|$)')
    }
    elseif ($supported -and $subtableMatches.Count -eq 1) {
        $mode = 'subtable'
        $containerText = $subtableMatches[0].Groups['body'].Value
        $line = '[ \t]*(?:"(?:\\.|[^"\\])*"|[A-Za-z0-9_-]+)[ \t]*=[ \t]*"(?:\\.|[^"\\])*"[ \t]*(?:#[^\r\n]*)?\r?'
        if ($containerText -notmatch ('(?s)^(?:(?:[ \t]*(?:#[^\r\n]*)?\r?)?\n|' + $line + '(?:\n|$))*$')) {
            $supported = $false
        }
        $expectedMatches = [regex]::Matches(
            $containerText,
            '(?mi)^[ \t]*(?:"X-EvolvMem-Expected-User"|X-EvolvMem-Expected-User)[ \t]*=[ \t]*"(?<value>(?:\\.|[^"\\])*)"[ \t]*(?:#[^\r\n]*)?\r?$')
    }
    else {
        $expectedMatches = @()
    }
    if ($expectedMatches.Count -gt 1) { $supported = $false }
    return [pscustomobject]@{
        Root = $root
        Mode = $mode
        Inline = if ($inlineMatches.Count -eq 1) { $inlineMatches[0] } else { $null }
        Subtable = if ($subtableMatches.Count -eq 1) { $subtableMatches[0] } else { $null }
        Supported = $supported
        HasExpectedUser = $expectedMatches.Count -eq 1
        ExpectedUser = if ($expectedMatches.Count -eq 1) { $expectedMatches[0].Groups['value'].Value } else { '' }
    }
}

function Get-UnmanagedMcpIdentity([string]$Text) {
    $match = [regex]::Match(
        $Text,
        '(?ms)^[ \t]*\[mcp_servers\.evolvmem\][ \t]*(?:#[^\r\n]*)?\r?\n(?<body>.*?)(?=^[ \t]*\[|\z)')
    if (-not $match.Success) { return $null }
    $body = $match.Groups['body'].Value
    $urlMatch = [regex]::Match($body, '(?m)^\s*url\s*=\s*"(?<value>(?:\\.|[^"])*)"\s*$')
    $tokenMatch = [regex]::Match($body, '(?m)^\s*bearer_token_env_var\s*=\s*"(?<value>(?:\\.|[^"])*)"\s*$')
    $headers = Get-UnmanagedMcpHeaderLayout $Text
    return [pscustomobject]@{
        Url = if ($urlMatch.Success) { $urlMatch.Groups['value'].Value } else { '' }
        TokenEnvVar = if ($tokenMatch.Success) { $tokenMatch.Groups['value'].Value } else { '' }
        HeadersSupported = $headers.Supported
        HasExpectedUser = $headers.HasExpectedUser
        ExpectedUser = $headers.ExpectedUser
    }
}

function Assert-McpIdentityCompatible([string]$Text) {
    $identity = Get-UnmanagedMcpIdentity $Text
    if ($null -eq $identity) { return }
    if (-not $identity.HeadersSupported) {
        throw 'The existing mcp_servers.evolvmem http_headers form cannot be updated safely.'
    }
    if ($identity.Url -cne $Url -or $identity.TokenEnvVar -cne $TokenEnvVar -or
        ($identity.HasExpectedUser -and $identity.ExpectedUser -cne $ExpectedUser)) {
        if (-not $ForceIdentity) {
            throw 'An existing mcp_servers.evolvmem identity differs. Re-run with -ForceIdentity only after reviewing the replacement.'
        }
    }
}

function Ensure-UnmanagedMcpExpectedUser([string]$Text) {
    $layout = Get-UnmanagedMcpHeaderLayout $Text
    if ($null -eq $layout) { return $Text }
    if (-not $layout.Supported) {
        throw 'The existing mcp_servers.evolvmem http_headers form cannot be updated safely.'
    }
    if ($layout.HasExpectedUser) { return $Text }
    $entry = '"X-EvolvMem-Expected-User" = "' + (Escape-Toml $ExpectedUser) + '"'
    if ($layout.Mode -eq 'inline') {
        $entries = $layout.Inline.Groups['entries'].Value.Trim()
        $addition = $(if ($entries) { ', ' } else { '' }) + $entry
        $absolute = $layout.Root.Groups['body'].Index + $layout.Inline.Groups['close'].Index
        return $Text.Substring(0, $absolute) + $addition + $Text.Substring($absolute)
    }
    if ($layout.Mode -eq 'subtable') {
        $body = $layout.Subtable.Groups['body'].Value
        $lineEnding = if ($body.Contains("`r`n")) { "`r`n" } else { "`n" }
        $separator = if (-not $body -or $body.EndsWith("`n")) { '' } else { $lineEnding }
        $addition = $separator + $entry + $lineEnding
        $absolute = $layout.Subtable.Groups['body'].Index + $layout.Subtable.Groups['body'].Length
        return $Text.Substring(0, $absolute) + $addition + $Text.Substring($absolute)
    }
    $insertAt = $layout.Root.Index + $layout.Root.Length
    $separator = if ($layout.Root.Value.EndsWith("`n")) { '' } else { "`r`n" }
    $guard = 'http_headers = { ' + $entry + ' }' + "`r`n"
    return $Text.Substring(0, $insertAt) + $separator + $guard + $Text.Substring($insertAt)
}

function Remove-EvolvMemMcpSections([string]$Text) {
    $lines = [regex]::Split($Text, '(?<=\n)')
    $kept = New-Object Collections.Generic.List[string]
    $skip = $false
    foreach ($line in $lines) {
        $header = [regex]::Match($line, '^\s*\[([^]]+)\]')
        if ($header.Success) {
            $name = $header.Groups[1].Value
            $skip = $name -eq 'mcp_servers.evolvmem' -or $name.StartsWith('mcp_servers.evolvmem.')
        }
        if (-not $skip) { $kept.Add($line) }
    }
    return ($kept -join '')
}

function Merge-McpConfig([string]$Text) {
    $block = @"
$beginMarker
[mcp_servers.evolvmem]
url = "$(Escape-Toml $Url)"
bearer_token_env_var = "$(Escape-Toml $TokenEnvVar)"
http_headers = { "X-EvolvMem-Expected-User" = "$(Escape-Toml $ExpectedUser)" }
startup_timeout_sec = 60
tool_timeout_sec = 60
$endMarker
"@
    $managedPattern = '(?ms)^' + [regex]::Escape($beginMarker) + '.*?^' + [regex]::Escape($endMarker) + '\s*(?:\r?\n)?'
    if ([regex]::IsMatch($Text, $managedPattern)) {
        return [regex]::Replace($Text, $managedPattern, $block + "`r`n")
    }
    $existing = Get-UnmanagedMcpIdentity $Text
    if ($null -ne $existing -and -not $ForceIdentity) {
        # Preserve the reviewed stanza while ensuring direct Codex requests
        # carry the same expected-user guard as the helper script.
        return Ensure-UnmanagedMcpExpectedUser $Text
    }
    if ($null -ne $existing) { $Text = Remove-EvolvMemMcpSections $Text }
    if ($Text -and -not $Text.EndsWith("`n")) { $Text += "`r`n" }
    return $Text + $block + "`r`n"
}

function Convert-ToOrderedMap($Object) {
    $map = [ordered]@{}
    if ($null -ne $Object) {
        foreach ($property in $Object.PSObject.Properties) { $map[$property.Name] = $property.Value }
    }
    return $map
}

function Remove-ManagedHookHandlers($Hooks) {
    if ($null -eq $Hooks) { return }
    foreach ($event in @($Hooks.PSObject.Properties)) {
        $groups = New-Object Collections.Generic.List[object]
        foreach ($group in @($event.Value)) {
            $remaining = @(@(Get-Value $group 'hooks' @()) | Where-Object {
                ([string](Get-Value $_ 'command' '')) -notmatch [regex]::Escape($installedClient)
            })
            if ($remaining.Count -gt 0) {
                $group.hooks = $remaining
                $groups.Add($group)
            }
        }
        $event.Value = $groups.ToArray()
    }
}

function Add-HookGroup($Hooks, [string]$EventName, [string]$ActionName, [int]$Timeout, [string]$Matcher = '') {
    $command = 'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
        $installedClient + '" -Action ' + $ActionName
    $handler = [ordered]@{ type = 'command'; command = $command; timeout = $Timeout }
    if ($EventName -eq 'SessionStart') {
        $handler.statusMessage = 'Loading EvolvMem context'
        $handler.additionalContextLimit = 8000
    }
    elseif ($EventName -eq 'UserPromptSubmit') { $handler.additionalContextLimit = 8000 }
    $group = [ordered]@{ hooks = @($handler) }
    if ($Matcher) { $group = [ordered]@{ matcher = $Matcher; hooks = @($handler) } }
    $property = $Hooks.PSObject.Properties[$EventName]
    $combined = New-Object Collections.Generic.List[object]
    if ($null -ne $property) {
        foreach ($existingGroup in @($property.Value)) { $combined.Add($existingGroup) }
    }
    $combined.Add($group)
    $Hooks | Add-Member -NotePropertyName $EventName -NotePropertyValue $combined.ToArray() -Force
}

function Merge-Hooks($Document) {
    if ($null -eq $Document) {
        $Document = [pscustomobject]@{ description = 'User lifecycle hooks, including EvolvMem.'; hooks = [pscustomobject]@{} }
    }
    if ($null -eq (Get-Value $Document 'hooks')) {
        $Document | Add-Member -NotePropertyName hooks -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    Remove-ManagedHookHandlers $Document.hooks
    Add-HookGroup $Document.hooks 'SessionStart' 'session-start' 12 'startup|resume|clear|compact'
    Add-HookGroup $Document.hooks 'UserPromptSubmit' 'prompt-submit' 12
    Add-HookGroup $Document.hooks 'Stop' 'snapshot' 3
    Add-HookGroup $Document.hooks 'PreCompact' 'snapshot' 3 'manual|auto'
    Add-HookGroup $Document.hooks 'SessionEnd' 'snapshot' 3
    Add-HookGroup $Document.hooks 'Interrupt' 'snapshot' 3
    return $Document
}

function Remove-ManagedHooks($Document) {
    if ($null -ne $Document -and $null -ne (Get-Value $Document 'hooks')) {
        Remove-ManagedHookHandlers $Document.hooks
    }
    return $Document
}

function Register-WorkerTask {
    if (-not $script:RunningOnWindows) { return }
    $taskCommand = 'powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
        $installedClient + '" -Action worker'
    $null = & schtasks.exe /Create /TN 'EvolvMem Codex Sync' /TR $taskCommand /SC MINUTE /MO 5 /F
    if ($LASTEXITCODE -ne 0) { throw 'Could not register the EvolvMem user scheduled task.' }
}

function Remove-WorkerTask {
    if (-not $script:RunningOnWindows) { return }
    $null = & schtasks.exe /Delete /TN 'EvolvMem Codex Sync' /F
}

if ($Uninstall) {
    Ensure-Directory $codexRoot
    if ([IO.File]::Exists($hooksPath)) {
        Backup-File $hooksPath
        Write-JsonAtomic $hooksPath (Remove-ManagedHooks (Read-JsonFile $hooksPath))
    }
    if ([IO.File]::Exists($codexConfigPath)) {
        $text = [IO.File]::ReadAllText($codexConfigPath, [Text.Encoding]::UTF8)
        $pattern = '(?ms)^' + [regex]::Escape($beginMarker) + '.*?^' + [regex]::Escape($endMarker) + '\s*(?:\r?\n)?'
        if ([regex]::IsMatch($text, $pattern)) {
            Backup-File $codexConfigPath
            Write-TextAtomic $codexConfigPath ([regex]::Replace($text, $pattern, ''))
        }
    }
    Remove-WorkerTask
    if ([IO.File]::Exists($installedClient)) { Remove-Item -LiteralPath $installedClient -Force }
    if ([IO.File]::Exists($clientConfigPath)) { Remove-Item -LiteralPath $clientConfigPath -Force }
    Write-Output 'EvolvMem Windows client configuration was removed. Existing hooks and encrypted local archive state were preserved.'
    exit 0
}

if (-not $Url -or -not $ExpectedUser) {
    throw '-Url and -ExpectedUser are required for installation.'
}
if (-not [Uri]::IsWellFormedUriString($Url, [UriKind]::Absolute) -or
    ([Uri]$Url).Scheme -notin @('http', 'https')) { throw '-Url must be an absolute HTTP(S) URI.' }
if ($TokenEnvVar -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw '-TokenEnvVar must be an environment variable name.' }
if (-not [IO.File]::Exists($sourceClient)) { throw 'evolvmem-codex.ps1 must be next to this installer.' }

$existingClient = Read-JsonFile $clientConfigPath
if ($null -ne $existingClient) {
    $identityChanged = [string](Get-Value $existingClient 'url' '') -cne $Url -or
        [string](Get-Value $existingClient 'expected_user' '') -cne $ExpectedUser -or
        [string](Get-Value $existingClient 'token_env_var' '') -cne $TokenEnvVar
    if ($identityChanged -and -not $ForceIdentity) {
        throw 'The installed EvolvMem identity differs. Re-run with -ForceIdentity only after reviewing the replacement.'
    }
}

$toml = if ([IO.File]::Exists($codexConfigPath)) {
    [IO.File]::ReadAllText($codexConfigPath, [Text.Encoding]::UTF8)
} else { '' }
Assert-McpIdentityCompatible $toml

$projects = Convert-ToOrderedMap (Get-Value $existingClient 'projects')
foreach ($mapping in $ProjectMapping) {
    $separator = $mapping.LastIndexOf('=')
    if ($separator -lt 1 -or $separator -eq $mapping.Length - 1) {
        throw 'Each -ProjectMapping must use PATH=PROJECT.'
    }
    $path = [IO.Path]::GetFullPath($mapping.Substring(0, $separator))
    $project = $mapping.Substring($separator + 1).Trim()
    if (-not $project) { throw 'Project mapping names cannot be empty.' }
    $projects[$path] = $project
}

Ensure-Directory $installRoot
Ensure-Directory $codexRoot
Copy-Item -LiteralPath $sourceClient -Destination $installedClient -Force

$deviceId = [string](Get-Value $existingClient 'device_id' '')
if (-not $deviceId) { $deviceId = 'win-' + [Guid]::NewGuid().ToString('D') }
$strictValue = if ($PSBoundParameters.ContainsKey('StrictInjection')) {
    [bool]$StrictInjection
} else {
    [bool](Get-Value $existingClient 'strict_injection' $false)
}
$clientConfig = [ordered]@{
    version = 1; url = $Url; token_env_var = $TokenEnvVar; expected_user = $ExpectedUser
    device_id = $deviceId; projects = $projects; strict_injection = $strictValue
}
if ([IO.File]::Exists($clientConfigPath)) { Backup-File $clientConfigPath }
Write-JsonAtomic $clientConfigPath $clientConfig

if ($toml -cne (Merge-McpConfig $toml)) {
    if ([IO.File]::Exists($codexConfigPath)) { Backup-File $codexConfigPath }
    Write-TextAtomic $codexConfigPath (Merge-McpConfig $toml)
}

$hooks = Read-JsonFile $hooksPath
if ([IO.File]::Exists($hooksPath)) { Backup-File $hooksPath }
Write-JsonAtomic $hooksPath (Merge-Hooks $hooks)
Register-WorkerTask

Write-Output ('EvolvMem Windows client installed for expected user ' + $ExpectedUser + ' with device ' + $deviceId + '.')
Write-Output 'Open Codex CLI in your project and use /hooks to review/trust the six EvolvMem hooks. Windows desktop may not show a review prompt. Then restart desktop Codex and verify a new session; self-test does not check hook trust or event delivery.'
Write-Output ('Status: powershell.exe -NoProfile -File "' + $installedClient + '" -Action status')
Write-Output ('Self-test: powershell.exe -NoProfile -File "' + $installedClient + '" -Action self-test')
if ($RunSelfTest) {
    & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $installedClient -Action self-test
}

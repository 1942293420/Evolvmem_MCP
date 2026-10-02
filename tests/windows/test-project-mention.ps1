[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$ClientPath)

# Synthetic Windows PowerShell 5.1 behavior test for explicit project mention
# recall on the UserPromptSubmit path.
#
# The real client is loaded through an empty snapshot event (no main dispatch,
# no stdin, no network, no queue and no DPAPI). Only Invoke-McpTool is replaced,
# and Invoke-PromptSubmit is called directly against a synthetic client home. It
# never reads the real EvolvMem client home, transcripts, queue payloads or
# credentials, and it never contacts a real server.

$ErrorActionPreference = 'Stop'
$utf8 = New-Object Text.UTF8Encoding($false)
$script:Failures = New-Object Collections.Generic.List[string]
$script:Checks = 0
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('evolvmem-project-mention-test-' + [Guid]::NewGuid().ToString('N'))
$previousClientHome = $env:EVOLVMEM_CLIENT_HOME

function Assert-True($Condition, [string]$Message) {
    $script:Checks += 1
    if (-not $Condition) { $script:Failures.Add($Message) }
}

function Assert-Equal($Expected, $Actual, [string]$Message) {
    $script:Checks += 1
    if ([string]$Expected -cne [string]$Actual) {
        $script:Failures.Add($Message + ' | expected=[' + [string]$Expected + '] actual=[' + [string]$Actual + ']')
    }
}

function Assert-Contains([string]$Text, [string]$Needle, [string]$Message) {
    $script:Checks += 1
    if ($null -eq $Text -or $Text.IndexOf($Needle, [StringComparison]::Ordinal) -lt 0) {
        $script:Failures.Add($Message + ' | missing=[' + $Needle + ']')
    }
}

function Assert-NotContains([string]$Text, [string]$Needle, [string]$Message) {
    $script:Checks += 1
    if ($null -ne $Text -and $Text.IndexOf($Needle, [StringComparison]::Ordinal) -ge 0) {
        $script:Failures.Add($Message + ' | unexpected=[' + $Needle + ']')
    }
}

function Invoke-Case([string]$Label, [scriptblock]$Body) {
    try { & $Body }
    catch {
        $script:Checks += 1
        $script:Failures.Add($Label + ' threw unexpectedly: ' + $_.Exception.Message)
    }
}

try {
    $clientHome = Join-Path $testRoot 'client'
    $workDir = Join-Path $testRoot 'work'
    [void][IO.Directory]::CreateDirectory($clientHome)
    [void][IO.Directory]::CreateDirectory($workDir)
    $env:EVOLVMEM_CLIENT_HOME = $clientHome
    $configSeed = [ordered]@{
        version = 1; url = 'http://127.0.0.1:1/mcp'; token_env_var = 'EVOLVMEM_SYNTHETIC_UNUSED_TOKEN'
        expected_user = 'synthetic-user'; device_id = 'synthetic-device'
        projects = [ordered]@{ $workDir = 'bound-project' }
    }
    [IO.File]::WriteAllText((Join-Path $clientHome 'config.json'), ($configSeed | ConvertTo-Json -Depth 6), $utf8)

    # Load the real client functions without running the main dispatch: an empty
    # snapshot event returns before reading a transcript, the queue or a server.
    [Console]::SetIn((New-Object IO.StringReader('{}')))
    . $ClientPath -Action snapshot

    # Replace only the MCP boundary after the dot-source. Every call is recorded
    # so the test can assert order, arguments and isolation.
    $script:Calls = New-Object Collections.Generic.List[object]
    $script:ProjectMode = 'ok'
    $script:ProjectBlock = ''
    $script:ExperienceMode = 'ok'
    $script:ExperienceResults = @()
    $script:MemoryRevision = 5
    function Invoke-McpTool($Config, [string]$Name, $Arguments, [string]$RpcId = '') {
        $copy = [ordered]@{}
        if ($null -ne $Arguments) {
            foreach ($key in @($Arguments.Keys)) { $copy[$key] = $Arguments[$key] }
        }
        $script:Calls.Add([pscustomobject]@{ Name = $Name; Arguments = $copy })
        switch ($Name) {
            'context_session_start' {
                return [pscustomobject]@{
                    authenticated_user = 'synthetic-user'; block = ('REFRESH-DATA ' * 1200)
                    selected_ids = @(1); memory_revision = $script:MemoryRevision
                    continuation = $null; continuation_code = 'NO_FOCUS'
                }
            }
            'memory_status' {
                return [pscustomobject]@{
                    authenticated_user = 'synthetic-user'; active_memories = 3
                    memory_revision = $script:MemoryRevision
                }
            }
            'context_project_recall' {
                if ($script:ProjectMode -eq 'fail') { throw 'synthetic project recall transport failure' }
                $block = if ($script:ProjectMode -eq 'empty') { '' } else { $script:ProjectBlock }
                $matched = if ($script:ProjectMode -eq 'empty') { @() } else { @('other-project') }
                return [pscustomobject]@{
                    authenticated_user = 'synthetic-user'; block = $block
                    selected_ids = @(11, 12); matched_projects = $matched
                }
            }
            'experience_recall' {
                if ($script:ExperienceMode -eq 'fail') { throw 'synthetic experience transport failure' }
                return [pscustomobject]@{
                    authenticated_user = 'synthetic-user'; results = @($script:ExperienceResults)
                }
            }
        }
        throw ('Unexpected synthetic MCP call: ' + $Name)
    }

    function New-SyntheticReceipt([string]$SessionId, [string]$Project) {
        # A successful start receipt makes the prompt path skip re-injection, so
        # the turn only exercises project and experience recall.
        $receipt = [ordered]@{
            version = 1; session_id = $SessionId; start_id = 'synthetic-start'
            source = 'startup'; status = 'success'; first_prompt_pending = $true
            retrieved_utc = '2026-09-21T00:00:00.0000000Z'; authenticated_user = 'synthetic-user'
            project = $Project; selected_count = 1; memory_revision = $script:MemoryRevision
            continuation = $null; continuation_code = 'NO_FOCUS'; delivery_status = 'retrieved_for_hook'
        }
        Write-JsonAtomic (Get-ReceiptPath $SessionId) $receipt
    }

    function Get-CallNames {
        return (@($script:Calls | ForEach-Object { $_.Name }) -join ',')
    }

    function Invoke-SyntheticPrompt([string]$SessionId, [string]$Prompt) {
        $event = [pscustomobject]@{
            session_id = $SessionId; cwd = $workDir; prompt = $Prompt
            hook_event_name = 'UserPromptSubmit'
        }
        $writer = New-Object IO.StringWriter
        $original = [Console]::Out
        try {
            [Console]::SetOut($writer)
            Invoke-PromptSubmit (Get-Config) $event
        }
        finally { [Console]::SetOut($original) }
        $lines = @($writer.ToString() -split "`r?`n" | Where-Object { $_.Trim() })
        if ($lines.Count -eq 0) { return $null }
        return ($lines[-1].Trim() | ConvertFrom-Json)
    }

    function Get-Context($Output) {
        return [string](Get-Value (Get-Value $Output 'hookSpecificOutput') 'additionalContext' '')
    }

    Invoke-Case 'empty experience still injects the project block' {
        $script:Calls.Clear()
        $script:ProjectMode = 'ok'
        $script:ProjectBlock = 'PROJECT-ALPHA: synthetic decisions and constraints.'
        $script:ExperienceMode = 'ok'
        $script:ExperienceResults = @()
        [void](New-SyntheticReceipt 'session-project-only' 'bound-project')
        $output = Invoke-SyntheticPrompt 'session-project-only' 'what did we decide for other-project?'
        $context = Get-Context $output
        Assert-Contains $context $script:ProjectBlock 'The project details must be injected even when experience recall returns no results.'
        Assert-NotContains $context '[EvolvMem related experience' 'An empty experience result must not add an experience section.'
        Assert-Equal 'memory_status,context_project_recall,experience_recall' (Get-CallNames) 'The project tool must be called independently and before experience.'
        Assert-Equal 'UserPromptSubmit' ([string](Get-Value (Get-Value $output 'hookSpecificOutput') 'hookEventName' '')) 'The injected context must stay on the UserPromptSubmit hook.'
    }

    Invoke-Case 'project tool failure fails open and keeps experience' {
        $script:Calls.Clear()
        $script:ProjectMode = 'fail'
        $script:ExperienceMode = 'ok'
        $script:ExperienceResults = @([pscustomobject]@{ id = 21; summary = 'synthetic transferable experience' })
        [void](New-SyntheticReceipt 'session-project-fail' 'bound-project')
        $output = Invoke-SyntheticPrompt 'session-project-fail' 'resume the migration'
        $context = Get-Context $output
        Assert-Contains $context 'synthetic transferable experience' 'A project recall failure must not suppress the existing experience injection.'
        Assert-Contains ([string](Get-Value $output 'systemMessage' '')) 'project mention recall' 'A project recall failure must fail open with a brief warning.'
        Assert-True ($null -eq (Get-Value $output 'decision')) 'A non-strict project recall failure must not block the prompt.'
        Assert-Equal 'memory_status,context_project_recall,experience_recall' (Get-CallNames) 'Experience recall must still run after a project recall failure.'
    }

    Invoke-Case 'empty project block adds nothing' {
        $script:Calls.Clear()
        $script:ProjectMode = 'empty'
        $script:ExperienceMode = 'ok'
        $script:ExperienceResults = @()
        [void](New-SyntheticReceipt 'session-project-unmatched' 'bound-project')
        $output = Invoke-SyntheticPrompt 'session-project-unmatched' 'plain question without any project name'
        Assert-True ($null -eq (Get-Value $output 'hookSpecificOutput')) 'An empty project block must not add a context section.'
        Assert-True ($null -eq (Get-Value $output 'systemMessage')) 'An empty project block must not add a warning.'
        Assert-Equal 'memory_status,context_project_recall,experience_recall' (Get-CallNames) 'The project tool must still be consulted once per prompt.'
    }

    Invoke-Case 'server project block is bounded by the client budget' {
        $script:Calls.Clear()
        $script:ProjectMode = 'ok'
        $script:ProjectBlock = ('PROJECT-OVERSIZE ' + (([string][char]0x8BE6) * 12000))
        $script:ExperienceMode = 'ok'
        $script:ExperienceResults = @()
        [void](New-SyntheticReceipt 'session-project-budget' 'bound-project')
        $output = Invoke-SyntheticPrompt 'session-project-budget' 'other-project status?'
        $context = Get-Context $output
        $projectCall = $script:Calls[1]
        Assert-Equal 'context_project_recall' $projectCall.Name 'Project recall must be the second synthetic call.'
        Assert-True ($null -ne $projectCall.Arguments['max_chars']) 'The project recall call must carry an explicit max_chars budget.'
        Assert-Equal $script:ProjectRecallMaxChars ([int]$projectCall.Arguments['max_chars']) 'The project recall budget must stay the bounded client constant.'
        Assert-True ([int]$projectCall.Arguments['max_chars'] -le ($script:MaxContextChars - $script:ConnectionMetadataReserveChars)) 'The project budget must fit inside the overall context budget.'
        Assert-Contains $context 'PROJECT-OVERSIZE' 'The head of the project block must survive client-side bounding.'
        Assert-True ($context.Length -le ($script:ProjectRecallMaxChars + 120)) 'An oversized server project block must be truncated to the project budget plus its short header.'
        Assert-True ($context.Length -lt $script:ProjectBlock.Length) 'An oversized server project block must not be injected in full.'
    }

    Invoke-Case 'large experience cannot truncate the project block' {
        $script:Calls.Clear()
        $script:ProjectMode = 'ok'
        $script:ProjectBlock = 'PROJECT-BETA: synthetic key decisions.'
        $script:ExperienceMode = 'ok'
        $script:ExperienceResults = @([pscustomobject]@{ id = 31; summary = ('experience-detail ' * 1500) })
        [void](New-SyntheticReceipt 'session-project-priority' 'bound-project')
        $output = Invoke-SyntheticPrompt 'session-project-priority' 'other-project decisions'
        $context = Get-Context $output
        $projectIndex = if ($context) { $context.IndexOf('[EvolvMem mentioned project', [StringComparison]::Ordinal) } else { -1 }
        $experienceIndex = if ($context) { $context.IndexOf('[EvolvMem related experience', [StringComparison]::Ordinal) } else { -1 }
        Assert-True ($projectIndex -ge 0) 'The project section must survive a large experience section.'
        Assert-True ($experienceIndex -lt 0 -or $projectIndex -lt $experienceIndex) 'The project section must precede the experience section.'
        Assert-Contains $context $script:ProjectBlock 'The project details must not be pushed out by experience snippets.'
        Assert-True ($context.Length -le $script:MaxContextChars) 'The combined prompt context must stay within MaxContextChars.'
    }

    Invoke-Case 'full refresh leaves room for explicit project recall' {
        $script:Calls.Clear()
        $script:ProjectMode = 'ok'
        $script:ProjectBlock = 'PROJECT-REFRESH: explicit mentioned project detail.'
        $script:ExperienceMode = 'ok'
        $script:ExperienceResults = @()
        # No receipt: request fresh SessionStart data in the same prompt turn.
        $output = Invoke-SyntheticPrompt 'session-project-full-refresh' 'other-project status'
        $context = Get-Context $output
        Assert-Contains $context '[EvolvMem connection metadata]' 'Refresh must retain connection metadata.'
        Assert-Contains $context 'PROJECT-REFRESH' 'A full refresh must not push the explicit project detail out.'
        Assert-True ($context.Length -le $script:MaxContextChars) 'The combined refresh stays bounded.'
    }

    Invoke-Case 'project recall keeps cwd project and continuity ownership' {
        $script:Calls.Clear()
        $script:ProjectMode = 'ok'
        $script:ProjectBlock = 'PROJECT-GAMMA: synthetic other-project details.'
        $script:ExperienceMode = 'ok'
        $script:ExperienceResults = @()
        [void](New-SyntheticReceipt 'session-project-ownership' 'bound-project')
        $output = Invoke-SyntheticPrompt 'session-project-ownership' 'recall other-project decisions'
        $projectCall = $script:Calls[1]
        $experienceCall = $script:Calls[2]
        Assert-Equal 'context_project_recall' $projectCall.Name 'The project tool must be the second synthetic call.'
        Assert-Equal 'query,max_chars' (($projectCall.Arguments.Keys) -join ',') 'Project recall must send only the query and its budget, so no workspace binding or focus can change.'
        Assert-Equal 'recall other-project decisions' ([string]$projectCall.Arguments['query']) 'Project recall must receive the original prompt text.'
        Assert-Equal 'experience_recall' $experienceCall.Name 'Experience recall must still follow on the cwd project.'
        Assert-Equal 'bound-project' ([string]$experienceCall.Arguments['project']) 'Experience recall must keep the cwd-resolved project.'
        Assert-NotContains (Get-CallNames) 'context_session_start' 'A fresh receipt must not trigger another SessionStart injection.'
        Assert-NotContains (Get-CallNames) 'continuity_' 'Project recall must not touch continuity focus.'
        $saved = Read-JsonFile (Get-ReceiptPath 'session-project-ownership')
        Assert-Equal 'bound-project' ([string](Get-Value $saved 'project' '')) 'The stored receipt must keep the cwd-resolved project.'
        Assert-Equal 'NO_FOCUS' ([string](Get-Value $saved 'continuation_code' '')) 'The stored receipt must keep its continuity code.'
        Assert-Equal $false ([bool](Get-Value $saved 'first_prompt_pending' $true)) 'The receipt must still be updated after project recall.'
    }

    Invoke-Case 'empty prompt does not call the project tool' {
        $script:Calls.Clear()
        $script:ProjectMode = 'ok'
        $script:ProjectBlock = 'PROJECT-DELTA: must not be requested.'
        $script:ExperienceMode = 'ok'
        $script:ExperienceResults = @()
        [void](New-SyntheticReceipt 'session-project-noprompt' 'bound-project')
        $output = Invoke-SyntheticPrompt 'session-project-noprompt' ''
        Assert-Equal 'memory_status' (Get-CallNames) 'An empty prompt must not trigger project or experience recall.'
        Assert-True ($null -eq (Get-Value $output 'hookSpecificOutput')) 'An empty prompt must not add prompt context.'
    }
}
finally {
    $env:EVOLVMEM_CLIENT_HOME = $previousClientHome
    Remove-Item -Recurse -Force $testRoot -ErrorAction SilentlyContinue
}

if ($script:Failures.Count -gt 0) {
    [ordered]@{
        passed = $false; powershell = $PSVersionTable.PSVersion.ToString()
        failure_count = $script:Failures.Count; checks = $script:Checks
        failures = $script:Failures.ToArray()
    } | ConvertTo-Json -Depth 5 -Compress
    exit 1
}
[ordered]@{
    passed = $true; powershell = $PSVersionTable.PSVersion.ToString(); checks = $script:Checks
} | ConvertTo-Json -Compress

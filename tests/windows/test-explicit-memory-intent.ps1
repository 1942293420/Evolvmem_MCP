param([Parameter(Mandatory=$true)][string]$ClientPath)
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('evolvmem-explicit-intent-'+[Guid]::NewGuid().ToString('N'))
$previousHome=$env:EVOLVMEM_CLIENT_HOME
$previousIn=[Console]::In
$utf8=New-Object Text.UTF8Encoding($false)
function Assert($condition,$message){if(-not $condition){throw $message}}
function Decode([string]$text){return [regex]::Unescape($text)}
try {
    [void][IO.Directory]::CreateDirectory($root)
    $env:EVOLVMEM_CLIENT_HOME=$root
    $seed=@{url='http://127.0.0.1:1/mcp';expected_user='fixture-user';token_env_var='UNUSED';device_id='fixture';projects=@{};strict_injection=$false}
    [IO.File]::WriteAllText((Join-Path $root 'config.json'),($seed|ConvertTo-Json),$utf8)
    [Console]::SetIn((New-Object IO.StringReader('{}')))
    . $ClientPath -Action snapshot
    Assert ($script:ClientHome -ceq $root) 'Fixture home isolation failed'
    $cfg=Get-Config
    $script:Calls=New-Object Collections.Generic.List[string]
    $script:FailPreflight=$false
    $script:BootstrapBudget=0
    $script:HookBudgetWatch=$null
    function Get-RemoteRevisionStatus($Config){
        if($script:FailPreflight){throw 'fixture transport failure'}
        return [pscustomobject]@{memory_revision='41.2'}
    }
    function Invoke-McpTool($Config,$Name,$Arguments){
        $script:Calls.Add($Name)
        switch($Name){
            'context_project_recall' {return [pscustomobject]@{block=('P'*12000)}}
            'context_session_start' {
                $script:BootstrapBudget=[int]$Arguments.max_chars
                return [pscustomobject]@{authenticated_user='fixture-user';memory_revision='41.2';block=('S'*12000)}
            }
            'experience_recall' {return [pscustomobject]@{results=@(@{summary='EXPERIENCE'})}}
            default {throw ('Unexpected tool: '+$Name)}
        }
    }
    function Run-Prompt([string]$Prompt,[string]$TurnId='', [string]$Session='fixture-session',[string]$EventName='UserPromptSubmit'){
        $writer=New-Object IO.StringWriter
        $old=[Console]::Out
        $script:Calls.Clear()
        try {
            [Console]::SetOut($writer)
            Invoke-PromptSubmit $cfg ([pscustomobject]@{session_id=$Session;turn_id=$TurnId;cwd=$root;prompt=$Prompt;hook_event_name=$EventName})
        } finally {[Console]::SetOut($old)}
        return $writer.ToString()|ConvertFrom-Json
    }
    function Hint($output){return [string]$output.hookSpecificOutput.additionalContext}
    function Id([string]$context){return [regex]::Match($context,'request_id=(manual-hook-[0-9a-f]{64})').Groups[1].Value}
    $command=Decode '\u8bf7\u8bb0\u4f4f\uff1aSYNTHETIC-NOT-ECHOED'
    $first=Hint (Run-Prompt $command 'turn-a')
    Assert ($first.StartsWith('[EvolvMem explicit memory intent]')) 'A clear user directive must take priority over recalled context'
    Assert ($first.Contains('scope=experience') -and $first.Contains('manual_experience_save')) 'Missing experience scope or save tool'
    Assert ((Id $first) -match '^manual-hook-[0-9a-f]{64}$') 'Expected a stable opaque request ID'
    Assert (-not $first.Contains('SYNTHETIC-NOT-ECHOED')) 'Never echo the original prompt in the hint'
    Assert ($first.Length -le 8000 -and $first.Contains('PPPP') -and $first.Contains('SSSS')) 'Hint and recall must fit the shared context budget'
    Assert ($script:BootstrapBudget -lt 2240) 'Reserve the intent hint in the bootstrap request budget'
    Assert (($script:Calls -join ',') -ceq 'context_project_recall,context_session_start,experience_recall') 'The hook should only call its existing retrieval tools'
    Assert ((Id (Hint (Run-Prompt $command 'turn-a'))) -ceq (Id $first)) 'Same event retry must reuse request ID'
    Assert ((Id (Hint (Run-Prompt $command 'turn-b'))) -cne (Id $first)) 'A later turn must have a different request ID'
    Assert ((Id (Hint (Run-Prompt $command))) -ceq (Id (Hint (Run-Prompt $command)))) 'Events without turn ID must use a stable session and prompt hash'
    Assert ((Id (Hint (Run-Prompt $command '' 'other-session'))) -cne (Id (Hint (Run-Prompt $command)))) 'Fallback IDs must be scoped to the session'
    $positive=@(
        @('\u5199\u5165\u5168\u5c40.md\uff1a\u8fd9\u6761\u89c4\u5219','global_rule'),
        @('\u8bf7\u628a\u8fd9\u6761\u5199\u5165\u9879\u76ee.md','project_rule'),
        @('\u8bf7\u5199\u5165 `\u9879\u76ee.md`\uff1a\u8fd9\u6761\u89c4\u5219','project_rule'),
        @('\u4f60\u4ee5\u540e\u90fd\u4f7f\u7528\u539f\u6709\u53e3\u5f84\u3002','experience'),
        @('\u4ee5\u540e\u4f60\u4e0d\u8981\u4e0a\u4f20\u5bc6\u94a5\u3002','experience'),
        @('\u8fd9\u6b21\u5df2\u7ecf\u505a\u5b8c\u3002\u8bb0\u4f4f\uff1a\u5148\u6838\u5bf9\u8d26\u53f7\u3002','experience')
    )
    foreach($case in $positive){
        $context=Hint (Run-Prompt (Decode $case[0]))
        Assert ($context.Contains(('scope='+$case[1]))) ('Expected scope '+$case[1]+' for '+$case[0])
        if($case[1] -ne 'experience'){
            Assert ($context.Contains('manual_experience_save') -and $context.Contains('actual requested rule file')) 'Rule file directive should still identify reusable experience and the file boundary'
        }
    }
    $negative=@(
        'A normal user request',
        '\u4f60\u4ee5\u540e\u4f1a\u600e\u4e48\u6837\uff1f',
        '\u8bb0\u4f4f\u8fd9\u4e2a\u8bcd\u4ec0\u4e48\u610f\u601d\uff1f',
        '\u5199\u5165\u9879\u76ee.md\u53ef\u4ee5\u5417',
        '\u4e0d\u8981\u8bb0\u4f4f\uff1a\u8fd9\u6761\u89c4\u5219',
        '\u4ee5\u540e\u4e0d\u8981\u8bb0\u4f4f\u8fd9\u6761',
        '\u201c\u8bb0\u4f4f\uff1a\u8fd9\u6761\u89c4\u5219\u201d',
        '\u300c\n\u8bb0\u4f4f\uff1a\u8fd9\u6761\u89c4\u5219\n\u300d',
        '\u4ed6\u8bf4\u201c\u53ef\u4ee5\u3002\u8bb0\u4f4f\uff1a\u8fd9\u6761\u89c4\u5219\u201d\u3002',
        '\u4ed6\u8bf4\u201c\n\u8bb0\u4f4f\uff1a\u8fd9\u6761\u89c4\u5219\n\u201d',
        '\u67e5\u770b \x60demo\u3002\u8bb0\u4f4f\uff1a\u8fd9\u6761\u89c4\u5219\x60',
        '\u4ee5\u4e0b\u662f\u539f\u6587\uff1a\n\u8bb0\u4f4f\uff1a\u8fd9\u6761\u89c4\u5219',
        '> \u8bb0\u4f4f\uff1a\u8fd9\u6761\u89c4\u5219',
        '`\u8bb0\u4f4f\uff1a\u8fd9\u6761\u89c4\u5219`',
        '```text\n\u8bb0\u4f4f\uff1a\u8fd9\u6761\u89c4\u5219\n```',
        '    \u8bb0\u4f4f\uff1a\u8fd9\u6761\u89c4\u5219',
        '\u6bd4\u5982\uff1a\n\u8bb0\u4f4f\uff1a\u8fd9\u6761\u89c4\u5219',
        '\u6211\u5728\u8003\u8651\u7c7b\u4f3c\n\u8bb0\u4f4f\n\u5199\u5165\u9879\u76ee.md\n\u8fd9\u79cd\u8bcd\u662f\u5426\u80fd\u89e6\u53d1'
    )
    foreach($case in $negative){
        Assert (-not (Hint (Run-Prompt (Decode $case))).Contains('[EvolvMem explicit memory intent]')) ('Unexpected intent for '+$case)
    }
    Assert (-not (Hint (Run-Prompt $command '' 'fixture-session' 'Stop')).Contains('[EvolvMem explicit memory intent]')) 'A non-user event must not trigger'
    Assert (-not (Hint (Run-Prompt $command '' '')).Contains('[EvolvMem explicit memory intent]')) 'Unidentified events must not get request IDs'
    $script:FailPreflight=$true
    $offline=Run-Prompt $command
    Assert ((Hint $offline).Contains('manual_experience_save') -and $offline.systemMessage.Contains('unavailable')) 'Offline retrieval must retain the local intent without claiming a write'
    Assert ($script:Calls.Count -eq 0) 'Failed authentication must not trigger business calls'
    $cfg.strict_injection=$true
    $blocked=Run-Prompt $command '' 'strict-session'
    Assert ($blocked.decision -ceq 'block' -and -not (Hint $blocked)) 'Strict missing-context failure must continue to block'
    Write-Host 'Explicit memory intent hook checks passed'
}
finally {
    [Console]::SetIn($previousIn)
    $env:EVOLVMEM_CLIENT_HOME=$previousHome
    if([IO.Directory]::Exists($root)){[IO.Directory]::Delete($root,$true)}
}

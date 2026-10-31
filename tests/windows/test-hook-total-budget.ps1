param([Parameter(Mandatory=$true)][string]$ClientPath)
$ErrorActionPreference='Stop'
$testRoot=Join-Path ([IO.Path]::GetTempPath()) ('evolvmem-total-budget-'+[Guid]::NewGuid().ToString('N'))
$previousHome=$env:EVOLVMEM_CLIENT_HOME
$utf8=New-Object Text.UTF8Encoding($false)
function Assert($condition,$message){if(-not $condition){throw $message}}
try {
    [void][IO.Directory]::CreateDirectory($testRoot)
    $env:EVOLVMEM_CLIENT_HOME=$testRoot
    $seed=@{url='http://127.0.0.1:1/mcp';expected_user='fixture-user';token_env_var='UNUSED';device_id='fixture';projects=@{};strict_injection=$false}
    [IO.File]::WriteAllText((Join-Path $testRoot 'config.json'),($seed|ConvertTo-Json),$utf8)
    [Console]::SetIn((New-Object IO.StringReader('{}')))
    . $ClientPath -Action snapshot
    Assert ($script:ClientHome -ceq $testRoot) 'Fixture home isolation failed'
    $script:Calls=New-Object Collections.Generic.List[object]
    function Get-ActionToken($Config){return 'fixture-token-no-network'}
    function Get-RepoSnapshot([string]$Path){return @{kind='unknown'}}
    function Invoke-WebRequest {
        param($Uri,$Method,$ContentType,$Headers,$Body,$TimeoutSec,[switch]$UseBasicParsing)
        if($Uri -cne 'http://127.0.0.1:1/mcp'){throw 'Unexpected fixture URI'}
        $rpc=[Text.Encoding]::UTF8.GetString($Body)|ConvertFrom-Json
        $name=[string]$rpc.params.name
        $script:Calls.Add(@{name=$name;timeout=[int]$TimeoutSec;elapsed_before=$script:HookBudgetWatch.ElapsedMilliseconds;expected_user=$Headers['X-EvolvMem-Expected-User']})
        $delay=0;if($script:Latency.ContainsKey($name)){$delay=[int]$script:Latency[$name]}
        if($null -ne $script:HookBudgetWatch){$script:HookBudgetWatch.ElapsedMilliseconds += [Math]::Min($delay,1000*$TimeoutSec)}
        if($delay -gt 1000*$TimeoutSec -or $script:FailTool -ceq $name){throw 'fixture transport failure'}
        $value=@{authenticated_user=$script:RemoteUser;memory_revision='41.2'}
        switch($name){
            'context_session_start' {$value.block='BOOTSTRAP-CONTEXT';$value.selected_ids=@()}
            'context_project_recall' {$value.block='PROJECT-CONTEXT-WITH-EMPTY-MEMORY-IDS';$value.event_ids=@(101);$value.selected_ids=@();$value.selected_refs=@()}
            'experience_recall' {$value.results=@(@{summary='EXPERIENCE-CONTEXT'})}
        }
        $result=@{content=@(@{type='text';text=($value|ConvertTo-Json -Depth 8 -Compress)});isError=$false}
        $response=@{jsonrpc='2.0';id=$rpc.id;result=$result}|ConvertTo-Json -Depth 12 -Compress
        return [pscustomobject]@{RawContentStream=(New-Object IO.MemoryStream(,([Text.Encoding]::UTF8.GetBytes($response))))}
    }
    function Reset-Fixture {
        $script:Calls.Clear();$script:IdentityValidated=$false;$script:PreflightStatus=$null
        $script:RemoteUser='fixture-user';$script:FailTool='';$script:Latency=@{}
        $script:RpcTimeoutSeconds=3;$script:HookBudgetWatch=[pscustomobject]@{ElapsedMilliseconds=0}
        $script:HookBudgetMilliseconds=10500;$script:HookOutputReserveMilliseconds=300
        $script:CaseId='case-'+[Guid]::NewGuid().ToString('N')
        $cfg.strict_injection=$false
    }
    function Run-Prompt {
        $writer=New-Object IO.StringWriter;$previousOut=[Console]::Out
        try {[Console]::SetOut($writer);Invoke-PromptSubmit $cfg ([pscustomobject]@{session_id=$script:CaseId;cwd=$testRoot;prompt='fixture query'})}
        finally {[Console]::SetOut($previousOut)}
        return $writer.ToString()
    }
    $cfg=Get-Config
    Reset-Fixture
    $script:Latency=@{memory_status=50;context_project_recall=100;context_session_start=8500}
    $out=Run-Prompt
    Assert ($script:Calls.Count -ge 3 -and $script:Calls[0].name -eq 'memory_status' -and $script:Calls[1].name -eq 'context_project_recall' -and $script:Calls[2].name -eq 'context_session_start') 'Project context must be fetched before expensive bootstrap'
    Assert ($script:Calls[0].timeout -eq 3 -and $script:Calls[1].timeout -eq 5 -and $script:Calls[1].expected_user -ceq 'fixture-user') 'Only hook project/experience caps may increase to five seconds; authentication stays at three'
    Assert ($out.Contains('PROJECT-CONTEXT-WITH-EMPTY-MEMORY-IDS') -and $out.Contains('BOOTSTRAP-CONTEXT')) 'Slow bootstrap must preserve project and bootstrap blocks'
    Assert (@($script:Calls|Where-Object {$_.name -eq 'experience_recall'}).Count -eq 0 -and $out.Contains('skipped')) 'Experience with less than two seconds left must be explicitly skipped'
    Assert ($script:Calls[2].timeout -eq 10 -and $script:HookBudgetWatch.ElapsedMilliseconds -le 10200) 'Bootstrap must fit the action deadline including output reserve'
    $receipt=Read-JsonFile (Get-ReceiptPath $script:CaseId)
    Assert ($receipt.status -eq 'success' -and $receipt.memory_revision -eq '41.2') 'Successful bootstrap alone may advance the receipt revision'
    Reset-Fixture
    $script:Latency=@{memory_status=50;context_project_recall=100;context_session_start=15000}
    $out=Run-Prompt
    Assert ($out.Contains('PROJECT-CONTEXT-WITH-EMPTY-MEMORY-IDS') -and $out.Contains('refresh is unavailable')) 'Timed out bootstrap must return the independently retrieved project block'
    Assert ($null -eq (Read-JsonFile (Get-ReceiptPath $script:CaseId))) 'Failed bootstrap must not create a success receipt'
    Assert ($script:HookBudgetWatch.ElapsedMilliseconds -le 10200) 'Timed out bootstrap must not consume the output reserve'
    Reset-Fixture
    $script:Latency=@{memory_status=2800;context_project_recall=2800;context_session_start=8500}
    $out=Run-Prompt
    Assert ($script:Calls[2].timeout -eq 4 -and $script:HookBudgetWatch.ElapsedMilliseconds -le 10200 -and $out.Contains('PROJECT-CONTEXT')) 'Earlier request time must reduce the subsequent bootstrap timeout'
    Reset-Fixture
    $script:FailTool='context_project_recall'
    $out=Run-Prompt
    Assert ($out.Contains('BOOTSTRAP-CONTEXT') -and $out.Contains('EXPERIENCE-CONTEXT') -and $out.Contains('project mention recall is unavailable')) 'Project failure must not erase other successful context'
    Reset-Fixture
    Write-JsonAtomic (Get-ReceiptPath $script:CaseId) @{status='success';memory_revision='41.2';private_cached_body='PRIVATE-OLD-BLOCK';first_prompt_pending=$true}
    $script:RemoteUser='unexpected-user'
    $out=Run-Prompt
    Assert ($script:Calls.Count -eq 1 -and -not $out.Contains('PRIVATE-OLD-BLOCK') -and -not $out.Contains('PROJECT-CONTEXT') -and -not $script:IdentityValidated) 'Authentication failure must not retry, call business tools, or leak old context'
    Reset-Fixture
    $script:FailTool='memory_status'
    $out=Run-Prompt
    Assert ($script:Calls.Count -eq 1 -and $null -eq $script:PreflightStatus) 'Failed preflight must not be retried through project and experience calls'
    Reset-Fixture
    $cfg.strict_injection=$true;$script:FailTool='context_session_start'
    $out=Run-Prompt
    Assert (($out|ConvertFrom-Json).decision -eq 'block' -and -not $out.Contains('PROJECT-CONTEXT')) 'Strict missing-context behavior must still block without partial injection'
    Reset-Fixture
    Write-JsonAtomic (Get-ReceiptPath $script:CaseId) @{status='success';memory_revision='41.2';first_prompt_pending=$true}
    $out=Run-Prompt
    Assert (@($script:Calls|Where-Object {$_.name -eq 'context_session_start'}).Count -eq 0) 'Same revision must avoid unnecessary bootstrap'
    Assert ($out.Contains('PROJECT-CONTEXT') -and $out.Contains('EXPERIENCE-CONTEXT')) 'Same-revision path must keep per-prompt retrieval'
    Reset-Fixture
    Write-JsonAtomic (Get-ReceiptPath $script:CaseId) @{status='success';memory_revision='41.2'}
    $script:Latency=@{memory_status=50;context_project_recall=3329;experience_recall=2886}
    $out=Run-Prompt
    Assert ($out.Contains('PROJECT-CONTEXT') -and $out.Contains('EXPERIENCE-CONTEXT') -and -not $out.Contains('systemMessage')) 'Observed healthy project and experience latency must both fit the bounded action'
    Assert ($script:Calls[1].timeout -eq 5 -and $script:Calls[2].timeout -eq 5 -and $script:HookBudgetWatch.ElapsedMilliseconds -eq 6265) 'Five-second caps must remain per request and share the same action clock'
    Reset-Fixture
    Write-JsonAtomic (Get-ReceiptPath $script:CaseId) @{status='success';memory_revision='41.2'}
    $script:Latency=@{memory_status=1000;context_project_recall=4800;experience_recall=7000}
    $out=Run-Prompt
    Assert ($script:Calls[2].timeout -eq 4 -and $script:HookBudgetWatch.ElapsedMilliseconds -eq 9800 -and $out.Contains('PROJECT-CONTEXT') -and $out.Contains('experience lookup is unavailable')) 'Slow earlier stages must clip experience to the remaining deadline and preserve the successful project block'
    Reset-Fixture
    $path=Get-ReceiptPath $script:CaseId
    Write-JsonAtomic $path @{status='success';memory_revision='40.2';first_prompt_pending=$true}
    $before=[IO.File]::ReadAllText($path);$script:FailTool='context_session_start'
    $out=Run-Prompt
    Assert ([IO.File]::ReadAllText($path) -ceq $before) 'Failed revision refresh must preserve the last successful receipt exactly'
    Reset-Fixture
    Write-JsonAtomic (Get-ReceiptPath $script:CaseId) @{status='success';memory_revision='41.2'}
    $script:Latency=@{memory_status=50;context_session_start=15000}
    $writer=New-Object IO.StringWriter;$previousOut=[Console]::Out
    try {[Console]::SetOut($writer);Invoke-SessionStart $cfg ([pscustomobject]@{session_id=$script:CaseId;cwd=$testRoot;source='resume'})}
    finally {[Console]::SetOut($previousOut)}
    $receipt=Read-JsonFile (Get-ReceiptPath $script:CaseId)
    Assert ($receipt.status -eq 'failed' -and $null -eq $receipt.memory_revision -and $writer.ToString().Contains('loading is unavailable')) 'Failed new lifecycle must not pretend its previous successful context was delivered again'
    Reset-Fixture
    $script:HookBudgetWatch.ElapsedMilliseconds=9500
    $out=Run-Prompt
    Assert ($script:Calls.Count -eq 0) 'Less than one second of RPC budget must not launch a request'
    Reset-Fixture
    [void](Invoke-Rpc $cfg 'initialize' @{name='context_project_recall'} 'fixture-init')
    Assert ($script:Calls[0].timeout -eq 3) 'Named recall caps require tools/call rather than any matching parameter name'
    Reset-Fixture
    $script:HookBudgetWatch=$null
    foreach($name in @('context_session_start','context_project_recall','experience_recall')){
        [void](Invoke-McpTool $cfg $name @{})
        Assert ($script:Calls[$script:Calls.Count-1].timeout -eq 3) 'Explicit non-hook business calls must keep their original default three-second cap'
    }
    Reset-Fixture
    $script:HookBudgetWatch=$null;$script:RpcTimeoutSeconds=30
    [void](Invoke-McpTool $cfg 'session_archive_upload' @{})
    Assert ($script:Calls.Count -eq 2 -and $script:Calls[0].timeout -eq 30 -and $script:Calls[1].timeout -eq 30) 'Background worker authentication and upload retain their existing 30 seconds'
    '{"passed":true,"internal_budget_ms":10500,"output_reserve_ms":300,"hook_recall_cap_seconds":5,"auth_cap_seconds":3,"non_hook_cap_seconds":3,"project_first":true,"slow_bootstrap":true,"explicit_experience_skip":true,"auth_no_retry":true,"strict":true,"same_revision_no_bootstrap":true,"failed_refresh_preserves_receipt":true,"worker_budget_unchanged":true}'
} finally {
    $env:EVOLVMEM_CLIENT_HOME=$previousHome
    $resolved=[IO.Path]::GetFullPath($testRoot)
    $tempRoot=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')+'\'
    if(-not $resolved.StartsWith($tempRoot,[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($resolved) -notlike 'evolvmem-total-budget-*'){throw 'Refuse unexpected fixture cleanup path'}
    if([IO.Directory]::Exists($resolved)){Remove-Item -LiteralPath $resolved -Recurse -Force}
}

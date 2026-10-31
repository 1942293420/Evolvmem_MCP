param([Parameter(Mandatory=$true)][string]$ClientPath)
$ErrorActionPreference='Stop'
$utf8=New-Object Text.UTF8Encoding($false)
$testRoot=Join-Path ([IO.Path]::GetTempPath()) ('evolvmem-revision-test-'+[Guid]::NewGuid().ToString('N'))
$previousHome=$env:EVOLVMEM_CLIENT_HOME
function Assert($condition,$message) { if(-not $condition){throw $message} }
try {
    [void][IO.Directory]::CreateDirectory($testRoot)
    $env:EVOLVMEM_CLIENT_HOME=$testRoot
    $seed=@{url='http://127.0.0.1:1/mcp';expected_user='fixture-user';token_env_var='UNUSED';device_id='fixture';projects=@{}}
    [IO.File]::WriteAllText((Join-Path $testRoot 'config.json'),($seed|ConvertTo-Json),$utf8)
    [Console]::SetIn((New-Object IO.StringReader('{}')))
    . $ClientPath -Action snapshot
    $script:Calls=New-Object Collections.Generic.List[object]
    function Invoke-Rpc($Config,[string]$Method,$Params,[string]$RpcId='') {
        if($Method -ne 'tools/call'){throw 'Unexpected RPC method in local fixture'}
        $script:Calls.Add($Params)
        $value=@{authenticated_user='fixture-user';memory_revision='31.7'}
        switch($Params.name){
            'memory_status' { if($Params.arguments.detail -eq 'revision'){$value.detail='revision'}else{$value.active_memories=42;$value.context_ready=$true} }
            'context_session_start' {$value.block='REFRESH-FROM-CHANGED-REVISION';$value.selected_ids=@()}
            'context_project_recall' {$value.block='ARCHIVE-PROJECT-BLOCK';$value.event_ids=@(101);$value.selected_ids=@()}
            'experience_recall' {$value.results=@()}
            default {throw 'Unexpected tool in local fixture'}
        }
        return [pscustomobject]@{content=@([pscustomobject]@{type='text';text=($value|ConvertTo-Json -Depth 8 -Compress)});isError=$false}
    }
    $cfg=Get-Config
    [void](Invoke-McpTool $cfg 'experience_recall' @{query='fixture'})
    Assert (-not $script:Calls[1].arguments.ContainsKey('mode')) 'Explicit experience calls must retain their default full mode.'
    Assert ($script:Calls[0].name -eq 'memory_status' -and $script:Calls[0].arguments.detail -eq 'revision') 'Authentication preflight must request only the lightweight revision shape.'
    $light=Invoke-McpTool $cfg 'memory_status' @{detail='revision'}
    Assert ($light.memory_revision -ceq '31.7' -and $script:Calls.Count -eq 2) 'The same invocation must reuse the authenticated revision preflight.'
    $full=Invoke-McpTool $cfg 'memory_status' @{}
    Assert ($full.active_memories -eq 42 -and $full.context_ready -eq $true -and $script:Calls.Count -eq 3) 'Explicit full status must not be replaced by the lightweight cache.'
    $script:IdentityValidated=$false;$script:PreflightStatus=$null;$script:Calls.Clear()
    Write-JsonAtomic (Get-ReceiptPath 'fixture-session') @{session_id='fixture-session';status='success';memory_revision='30.7';first_prompt_pending=$false}
    $writer=New-Object IO.StringWriter
    $previousOut=[Console]::Out
    try {
        [Console]::SetOut($writer)
        Invoke-PromptSubmit $cfg ([pscustomobject]@{session_id='fixture-session';cwd=$testRoot;prompt='fixture project'})
    } finally {[Console]::SetOut($previousOut)}
    $output=$writer.ToString()
    Assert ($output.Contains('REFRESH-FROM-CHANGED-REVISION')) 'Changed revision must still trigger session context refresh.'
    Assert ($output.Contains('ARCHIVE-PROJECT-BLOCK')) 'Archive block with empty memory IDs must survive the refreshed prompt.'
    $statusCalls=@($script:Calls|Where-Object {$_.name -eq 'memory_status'})
    Assert ($statusCalls.Count -eq 1 -and $statusCalls[0].arguments.detail -eq 'revision') 'Prompt revision check must not invoke a full status scan.'
    $script:IdentityValidated=$false;$script:PreflightStatus=$null;$script:Calls.Clear()
    $cfg.expected_user='different-user'
    $rejected=$false
    try {[void](Invoke-McpTool $cfg 'experience_recall' @{query='fixture'})}catch{$rejected=$true}
    Assert ($rejected -and $script:Calls.Count -eq 1) 'Identity mismatch must still stop before the requested business tool.'
    '{"passed":true,"lightweight_preflight":true,"same_action_cache":true,"explicit_full_status":true,"revision_refresh":true,"empty_memory_ids":true,"identity_guard":true}'
} finally {
    $env:EVOLVMEM_CLIENT_HOME=$previousHome
    if([IO.Directory]::Exists($testRoot)){[IO.Directory]::Delete($testRoot,$true)}
}

param([Parameter(Mandatory=$true)][string]$ClientPath,
      [Parameter(Mandatory=$true)][string]$FixtureUrl,
      [ValidateSet('hang','slowdrip','prompt','clipped','success')][string]$Case='hang')
$ErrorActionPreference='Stop'
$testRoot=Join-Path ([IO.Path]::GetTempPath()) ('evolvmem-experience-deadline-'+[Guid]::NewGuid().ToString('N'))
$previousHome=$env:EVOLVMEM_CLIENT_HOME
$utf8=New-Object Text.UTF8Encoding($false)
function Assert($condition,$message){if(-not $condition){throw $message}}
try {
    [void][IO.Directory]::CreateDirectory($testRoot)
    $env:EVOLVMEM_CLIENT_HOME=$testRoot
    $seed=@{url=$FixtureUrl;expected_user='fixture-user';token_env_var='UNUSED';device_id='fixture';projects=@{};strict_injection=$false}
    [IO.File]::WriteAllText((Join-Path $testRoot 'config.json'),($seed|ConvertTo-Json),$utf8)
    [Console]::SetIn((New-Object IO.StringReader('{}')))
    . $ClientPath -Action snapshot
    Assert ($script:ClientHome -ceq $testRoot) 'Fixture home isolation failed'
    function Get-ActionToken($Config){return 'fixture-token-no-secret'}
    function Get-RepoSnapshot([string]$Path){return @{kind='unknown'}}
    $cfg=Get-Config
    $script:RpcTimings=New-Object Collections.Generic.List[object]
    $script:ActualRpc=(Get-Command Invoke-Rpc).ScriptBlock
    function Invoke-Rpc($Config,[string]$Method,$Parameters,[string]$RpcId){
        $clock=[Diagnostics.Stopwatch]::StartNew()
        try {& $script:ActualRpc $Config $Method $Parameters $RpcId}
        finally {$script:RpcTimings.Add(@{name=[string](Get-RecordValue $Parameters 'name' $Method);elapsed_ms=$clock.ElapsedMilliseconds})}
    }
    $script:HookBudgetMilliseconds=10500;$script:HookOutputReserveMilliseconds=300
    $script:HookBudgetWatch=[Diagnostics.Stopwatch]::StartNew()
    $script:IdentityValidated=$true
    $script:RpcTimeoutSeconds=3
    $timedOut=$false
    $clock=[Diagnostics.Stopwatch]::StartNew()
    if($Case -eq 'prompt'){
        $script:IdentityValidated=$false;$script:PreflightStatus=$null
        $session='fixture-budget-session'
        Write-JsonAtomic (Get-ReceiptPath $session) @{status='success';memory_revision='41.2';first_prompt_pending=$true}
        $longPrompt='START-'+('x'*6000)+'-END'
        $writer=New-Object IO.StringWriter;$prior=[Console]::Out
        try {[Console]::SetOut($writer);Invoke-PromptSubmit $cfg ([pscustomobject]@{session_id=$session;cwd=$testRoot;prompt=$longPrompt})}
        finally {[Console]::SetOut($prior)}
        $output=$writer.ToString()
        Assert ($output.Contains('PROJECT-CONTEXT') -and $output.Contains('experience lookup is unavailable')) 'Experience deadline must preserve project context and report unavailable'
    }
    else {
        if($Case -eq 'clipped'){$script:HookBudgetMilliseconds=2400}
        try {$reply=Invoke-McpTool $cfg 'experience_recall' ([ordered]@{query='fixture current query';project='';session_id='fixture-budget-session';mode='hook'})}
        catch {$timedOut=$true}
    }
    $elapsed=$clock.ElapsedMilliseconds
    $experience=@($script:RpcTimings|Where-Object {$_.name -ceq 'experience_recall'})
    $result=@{case=$Case;elapsed_ms=$elapsed;timed_out=$timedOut;rpc_timings=$script:RpcTimings.ToArray();powershell=$PSVersionTable.PSVersion.ToString()}
    [Console]::WriteLine(($result|ConvertTo-Json -Depth 6 -Compress))
    Assert ($experience.Count -eq 1 -and $experience[0].elapsed_ms -lt 2000) 'Experience RPC including response body exceeded the 2-second envelope'
    if($Case -eq 'success'){Assert (-not $timedOut -and $reply.results[0].summary -ceq 'EXPERIENCE-CONTEXT') 'Completed response must still decode and authenticate'}
    elseif($Case -ne 'prompt'){Assert $timedOut 'A hanging/slow-drip server must fail before it completes'}
} finally {
    $env:EVOLVMEM_CLIENT_HOME=$previousHome
    $resolved=[IO.Path]::GetFullPath($testRoot)
    $tempRoot=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')+'\'
    if(-not $resolved.StartsWith($tempRoot,[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($resolved) -notlike 'evolvmem-experience-deadline-*'){throw 'Refuse unexpected fixture cleanup path'}
    if([IO.Directory]::Exists($resolved)){Remove-Item -LiteralPath $resolved -Recurse -Force}
}

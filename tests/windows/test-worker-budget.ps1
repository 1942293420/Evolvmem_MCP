param([string]$ClientPath)
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('evo-budget-'+[guid]::NewGuid().ToString('N'))
$env:EVOLVMEM_CLIENT_HOME=$root
[void][IO.Directory]::CreateDirectory($root)
$config=@{url='http://127.0.0.1:1/mcp';token_env_var='UNUSED';expected_user='test';device_id='test'}
[IO.File]::WriteAllText((Join-Path $root 'config.json'),($config|ConvertTo-Json))
[Console]::SetIn((New-Object IO.StringReader('{}')))
. $ClientPath -Action snapshot
foreach($sid in @('a','b','c')) {
 $path=Join-Path $root ($sid+'.jsonl')
 [IO.File]::WriteAllText($path,('{"type":"session_meta","payload":{"id":"'+$sid+'","cwd":"D:/test"}}'+"`n"))
 [void](Capture-Transcript (Get-Config) ([pscustomobject]@{session_id=$sid;transcript_path=$path;cwd='D:/test'}))
}
$script:calls=0
function Invoke-McpTool($Config,[string]$Name,$Arguments,[string]$RpcId) {
 $script:calls++
 Start-Sleep -Milliseconds 220
 return [pscustomobject]@{status='archived';next_offset=$Arguments.total_bytes;source_sha256=$Arguments.sha256;source_order=$Arguments.source_order;current=$true;archive_id='fixture';extraction_status='disabled'}
}
$script:WorkerBudgetMilliseconds=100
$script:WorkerBudgetWatch=[Diagnostics.Stopwatch]::StartNew()
[void](Invoke-UploadQueue (Get-Config))
if($script:calls -gt 1){throw 'Worker exceeded voluntary budget across queued sessions'}
$script:WorkerBudgetWatch=$null
[void](Invoke-UploadQueue (Get-Config))
if(@(Get-ChildItem (Join-Path $root 'queue') -Filter '*.json').Count -ne 0){throw 'Budgeted work did not resume next round'}
'worker_budget_and_resume_passed'

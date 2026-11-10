param([string]$ClientPath)
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('evo-active-'+[guid]::NewGuid().ToString('N'))
$env:EVOLVMEM_CLIENT_HOME=$root
[void][IO.Directory]::CreateDirectory($root)
$config=@{url='http://127.0.0.1:1/mcp';token_env_var='UNUSED';expected_user='test';device_id='test';upload_chunks_per_session=16}
[IO.File]::WriteAllText((Join-Path $root 'config.json'),($config|ConvertTo-Json))
[Console]::SetIn((New-Object IO.StringReader('{}')))
. $ClientPath -Action snapshot
$script:ChunkBytes=64
$script:heads=0
function Invoke-McpTool($Config,[string]$Name,$Arguments,[string]$RpcId) {
 $next=$Arguments.offset+[Convert]::FromBase64String($Arguments.content_b64).Length
 if($next -eq $Arguments.total_bytes) {
  if($Arguments.Contains('current_sha256')) {$script:heads++}
  return [pscustomobject]@{status='archived';next_offset=$next;source_sha256=$Arguments.sha256;source_order=$Arguments.source_order;current=$Arguments.Contains('current_sha256');archive_id='fixture';extraction_status='disabled'}
 }
 return [pscustomobject]@{status='receiving';next_offset=$next}
}
$path=Join-Path $root 'active.jsonl'
[IO.File]::WriteAllText($path,('{"type":"session_meta","payload":{"id":"active","cwd":"D:/test"}}'+"`n"+'{"message":"'+('x'*2100)+'"}'+"`n"))
Save-SessionRegistration (Get-Config) ([pscustomobject]@{session_id='active';transcript_path=$path;cwd='D:/test'})
for($i=0;$i -lt 6;$i++) {
 [IO.File]::AppendAllText($path,('{"message":"append'+$i+'"}'+"`n"))
 Invoke-Worker (Get-Config)
}
if($script:heads -lt 2){throw 'Continuous append starved current server head'}
'active_head_progress_passed'
# Even when capture has no budget, an existing pending upload must run.
[IO.File]::AppendAllText($path,('{"message":"later"}'+"`n"))
[void](Capture-Transcript (Get-Config) ([pscustomobject]@{session_id='active';transcript_path=$path;cwd='D:/test'}))
$script:CaptureBudgetMilliseconds=0
$before=@(Get-SessionQueueEntries 'active')[0].Manifest.next_offset
Invoke-Worker (Get-Config)
$after=@(Get-SessionQueueEntries 'active')[0].Manifest.next_offset
if($after -le $before){throw 'Capture budget exhausted blocked all upload progress'}
'capture_budget_reserves_upload_passed'

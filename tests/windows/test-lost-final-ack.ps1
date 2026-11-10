param([string]$ClientPath)
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('evo-lost-'+[guid]::NewGuid().ToString('N'))
$env:EVOLVMEM_CLIENT_HOME=$root
[void][IO.Directory]::CreateDirectory($root)
$config=@{url='http://127.0.0.1:1/mcp';token_env_var='UNUSED';expected_user='test';device_id='test'}
[IO.File]::WriteAllText((Join-Path $root 'config.json'),($config|ConvertTo-Json))
[Console]::SetIn((New-Object IO.StringReader('{}')))
. $ClientPath -Action snapshot
$source=Join-Path $root 'fixture.jsonl'
[IO.File]::WriteAllText($source,('{"type":"session_meta","payload":{"id":"lost","cwd":"D:/test"}}'+"`n"))
[void](Capture-Transcript (Get-Config) ([pscustomobject]@{session_id='lost';transcript_path=$source;cwd='D:/test'}))
$script:uploads=0;$script:remote=$null;$script:WrongOrder=$true
function Invoke-McpTool($Config,[string]$Name,$Arguments,[string]$RpcId) {
 if($Name -ceq 'session_archive_upload') {
  $script:uploads++
  $script:remote=[pscustomobject]@{status='archived';next_offset=$Arguments.total_bytes;total_bytes=$Arguments.total_bytes;source_sha256=$Arguments.sha256;source_order=$Arguments.source_order;current=$true;archive_id='fixture';extraction_status='disabled';source_format='codex-jsonl';payload_state='available';authenticated_user='test'}
  throw (New-Object TimeoutException('Simulated final HTTP response lost after server commit'))
 }
 if($Name -ceq 'session_archive_status') {
  if($script:WrongOrder){$copy=$script:remote|ConvertTo-Json|ConvertFrom-Json;$copy.source_order++;return $copy}
  return $script:remote
 }
 throw 'Unexpected mock RPC'
}
[void](Invoke-UploadQueue (Get-Config))
[void](Invoke-UploadQueue (Get-Config))
if($script:uploads -ne 1 -or @(Get-SessionQueueEntries 'lost').Count -ne 1){throw 'Final grace must not replay or accept a different order'}
$script:WrongOrder=$false
[void](Invoke-UploadQueue (Get-Config))
if($script:uploads -ne 1 -or @(Get-SessionQueueEntries 'lost').Count -ne 0){throw 'Exact status read-back must recover lost final ACK without retransmission'}
$receipt=Read-JsonFile (Join-Path $root ('archive-status\'+$script:remote.source_sha256+'.json'))
if($receipt.receipt_evidence -cne 'status_readback' -or $receipt.total_bytes -ne $script:remote.total_bytes){throw 'Recovered receipt lacks exact evidence'}
'lost_final_ack_wrong_order_grace_and_exact_recovery_passed'

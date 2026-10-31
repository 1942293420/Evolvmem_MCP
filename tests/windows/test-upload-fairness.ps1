param([Parameter(Mandatory=$true)][string]$ClientPath,[string]$ResultPath)
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('evo-fair-'+[Guid]::NewGuid().ToString('N').Substring(0,8))
$env:EVOLVMEM_CLIENT_HOME=Join-Path $root 'client'
[void][IO.Directory]::CreateDirectory($env:EVOLVMEM_CLIENT_HOME)
$utf8=New-Object Text.UTF8Encoding($false)
$config=@{url='http://127.0.0.1:1/mcp';expected_user='fixture';token_env_var='UNUSED';device_id='fixture';projects=@{}}
[IO.File]::WriteAllText((Join-Path $env:EVOLVMEM_CLIENT_HOME 'config.json'),($config|ConvertTo-Json),$utf8)
[Console]::SetIn((New-Object IO.StringReader('{}')))
. $ClientPath -Action snapshot
$script:ChunkBytes=64
$script:remote=@{};$script:remoteOrders=@{};$script:archived=@{};$script:heads=@{};$script:rpcCount=0
$script:OnRpc=$null;$script:FailNext=$false;$script:requests=New-Object Collections.Generic.List[object]
function Assert($test,$message){if(-not $test){throw $message}}
function Invoke-McpTool($Config,[string]$Name,$Arguments,[string]$RpcId) {
 Assert ($Name-ceq 'session_archive_upload') 'unexpected_rpc'
 $script:rpcCount++
 $sid=$Arguments.session_id;$sha=$Arguments.sha256;$offset=[int]$Arguments.offset
 $script:requests.Add([pscustomobject]@{sid=$sid;sha=$sha;offset=$offset;order=$Arguments.source_order;current=$Arguments.Contains('current_sha256');request_id=$RpcId})
 if($script:FailNext){$script:FailNext=$false;throw 'synthetic_transport_failure'}
 # Mirror the server's immutable-version order rule, including its narrow
 # current-recapture exception. A historical retry cannot invent a new order.
 if($script:remoteOrders.ContainsKey($sha)-and [long]$script:remoteOrders[$sha]-ne [long]$Arguments.source_order){
  $aboveHead=-not $script:heads.ContainsKey($sid)-or [long]$Arguments.source_order-gt [long]$script:heads[$sid].order
  if(-not ($Arguments.Contains('current_sha256')-and $Arguments.current_sha256-ceq $sha-and [long]$Arguments.source_order-gt [long]$script:remoteOrders[$sha]-and $aboveHead)){
   throw (New-ClientError 'upload_metadata_conflict' 'synthetic immutable version order conflict')
  }
 }
 $script:remoteOrders[$sha]=$Arguments.source_order
 $part=[Convert]::FromBase64String($Arguments.content_b64)
 if(-not $script:remote.ContainsKey($sha)){$script:remote[$sha]=New-Object IO.MemoryStream}
 $stream=$script:remote[$sha]
 Assert ($offset-le $stream.Length) 'remote_gap'
 if($offset-lt $stream.Length){$existing=$stream.ToArray();for($i=0;$i-lt $part.Length;$i++){Assert ($existing[$offset+$i]-eq $part[$i]) 'replay_bytes_changed'}}
 else{$stream.Write($part,0,$part.Length)}
 if($stream.Length-eq $Arguments.total_bytes){
  Assert ((Get-Sha256Hex $stream.ToArray())-ceq $sha) 'whole_checksum'
  $script:archived[$sha]=$true
  if($Arguments.Contains('current_sha256') -and $Arguments.current_sha256-ceq $sha){
   $local=Read-JsonFile (Get-SessionPath $sid)
   Assert ($local.current_sha256-ceq $sha -and [long]$local.source_order-eq [long]$Arguments.source_order) 'stale_current_declaration'
   if(-not $script:heads.ContainsKey($sid)-or $Arguments.source_order-gt $script:heads[$sid].order){$script:heads[$sid]=@{sha=$sha;order=$Arguments.source_order}}
  }
  $current=$script:heads.ContainsKey($sid)-and $script:heads[$sid].sha-ceq $sha
  $reply=[pscustomobject]@{status='archived';next_offset=$stream.Length;source_sha256=$sha;archive_id=1;source_order=$Arguments.source_order;current=$current;extraction_status='pending'}
  if($script:OnRpc){& $script:OnRpc $Arguments $true}
  return $reply
 }
 if($script:OnRpc){& $script:OnRpc $Arguments $false}
 return [pscustomobject]@{status='receiving';next_offset=$stream.Length}
}
$sid='fair-fixture';$source=Join-Path $root 'fixture.jsonl'
$initial='{"type":"session_meta","payload":{"id":"fair-fixture","cwd":"C:/fixture"}}'+"`n"+'{"type":"event_msg","payload":{"message":"'+('a'*2200)+'"}}'+"`n"
[IO.File]::WriteAllText($source,$initial,$utf8)
$event=[pscustomobject]@{session_id=$sid;transcript_path=$source;cwd='C:/fixture'}
$snapshots=New-Object Collections.Generic.List[string]
$rounds=New-Object Collections.Generic.List[object]
for($tick=0;$tick-lt 5;$tick++){
 if($tick-gt 0){[IO.File]::AppendAllText($source,('{"type":"event_msg","payload":{"message":"tick'+$tick+'"}}'+"`n"),$utf8)}
 [void](Capture-Transcript $config $event)
 $reg=Read-JsonFile (Get-SessionPath $sid);$snapshots.Add($reg.current_sha256)
 $callsBefore=$script:rpcCount
 [void](Invoke-UploadQueue $config)
 Assert (($script:rpcCount-$callsBefore)-le 16) 'per_session_chunk_budget_exceeded'
 $reg=Read-JsonFile (Get-SessionPath $sid)
 $rounds.Add([pscustomobject]@{tick=$tick;rpc_calls=($script:rpcCount-$callsBefore);archived_count=$script:archived.Count;anchor_confirmed=($reg.anchor_confirmed_sha256-ceq $reg.current_sha256)})
}
$earlyCompleted=$script:archived.ContainsKey($snapshots[0])
$preserved=@(Get-SessionQueueEntries $sid).Count+$script:archived.Count -ge $snapshots.Count
for($tick=0;$tick-lt 20 -and @(Get-SessionQueueEntries $sid).Count-gt 0;$tick++){
 $callsBefore=$script:rpcCount;[void](Invoke-UploadQueue $config)
 Assert (($script:rpcCount-$callsBefore)-le 16) 'quiescent_per_session_chunk_budget_exceeded'
}
$reg=Read-JsonFile (Get-SessionPath $sid)
$result=[ordered]@{first_selected_completed_during_active_updates=$earlyCompleted;all_original_snapshots_retained_or_archived=$preserved;all_eventually_archived=($script:archived.Count-eq $snapshots.Count);pending=@(Get-SessionQueueEntries $sid).Count;latest_anchor_confirmed=($reg.anchor_confirmed_sha256-ceq $reg.current_sha256);rounds=$rounds.ToArray();fixture_root=$root;production_writes=0;real_network_calls=0}
if($ResultPath){[IO.File]::WriteAllText($ResultPath,($result|ConvertTo-Json -Depth 6),$utf8)}
$result|ConvertTo-Json -Depth 6
Assert $earlyCompleted 'starvation_first_selected_never_completed_while_current_changed'
Assert $preserved 'unacknowledged_snapshot_discarded'
Assert $result.all_eventually_archived 'quiescent_queue_did_not_drain'
Assert $result.latest_anchor_confirmed 'latest_not_confirmed_after_quiescence'

# Edge cases use the actual bridge's capture, durable queue and DPAPI path.
# Only the network endpoint is replaced; no production client state is loaded.
$mockTransport=${function:Invoke-McpTool}
# Conversion must preserve business keys even when a caller already holds a
# dictionary, including names also exposed by PowerShell's object adapter.
foreach($record in @(@{session_id='s';Count=23;Keys='business-keys'},[ordered]@{session_id='s';Count=23;Keys='business-keys'},[pscustomobject]@{session_id='s';Count=23;Keys='business-keys'})){
 $copy=ConvertTo-OrderedRecord $record
 Assert ($copy['session_id']-ceq 's' -and $copy['Count']-eq 23 -and $copy['Keys']-ceq 'business-keys') 'record_conversion_lost_business_keys'
 Assert ($copy.Contains('Values')-eq $false -and $copy.Contains('IsReadOnly')-eq $false) 'record_conversion_copied_adapter_metadata'
}
Assert ((ConvertTo-OrderedRecord $null).Count-eq 0) 'null_record_conversion_not_empty'
function Start-Case([string]$Name) {
 $script:ClientHome=Join-Path $root $Name;$env:EVOLVMEM_CLIENT_HOME=$script:ClientHome
 foreach($dir in @('','queue','sessions','session-cache','receipts','archive-status')){Ensure-Directory (Join-Path $script:ClientHome $dir)}
 [IO.File]::WriteAllText((Join-Path $script:ClientHome 'config.json'),($config|ConvertTo-Json),$utf8)
 $script:caseSid='fixture-'+$Name;$script:caseSource=Join-Path $root ($Name+'.jsonl')
 $script:caseA='{"type":"session_meta","payload":{"id":"'+$script:caseSid+'","cwd":"C:/fixture"}}'+"`n"+'{"type":"event_msg","payload":{"message":"'+('a'*2200)+'"}}'+"`n"
 [IO.File]::WriteAllText($script:caseSource,$script:caseA,$utf8)
 $script:caseEvent=[pscustomobject]@{session_id=$script:caseSid;transcript_path=$script:caseSource;cwd='C:/fixture'}
 $script:remote=@{};$script:remoteOrders=@{};$script:archived=@{};$script:heads=@{};$script:rpcCount=0
 $script:OnRpc=$null;$script:FailNext=$false;$script:requests=New-Object Collections.Generic.List[object]
 [void](Capture-Transcript $config $script:caseEvent)
}
$checks=New-Object Collections.Generic.List[string]
$checks.Add('dictionary_and_object_conversion_preserves_business_count_and_keys')
Start-Case 'restart'
$a=(Read-JsonFile (Get-SessionPath $script:caseSid)).current_sha256
[void](Invoke-UploadQueue $config)
$beforeRestart=@(Get-SessionQueueEntries $script:caseSid)[0].Manifest
Assert ($beforeRestart.upload_inflight-eq $true -and $beforeRestart.next_offset-eq 1024) 'durable_selection_and_offset_required'
# Reload the whole bridge to prove selection is persisted, not process-local.
[Console]::SetIn((New-Object IO.StringReader('{}')))
. $ClientPath -Action snapshot
Set-Item -Path Function:Invoke-McpTool -Value $mockTransport
$script:ChunkBytes=64
[IO.File]::AppendAllText($script:caseSource,('{"type":"event_msg","payload":{"message":"new"}}'+"`n"),$utf8)
[void](Capture-Transcript $config $script:caseEvent)
$requestIndex=$script:requests.Count
[void](Invoke-UploadQueue $config)
Assert ($script:requests[$requestIndex].sha-ceq $a -and $script:requests[$requestIndex].offset-eq 1024 -and -not $script:requests[$requestIndex].current) 'restart_did_not_resume_exact_history_offset'
Assert ([long]$script:requests[$requestIndex].order-eq [long]$beforeRestart.source_order) 'restart_changed_order'
[void](Invoke-UploadQueue $config)
Assert $script:archived.ContainsKey($a) 'restart_did_not_complete_selected_version'
Assert (-not (Read-JsonFile (Get-SessionPath $script:caseSid)).anchor_confirmed_sha256) 'old_completion_confirmed_new_current'
$checks.Add('durable_restart_and_current_to_history')

Start-Case 'midchunk'
$a=(Read-JsonFile (Get-SessionPath $script:caseSid)).current_sha256
$script:injectOnce=$true
$script:OnRpc={param($Arguments,$Complete)
 if($script:injectOnce){$script:injectOnce=$false;[IO.File]::AppendAllText($script:caseSource,('{"type":"event_msg","payload":{"message":"interleave"}}'+"`n"),$utf8);[void](Capture-Transcript $config $script:caseEvent)}
}
[void](Invoke-UploadQueue $config)
Assert ($script:requests[0].current -and -not $script:requests[1].current) 'current_not_rechecked_between_chunks'
Assert (@(Get-SessionQueueEntries $script:caseSid).Count-eq 2) 'capture_queue_overwritten'
$checks.Add('capture_between_chunks_preserves_new_queue')

Start-Case 'aba'
$a=(Read-JsonFile (Get-SessionPath $script:caseSid)).current_sha256
[void](Invoke-UploadQueue $config);[void](Invoke-UploadQueue $config)
$priorOrder=(Read-JsonFile (Get-SessionPath $script:caseSid)).source_order
$script:injectOnce=$true
$script:OnRpc={param($Arguments,$Complete)
 if($Complete-and $script:injectOnce){
  $script:injectOnce=$false
  [IO.File]::AppendAllText($script:caseSource,('{"type":"event_msg","payload":{"message":"B"}}'+"`n"),$utf8);[void](Capture-Transcript $config $script:caseEvent)
  [IO.File]::WriteAllText($script:caseSource,$script:caseA,$utf8);[void](Capture-Transcript $config $script:caseEvent)
 }
}
[void](Invoke-UploadQueue $config)
$reg=Read-JsonFile (Get-SessionPath $script:caseSid)
Assert ($reg.current_sha256-ceq $a -and [long]$reg.source_order-gt [long]$priorOrder -and -not $reg.anchor_confirmed_sha256) 'aba_old_order_falsely_confirmed'
Assert (@(Get-SessionQueueEntries $script:caseSid|Where-Object {$_.Manifest.sha256-ceq $a}).Count-eq 1) 'aba_new_order_queue_deleted'
$script:OnRpc=$null
[void](Invoke-UploadQueue $config)
$reg=Read-JsonFile (Get-SessionPath $script:caseSid)
Assert ($reg.anchor_confirmed_sha256-ceq $a -and $script:heads[$script:caseSid].order-eq $reg.source_order) 'aba_new_order_not_confirmed'
$duplicateOffsets=@($script:requests|Group-Object sha,offset|Where-Object {$_.Count-gt 1})
foreach($group in $duplicateOffsets){Assert (@($group.Group.request_id|Select-Object -Unique).Count-eq 1) 'resume_changed_request_id'}
$checks.Add('aba_terminal_order_and_stable_request_id')

Start-Case 'aba-history-conflict'
$a=(Read-JsonFile (Get-SessionPath $script:caseSid)).current_sha256
[void](Invoke-UploadQueue $config)
$originalOrder=$script:remoteOrders[$a]
$originalOffset=$script:remote[$a].Length
# Revisit A with a new local order, then leave it before that order is sent.
[IO.File]::AppendAllText($script:caseSource,('{"type":"event_msg","payload":{"message":"B"}}'+"`n"),$utf8);[void](Capture-Transcript $config $script:caseEvent)
[IO.File]::WriteAllText($script:caseSource,$script:caseA,$utf8);[void](Capture-Transcript $config $script:caseEvent)
$revisitedOrder=(Read-JsonFile (Get-SessionPath $script:caseSid)).source_order
[IO.File]::AppendAllText($script:caseSource,('{"type":"event_msg","payload":{"message":"B-new"}}'+"`n"),$utf8);[void](Capture-Transcript $config $script:caseEvent)
$b=(Read-JsonFile (Get-SessionPath $script:caseSid)).current_sha256
$nextIndex=$script:requests.Count
[void](Invoke-UploadQueue $config)
$conflict=@(Get-SessionQueueEntries $script:caseSid|Where-Object {$_.Manifest.sha256-ceq $a})[0]
Assert ($conflict.Manifest.last_error-ceq 'upload_metadata_conflict' -and $conflict.Manifest.next_offset-eq $originalOffset) 'history_order_conflict_not_retained'
Assert ([long]$conflict.Manifest.source_order-eq [long]$revisitedOrder -and [long]$revisitedOrder-gt [long]$originalOrder) 'history_conflict_rewrote_order'
Assert ($conflict.Manifest.upload_inflight-eq $true -and [IO.File]::Exists(($conflict.Path-replace '\.json$','.bin'))) 'history_conflict_discarded_payload_or_marker'
Assert (-not $script:requests[$nextIndex].current -and -not $script:archived.ContainsKey($a)) 'history_conflict_forged_current_or_archive'
Assert ($script:remote[$a].Length-eq $originalOffset -and $script:remoteOrders[$a]-eq $originalOrder) 'history_conflict_changed_server_prefix'
$nextIndex=$script:requests.Count
[void](Invoke-UploadQueue $config)
Assert ($script:requests[$nextIndex].sha-ceq $b -and $script:requests[$nextIndex].current) 'history_conflict_starved_healthy_current'
Assert (-not (Read-JsonFile (Get-SessionPath $script:caseSid)).anchor_confirmed_sha256) 'history_conflict_falsely_confirmed_current'
$checks.Add('partial_aba_history_conflict_preserves_error_and_yields_to_healthy_current')

Start-Case 'failed'
$a=(Read-JsonFile (Get-SessionPath $script:caseSid)).current_sha256
$script:FailNext=$true
[void](Invoke-UploadQueue $config)
$failed=@(Get-SessionQueueEntries $script:caseSid)[0]
Assert ([string]$failed.Manifest.last_error) 'failed_selection_not_recorded'
$failedCalls=@($script:requests|Where-Object {$_.sha-ceq $a}).Count
[IO.File]::AppendAllText($script:caseSource,('{"type":"event_msg","payload":{"message":"next"}}'+"`n"),$utf8);[void](Capture-Transcript $config $script:caseEvent)
$b=(Read-JsonFile (Get-SessionPath $script:caseSid)).current_sha256
[void](Invoke-UploadQueue $config)
Assert (@($script:requests|Where-Object {$_.sha-ceq $a}).Count-eq $failedCalls) 'failed_selection_replayed'
Assert (@($script:requests|Where-Object {$_.sha-ceq $b}).Count-gt 0) 'failed_selection_starved_new_current'
Assert ([IO.File]::Exists(($failed.Path-replace '\.json$','.bin'))) 'failed_payload_deleted'
$checks.Add('failed_selection_preserved_without_blocking_new_current')

Start-Case 'transient'
$a=(Read-JsonFile (Get-SessionPath $script:caseSid)).current_sha256
[void](Invoke-UploadQueue $config)
$script:FailNext=$true
[void](Invoke-UploadQueue $config)
$failed=@(Get-SessionQueueEntries $script:caseSid)[0]
Assert ([string]$failed.Manifest.last_error -and $failed.Manifest.next_offset-eq 1024) 'transient_failure_progress_lost'
$failedRequest=$script:requests[$script:requests.Count-1]
$nextIndex=$script:requests.Count
[void](Invoke-UploadQueue $config)
Assert ($script:requests[$nextIndex].offset-eq 1024 -and $script:requests[$nextIndex].request_id-ceq $failedRequest.request_id -and $script:requests[$nextIndex].order-eq $failedRequest.order) 'transient_retry_not_idempotent'
[void](Invoke-UploadQueue $config)
Assert ($script:archived.ContainsKey($a) -and (Read-JsonFile (Get-SessionPath $script:caseSid)).anchor_confirmed_sha256-ceq $a) 'unchanged_current_permanently_frozen_after_transient_error'
$checks.Add('transient_retry_preserves_request_order_offset_and_completes_unchanged_current')

Start-Case 'missing'
[void](Invoke-UploadQueue $config)
$missing=@(Get-SessionQueueEntries $script:caseSid)[0]
# Synthetic payload is retained under a different name to model an unavailable
# cache; no production queue files are moved or deleted by this fixture.
[IO.File]::Move(($missing.Path-replace '\.json$','.bin'),($missing.Path+'.held'))
[IO.File]::AppendAllText($script:caseSource,('{"type":"event_msg","payload":{"message":"next"}}'+"`n"),$utf8);[void](Capture-Transcript $config $script:caseEvent)
$b=(Read-JsonFile (Get-SessionPath $script:caseSid)).current_sha256
[void](Invoke-UploadQueue $config)
Assert (@($script:requests|Where-Object {$_.sha-ceq $b}).Count-gt 0) 'missing_selection_starved_new_current'
Assert ([IO.File]::Exists($missing.Path)) 'missing_payload_manifest_deleted'
$checks.Add('missing_cache_preserves_manifest_and_allows_new_current')

Start-Case 'rebuilt-anchor-order'
$sessionPath=Get-SessionPath $script:caseSid
$reg=ConvertTo-OrderedRecord (Read-JsonFile $sessionPath)
$reg['manual_binding_fixture']='preserve-this-value'
Write-JsonAtomic $sessionPath $reg
$originalOrder=[long]$reg.source_order
$a=[string]$reg.current_sha256
$old=@(Get-SessionQueueEntries $script:caseSid)[0]
[IO.File]::Move($old.Path,($old.Path+'.held'))
[IO.File]::Move(($old.Path-replace '\.json$','.bin'),($old.Path+'.bin.held'))
[void](Invoke-WithClientLock { Initialize-SessionSourceState $config $script:caseSid } 5000 (Get-SessionLockScope $script:caseSid))
$rebuilt=@(Get-SessionQueueEntries $script:caseSid)[0]
$reg=Read-JsonFile $sessionPath
Assert ($reg.current_sha256-ceq $a -and [long]$reg.source_order-gt $originalOrder -and [long]$reg.source_order-eq [long]$rebuilt.Manifest.source_order) 'rebuilt_anchor_order_not_registered'
Assert ($reg.manual_binding_fixture-ceq 'preserve-this-value' -and -not $reg.anchor_confirmed_sha256) 'rebuilt_anchor_overwrote_binding_or_confirmed_without_receipt'
$preparedOrder=[long]$reg.source_order
[void](Invoke-WithClientLock { Initialize-SessionSourceState $config $script:caseSid } 5000 (Get-SessionLockScope $script:caseSid))
Assert ([long](Read-JsonFile $sessionPath).source_order-eq $preparedOrder) 'repeated_initialization_raised_anchor_order'
for($tick=0;$tick-lt 3;$tick++){[void](Invoke-UploadQueue $config)}
$reg=Read-JsonFile $sessionPath
Assert ($reg.anchor_confirmed_sha256-ceq $a -and $reg.manual_binding_fixture-ceq 'preserve-this-value') 'rebuilt_anchor_not_confirmed_or_binding_lost'
[void](Invoke-WithClientLock { Initialize-SessionSourceState $config $script:caseSid } 5000 (Get-SessionLockScope $script:caseSid))
$reg=Read-JsonFile $sessionPath
Assert ([long]$reg.source_order-eq $preparedOrder -and $reg.anchor_confirmed_sha256-ceq $a -and @(Get-SessionQueueEntries $script:caseSid).Count-eq 0) 'confirmed_anchor_recreated_or_renumbered'
$checks.Add('rebuilt_anchor_order_is_monotonic_idempotent_and_preserves_registration')

Start-Case 'mismatched-cache-order'
$sessionPath=Get-SessionPath $script:caseSid
$reg=Read-JsonFile $sessionPath
$a=[string]$reg.current_sha256;$originalOrder=[long]$reg.source_order
$old=@(Get-SessionQueueEntries $script:caseSid)[0]
[IO.File]::Move($old.Path,($old.Path+'.held'))
[IO.File]::Move(($old.Path-replace '\.json$','.bin'),($old.Path+'.bin.held'))
Save-ProtectedBytes (Join-Path (Join-Path $script:ClientHome 'session-cache') $reg.cache_file) ($utf8.GetBytes($script:caseA+' '))
[void](Invoke-WithClientLock { Initialize-SessionSourceState $config $script:caseSid } 5000 (Get-SessionLockScope $script:caseSid))
$reg=Read-JsonFile $sessionPath
Assert ($reg.current_sha256-ceq $a -and [long]$reg.source_order-eq $originalOrder -and -not $reg.anchor_confirmed_sha256) 'different_cached_sha_rebound_current_order'
$checks.Add('mismatched_cache_cannot_rebind_current_order_or_confirmation')
$result['regressions']=$checks.ToArray();$result['regression_count']=$checks.Count
if($ResultPath){[IO.File]::WriteAllText($ResultPath,($result|ConvertTo-Json -Depth 6),$utf8)}
@{all_passed=$true;regressions=$checks.ToArray();production_writes=0;real_network_calls=0}|ConvertTo-Json -Depth 4

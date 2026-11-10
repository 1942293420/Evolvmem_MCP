param([string]$ClientPath,[string]$CoveragePath)
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('evo-prefix-'+[guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($root)
$cfg=@{url='http://127.0.0.1:1/mcp';expected_user='test';device_id='test';token_env_var='UNUSED'}
[IO.File]::WriteAllText((Join-Path $root 'config.json'),($cfg|ConvertTo-Json))
. $CoveragePath -ClientPath $ClientPath -ClientHome $root -LoadOnly
$source=Join-Path $root 'fixture.jsonl'
$event=[pscustomobject]@{session_id='test-prefix';transcript_path=$source;cwd='D:/test'}
$header='{"type":"session_meta","payload":{"id":"test-prefix","cwd":"D:/test"}}'+"`n"
$a=$header+'{"message":"a"}'+"`n"
$branch=$header+'{"message":"x"}'+"`n"
$b=$a+'{"message":"b"}'+"`n"
foreach($text in @($a,$branch,$b)) {
 [IO.File]::WriteAllText($source,$text,(New-Object Text.UTF8Encoding($false)))
 [void](Capture-Transcript (Get-Config) $event)
}
$reg=Read-JsonFile (Get-SessionPath 'test-prefix')
$receipt=[ordered]@{session_id='test-prefix';sha256=$reg.current_sha256;source_order=$reg.source_order;total_bytes=$reg.source_bytes;device_id='test';source_format='codex-jsonl';archive_status='archived';archive_id='fixture';current=$true;extraction_status='disabled'}
Write-JsonAtomic (Join-Path $root ('archive-status\'+$reg.current_sha256+'.json')) $receipt
$r=Invoke-PrefixCoverage (Get-Config) 'test-prefix' 100
if($r.covered_versions -ne 2 -or $r.divergent_versions -ne 1){throw 'Prefix matching must cover exact prefix and exact equal only, preserving divergent branch'}
if(@(Get-SessionQueueEntries 'test-prefix').Count -ne 1){throw 'Divergent queued source lost'}
$ledgerPath=@([IO.Directory]::GetFiles((Join-Path $root 'covered-prefix'),'*.coverage.json',[IO.SearchOption]::AllDirectories))[0]
$ledger=ConvertTo-OrderedRecord (Read-JsonFile $ledgerPath)
[IO.File]::Move($ledger.covered_json,$ledger.source_json)
$ledger.state='prepared';Write-JsonAtomic $ledgerPath $ledger
$recovery=Invoke-PrefixCoverage (Get-Config) 'test-prefix' 100
if($recovery.recovered_moves -ne 1 -or -not [IO.File]::Exists($ledger.covered_bin) -or -not [IO.File]::Exists($ledger.covered_json)){throw 'Prepared move recovery lost immutable payload'}
# Re-capture older prefix with a newer observation order must remain queued.
[IO.File]::WriteAllText($source,$a,(New-Object Text.UTF8Encoding($false)))
[void](Capture-Transcript (Get-Config) $event)
[IO.File]::WriteAllText($source,$b,(New-Object Text.UTF8Encoding($false)))
$newer=Invoke-PrefixCoverage (Get-Config) 'test-prefix' 100
if($newer.covered_versions -ne 0 -or @(Get-SessionQueueEntries 'test-prefix').Count -ne 2){throw 'Newer recaptured prefix must preserve its current ordering'}
# A prepared ledger cannot consume a newer observation after a restart.
$ledger=ConvertTo-OrderedRecord (Read-JsonFile $ledgerPath)
if([IO.File]::Exists($ledger.source_json)){[IO.File]::Delete($ledger.source_json)}
[IO.File]::Move($ledger.covered_json,$ledger.source_json)
$changed=ConvertTo-OrderedRecord (Read-JsonFile $ledger.source_json)
$changed['source_order']=[long]$ledger.source_order+100
Write-JsonAtomic $ledger.source_json $changed
$ledger.state='prepared';Write-JsonAtomic $ledgerPath $ledger
$rejected=$false
try {[void](Invoke-PrefixCoverage (Get-Config) 'test-prefix' 100)}catch {$rejected=$_.Exception.Message -like '*manifest changed*'}
if(-not $rejected -or -not [IO.File]::Exists($ledger.source_json) -or -not [IO.File]::Exists($ledger.covered_bin)){throw 'Crash recovery consumed newer recaptured observation'}
'prefix_equal_divergent_crash_recovery_and_newer_order_passed'

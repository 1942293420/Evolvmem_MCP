param([string]$ClientPath,[string]$CoveragePath,[string]$PythonPath)
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('evo-declared-prefix-'+[guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($root)
$cfg=@{url='http://127.0.0.1:1/mcp';expected_user='test';device_id='test';token_env_var='UNUSED';decoder_python=$PythonPath}
[IO.File]::WriteAllText((Join-Path $root 'config.json'),($cfg|ConvertTo-Json))
. $CoveragePath -ClientPath $ClientPath -ClientHome $root -LoadOnly
$script:ServerAvailable=$false
function Get-ConfirmedCurrentArchive($Config,$Sid,$Sha,$Order,$Bytes) {if($script:ServerAvailable){return @{archive_id='fixture'}};return $null}
$source=Join-Path $root 'fixture.jsonl'
$event=[pscustomobject]@{session_id='test-prefix';transcript_path=$source;cwd='C:/test'}
$header='{"type":"session_meta","payload":{"id":"test-prefix","cwd":"C:/test"}}'+"`n"
$a=$header+'{"message":"a"}'+"`n"
$branch=$header+'{"message":"x"}'+"`n"
$b=$a+'{"message":"b"}'+"`n"
foreach($text in @($a,$branch,$b)) {
 [IO.File]::WriteAllText($source,$text,(New-Object Text.UTF8Encoding($false)))
 [void](Capture-Transcript (Get-Config) $event)
}
$reg=Read-JsonFile (Get-SessionPath 'test-prefix')
$receipt=[ordered]@{session_id='test-prefix';sha256=$reg.current_sha256;source_order=$reg.source_order;total_bytes=$reg.source_bytes;device_id='test';source_format='codex-jsonl';archive_status='archived';archive_id='fixture';current=$true}
$receiptPath=Join-Path $root ('archive-status\'+$reg.current_sha256+'.json')
Write-JsonAtomic $receiptPath $receipt
$r=Invoke-PrefixCoverage (Get-Config) 'test-prefix' 100 $true
if($r.covered_versions -ne 0 -or $r.unavailable -ne 1){throw 'Unavailable server archive must retain everything'}
$script:ServerAvailable=$true
$receipt.archive_id='wrong';Write-JsonAtomic $receiptPath $receipt
$r=Invoke-PrefixCoverage (Get-Config) 'test-prefix' 100 $true
if($r.covered_versions -ne 0 -or $r.unavailable -ne 1){throw 'Mismatched server archive ID must retain everything'}
$receipt.archive_id='fixture'
foreach($field in @('device_id','source_format','session_id')) {
 $prior=$receipt[$field];$receipt[$field]='wrong';Write-JsonAtomic $receiptPath $receipt
 $r=Invoke-PrefixCoverage (Get-Config) 'test-prefix' 100 $true
 if($r.covered_versions -ne 0){throw 'Wrong identity/format must not cover'}
 $receipt[$field]=$prior
}
Write-JsonAtomic $receiptPath $receipt
$receipt.sha256='0'*64;Write-JsonAtomic $receiptPath $receipt
$rejected=$false
try {[void](Invoke-PrefixCoverage (Get-Config) 'test-prefix' 100 $true)}catch {$rejected=$true}
if(-not $rejected -or @(Get-SessionQueueEntries 'test-prefix').Count -ne 3){throw 'Wrong whole source SHA must move nothing'}
$receipt.sha256=$reg.current_sha256;Write-JsonAtomic $receiptPath $receipt
$old=@(Get-SessionQueueEntries 'test-prefix'|Sort-Object {$_.Manifest.source_order})[0]
$oldManifest=ConvertTo-OrderedRecord $old.Manifest;$oldManifest['last_error']='old_error_retained';Write-JsonAtomic $old.Path $oldManifest
$oldBin=$old.Path -replace '\.json$','.bin'
[IO.File]::WriteAllBytes($oldBin,[byte[]](1,2,3,4))
$cipherHash=(Get-FileHash -LiteralPath $oldBin).Hash
$r=Invoke-PrefixCoverage (Get-Config) 'test-prefix' 100 $true
if($r.covered_versions -ne 2 -or $r.divergent_versions -ne 1){throw 'Must cover declared prefix/equal and retain branch'}
$ledgers=@([IO.Directory]::GetFiles((Join-Path $root 'covered-prefix'),'*.coverage.json',[IO.SearchOption]::AllDirectories))
$ledgerPath=@($ledgers|Where-Object {(Read-JsonFile $_).sha256 -ceq $old.Manifest.sha256})[0]
$ledger=ConvertTo-OrderedRecord (Read-JsonFile $ledgerPath)
if($ledger.verification -cne 'prefix-sha256-declared-source' -or $ledger.cipher_integrity -cne 'not_checked'){throw 'Evidence must not claim cipher validation'}
if((Get-FileHash -LiteralPath $ledger.covered_bin).Hash -cne $cipherHash -or (Read-JsonFile $ledger.covered_json).last_error -cne 'old_error_retained'){throw 'Cipher and original error must be retained'}
[IO.File]::Move($ledger.covered_json,$ledger.source_json);$ledger.state='prepared';Write-JsonAtomic $ledgerPath $ledger
$ledger.device_id='wrong';Write-JsonAtomic $ledgerPath $ledger
$rejected=$false
try {[void](Invoke-PrefixCoverage (Get-Config) 'test-prefix' 100 $true)}catch {$rejected=$_.Exception.Message -like '*proof identity*'}
if(-not $rejected -or -not [IO.File]::Exists($ledger.source_json)){throw 'Prepared proof from wrong device must retain files'}
$ledger.device_id='test';Write-JsonAtomic $ledgerPath $ledger
$r=Invoke-PrefixCoverage (Get-Config) 'test-prefix' 100 $true
if($r.recovered_moves -ne 1){throw 'Interrupted move did not recover'}
[IO.File]::WriteAllText($source,$a,(New-Object Text.UTF8Encoding($false)));[void](Capture-Transcript (Get-Config) $event)
[IO.File]::WriteAllText($source,$b,(New-Object Text.UTF8Encoding($false)))
$r=Invoke-PrefixCoverage (Get-Config) 'test-prefix' 100 $true
if($r.covered_versions -ne 0 -or @(Get-SessionQueueEntries 'test-prefix').Count -ne 2){throw 'Newer observation must remain queued'}
'declared_prefix_server_gate_whole_sha_branch_cipher_retention_recovery_and_order_passed'

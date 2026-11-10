param([string]$ClientPath)
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('evo-logical-'+[guid]::NewGuid().ToString('N'))
$env:EVOLVMEM_CLIENT_HOME=$root
[void][IO.Directory]::CreateDirectory($root)
$config=@{url='http://127.0.0.1:1/mcp';token_env_var='UNUSED';expected_user='test';device_id='test'}
[IO.File]::WriteAllText((Join-Path $root 'config.json'),($config|ConvertTo-Json))
[Console]::SetIn((New-Object IO.StringReader('{}')))
. $ClientPath -Action snapshot
$source=Join-Path $root 'fixture.jsonl'
[IO.File]::WriteAllText($source,('{"type":"session_meta","payload":{"id":"logical","cwd":"D:/test"}}'+"`n"))
$event=[pscustomobject]@{session_id='logical';transcript_path=$source;cwd='D:/test'}
[void](Capture-Transcript (Get-Config) $event)
$old=Read-JsonFile (Get-SessionPath 'logical')
$savedUnprotect=${function:Unprotect-Bytes}
function Unprotect-Bytes([byte[]]$Bytes){throw 'Cache decryption must not run for unchanged logical bytes'}
[IO.File]::SetLastWriteTimeUtc($source,[DateTime]::UtcNow.AddSeconds(2))
[void](Capture-Transcript (Get-Config) $event)
$now=Read-JsonFile (Get-SessionPath 'logical')
if($now.current_sha256 -cne $old.current_sha256 -or $now.source_last_write_utc -ceq $old.source_last_write_utc){throw 'Equal logical bytes did not refresh only physical metadata'}
# A same-size rewrite has a different hash and must fall back to validation.
[IO.File]::WriteAllText($source,('{"type":"session_meta","payload":{"id":"logical","cwd":"E:/test"}}'+"`n"))
$rejected=$false
try {[void](Capture-Transcript (Get-Config) $event)}catch{$rejected=$_.Exception.Message -like '*Cache decryption must not run*'}
if(-not $rejected){throw 'Different complete source SHA was accepted as unchanged'}
Set-Item Function:Unprotect-Bytes $savedUnprotect
[void](Capture-Transcript (Get-Config) $event)
if((Read-JsonFile (Get-SessionPath 'logical')).current_sha256 -ceq $old.current_sha256){throw 'Real same-size rewrite did not create a new immutable version'}
'logical_sha_fastpath_and_divergent_fallback_passed'

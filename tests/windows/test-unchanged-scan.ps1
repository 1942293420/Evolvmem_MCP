param([string]$ClientPath)
$ErrorActionPreference='Stop'
$root=Join-Path ([IO.Path]::GetTempPath()) ('evo-scan-'+[guid]::NewGuid().ToString('N'))
$env:EVOLVMEM_CLIENT_HOME=Join-Path $root 'client'
[void][IO.Directory]::CreateDirectory($env:EVOLVMEM_CLIENT_HOME)
[void][IO.Directory]::CreateDirectory((Join-Path $root 'sessions'))
$config=@{url='http://127.0.0.1:1/mcp';token_env_var='UNUSED';expected_user='test';device_id='test';codex_root=$root;capture_since_utc='2026-01-01T00:00:00Z'}
[IO.File]::WriteAllText((Join-Path $env:EVOLVMEM_CLIENT_HOME 'config.json'),($config|ConvertTo-Json))
[Console]::SetIn((New-Object IO.StringReader('{}')))
. $ClientPath -Action snapshot
$path=Join-Path $root 'sessions\same.jsonl'
[IO.File]::WriteAllText($path,('{"type":"session_meta","payload":{"id":"same","cwd":"D:/test"}}'+"`n"))
$event=[pscustomobject]@{session_id='same';transcript_path=$path;cwd='D:/test'}
[void](Capture-Transcript (Get-Config) $event)
[IO.File]::SetLastWriteTimeUtc($path,[DateTime]::UtcNow.AddSeconds(2))
[void](Capture-Transcript (Get-Config) $event)
function Read-TranscriptHeader([string]$Path){throw 'Unchanged registered transcript must not re-decode its header'}
$found=Find-LocalSessions (Get-Config)
if($found.Errors -ne 0){throw 'Discovery re-decoded known registered transcript'}
[void](Capture-Transcript (Get-Config) $event)
'unchanged_capture_and_discovery_passed'
foreach($alias in @($path.Replace('\','/'),$path.ToUpperInvariant())) {
 $record=ConvertTo-OrderedRecord (Read-JsonFile (Get-SessionPath 'same'))
 $record['transcript_path']=$alias
 Write-JsonAtomic (Get-SessionPath 'same') $record
 $found=Find-LocalSessions (Get-Config)
 if($found.Errors -ne 0){throw 'Equivalent slash/case path re-decoded during discovery'}
 [void](Capture-Transcript (Get-Config) $event)
}
'unchanged_capture_and_discovery_passed'

param([string]$ClientPath,[switch]$ExpectDeferred)
$ErrorActionPreference='Stop'
$root='D:\EvolvMem-data\repair-test-long-hook-'+[Guid]::NewGuid().ToString('N').Substring(0,8)
[void][IO.Directory]::CreateDirectory($root)
$env:EVOLVMEM_CLIENT_HOME=$root
$config=@{url='http://127.0.0.1:1/mcp';token_env_var='UNUSED';expected_user='test';device_id='test'}
[IO.File]::WriteAllText((Join-Path $root 'config.json'),($config|ConvertTo-Json))
[Console]::SetIn((New-Object IO.StringReader('{}')))
. $ClientPath -Action snapshot
$transcript=Join-Path $root 'long.jsonl'
$stream=[IO.File]::Create($transcript)
try {
  $header=[Text.Encoding]::UTF8.GetBytes('{"type":"session_meta","payload":{"id":"test-long","cwd":"D:\\test"}}'+"`n")
  $stream.Write($header,0,$header.Length)
  $row=[Text.Encoding]::UTF8.GetBytes('{"type":"event_msg","payload":{"message":"'+('x'*65500)+'"}}'+"`n")
  for($n=0;$n -lt 1664;$n++){$stream.Write($row,0,$row.Length)}
} finally {$stream.Dispose()}
$event=[pscustomobject]@{session_id='test-long';transcript_path=$transcript;cwd='D:\test'}
$watch=[Diagnostics.Stopwatch]::StartNew()
Invoke-Snapshot (Get-Config) $event
$elapsed=$watch.ElapsedMilliseconds
$state=Read-JsonFile (Get-SessionPath 'test-long')
$result=[ordered]@{elapsed_ms=$elapsed;source_file_bytes=(Get-Item $transcript).Length;captured_bytes=$state.source_bytes;deferred=($state.source_bytes -eq 0)}
$result|ConvertTo-Json -Compress
if($ExpectDeferred -and ($elapsed -ge 1500 -or $state.source_bytes -ne 0)){throw 'Snapshot must durably register and defer bulk capture within hook budget'}

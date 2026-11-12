param([string]$ClientPath, [string]$PythonPath)
$ErrorActionPreference = 'Stop'
$root = Join-Path ([IO.Path]::GetTempPath()) ('evolvmem-compressed-' + [Guid]::NewGuid().ToString('N'))
$utf8 = New-Object Text.UTF8Encoding($false)
function Assert($value, $message) { if (-not $value) { throw $message } }
try {
    $env:EVOLVMEM_CLIENT_HOME = Join-Path $root 'client'
    $sources = Join-Path $root 'codex/sessions'
    [void][IO.Directory]::CreateDirectory($env:EVOLVMEM_CLIENT_HOME)
    [void][IO.Directory]::CreateDirectory($sources)
    $config = @{url='http://127.0.0.1:1/mcp'; expected_user='fixture';token_env_var='UNUSED';device_id='fixture';projects=@{};codex_root=(Join-Path $root 'codex');capture_since_utc=[DateTime]::UtcNow.AddDays(-1).ToString('o');decoder_python=$PythonPath}
    [IO.File]::WriteAllText((Join-Path $env:EVOLVMEM_CLIENT_HOME 'config.json'), ($config|ConvertTo-Json), $utf8)
    $expectedByExtension=@{}
    foreach ($extension in @('zst','zstd')) {
        $plain = Join-Path $sources ('fixture-' + $extension + '.jsonl')
        $content = '{"type":"session_meta","payload":{"id":"fixture-' + $extension + '","timestamp":"' + [DateTime]::UtcNow.ToString('o') + '","cwd":"C:/fixture"}}' + "`n" + '{"type":"event_msg","payload":{"message":"complete"}}' + "`n"
        $expectedByExtension[$extension]=$content
        [IO.File]::WriteAllText($plain, $content + '{"incomplete":', $utf8)
        & $PythonPath -c 'import pathlib,sys,zstandard as z;p=pathlib.Path(sys.argv[1]);pathlib.Path(sys.argv[2]).write_bytes(z.ZstdCompressor().compress(p.read_bytes()))' $plain ($plain + '.' + $extension)
        [IO.File]::Delete($plain)
    }
    $old = Join-Path $sources 'old.jsonl.zst'
    [IO.File]::WriteAllText(($old+'.input'), ('{"type":"session_meta","payload":{"id":"historic","timestamp":"2020-01-01T00:00:00Z","cwd":"C:/fixture"}}'+"`n"), $utf8)
    & $PythonPath -c 'import pathlib,sys,zstandard as z;pathlib.Path(sys.argv[2]).write_bytes(z.ZstdCompressor().compress(pathlib.Path(sys.argv[1]).read_bytes()))' ($old+'.input') $old
    [IO.File]::SetLastWriteTimeUtc($old, [DateTime]::UtcNow.AddDays(-90))
    [Console]::SetIn((New-Object IO.StringReader('{}')))
    . $ClientPath -Action snapshot
    $cfg = Get-Config
    $found = Find-LocalSessions $cfg
    Assert ($found.Found -eq 2) 'Compressed recent sources must be discovered; old source must remain outside cutoff.'
    [IO.File]::SetLastWriteTimeUtc($old, [DateTime]::UtcNow)
    Assert ((Find-LocalSessions $cfg).Found -eq 0) 'Recent compression mtime must not silently import an old session.'
    foreach ($extension in @('zst','zstd')) {
        $event = [pscustomobject]@{session_id=('fixture-'+$extension);transcript_path=(Join-Path $sources ('fixture-'+$extension+'.jsonl.'+$extension));cwd='C:/fixture'}
        Assert (Capture-Transcript $cfg $event) 'First compressed capture must enqueue.'
        Assert (-not (Capture-Transcript $cfg $event)) 'Unchanged compressed capture must be idempotent.'
        $session = Read-JsonFile (Get-SessionPath $event.session_id)
        $cache = Unprotect-Bytes ([IO.File]::ReadAllBytes((Join-Path $script:ClientHome ('session-cache/'+$session.cache_file))))
        $expected = $expectedByExtension[$extension]
        Assert ($utf8.GetString($cache) -ceq $expected) 'Decoded DPAPI cache must preserve exact complete JSONL bytes.'
    }
    $history = Find-LocalSessions $cfg -Historical
    Assert ($history.Sources.Count -eq 3) 'Explicit history discovery must list old compressed source.'
    Assert (-not [IO.File]::Exists((Get-SessionPath 'historic'))) 'Historical discovery must not register or queue old source.'
    Assert (@(Get-ChildItem (Join-Path $script:ClientHome 'queue') -Filter '*.json').Count -eq 2) 'History inventory must not enqueue or extract.'
    [IO.File]::WriteAllBytes((Join-Path $sources 'bad.jsonl.zst'), [byte[]]@(1,2,3))
    $failedDiscovery=Find-LocalSessions $cfg
    Assert ($failedDiscovery.Errors -eq 1) 'Corrupt compressed source must report discovery error.'
    Assert ('compressed_decode_failed' -in $failedDiscovery.ErrorCodes) 'Discovery must expose bounded decoder failure code.'
    [ordered]@{passed=$true;compressed_extensions=2;exact_bytes=$true;dpapi=$true;cutoff_preserved=$true;history_read_only=$true;corrupt_reported=$true}|ConvertTo-Json -Compress
} finally { if ([IO.Directory]::Exists($root)) { [IO.Directory]::Delete($root,$true) } }

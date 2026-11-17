[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$ClientPath)

# Run in a fresh Windows PowerShell 5.1 process with -NoProfile:
# removing the client's DPAPI assembly initialization must fail this test.
$ErrorActionPreference = 'Stop'
$utf8 = New-Object Text.UTF8Encoding($false)
$OutputEncoding = $utf8
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('evolvmem-dpapi-test-' + [Guid]::NewGuid().ToString('N'))
$previousClientHome = $env:EVOLVMEM_CLIENT_HOME

try {
    [void][IO.Directory]::CreateDirectory($testRoot)
    $env:EVOLVMEM_CLIENT_HOME = Join-Path $testRoot 'client'
    [void][IO.Directory]::CreateDirectory($env:EVOLVMEM_CLIENT_HOME)
    $projects = @{}; $projects[$testRoot] = 'native-dpapi-test'
    $config = @{ version=1; url='http://127.0.0.1:1/mcp'; token_env_var='EVOLVMEM_UNUSED_TEST_TOKEN'; expected_user='native-test'; device_id='native-test'; projects=$projects }
    [IO.File]::WriteAllText((Join-Path $env:EVOLVMEM_CLIENT_HOME 'config.json'), ($config | ConvertTo-Json -Depth 4), $utf8)
    # Empty worker mode loads the actual client without stdin or network access.
    . $ClientPath -Action worker
    $sample = [Text.Encoding]::UTF8.GetBytes('dpapi-array-regression')
    $protectedSample = Protect-Bytes $sample
    $plainSample = Unprotect-Bytes $protectedSample
    if ($protectedSample -isnot [byte[]] -or $plainSample -isnot [byte[]]) {
        throw 'DPAPI must preserve byte arrays without expanding one PowerShell object per byte.'
    }
    $testConfig = Get-Config
    $transcript = Join-Path $testRoot 'rollout.jsonl'
    $chinese = ([string][char]0x4e2d) + [char]0x6587
    $first = '{"type":"user","text":"' + $chinese + '"}' + "`n"
    [IO.File]::WriteAllText($transcript, $first + '{"partial":', $utf8)
    $event = @{session_id='native-dpapi-regression'; cwd=$testRoot; transcript_path=$transcript; hook_event_name='SessionEnd'}
    $event = [pscustomobject]$event
    [void](Capture-Transcript $testConfig $event)
    $queue = Join-Path $env:EVOLVMEM_CLIENT_HOME 'queue'
    $versions = @(Get-ChildItem $queue -Filter '*.bin' -ErrorAction SilentlyContinue)
    if ($versions.Count -ne 1) { throw ('Expected one encrypted snapshot; received ' + $versions.Count) }

    # Decrypt independently after production has already created its snapshot.
    Add-Type -AssemblyName System.Security
    $entropy = $utf8.GetBytes('evolvmem-codex-archive-v1')
    $encrypted = [IO.File]::ReadAllBytes($versions[0].FullName)
    $plain = [Security.Cryptography.ProtectedData]::Unprotect($encrypted, $entropy, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    if ($utf8.GetString($plain) -cne $first) { throw 'The first snapshot lost UTF-8 content or included the incomplete line.' }
    if ([Convert]::ToBase64String($encrypted) -ceq [Convert]::ToBase64String($plain)) { throw 'Snapshot was stored as plaintext.' }

    $second = $first + '{"type":"assistant","text":"complete"}' + "`n"
    [IO.File]::WriteAllText($transcript, $second + 'partial', $utf8)
    [void](Capture-Transcript $testConfig $event)
    $versions = @(Get-ChildItem $queue -Filter '*.bin')
    if ($versions.Count -ne 2) { throw 'The client could not append a second encrypted snapshot.' }
    $decoded = @($versions | ForEach-Object {
        $bytes = [IO.File]::ReadAllBytes($_.FullName)
        $utf8.GetString([Security.Cryptography.ProtectedData]::Unprotect($bytes, $entropy, [Security.Cryptography.DataProtectionScope]::CurrentUser))
    })
    if ($first -cnotin $decoded -or $second -cnotin $decoded) { throw 'Snapshot versions were overwritten or captured incorrectly.' }
    [ordered]@{passed=$true; powershell=$PSVersionTable.PSVersion.ToString(); encrypted_versions=2; utf8_roundtrip=$true; partial_line_excluded=$true} | ConvertTo-Json -Compress
}
finally {
    $env:EVOLVMEM_CLIENT_HOME = $previousClientHome
    if ([IO.Directory]::Exists($testRoot)) { [IO.Directory]::Delete($testRoot, $true) }
}

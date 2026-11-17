param(
 [string]$ClientPath='',
 [string]$ClientHome='',
 [string]$SessionId='',
 [int]$Limit=50,
 [switch]$DeclaredPrefixProof,
 [switch]$LoadOnly
)
$ErrorActionPreference='Stop'
if(-not $ClientHome){$ClientHome=if($env:EVOLVMEM_CLIENT_HOME){$env:EVOLVMEM_CLIENT_HOME}else{Join-Path $env:LOCALAPPDATA 'EvolvMem\Codex'}}
if(-not $ClientPath){$ClientPath=Join-Path $ClientHome 'evolvmem-codex.ps1'}
$env:EVOLVMEM_CLIENT_HOME=$ClientHome
[Console]::SetIn((New-Object IO.StringReader('{}')))
. $ClientPath -Action snapshot

function Test-ExactPrefix([byte[]]$Small,[byte[]]$Large) {
 if($Small.Length -gt $Large.Length){return $false}
 if($null -eq ('EvolvMemPrefixBytes' -as [type])) {
  Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class EvolvMemPrefixBytes {
 [DllImport("msvcrt.dll", CallingConvention=CallingConvention.Cdecl, EntryPoint="memcmp")]
 private static extern int Compare(byte[] left, byte[] right, UIntPtr count);
 public static bool EqualPrefix(byte[] left, byte[] right) {
  return left.Length <= right.Length && Compare(left, right, (UIntPtr)(uint)left.Length) == 0;
 }
}
'@
 }
 return [EvolvMemPrefixBytes]::EqualPrefix($Small,$Large)
}

function Assert-CoverageProofIdentity($Ledger,$Config) {
 $target=$Ledger.target_receipt
 if($null -eq $target -or $Ledger.device_id -cne [string]$Config.device_id -or $Ledger.source_format -cne 'codex-jsonl' -or
    $target.device_id -cne $Ledger.device_id -or $target.session_id -cne $Ledger.session_id -or $target.source_format -cne $Ledger.source_format -or
    $target.archive_status -cne 'archived' -or -not $target.archive_id -or $target.current -ne $true -or
    $target.sha256 -cne $Ledger.covered_by_sha256 -or [long]$target.total_bytes -ne [long]$Ledger.covered_by_total_bytes -or
    [string]$target.archive_id -cne [string]$Ledger.covered_by_archive_id -or
    $Ledger.sha256 -cnotmatch '^[0-9a-f]{64}$' -or $target.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
    [long]$Ledger.total_bytes -le 0 -or [long]$Ledger.total_bytes -gt [long]$target.total_bytes -or
    [long]$Ledger.source_order -le 0 -or [long]$Ledger.source_order -gt [long]$target.source_order -or
    ([long]$Ledger.source_order -eq [long]$target.source_order -and ($Ledger.sha256 -cne $target.sha256 -or [long]$Ledger.total_bytes -ne [long]$target.total_bytes)) -or
    $Ledger.verification -notin @('exact-byte-prefix-and-both-sha256','prefix-sha256-declared-source')) {
  throw 'coverage proof identity mismatch; preserve both locations for review'
 }
}

function Complete-CoveredMove($Ledger,[string]$LedgerPath,$Config) {
 # A durable prepared ledger precedes either rename. A crash after the bin
 # move still leaves the exact immutable proof and both original locations.
 Assert-CoverageProofIdentity $Ledger $Config
 $manifestPath=if([IO.File]::Exists([string]$Ledger.source_json)){[string]$Ledger.source_json}else{[string]$Ledger.covered_json}
 $observed=Read-JsonFile $manifestPath
 if($null -eq $observed -or [string]$observed.session_id -cne [string]$Ledger.session_id -or [string]$observed.sha256 -cne [string]$Ledger.sha256 -or
    [long]$observed.source_order -ne [long]$Ledger.source_order -or [long]$observed.total_bytes -ne [long]$Ledger.total_bytes){
  throw 'prepared coverage manifest changed; preserve both locations for review'
 }
 foreach($part in @('bin','json')) {
  $from=[string]$Ledger[('source_'+$part)]
  $to=[string]$Ledger[('covered_'+$part)]
  $queueRoot=[IO.Path]::GetFullPath((Join-Path $script:ClientHome 'queue'))+[IO.Path]::DirectorySeparatorChar
  $coveredRoot=[IO.Path]::GetFullPath((Join-Path $script:ClientHome 'covered-prefix'))+[IO.Path]::DirectorySeparatorChar
  if(-not [IO.Path]::GetFullPath($from).StartsWith($queueRoot,[StringComparison]::OrdinalIgnoreCase) -or
     -not [IO.Path]::GetFullPath($to).StartsWith($coveredRoot,[StringComparison]::OrdinalIgnoreCase)){throw 'coverage path outside client queue'}
  if([IO.File]::Exists($to)) {
   if([IO.File]::Exists($from)){throw 'ambiguous duplicate during covered move'}
  } elseif([IO.File]::Exists($from)) {[IO.File]::Move($from,$to)}
  else {throw 'covered payload or manifest is missing'}
 }
 $Ledger['state']='covered'
 $Ledger['completed_utc']=[DateTime]::UtcNow.ToString('o')
 Write-JsonAtomic $LedgerPath $Ledger
}

function Get-DeclaredPrefixHashes($Config,[string]$Source,$Receipt,$Entries) {
 $python=[string](Get-Value $Config 'decoder_python' '')
 $helper=Join-Path $PSScriptRoot 'prefix_hashes.py'
 if(-not [IO.File]::Exists($python) -or -not [IO.File]::Exists($helper)){throw 'prefix hash runtime unavailable'}
 $request=@{path=$Source;total_bytes=[long]$Receipt.total_bytes;sha256=[string]$Receipt.sha256;lengths=@($Entries|ForEach-Object {[long]$_.Manifest.total_bytes})}
 $info=New-Object Diagnostics.ProcessStartInfo
 $info.FileName=$python;$info.Arguments='"'+$helper+'"';$info.UseShellExecute=$false;$info.CreateNoWindow=$true
 $info.RedirectStandardInput=$true;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
 $process=New-Object Diagnostics.Process;$process.StartInfo=$info
 try {
  [void]$process.Start()
  $output=$process.StandardOutput.ReadToEndAsync();$errorOutput=$process.StandardError.ReadToEndAsync()
  $process.StandardInput.WriteLine(($request|ConvertTo-Json -Depth 5 -Compress));$process.StandardInput.Close()
  if(-not $process.WaitForExit(120000)){$process.Kill();throw 'prefix source verification timed out'}
  if($process.ExitCode -ne 0){throw 'prefix source verification failed'}
  $result=$output.Result|ConvertFrom-Json
  if([string]$result.sha256 -cne [string]$Receipt.sha256 -or [long]$result.total_bytes -ne [long]$Receipt.total_bytes){throw 'prefix source verification mismatch'}
  foreach($length in $request.lengths) {
   $digest=[string](Get-Value $result.prefixes ([string]$length) '')
   if($digest -cnotmatch '^[0-9a-f]{64}$'){throw 'prefix source digest missing'}
  }
  return $result.prefixes
 } finally {$process.Dispose()}
}

function Invoke-PrefixCoverage($Config,[string]$OnlySessionId,[int]$MaxVersions,[bool]$UseDeclaredPrefixProof=$false) {
 $covered=0;$bytes=[int64]0;$divergent=0;$unavailable=0;$recovered=0
 $destination=Join-Path $script:ClientHome 'covered-prefix'
 Ensure-Directory $destination
 # Finish interrupted, already verified renames before discovering new work.
 foreach($path in @([IO.Directory]::GetFiles($destination,'*.coverage.json',[IO.SearchOption]::AllDirectories))) {
  $ledger=ConvertTo-OrderedRecord (Read-JsonFile $path)
  if($OnlySessionId -and $ledger.session_id -cne $OnlySessionId){continue}
  if($ledger.state -ceq 'prepared') {
   Invoke-WithClientLock {Complete-CoveredMove $ledger $path $Config} 120000 (Get-SessionLockScope $ledger.session_id)
   $recovered++
  }
 }
 $receipts=@([IO.Directory]::GetFiles((Join-Path $script:ClientHome 'archive-status'),'*.json')|ForEach-Object {Read-JsonFile $_}|Where-Object {
  $_.archive_status -ceq 'archived' -and $_.current -eq $true -and $_.archive_id -and
  $_.device_id -ceq [string]$Config.device_id -and $_.source_format -ceq 'codex-jsonl' -and
  [long]$_.total_bytes -gt 0 -and (-not $OnlySessionId -or $_.session_id -ceq $OnlySessionId)
 }|Sort-Object source_order -Descending)
 $seen=@{}
 foreach($receipt in $receipts) {
  if($covered -ge $MaxVersions){break}
  $sid=[string]$receipt.session_id
  if($seen.ContainsKey($sid)){continue};$seen[$sid]=$true
  $eligible=@(Get-SessionQueueEntries $sid|Where-Object {
   [long]$_.Manifest.total_bytes -le [long]$receipt.total_bytes -and [long]$_.Manifest.source_order -le [long]$receipt.source_order
  })
  if($eligible.Count -eq 0){continue}
  $session=Read-JsonFile (Get-SessionPath $sid)
  $source=[string]$session.transcript_path
  # Reconstruct exactly the acknowledged immutable byte sequence, never the
  # unverified growing tail. Its whole SHA must match the terminal receipt.
  if(-not $source -or -not [IO.File]::Exists($source)){$unavailable++;continue}
 $target=$null;$prefixes=$null
  try {
   if($UseDeclaredPrefixProof) {
    $confirmed=Get-ConfirmedCurrentArchive $Config $sid ([string]$receipt.sha256) ([long]$receipt.source_order) ([long]$receipt.total_bytes)
    if($null -eq $confirmed -or [string]$confirmed.archive_id -cne [string]$receipt.archive_id){$unavailable++;continue}
    $eligible=@($eligible|Where-Object {
     $_.Manifest.sha256 -cmatch '^[0-9a-f]{64}$' -and [long]$_.Manifest.total_bytes -gt 0 -and [long]$_.Manifest.source_order -gt 0 -and
     ([long]$_.Manifest.source_order -lt [long]$receipt.source_order -or
      ($_.Manifest.sha256 -ceq $receipt.sha256 -and [long]$_.Manifest.total_bytes -eq [long]$receipt.total_bytes))
    })
    if($eligible.Count -eq 0){continue}
    $prefixes=Get-DeclaredPrefixHashes $Config $source $receipt $eligible
   } else {
   $stream=Open-TranscriptStream $source
   try {
    $size=[long]$receipt.total_bytes
    if($stream.Length -lt $size -or $size -gt [int]::MaxValue){$unavailable++;continue}
    $target=New-Object byte[] ([int]$size);$read=0
    while($read -lt $size){$n=$stream.Read($target,$read,$target.Length-$read);if($n -le 0){throw 'target source changed while reading'};$read+=$n}
   } finally {$stream.Dispose()}
   if((Get-Sha256Hex $target) -cne [string]$receipt.sha256){$unavailable++;continue}
   }
   foreach($entry in $eligible) {
    if($covered -ge $MaxVersions){break}
    $manifest=$entry.Manifest
    if([long]$manifest.total_bytes -gt [long]$receipt.total_bytes){continue}
    # Recaptured A after B is a newer observation even when A is a byte prefix
    # of B. It still needs its own current/order acknowledgement.
    if([long]$manifest.source_order -gt [long]$receipt.source_order){continue}
    $sourceJson=[string]$entry.Path;$sourceBin=$sourceJson -replace '\.json$','.bin'
    if(-not [IO.File]::Exists($sourceBin)){continue}
    $plain=$null
    try {
     if($UseDeclaredPrefixProof) {
      if([string](Get-Value $prefixes ([string]$manifest.total_bytes) '') -cne [string]$manifest.sha256){$divergent++;continue}
     } else {
      $plain=Unprotect-Bytes ([IO.File]::ReadAllBytes($sourceBin))
      if($plain.Length -ne [long]$manifest.total_bytes -or (Get-Sha256Hex $plain) -cne [string]$manifest.sha256){$unavailable++;continue}
      if(-not (Test-ExactPrefix $plain $target)){$divergent++;continue}
     }
     $dir=Join-Path $destination ((Get-TextSha256 $sid).Substring(0,16));Ensure-Directory $dir
     $stem=[string]$manifest.sha256+'-'+[string]$manifest.source_order
     $ledgerPath=Join-Path $dir ($stem+'.coverage.json')
     $ledger=[ordered]@{
      version=1;state='prepared';session_id=$sid;device_id=[string]$Config.device_id;source_format='codex-jsonl'
      sha256=[string]$manifest.sha256;total_bytes=[long]$manifest.total_bytes;source_order=[long]$manifest.source_order
      covered_by_sha256=[string]$receipt.sha256;covered_by_total_bytes=[long]$receipt.total_bytes
      covered_by_archive_id=[string]$receipt.archive_id;target_receipt=$receipt
      verification=$(if($UseDeclaredPrefixProof){'prefix-sha256-declared-source'}else{'exact-byte-prefix-and-both-sha256'});verified_utc=[DateTime]::UtcNow.ToString('o')
      cipher_integrity=$(if($UseDeclaredPrefixProof){'not_checked'}else{'verified'})
      server_status_rechecked_utc=$(if($UseDeclaredPrefixProof){[DateTime]::UtcNow.ToString('o')}else{''})
      source_bin=$sourceBin;source_json=$sourceJson;covered_bin=(Join-Path $dir ($stem+'.bin'));covered_json=(Join-Path $dir ($stem+'.json'))
     }
     Invoke-WithClientLock {
      $fresh=Read-JsonFile $sourceJson
      if($null -eq $fresh -or $fresh.session_id -cne $manifest.session_id -or $fresh.sha256 -cne $manifest.sha256 -or [long]$fresh.source_order -ne [long]$manifest.source_order -or [long]$fresh.total_bytes -ne [long]$manifest.total_bytes){throw 'coverage selection changed'}
      Write-JsonAtomic $ledgerPath $ledger
      Complete-CoveredMove $ledger $ledgerPath $Config
     } 120000 (Get-SessionLockScope $sid)
     $covered++;$bytes += [long]$manifest.total_bytes
    } finally {$plain=$null;[GC]::Collect()}
   }
  } finally {$target=$null;[GC]::Collect()}
 }
 return [ordered]@{covered_versions=$covered;covered_bytes=$bytes;divergent_versions=$divergent;unavailable=$unavailable;recovered_moves=$recovered;retained_root=$destination;completed_utc=[DateTime]::UtcNow.ToString('o')}
}

if(-not $LoadOnly) {
 $result=Invoke-WithClientLock {Invoke-PrefixCoverage (Get-Config) $SessionId $Limit ([bool]$DeclaredPrefixProof)} 100 'upload'
 $result|ConvertTo-Json -Depth 6 -Compress
}

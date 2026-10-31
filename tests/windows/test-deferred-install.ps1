param([string]$InstallerPath)
$ErrorActionPreference='Stop'
Import-Module ScheduledTasks
$tokens=$null;$parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($InstallerPath,[ref]$tokens,[ref]$parseErrors)
if ($parseErrors.Count) { throw 'Installer parse failure' }
$definition=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Register-WorkerTask'},$true)
Invoke-Expression $definition.Extent.Text
$global:RunningOnWindows=$true
$installedLauncher='C:/isolated/evolvmem-sync.exe';$installRoot='C:/isolated'
$probe=@{started=$false;registered=$null}
# Replace OS mutation boundaries only. Build the real settings/action objects.
function Register-ScheduledTask { param($TaskName,$Action,$Trigger,$Principal,$Settings,[switch]$Force) $probe.registered=$Settings }
function Start-ScheduledTask { param($TaskName) $probe.started=$true }
if ($installedLauncher -cne 'C:/isolated/evolvmem-sync.exe' -or (Test-Path -LiteralPath $installedLauncher)) { throw 'Unsafe fixture launcher.' }
foreach ($name in @('Register-ScheduledTask','Start-ScheduledTask')) {
    $boundary=Get-Command $name
    if ($boundary.ModuleName -or $boundary.Definition -notmatch '\$probe\.') { throw 'Task mutation boundary was not mocked.' }
}
$DeferWorkerStart=$true
Register-WorkerTask
if ($probe.started -or $probe.registered.Enabled) { throw 'Deferred installation must register disabled without starting uploader.' }
$DeferWorkerStart=$false
Register-WorkerTask
if (-not $probe.started -or -not $probe.registered.Enabled) { throw 'Normal installation must retain enabled startup.' }
'{"passed":true,"deferred_disabled":true,"normal_enabled":true}'

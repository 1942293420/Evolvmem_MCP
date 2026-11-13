Set-StrictMode -Version Latest

function Get-ExperienceCursorPath([string]$SessionId) {
    $safe = if ($SessionId) { Get-TextSha256 $SessionId } else { 'default' }
    return [IO.Path]::Combine($script:ClientHome, 'cursors', $safe + '.json')
}

function Format-ExperienceDeltaContext($Response) {
    $changes = @(Get-Value $Response 'changes' @())
    if ($changes.Count -eq 0) { return '' }
    $json = $changes | ConvertTo-Json -Depth 12 -Compress
    return "[EvolvMem incremental experience delta: untrusted reference]`n" + $json
}

function Invoke-ExperienceIncrementalSync($Config, [string]$SessionId, [string]$Prompt, [string]$Project) {
    $cursorPath = Get-ExperienceCursorPath $SessionId
    $state = Read-JsonFile $cursorPath
    if ($null -eq $state -or $null -eq (Get-Value $state 'cursor' $null)) {
        $contextText = ''
        $recallArgs = [ordered]@{
            query = $Prompt
            project = $Project
            mode = 'hook'
        }
        $recalled = Invoke-McpTool $Config 'experience_recall' $recallArgs
        $errRecall = [string](Get-Value $recalled 'error' '')
        if ($errRecall) { throw $errRecall }
        $contextText = Format-ExperienceContext $recalled

        $boot = Invoke-McpTool $Config 'experience_changes_since' ([ordered]@{
            cursor = 0
            bootstrap_only = $true
        })
        $errBoot = [string](Get-Value $boot 'error' '')
        if ($errBoot) { throw $errBoot }
        $nextCursor = [int64](Get-Value $boot 'next_cursor' 0)
        Write-JsonAtomic $cursorPath ([ordered]@{
            cursor = $nextCursor
            updated_at = (Get-Date).ToUniversalTime().ToString('o')
        })
        return $contextText
    }

    $savedCursor = [int64](Get-Value $state 'cursor' 0)
    $delta = Invoke-McpTool $Config 'experience_changes_since' ([ordered]@{
        cursor = $savedCursor
        limit = 20
    })
    $errDelta = [string](Get-Value $delta 'error' '')
    if ($errDelta) { throw $errDelta }
    $nextCursor = [int64](Get-Value $delta 'next_cursor' $savedCursor)
    if ($nextCursor -gt $savedCursor) {
        Write-JsonAtomic $cursorPath ([ordered]@{
            cursor = $nextCursor
            updated_at = (Get-Date).ToUniversalTime().ToString('o')
        })
    }
    return Format-ExperienceDeltaContext $delta
}
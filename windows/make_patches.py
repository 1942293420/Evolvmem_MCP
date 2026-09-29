#!/usr/bin/env python3
"""Generate the proposed minimal fix as unified diffs WITHOUT touching the source tree.

The live LAN server imports evolvmem/*.py from this repository, so the fix is built
against scratch copies under windows/scratch/ and emitted as .patch files for review.
"""
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = ROOT / 'windows' / 'scratch'
SCRATCH.mkdir(parents=True, exist_ok=True)


def edit(path: pathlib.Path, old: str, new: str, label: str) -> None:
    text = path.read_text(encoding='utf-8')
    count = text.count(old)
    if count != 1:
        sys.exit(f'anchor not unique ({count}) for {label} in {path.name}')
    path.write_text(text.replace(old, new, 1), encoding='utf-8')


def prepare(name: str) -> pathlib.Path:
    src = ROOT / name
    dst = SCRATCH / pathlib.Path(name).name
    shutil.copy2(src, dst)
    return dst


def diff(name: str, target: str) -> str:
    original = ROOT / name
    modified = SCRATCH / pathlib.Path(name).name
    out = subprocess.run(['diff', '-u', '--label', 'a/' + name, '--label', 'b/' + name,
                          str(original), str(modified)], capture_output=True, text=True)
    path = ROOT / 'windows' / target
    path.write_text(out.stdout, encoding='utf-8')
    return f'{target}: {len(out.stdout.splitlines())} diff lines'


# ---------------------------------------------------------------- server: lan_capture
capture = prepare('evolvmem/lan_capture.py')

edit(capture, """    upload = {**identity, 'project': {'type': 'string', 'maxLength': 128},
              'sha256': {'type': 'string', 'minLength': 64, 'maxLength': 64},
              'total_bytes': {'type': 'integer', 'minimum': 1},
              'offset': {'type': 'integer', 'minimum': 0},
              'content_b64': {'type': 'string', 'maxLength': 349528},
              'extract': {'type': 'boolean'},
              'request_id': {'type': 'string', 'minLength': 1, 'maxLength': 128}}""",
     """    upload = {**identity, 'project': {'type': 'string', 'maxLength': 128},
              'sha256': {'type': 'string', 'minLength': 64, 'maxLength': 64},
              'total_bytes': {'type': 'integer', 'minimum': 1},
              'offset': {'type': 'integer', 'minimum': 0},
              'content_b64': {'type': 'string', 'maxLength': 349528},
              'extract': {'type': 'boolean'},
              # Optional, so already deployed clients keep validating unchanged.
              'parent_sha256': {'type': 'string', 'maxLength': 64},
              'request_id': {'type': 'string', 'minLength': 1, 'maxLength': 128}}""",
     'upload schema')

edit(capture, """                                   'required': list(props), 'additionalProperties': False}}""",
     """                                   'required': [k for k in props if k != 'parent_sha256'],
                                   'additionalProperties': False}}""",
     'required list')

edit(capture, """        return self.conn.execute('''SELECT * FROM lan_session_uploads
            WHERE device_id=? AND session_id=? AND archive_id IS NOT NULL
            ORDER BY total_bytes DESC, rowid DESC LIMIT 1''', identity).fetchone()""",
     """        return self.conn.execute('''SELECT * FROM lan_session_uploads
            WHERE device_id=? AND session_id=? AND archive_id IS NOT NULL
            ORDER BY received_at DESC, rowid DESC LIMIT 1''', identity).fetchone()""",
     '_latest ordering')

edit(capture, """        key = (*identity, digest)
        total, offset = args['total_bytes'], args['offset']""",
     """        key = (*identity, digest)
        # An explicitly empty parent_sha256 declares an intentional new lineage root.
        lineage_root = args.get('parent_sha256') == ''
        total, offset = args['total_bytes'], args['offset']""",
     'lineage_root flag')

edit(capture, """        details = {}
        reason = row['attribution_reason'] if row is not None else ''""",
     """        details = {}
        reason = row['attribution_reason'] if row is not None else ''
        new_root = False""",
     'new_root flag')

edit(capture, """                if prior is not None and not raw.startswith(prior):
                    raise LanError('transcript_fork')""",
     """                if prior is not None and not raw.startswith(prior):
                    if not lineage_root:
                        raise LanError('transcript_fork')
                    # The rollout was rewritten. Older archives stay immutable; this
                    # version is recorded as a new root instead of stalling the queue.
                    new_root = True""",
     'fork branch')

edit(capture, """        if complete:
            path.unlink(missing_ok=True)
        return self._receipt(self.conn.execute('SELECT * FROM lan_session_uploads WHERE device_id=? AND session_id=? AND sha256=?', key).fetchone())""",
     """        if complete:
            path.unlink(missing_ok=True)
        receipt = self._receipt(self.conn.execute('SELECT * FROM lan_session_uploads WHERE device_id=? AND session_id=? AND sha256=?', key).fetchone())
        if new_root:
            receipt['lineage'] = 'new_root'
        return receipt""",
     'receipt marker')

print(diff('evolvmem/lan_capture.py', 'patch-1-server-lineage-root.patch'))

# -------------------------------------------------------------- client: codex worker
ps1 = prepare('scripts/windows/evolvmem-codex.ps1')

edit(ps1, """    if (Get-Value $value 'error') { throw 'remote MCP tool failed' }""",
     """    if (Get-Value $value 'error') { throw ('remote MCP tool failed: ' + [string](Get-Value $value 'error')) }""",
     'surface remote error code')

edit(ps1, """    $prior = [byte[]]@()
    if ($sourceBytes -gt 0 -and [IO.File]::Exists($cachePath)) {
        $prior = Unprotect-Bytes ([IO.File]::ReadAllBytes($cachePath))
        if ($prior.Length -ne $sourceBytes) { $sourceBytes = 0; $prior = [byte[]]@() }
    }
    elseif ($sourceBytes -gt 0) { $sourceBytes = 0 }
    if ($file.Length -lt $sourceBytes) { $sourceBytes = 0; $prior = [byte[]]@() }
    elseif ($sourceBytes -gt 0 -and -not (Test-FilePrefix $transcriptPath $prior)) {""",
     """    $prior = [byte[]]@()
    $lineageBreak = $false
    if ($sourceBytes -gt 0 -and [IO.File]::Exists($cachePath)) {
        $prior = Unprotect-Bytes ([IO.File]::ReadAllBytes($cachePath))
        if ($prior.Length -ne $sourceBytes) { $sourceBytes = 0; $prior = [byte[]]@(); $lineageBreak = $true }
    }
    elseif ($sourceBytes -gt 0) { $sourceBytes = 0 }
    if ($file.Length -lt $sourceBytes) { $sourceBytes = 0; $prior = [byte[]]@() }
    elseif ($sourceBytes -gt 0 -and -not (Test-FilePrefix $transcriptPath $prior)) {""",
     'track lineage break (cache mismatch)')

edit(ps1, """        # A rewritten or divergent transcript begins a new immutable content
        # version. Older unacknowledged queue entries remain untouched.
        $sourceBytes = 0
        $prior = [byte[]]@()
    }""",
     """        # A rewritten or divergent transcript begins a new immutable content
        # version. Older unacknowledged queue entries remain untouched.
        $sourceBytes = 0
        $prior = [byte[]]@()
        $lineageBreak = $true
    }""",
     'track lineage break (divergent file)')

edit(ps1, """            sha256 = $sha; total_bytes = $combined.Length; next_offset = 0; extract = $true
            created_utc = [DateTime]::UtcNow.ToString('o')""",
     """            sha256 = $sha; total_bytes = $combined.Length; next_offset = 0; extract = $true
            lineage_break = [bool]$lineageBreak
            created_utc = [DateTime]::UtcNow.ToString('o')""",
     'manifest lineage_break')

edit(ps1, """                $response = Invoke-McpTool $Config 'session_archive_upload' $arguments $requestId
                $sentChunks += 1""",
     """                if ([bool](Get-Value $manifest 'lineage_break' $false)) { $arguments['parent_sha256'] = '' }
                try {
                    $response = Invoke-McpTool $Config 'session_archive_upload' $arguments $requestId
                }
                catch {
                    # A version captured after a rewritten rollout is refused until it is
                    # declared as a new immutable lineage root. Retry the same chunk only.
                    if ($_.Exception.Message -notlike '*transcript_fork*') { throw }
                    $arguments['parent_sha256'] = ''
                    $response = Invoke-McpTool $Config 'session_archive_upload' $arguments $requestId
                    $manifest.lineage_break = $true
                }
                $sentChunks += 1""",
     'declare lineage root on fork')

edit(ps1, """        catch {
            # Unacknowledged data and its stable request position remain queued.
            continue
        }""",
     """        catch {
            # Unacknowledged data and its stable request position remain queued.
            # Keep the remote error code visible instead of only reporting retry_pending.
            try {
                $manifest.last_error = [string]$_.Exception.Message
                $manifest.last_error_utc = [DateTime]::UtcNow.ToString('o')
                Invoke-WithClientLock { Write-JsonAtomic $manifestPath $manifest } 5000 $sessionScope
            }
            catch { }
            continue
        }""",
     'record last_error')

print(diff('scripts/windows/evolvmem-codex.ps1', 'patch-2-client-lineage-root.patch'))

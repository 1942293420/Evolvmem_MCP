"""Explicit native format dispatch. Never guess a client from body text."""
from evolvmem.lan_sharing import LanError
CODEX_FORMAT='codex-jsonl'
DSH_FORMAT='dsh-session-jsonl-v1'
SOURCE_FORMATS=(CODEX_FORMAT,DSH_FORMAT)

def source_format(value=CODEX_FORMAT):
    if value not in SOURCE_FORMATS:raise LanError('unsupported_source_format')
    return value

def adapter_name(value=CODEX_FORMAT):
    return 'dsh' if source_format(value)==DSH_FORMAT else 'codex'

def parse_transcript(raw,session_id,format=CODEX_FORMAT):
    if source_format(format)==DSH_FORMAT:
        from evolvmem.dsh_transcript import parse_transcript as parse
    else:
        from evolvmem.codex_transcript import parse_transcript as parse
    return parse(raw,session_id)

def workspace_details(rows,format=CODEX_FORMAT):
    if source_format(format)==DSH_FORMAT:
        from evolvmem.dsh_transcript import workspace_details as details
    else:
        from evolvmem.codex_transcript import workspace_details as details
    return details(rows)

"""Hash native source prefixes only after its acknowledged whole-prefix SHA agrees.

stdin: {path, total_bytes, sha256, lengths}; stdout is one JSON result or one
fixed error code. No source text, path, credential, or partial hash result is
printed. Raw .jsonl and Zstandard .zst/.zstd sources are read once, forward only.
This proves source-byte prefix equality, not decryption of queued ciphertext.

Only the first total_bytes decoded bytes are in scope. Appended raw/compressed
tails (including an unfinished later frame/trailer) are not read or validated.
Failure to obtain the acknowledged decoded prefix, or a SHA mismatch, fails.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys

MAX_TOTAL_BYTES = 2**31-1
MAX_LENGTHS = 10000
MAX_INPUT_CHARS = 2*1024*1024
READ_BYTES = 128*1024


class PrefixError(Exception):
    """Fixed codes only; never include an underlying exception or file path."""


def _no_duplicates(pairs):
    value={}
    for key,item in pairs:
        if key in value:raise ValueError()
        value[key]=item
    return value


def _invalid_constant(_):
    raise ValueError()


def validate_request(request):
    if not isinstance(request,dict):raise PrefixError('invalid_input')
    if set(request)!={'path','total_bytes','sha256','lengths'}:raise PrefixError('invalid_input')
    path=request['path'];total=request['total_bytes'];sha=request['sha256'];lengths=request['lengths']
    if not isinstance(path,str)or not path or '\x00'in path:raise PrefixError('invalid_input')
    if type(total)is not int or not 0<=total<=MAX_TOTAL_BYTES:raise PrefixError('invalid_input')
    if not isinstance(sha,str)or re.fullmatch(r'[0-9a-fA-F]{64}',sha)is None:raise PrefixError('invalid_input')
    if not isinstance(lengths,list)or len(lengths)>MAX_LENGTHS:raise PrefixError('invalid_input')
    if any(type(n)is not int or not 0<=n<=total for n in lengths):raise PrefixError('invalid_input')
    return Path(path),total,sha.lower(),sorted(set(lengths))


def hash_prefixes(reader,total,expected_sha,lengths):
    """Single hashlib state; sorted boundary copies, no seek or reread."""
    digest=hashlib.sha256();position=0;prefixes={};requested=set(lengths)
    for boundary in sorted(requested|{total}):
        while position<boundary:
            chunk=reader.read(min(READ_BYTES,boundary-position))
            if not chunk:raise PrefixError('source_too_short')
            digest.update(chunk);position+=len(chunk)
        if boundary in requested:prefixes[str(boundary)]=digest.copy().hexdigest()
    actual=digest.hexdigest()
    if actual!=expected_sha:raise PrefixError('sha256_mismatch')
    return {'sha256':actual,'total_bytes':total,'prefixes':prefixes}


def compute(request):
    path,total,sha,lengths=validate_request(request)
    try:
        # Check before opening to avoid blocking on named pipes/FIFOs, then
        # check the actual descriptor as well. Symlinks to ordinary files work.
        if not path.is_file():raise PrefixError('source_unavailable')
        with path.open('rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):raise PrefixError('source_unavailable')
            if path.suffix.lower()in ('.zst','.zstd'):
                try:import zstandard
                except ImportError:raise PrefixError('decoder_unavailable') from None
                try:
                    # A bounded read_size and output request keep decoded memory
                    # bounded. read_across_frames supports native concatenated logs.
                    with zstandard.ZstdDecompressor().stream_reader(
                            source,read_size=READ_BYTES,read_across_frames=True,closefd=False) as reader:
                        return hash_prefixes(reader,total,sha,lengths)
                except zstandard.ZstdError:raise PrefixError('invalid_compressed_source') from None
            return hash_prefixes(source,total,sha,lengths)
    except OSError:raise PrefixError('source_unavailable') from None


def run(stdin,stdout):
    try:
        text=stdin.read(MAX_INPUT_CHARS+1)
        if len(text)>MAX_INPUT_CHARS:raise PrefixError('invalid_input')
        try:request=json.loads(text,object_pairs_hook=_no_duplicates,parse_constant=_invalid_constant)
        except (ValueError,TypeError,RecursionError):raise PrefixError('invalid_input') from None
        result=compute(request)
    except PrefixError as error:
        stdout.write(json.dumps({'error':str(error)},separators=(',',':'))+'\n')
        return 1
    except Exception:
        stdout.write('{"error":"prefix_hash_failed"}\n')
        return 1
    stdout.write(json.dumps(result,separators=(',',':'))+'\n')
    return 0


if __name__=='__main__':
    # Binary stdin is explicitly UTF-8; Windows locale must not corrupt paths.
    try:
        payload=sys.stdin.buffer.read(MAX_INPUT_CHARS+1)
        if len(payload)>MAX_INPUT_CHARS:raise ValueError()
        text=payload.decode('utf-8-sig')
    except (ValueError,UnicodeError,OSError):
        sys.stdout.write('{"error":"invalid_input"}\n')
        raise SystemExit(1)
    raise SystemExit(run(io.StringIO(text),sys.stdout))

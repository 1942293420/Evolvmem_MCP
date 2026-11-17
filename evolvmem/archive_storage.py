"""Lossless encrypted manifests over shared, compressed content blocks.

JSON field boundaries keep an appended transcript aligned even when its
metadata or cleaned conversation changes. No previous archive is needed to
read a version: its manifest names immutable blocks directly.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import zlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b'EVOM-ARCHIVE\x02\r\n'
BLOCK_SIZE = 64 * 1024
INLINE_SIZE = 2048
_ID = re.compile(r'^[0-9a-f]{64}$')
_MANIFEST_AAD = b'evolvmem.archive.manifest.v2'
_BLOCK_AAD = b'evolvmem.archive.block.v2'


class StorageSpaceLow(OSError):
    """A retryable write refusal; existing sources stay readable."""


def sync_directory(path):
    """Persist directory entries before committing their SQLite pointers."""
    if os.name == 'nt':
        return  # Windows does not support opening directories for fsync.
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def require_space(config, incoming_bytes=0):
    reserve = config.context_archive_min_free_mb * 1024 * 1024
    if shutil.disk_usage(config.data_dir).free < reserve + incoming_bytes * 2 + 65536:
        raise StorageSpaceLow('archive_storage_space_low')


def _segments(payload):
    """Return exact slices, not a JSON reserialization (whitespace is evidence)."""
    decoder = json.JSONDecoder()
    spans = []
    try:
        pos = len(payload) - len(payload.lstrip())
        if payload[pos] != '{':
            raise ValueError
        pos += 1
        cursor = 0
        while True:
            while payload[pos].isspace():
                pos += 1
            if payload[pos] == '}':
                pos += 1
                if payload[pos:].strip():
                    raise ValueError
                spans.append((cursor, len(payload)))
                break
            key, pos = decoder.raw_decode(payload, pos)
            if not isinstance(key, str):
                raise ValueError
            while payload[pos].isspace():
                pos += 1
            if payload[pos] != ':':
                raise ValueError
            pos += 1
            while payload[pos].isspace():
                pos += 1
            start = pos
            value, pos = decoder.raw_decode(payload, pos)
            del value
            spans.extend(((cursor, start), (start, pos)))
            cursor = pos
            while payload[pos].isspace():
                pos += 1
            if payload[pos] == ',':
                pos += 1
            elif payload[pos] != '}':
                raise ValueError
    except (ValueError, IndexError, RecursionError):
        spans = [(0, len(payload))]
    for start, end in spans:
        raw = payload[start:end].encode('utf-8')
        for offset in range(0, len(raw), BLOCK_SIZE):
            yield raw[offset:offset + BLOCK_SIZE]


class ArchiveStorage:
    def __init__(self, config, key):
        self.config = config
        self.key = key
        self.directory = config.data_dir / 'session_chunks'
        self.aes = AESGCM(key)

    def _digest(self, raw):
        return hmac.new(self.key, raw, hashlib.sha256).hexdigest()

    def _path(self, digest):
        if not isinstance(digest, str) or not _ID.fullmatch(digest):
            raise ValueError('invalid_archive_block')
        return self.directory / (digest + '.bin')

    def _read_block(self, digest):
        blob = self._path(digest).read_bytes()
        raw = zlib.decompress(self.aes.decrypt(blob[:12], blob[12:], _BLOCK_AAD))
        if len(raw) > BLOCK_SIZE or self._digest(raw) != digest:
            raise ValueError('archive_block_changed')
        return raw

    def _write_block(self, raw):
        digest = self._digest(raw)
        path = self._path(digest)
        if path.exists():
            if self._read_block(digest) != raw:
                raise ValueError('archive_block_changed')
            return digest
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        nonce = os.urandom(12)
        blob = nonce + self.aes.encrypt(nonce, zlib.compress(raw, 3), _BLOCK_AAD)
        fd, temporary = tempfile.mkstemp(prefix='.block-', suffix='.tmp', dir=self.directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(blob)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return digest

    def pack(self, payload):
        raw = payload.encode('utf-8')
        require_space(self.config, len(raw))
        parts = []
        for piece in _segments(payload):
            if len(piece) <= INLINE_SIZE:
                parts.append({'inline': base64.b64encode(piece).decode('ascii')})
            else:
                parts.append(self._write_block(piece))
        if self.directory.exists():
            sync_directory(self.directory)
        manifest = {'version': 2, 'size': len(raw),
                    'sha256': hashlib.sha256(raw).hexdigest(), 'parts': parts}
        nonce = os.urandom(12)
        body = zlib.compress(json.dumps(manifest, separators=(',', ':')).encode(), 3)
        blob = MAGIC + nonce + self.aes.encrypt(nonce, body, _MANIFEST_AAD)
        return blob, manifest

    def manifest(self, blob):
        if not blob.startswith(MAGIC):
            # Validate legacy ciphertext too: an unreadable live archive must
            # stop collection, rather than being mistaken for an empty one.
            self.aes.decrypt(blob[:12], blob[12:], None)
            return None
        encrypted = blob[len(MAGIC):]
        result = json.loads(zlib.decompress(self.aes.decrypt(
            encrypted[:12], encrypted[12:], _MANIFEST_AAD)))
        if result['version'] != 2 or not isinstance(result['parts'], list):
            raise ValueError('invalid_archive_manifest')
        for part in result['parts']:
            if isinstance(part, str):
                self._path(part)
            elif not isinstance(part, dict) or set(part) != {'inline'}:
                raise ValueError('invalid_archive_manifest')
        return result

    def unpack(self, blob):
        manifest = self.manifest(blob)
        if manifest is None:
            return self.aes.decrypt(blob[:12], blob[12:], None).decode('utf-8')
        raw = b''.join(self._read_block(part) if isinstance(part, str)
                       else base64.b64decode(part['inline'], validate=True)
                       for part in manifest['parts'])
        if len(raw) != manifest['size'] or hashlib.sha256(raw).hexdigest() != manifest['sha256']:
            raise ValueError('archive_content_changed')
        return raw.decode('utf-8')

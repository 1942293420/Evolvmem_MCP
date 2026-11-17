"""Lexical paths from client metadata; never resolve client paths on the server."""
from __future__ import annotations

import hashlib
import ntpath
import posixpath


def normalize_workspace_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        return ''
    value = value.strip()
    if any(ord(char) < 32 for char in value):
        return ''
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    drive, _ = ntpath.splitdrive(value)
    if drive:
        if not ntpath.isabs(value):
            return ''
        return ntpath.normpath(value)
    if not value.startswith('/'):
        return ''
    return posixpath.normpath(value)


def workspace_key(path: str) -> str:
    path = normalize_workspace_path(path)
    if not path:
        return ''
    identity = ntpath.normcase(path).casefold() if ntpath.splitdrive(path)[0] else path
    return 'workspace-' + hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]


def workspace_name(path: str) -> str:
    path = normalize_workspace_path(path)
    if not path:
        return ''
    module = ntpath if ntpath.splitdrive(path)[0] else posixpath
    return module.basename(path) or path

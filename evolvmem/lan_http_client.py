"""Small bounded credential-file HTTP client shared by Linux adapters."""
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class JsonClient:
    def __init__(self, url, token_file):
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('invalid_http_configuration')
        self.url = url
        self.token_file = Path(token_file)
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def post(self, data):
        try:
            with self.token_file.open('rb') as handle:
                info = os.fstat(handle.fileno())
                if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or not stat.S_ISREG(info.st_mode):
                    raise ValueError('unsafe credential')
                token = handle.read(4097).decode().strip()
            if not token or len(token) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ValueError('invalid credential')
            raw = json.dumps(data, allow_nan=False).encode()
            if len(raw) > 1024 * 1024:
                raise ValueError('request too large')
            request = Request(self.url, raw, {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}, method='POST')
            with self.opener.open(request, timeout=10) as response:
                body = response.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise ValueError('response too large')
            return json.loads(body) if body else None
        except Exception:
            raise RuntimeError('lan_http_unavailable') from None

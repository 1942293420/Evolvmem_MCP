"""Opt-in Linux stdio forwarding: no local model, store, or transcript I/O."""
import ipaddress
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
import uuid

from evolvmem.lan_http_client import JsonClient


def run(config_path):
    client = None
    try:
        config = json.loads(Path(config_path).read_text())
        url = urlsplit(config['url'])
        if url.path != '/owner/mcp' or not ipaddress.ip_address(url.hostname).is_loopback:
            raise ValueError('invalid owner URL')
        client = JsonClient(config['url'], config['token_file'])
    except Exception:
        pass
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError('invalid request')
            if request.get('method') == 'tools/call' and isinstance(request.get('params'), dict):
                arguments = request['params'].setdefault('arguments', {})
                if isinstance(arguments, dict):
                    arguments.setdefault('request_id', uuid.uuid4().hex)
            if client is None:
                raise ValueError('unavailable')
            # No automatic retry. This generated ID would be retained if a retry
            # is added; a failed write may have committed and must be inspected.
            response = client.post(request)
        except Exception:
            response = {'jsonrpc': '2.0', 'id': request.get('id') if isinstance(request, dict) else None,
                        'error': {'code': -32000, 'message': 'LAN MCP unavailable'}}
        if isinstance(request, dict) and 'id' not in request:
            continue
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)

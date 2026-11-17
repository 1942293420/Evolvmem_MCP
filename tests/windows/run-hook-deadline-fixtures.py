"""Run real Windows PowerShell against hanging and slow-drip loopback fixtures."""
import argparse
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--client', required=True)
parser.add_argument('--cases', nargs='+', default=['hang', 'slowdrip', 'clipped', 'prompt', 'success'])
args = parser.parse_args()
records = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *unused):
        pass

    def do_POST(self):
        rpc = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        tool = rpc.get('params', {}).get('name', '')
        params = rpc.get('params', {}).get('arguments', {})
        record = dict(tool=tool, query_length=len(params.get('query', '')),
                      timeout_ms=params.get('timeout_ms'), session_id=params.get('session_id'),
                      budget_header=self.headers.get('X-EvolvMem-Recall-Budget-Ms'))
        if tool == 'experience_recall':
            query = params.get('query', '')
            record['query_kept_start_end'] = query.startswith('START-') and query.endswith('-END')
        records.append(record)
        value = dict(authenticated_user='fixture-user', memory_revision='41.2')
        if tool == 'context_project_recall':
            value['block'] = 'PROJECT-CONTEXT'
            time.sleep(.1)
        elif tool == 'memory_status':
            time.sleep(.05)
        elif tool == 'experience_recall':
            value['results'] = [dict(summary='EXPERIENCE-CONTEXT')]
        result = dict(content=[dict(type='text', text=json.dumps(value))], isError=False)
        body = json.dumps(dict(jsonrpc='2.0', id=rpc.get('id'), result=result)).encode()
        if tool == 'experience_recall' and self.path not in ('/slowdrip', '/success'):
            time.sleep(3.2)
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if tool == 'experience_recall' and self.path == '/slowdrip':
                for b in body:
                    self.wfile.write(bytes([b]))
                    self.wfile.flush()
                    time.sleep(.04)
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
failed = False
try:
    for case in args.cases:
        records.clear()
        result = subprocess.run([
            'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            str(Path(__file__).with_name('test-hook-experience-deadline.ps1')),
            '-ClientPath', args.client, '-FixtureUrl',
            f'http://127.0.0.1:{server.server_port}/{case}', '-Case', case,
        ], capture_output=True, text=True, encoding='utf-8', timeout=18)
        record = dict(case=case, exit_code=result.returncode, stdout=result.stdout.strip(),
                      stderr=result.stderr.strip(), requests=list(records))
        print(json.dumps(record, ensure_ascii=False), flush=True)
        failed |= bool(result.returncode)
        if not result.returncode:
            exp = [r for r in records if r['tool'] == 'experience_recall']
            if (len(exp) != 1 or not (0 < exp[0]['timeout_ms'] <= 1800)
                    or exp[0]['session_id'] != 'fixture-budget-session'
                    or exp[0]['budget_header'] != str(exp[0]['timeout_ms'])):
                failed = True
            if case == 'prompt' and (exp[0]['query_length'] > 4000 or not exp[0]['query_kept_start_end']):
                failed = True
finally:
    server.shutdown()
    server.server_close()
raise SystemExit(int(failed))

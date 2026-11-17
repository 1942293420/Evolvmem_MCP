"""Same synthetic workload for pre-identity and current code, never production.

Run this file with --repo pointing at either checkout. The fixture-only advance
endpoint introduces the next Agent's source AFTER the previous human save.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path
from http.server import ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument('--repo', type=Path, required=True)
parser.add_argument('--port', type=int, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.repo.resolve()))

from evolvmem.config import Config
from evolvmem.context_models import ContextMode
from evolvmem import auto_organization as org
from evolvmem.web_server import make_handler
from tests.test_web_server import _make_service
from tests.test_cross_batch_context import craft

FACT = 'Demo 的运行环境固定为 Linux，执行器部署在 Windows。'

with tempfile.TemporaryDirectory(prefix='evo-acceptance-') as folder:
    service = _make_service(Config(data_dir=Path(folder), apply_environment=False), mode=ContextMode.SHADOW)
    service.knowledge().save_project({'project': 'demo', 'display_name': 'Demo'})
    tasks, steps, saves = [], [], []

    def rebuild(task_id, title):
        task = org._task_row(service, task_id)
        body = task['source_snapshot']
        org.OrganizationWorker(service.config)._store_units(service, task, [{
            'source_start': 0, 'source_end': len(body), 'source_ranges': [[0, len(body)]],
            'text': body, 'title': title, 'cleaned_text': body, 'category': 'environment',
            'project_hint': '', 'evidence_quote': ''}])
        org.OrganizationWorker(service.config)._ingest(service, org.task_view(service, task_id), extract=False)
        org._refresh_task_state(service, task_id)

    def advance():
        index = len(steps)
        if index < 3:
            source = craft(service, session=f'{index+1:08d}-0000-4000-8000-000000000001',
                           start=1, end=4, text=FACT)
            task = org.enqueue(service, [f'archive:{source.id}'])['items'][0]
            tasks.append(task)
            rebuild(task['id'], f'部署说明 {index+1}')
        elif index < 6:
            task = tasks[0]
            rebuild(task['id'], f'重新整理的摘要 {index-2}')
        else:
            raise ValueError('fixture_finished')
        unit = org.unit_views(service, task['id'])[0]
        steps.append({'step': index, 'pending': unit['decision'] != 'manual', 'item_id': unit['item_id']})
        return steps[-1]

    def result():
        units = [org.unit_views(service, t['id'])[0] for t in tasks]
        ids = sorted({u['item_id'] for u in units if u['item_id']})
        source_ids = {s['archive_id'] for item in ids for s in service.store.list_item_sources(item)
                      if s['archive_id'] is not None}
        return {'steps': steps, 'human_saves': len(saves), 'repeat_confirmations': max(0, len(saves)-1),
                'active_item_ids': ids, 'source_count': len(source_ids),
                'missing_sources': len({int(t['source_key'].split(':')[1]) for t in tasks} - source_ids),
                'wrong_projects': sum(u['project'] != 'demo' for u in units)}

    class Handler(make_handler(service)):
        def do_POST(self):
            if self.path == '/__acceptance__/next':
                self._send_json(advance()); return
            if self.path == '/__acceptance__/result':
                self._send_json(result()); return
            if self.path == '/api/knowledge/organization/archive':
                saves.append(self.path)
            super().do_POST()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print('Acceptance fixture ready', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()

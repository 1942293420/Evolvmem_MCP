"""Disposable HTTP fixture for history date/provenance UI checks; no production access."""
from tests.test_project_archive_store import save_manual_event
import argparse
from pathlib import Path
import signal
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evolvmem.config import Config
from evolvmem.context_models import ContextMode
from evolvmem.context_store import _now_iso
from evolvmem.knowledge_restructure import migrate_batch
from evolvmem.project_archive_store import ProjectArchiveStore, ProjectEventDraft
from evolvmem.web_server import make_http_server
from tests.test_project_history_dates import legacy, register
from tests.test_web_server import _make_service


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=39482)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='evo-history-date-fixture-') as folder:
        config = Config(data_dir=Path(folder))
        service = _make_service(config, mode=ContextMode.SHADOW)
        register(service.store)
        for tag in ('2026-07-20-2328', '2026-08-05-1144', '2026-08-31-1730', 'undated'):
            legacy(service.store, tag)
        migrate_batch(SimpleNamespace(store=service.store, config=config))
        with service.store.transaction():
            conn = service.store._connection()
            conn.execute("UPDATE project_events SET recorded_at='2026-10-28 12:00:00'")
            conn.execute("UPDATE project_archives SET started_at='2026-10-28',display_name='ai_purchase',summary='开始时间：2026-10-28'")
            conn.execute('INSERT INTO context_project_registry(project,display_name,created_at,updated_at) VALUES(?,?,?,?)',
                         ('unknown', '日期未知项目', _now_iso(), _now_iso()))
        archives = ProjectArchiveStore(service.store)
        save_manual_event(archives, ProjectEventDraft(project='unknown',event_type='milestone',title='日期未知',content='没有可靠历史日期'))
        archives.rebuild('unknown')
        server = make_http_server(service, port=args.port)
        def stop(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        print(f'History date fixture ready on {args.port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            service.close()


if __name__ == '__main__':
    main()

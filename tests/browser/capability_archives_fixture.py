"""Disposable Signal server for capability and project archive browser checks."""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evolvmem.capability_store import CapabilityMemoryDraft, CapabilityStore
from evolvmem.capability_web import create_category_suggestion
from evolvmem.config import Config
from evolvmem.context_models import ContextMode
from evolvmem.context_store import _now_iso
from evolvmem.project_archive_store import ProjectArchiveStore, ProjectEventDraft
from evolvmem.web_server import make_http_server
from tests.test_web_server import _make_service

config = Config(data_dir=Path(tempfile.mkdtemp(prefix="evo-capability-browser-")))
service = _make_service(config, mode=ContextMode.SHADOW)
with service.store.transaction():
    now = _now_iso()
    service.store._connection().execute(
        "INSERT INTO context_project_registry(project,display_name,created_at,updated_at) "
        "VALUES('bluewhale','蓝鲸',?,?)", (now, now))
    service.store._connection().execute(
        "INSERT INTO capability_intake(title,content,keywords,suggested_category,source_project,"
        "source_ref,fingerprint,created_at,updated_at) VALUES('移动端离线页面','离线时保留草稿。',"
        "'[\"移动端\"]','mobile','bluewhale','archive:intake','intake-fp',?,?)", (now, now))
CapabilityStore(service.store).upsert(CapabilityMemoryDraft(
    category="frontend", title="Vue表单校验", content="提交前统一校验字段。",
    keywords=("Vue", "表单"), source_project="bluewhale", source_ref="archive:memory"))
create_category_suggestion(service, {"label":"移动端", "reason":"出现独立移动开发记忆"})
archives = ProjectArchiveStore(service.store)
archives.record_event(ProjectEventDraft(
    project="bluewhale", event_type="feature", title="新增权限管理",
    content="蓝鲸完成统一权限管理。", source_ref="archive:event",
    occurred_at="2026-01-01 00:00:00"))
archives.rebuild("bluewhale")
server = make_http_server(service, port=39479)
print("Capability browser fixture ready", flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
    service.close()
    shutil.rmtree(config.data_dir)

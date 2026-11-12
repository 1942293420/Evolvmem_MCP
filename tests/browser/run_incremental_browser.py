"""Synthetic fixture only; no live cookies or source databases are opened."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import shutil

from tests.browser.launch_console_fixture import Fixture
from tests.test_history_experience_reads import MATERIAL_DDL


def main():
    directory=Path(tempfile.mkdtemp(prefix='evolvmem-incremental-test-'))
    fixture=Fixture(directory/'fixture')
    conn=fixture.owner.store._connection()
    conn.execute(MATERIAL_DDL)
    for digit,owner,review,projects in [('a','jiangli','accepted',['demo']),
            ('b','jiangli','pending_session_review',['demo','other']),('c','other','accepted',['private-other'])]:
        raw=dict(title='前端分页完整性检查 '+digit,problem='分页后数据遗漏',category='前端开发',tags=['分页'],
                 conditions=['分页列表'],steps=['核对实际条数'],key_changes=['增加完整性检查'],
                 result={'outcome':'unknown','summary':'未独立验证'},applicability=['列表页'],exclusions=[],
                 sources=[{'ordinal':2,'kind':'solution','quote':'精确原问答 <img src=x onerror=alert(1)>'}])
        conn.execute('''INSERT INTO history_experience_ingest_materials VALUES(
            ?,?,?,?,?,?,?,?,?,?,?,?)''',
            (digit*64,owner,'synthetic-source',json.dumps(raw,ensure_ascii=False),json.dumps(projects),
             None,review,'pending_project_mapping',None,0,None,'2026-11-08 15:00:00'))
    conn.commit()
    fixture.start()
    output=directory/'screenshots';output.mkdir()
    env={**os.environ,'EVOLVMEM_TEST_URL':fixture.info['url'].replace('127.0.0.1','knowledge-console.test'),
         'EVOLVMEM_SYNTHETIC_FIXTURE':'1','EVOLVMEM_SCREENSHOTS':str(output),
         'PLAYWRIGHT_MODULE':os.environ.get('PLAYWRIGHT_MODULE','playwright'),
         'CHROMIUM_EXECUTABLE':'/usr/bin/google-chrome'}
    try:
        for script in ('experience_incremental.e2e.cjs','product_modules.e2e.cjs'):
            node=shutil.which('node') or str(Path.home()/'.local/bin/node')
            result=subprocess.run([node,'tests/browser/'+script],env=env,timeout=180)
            if result.returncode:
                return result.returncode
        print(json.dumps({'ok':True,'screenshots':str(output)}))
    finally:
        fixture.close()
    return 0


if __name__=='__main__':
    sys.exit(main())

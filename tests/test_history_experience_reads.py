import json
import sqlite3

import pytest

from evolvmem import history_experience_reads as reads

MATERIAL_DDL = '''CREATE TABLE history_experience_ingest_materials(
    card_key TEXT PRIMARY KEY,owner_id TEXT,source_block_id TEXT,raw_card_json TEXT,
    source_projects_json TEXT,canonical_project TEXT,review_state TEXT,mapping_state TEXT,
    item_id INTEGER,tombstoned INTEGER,superseded_by TEXT,updated_at TEXT)'''


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE context_items(id INTEGER PRIMARY KEY,status TEXT)')
    conn.execute(MATERIAL_DDL)
    yield conn
    conn.close()


def add(conn, digit='a', owner='jiangli', projects=None, **kw):
    card = dict(title='分页方法', problem='前端分页后漏数据', category='前端开发',
                tags=['分页'], conditions=['真实分页'], steps=['核对总数'],
                key_changes=['补完整性检查'], result={'outcome':'unknown','summary':'尚未独立验证'},
                applicability=['分页列表'], exclusions=[], sources=[{'ordinal':2,'quote':'保留原文依据'}])
    row = dict(card_key=digit*64,owner_id=owner,source_block_id='source-1',raw_card_json=json.dumps(card,ensure_ascii=False),
               source_projects_json=json.dumps(projects or ['web-app']),canonical_project=None,
               review_state='pending_session_review',mapping_state='pending_project_mapping',item_id=None,
               tombstoned=0,superseded_by=None,updated_at='2026-11-08 15:00:00')
    row.update(kw)
    conn.execute('INSERT INTO history_experience_ingest_materials VALUES('+','.join('?'*len(row))+')',list(row.values()))
    conn.commit()
    return 'history-'+digit*64


def test_uninitialized_library_read_does_not_create_tables():
    conn=sqlite3.connect(':memory:')
    assert reads.experiences(conn,'jiangli',{})['total']==0
    assert conn.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]==0
    conn.close()


def test_owner_and_detail_boundary_without_writes(db):
    a=add(db)
    b=add(db,'b',owner='kane')
    before=db.total_changes
    assert reads.experiences(db,'jiangli',{})['total']==1
    assert reads.experience(db,'jiangli',a)['source_block_id']=='source-1'
    with pytest.raises(LookupError):
        reads.experience(db,'jiangli',b)
    assert db.total_changes==before


def test_filters_are_applied_before_pagination_and_full_source_context_is_kept(db):
    add(db,'a',projects=['project-one','project-two'])
    add(db,'b',projects=['project-two'],review_state='accepted')
    add(db,'c',projects=['other'])
    result=reads.experiences(db,'jiangli',{'project':'project-two','category':'前端开发','q':'核对总数','page_size':1,'page':2})
    assert result['total']==2 and len(result['rows'])==1
    assert result['rows'][0]['id']=='history-'+'b'*64
    row=reads.experience(db,'jiangli','history-'+'a'*64)
    assert row['source_projects']==['project-one','project-two'] and row['project']==''
    assert reads.experiences(db,'jiangli',{'project':"' OR 1=1 --"})['total']==0
    assert reads.experiences(db,'jiangli',{'review':'accepted'})['total']==1


def test_accepted_extraction_is_never_presented_as_verified(db):
    reference=add(db,review_state='accepted')
    row=reads.experience(db,'jiangli',reference)
    assert row['status']=='candidate' and row['success_count']==0
    assert row['validation_level']=='unverified' and not row['independent_verification']
    assert reads.experiences(db,'jiangli',{'status':'verified'})['total']==0


def test_window_span_references_are_shown_as_exact_bound_quotes(db):
    reference=add(db)
    original=json.loads(db.execute('SELECT raw_card_json FROM history_experience_ingest_materials').fetchone()[0])
    span_card={**original,'sources':[{'span_id':'s-0002','kind':'solution'}]}
    db.execute('ALTER TABLE history_experience_ingest_materials ADD COLUMN envelope_item_json TEXT')
    db.execute('UPDATE history_experience_ingest_materials SET raw_card_json=?,envelope_item_json=?',
               (json.dumps(span_card),json.dumps({'bound_card':original})))
    assert reads.experience(db,'jiangli',reference)['sources'][0]['quote']=='保留原文依据'


def test_revision_requests_are_searchable_without_becoming_accepted(db):
    reference=add(db,review_state='needs_revision')
    assert reads.experiences(db,'jiangli',{'review':'needs_revision'})['rows'][0]['id']==reference
    assert reads.experiences(db,'jiangli',{'review':'accepted'})['total']==0
    assert reads.experience(db,'jiangli',reference)['status']=='candidate'


def test_deleted_tombstoned_and_superseded_materials_stay_hidden(db):
    db.execute("INSERT INTO context_items VALUES(3,'deleted')")
    a=add(db,'a',item_id=3)
    b=add(db,'b',tombstoned=1)
    c=add(db,'c',superseded_by='d'*64)
    assert reads.experiences(db,'jiangli',{})['total']==0
    for reference in (a,b,c):
        with pytest.raises(LookupError):
            reads.experience(db,'jiangli',reference)


@pytest.mark.parametrize('params',[{'category':'fake'},{'review':'active'},{'page':'invalid'},{'status':'active'}])
def test_invalid_filters_fail_bounded(db,params):
    with pytest.raises(ValueError):
        reads.experiences(db,'jiangli',params)

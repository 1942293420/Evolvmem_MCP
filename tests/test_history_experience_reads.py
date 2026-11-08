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


def test_empty_library_has_complete_zero_summary_without_initialization():
    conn=sqlite3.connect(':memory:')
    result=reads.experiences(conn,'jiangli',{'status':'verified'})
    assert result['summary']==dict(total=0,
        reviews={name:0 for name in ('accepted','pending_session_review','pending_root_acceptance','needs_revision')},
        categories={name:dict(all=0,accepted=0,pending_session_review=0,pending_root_acceptance=0,needs_revision=0)
                    for name in reads.CATEGORIES})
    assert conn.execute('SELECT count(*) FROM sqlite_master').fetchone()[0]==0
    conn.close()


def test_library_summary_is_owner_scoped_and_independent_of_every_list_filter(db):
    add(db,'a',review_state='accepted')
    add(db,'b',review_state='pending_session_review')
    backend=dict(title='接口',problem='接口失败',category='后端与接口',steps=['读取响应'],result={'outcome':'unknown','summary':'待核对'})
    add(db,'c',review_state='pending_root_acceptance',raw_card_json=json.dumps(backend,ensure_ascii=False))
    ai={**backend,'category':'AI工具与协作'}
    add(db,'d',review_state='needs_revision',raw_card_json=json.dumps(ai,ensure_ascii=False))
    add(db,'e',owner='other',review_state='accepted')
    add(db,'f',tombstoned=1,review_state='accepted')
    add(db,'1',superseded_by='2'*64,review_state='accepted')
    db.execute("INSERT INTO context_items VALUES(7,'deleted')")
    add(db,'2',item_id=7,review_state='accepted')
    before=db.total_changes
    expected=reads.experiences(db,'jiangli',{})['summary']
    assert expected['total']==4
    assert expected['reviews']==dict(accepted=1,pending_session_review=1,pending_root_acceptance=1,needs_revision=1)
    assert set(expected['categories'])==set(reads.CATEGORIES)
    assert expected['categories']['前端开发']==dict(all=2,accepted=1,pending_session_review=1,pending_root_acceptance=0,needs_revision=0)
    assert expected['categories']['后端与接口']['pending_root_acceptance']==1
    assert expected['categories']['AI工具与协作']['needs_revision']==1
    assert expected['categories']['部署与运维']['all']==0
    for params in ({'q':'no-match'},{'project':'no-project'},{'category':'数据库与数据处理'},
                   {'review':'accepted'},{'review':'pending'},{'status':'verified'},
                   {'status':'inactive'},{'page':9,'page_size':1},
                   {'q':'missing','project':'none','category':'前端开发','review':'needs_revision'}):
        assert reads.experiences(db,'jiangli',params)['summary']==expected
    other=reads.experiences(db,'other',{})['summary']
    assert other['total']==1 and other['reviews']['accepted']==1
    assert db.total_changes==before


def test_pending_review_alias_merges_only_three_unfinished_states_before_pagination(db):
    for digit,review in zip('abcd',('accepted','pending_session_review','pending_root_acceptance','needs_revision')):
        add(db,digit,review_state=review)
    add(db,'e',owner='other',review_state='pending_root_acceptance')
    result=reads.experiences(db,'jiangli',{'review':'pending','page_size':1,'page':2})
    assert result['total']==3 and len(result['rows'])==1
    assert result['rows'][0]['id']=='history-'+'c'*64
    assert reads.experiences(db,'jiangli',{'review':'all'})['total']==4
    assert reads.experiences(db,'jiangli',{'review':'accepted'})['total']==1
    assert all(row['status']=='candidate' and row['success_count']==0
               for row in reads.experiences(db,'jiangli',{'review':'pending'})['rows'])


def test_list_method_and_result_summaries_use_displayed_bound_card_without_promotion(db):
    add(db)
    row=reads.experiences(db,'jiangli',{})['rows'][0]
    assert row['method_summary']=='核对总数'
    assert row['result_summary']=='尚未独立验证'
    db.execute('ALTER TABLE history_experience_ingest_materials ADD COLUMN envelope_item_json TEXT')
    raw=json.loads(db.execute('SELECT raw_card_json FROM history_experience_ingest_materials').fetchone()[0])
    bound={**raw,'steps':['绑定原文中的首个步骤','第二步'], 'result':{'outcome':'unknown','summary':'只记录原文结果'}}
    db.execute('UPDATE history_experience_ingest_materials SET envelope_item_json=?',(json.dumps({'bound_card':bound}),))
    row=reads.experiences(db,'jiangli',{})['rows'][0]
    assert row['method_summary']=='绑定原文中的首个步骤' and row['result_summary']=='只记录原文结果'
    assert row['status']=='candidate' and row['validation_level']=='unverified'
    bound['steps']=[];bound['result']=None
    db.execute('UPDATE history_experience_ingest_materials SET envelope_item_json=?',(json.dumps({'bound_card':bound}),))
    row=reads.experiences(db,'jiangli',{})['rows'][0]
    assert row['method_summary']=='' and row['result_summary']==''

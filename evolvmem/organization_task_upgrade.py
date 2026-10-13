"""Resume legacy pending sources through complete tasks, without a user click.

Only unfinished organization is migrated. Settled history and human decisions
remain stored. A durable per-source outcome prevents a bad archive from either
blocking the next source or being retried indefinitely on every worker tick.
"""
from evolvmem.context_store import _now_iso

_CANDIDATES = """SELECT t.id,t.source_key FROM organization_tasks t
    WHERE t.source_key NOT LIKE 'conversation:%'
    AND t.status IN ('pending','review','failed')
    AND NOT EXISTS(SELECT 1 FROM organization_task_upgrades m WHERE m.task_id=t.id)
    ORDER BY t.id"""


def advance(service, *, limit=1):
    from evolvmem import auto_organization as org, conversation_tasks
    conn=service.store._connection()
    rows=conn.execute(_CANDIDATES+' LIMIT ?', (limit,)).fetchall()
    created=0
    for row in rows:
        if org.settle_ineligible_source(service,row['id'],row['source_key']):
            continue
        try:
            result=conversation_tasks.enqueue(service,[row['source_key']],expand=True)
            created+=result['created']
        except (ValueError,OSError,KeyError) as error:
            # Only known content-free codes may enter the database/UI.
            code=str(error) if isinstance(error,ValueError) and str(error) in (
                'cleaning_source_deleted','cleaning_source_unavailable','classification_no_longer_unassigned',
                'classification_source_changed','conversation_overlap','conversation_source_changed') else 'task_upgrade_failed'
            with service.store.transaction():
                conn.execute('INSERT OR REPLACE INTO organization_task_upgrades(task_id,error_code,updated_at) VALUES(?,?,?)',
                             (row['id'],code,_now_iso()))
                conn.execute("UPDATE organization_tasks SET status='failed',error_code=?,error_detail=?,updated_at=? WHERE id=?",
                             (code,'完整任务整理未开始：原始会话需要核对。',_now_iso(),row['id']))
    return {'created':created,'examined':len(rows)}


def status(service):
    conn=service.store._connection()
    waiting=conn.execute('SELECT count(*) FROM ('+_CANDIDATES+')').fetchone()[0]
    counts={'waiting':waiting,'processing':0,'ready':0,'failed':0}
    counts['failed']=conn.execute("SELECT count(*) FROM organization_task_upgrades WHERE error_code!=''").fetchone()[0]
    # Use the latest successor when fresh conversation content created another
    # job. Counts refer to conversations, not the number of original fragments.
    rows=conn.execute("SELECT t.status,count(*) n FROM organization_tasks t WHERE t.source_key LIKE 'conversation:%' "
        "AND t.status!='superseded' GROUP BY t.status")
    for row in rows:
        key='processing' if row['status'] in ('pending','running') else 'failed' if row['status']=='failed' else 'ready'
        counts[key]+=row['n']
    return counts

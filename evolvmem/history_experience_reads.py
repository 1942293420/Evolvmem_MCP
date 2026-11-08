"""Read-only projection of incrementally extracted history experience candidates.

The caller supplies an authorized library snapshot and its registry owner. No
model, schema initialization, validation evidence or database writes occur here.
"""
import json
import re


CATEGORIES = ('前端开发', '后端与接口', '数据库与数据处理', '画图与表达',
              '业务规则与流程', '自动化与平台集成', '部署与运维', 'AI工具与协作')
REVIEWS = ('all', 'accepted', 'pending_session_review', 'pending_root_acceptance', 'needs_revision')
PREFIX = 'history-'


def _available(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='history_experience_ingest_materials'").fetchone() is not None


def _visible():
    return ("m.owner_id=? AND m.tombstoned=0 AND m.superseded_by IS NULL "
            "AND (m.item_id IS NULL OR EXISTS (SELECT 1 FROM context_items i "
            "WHERE i.id=m.item_id AND i.status!='deleted'))")


def _card(row, *, detail=False):
    raw = json.loads(row['raw_card_json'])
    if 'envelope_item_json' in row.keys():
        # Window outputs cite stable span IDs; use their source-bound view for
        # readable exact quotes while retaining the original JSON in storage.
        raw = json.loads(row['envelope_item_json']).get('bound_card', raw)
    projects = json.loads(row['source_projects_json'])
    project = row['canonical_project'] or ''
    result = dict(id=PREFIX + row['card_key'], title=raw.get('title', raw['problem']),
                  project=project, source_projects=projects, problem=raw['problem'],
                  category=raw.get('category', ''), tags=raw.get('tags', []),
                  conditions=raw.get('conditions', []), status='candidate',
                  validation_level='unverified', independent_verification=False,
                  success_count=0, failure_count=0, expired=False,
                  review_state=row['review_state'], mapping_state=row['mapping_state'],
                  origin='history', updated_at=row['updated_at'])
    if detail:
        result.update(steps=raw.get('steps', []), key_changes=raw.get('key_changes', []),
                      result=raw.get('result'), applicability=raw.get('applicability', []),
                      exclusions=raw.get('exclusions', []), sources=raw.get('sources', []),
                      source_block_id=row['source_block_id'], core_item_id=row['item_id'])
    return result


def experiences(conn, owner_id, params):
    status = params.get('status', 'all') or 'all'
    review = params.get('review', 'all') or 'all'
    category = params.get('category', '') or ''
    if status not in {'all', 'candidate', 'verified', 'inactive'} or review not in REVIEWS:
        raise ValueError('invalid status')
    if category and category not in CATEGORIES:
        raise ValueError('invalid category')
    try:
        page, size = max(1, int(params.get('page', 1))), max(1, min(100, int(params.get('page_size', 25))))
    except (TypeError, ValueError):
        raise ValueError('invalid page') from None
    query, project = params.get('q', '').strip(), params.get('project', '').strip()
    if len(query) > 500 or len(project) > 100:
        raise ValueError('filter too long')
    result = dict(rows=[], total=0, page=page, page_size=size, origin='history')
    if not _available(conn):
        return result
    where, args = [_visible()], [owner_id]
    # Extraction/review is never independent verification. Typed cases remain
    # candidates; real verified experience stays in the existing Core view.
    if status in {'verified', 'inactive'}:
        where.append('0')
    if query:
        where.append('instr(lower(m.raw_card_json),lower(?))>0')
        args.append(query)
    if category:
        where.append("json_extract(m.raw_card_json,'$.category')=?")
        args.append(category)
    if project:
        where.append('(m.canonical_project=? OR EXISTS (SELECT 1 FROM '
                     'json_each(m.source_projects_json) p WHERE p.value=?))')
        args.extend((project, project))
    if review != 'all':
        where.append('m.review_state=?')
        args.append(review)
    clause = ' FROM history_experience_ingest_materials m WHERE ' + ' AND '.join(where)
    result['total'] = conn.execute('SELECT count(*)' + clause, args).fetchone()[0]
    rows = conn.execute('SELECT m.*' + clause + ' ORDER BY m.updated_at DESC,m.card_key '
                        'LIMIT ? OFFSET ?', (*args, size, (page - 1) * size))
    result['rows'] = [_card(row) for row in rows]
    return result


def experience(conn, owner_id, reference):
    if not re.fullmatch(r'history-[a-f0-9]{64}', reference) or not _available(conn):
        raise LookupError('experience not found')
    row = conn.execute('SELECT m.* FROM history_experience_ingest_materials m WHERE '
                       + _visible() + ' AND m.card_key=?', (owner_id, reference[len(PREFIX):])).fetchone()
    if row is None:
        raise LookupError('experience not found')
    return _card(row, detail=True)

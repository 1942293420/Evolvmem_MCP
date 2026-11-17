"""Version-bound native evidence adapted to canonical experience case semantics."""
from copy import deepcopy
import json
from evolvmem.experience_ranking import describe_case,task_verdicts
from evolvmem.library_foundation import FoundationError


def current_events(row, sources):
    by_id={s['id']:s for s in sources}
    latest={}
    # Select the latest revision before filtering state: withdrawal cannot revive
    # an earlier version of the same event. event_order is DB identity ordered.
    for e in row['evidence']:
        key=(e['task_id'],e['event_id'])
        prior=latest.get(key)
        if prior is None or (e['revision'],e['event_order'])>(prior['revision'],prior['event_order']):
            latest[key]=e
    events=[]
    for e in sorted(latest.values(),key=lambda e:e['event_order']):
        source=by_id.get(e['source_id'])
        state=source['qualification'] if source else None
        if (source is None or state is None or e['state']!='active' or e['verification']!='verified'
                or e['role'] not in ('user','tool') or e['item_revision']!=row['item']['revision']
                or row['item_l1_hash'] is None or e['item_content_hash']!=row['item_l1_hash']
                or e['source_revision']!=state['source_revision'] or e['source_hash']!=state['content_hash']
                or source['source_kind'] not in ('tool_result','user_confirmation','historical_record')):
            continue
        level=e['verification_level']
        if (source['source_kind']=='user_confirmation' and level!='user_confirmed'
                or source['source_kind']=='tool_result' and level=='user_confirmed'):
            raise FoundationError('unsupported_experience_level')
        if (not isinstance(e['conditions'],dict) or len(e['conditions'])>20
                or any(not isinstance(k,str) or not isinstance(v,str) for k,v in e['conditions'].items())):
            raise FoundationError('unsupported_experience_conditions')
        events.append(dict(id=e['event_order'],task_id=e['task_id'],event_key=json.dumps([e['task_id'],e['event_id']]),
            outcome=e['outcome'],verification_level=level,conditions_json=json.dumps(e['conditions'],ensure_ascii=False),
            source_id=e['source_id'],revision=e['revision']))
    return events


def experience_current(row,sources):
    verdicts=task_verdicts(current_events(row,sources))
    return ('contradicted' not in verdicts.values()
            and any(v in ('success','confirmed') for v in verdicts.values()))


def case_from_row(row,sources):
    payload=row['item']['experience_payload']
    if not isinstance(payload,dict):raise FoundationError('unsupported_experience_payload')
    required={'project','problem','conditions','steps','rationale','result','applicability','exclusions',
              'transferable','parent_experience_id'}
    if (not required<=payload.keys() or payload['project']!=row['item']['project']
            or not isinstance(payload['conditions'],dict) or type(payload['transferable']) is not bool
            or any(not isinstance(payload[k],str) for k in ('problem','rationale','result'))
            or any(not isinstance(payload[k],list) or any(not isinstance(v,str) for v in payload[k])
                   for k in ('steps','applicability','exclusions'))):
        raise FoundationError('unsupported_experience_payload')
    events=current_events(row,sources)
    verdicts=task_verdicts(events)
    successes=sum(v in ('success','confirmed') for v in verdicts.values())
    failures=sum(v in ('failure','contradicted') for v in verdicts.values())
    summaries=[dict(id=s['id'],source_kind=s['source_kind'],source_ref=s['source_ref'],
        extraction_version=s['extraction_version'],digest=s['qualification']['content_hash'])
        for s in sources if s['qualification'] is not None]
    item=row['item']
    return describe_case(deepcopy(payload),item_id=item['id'],status=item['status'],
        success_count=successes,failure_count=failures,sources=summaries,events=events,updated_at=item['updated_at'])

"""Canonical evidence-backed experience filtering, scoring and bounded output.

Pure functions: no database, model calls, cache, permissions or source loading.
Adapters supply currently qualified cases and already-authorized semantic hits.
"""
import json
import re
from evolvmem.context_models import ContextMatchType

_LOW_SIGNAL_TERMS = {
    '任务', '使用', '出现', '处理', '失败', '恢复', '数据', '服务', '本地',
    '检查', '测试', '状态', '结果', '系统', '请求', '调用', '验证', '项目',
    '返回', '错误', '平台',
}


def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',', ':'))

def terms(text):
    words = re.findall(r'[a-z0-9_]+|[一-鿿]+', text.lower())
    terms = set()
    for word in words:
        if re.fullmatch('[一-鿿]+', word):
            terms.update(word[i:i+2] for i in range(len(word)-1))
        elif len(word) > 2:
            terms.add(word)
    return terms - {'这个', '那个', '一下', '帮我', '我们', '现在', '问题', '如何', '怎么'}


def task_verdicts(events):
    verdicts={}
    for event in events:
        if event['outcome'] in {'success','confirmed','failure','contradicted','inapplicable'}:
            verdicts[event['task_id']]=event['outcome']
    return verdicts

def successful_verifications(events, sources):
    latest_by_task = {}
    for event in events:
        if event['outcome'] in {'success','confirmed','failure','contradicted','inapplicable'}:
            latest_by_task[event['task_id']] = event
    source_by_id = {source['id']: source for source in sources}
    result = []
    for task_id, event in latest_by_task.items():
        if event['outcome'] not in {'success','confirmed'}:
            continue
        source = source_by_id.get(event['source_id'])
        source_summary = None
        if source is not None:
            digest = source.get('digest', source.get('extraction_version', '').partition(':')[2])
            source_summary = {
                'id': source['id'], 'kind': source['source_kind'],
                'ref': source['source_ref'], 'digest': digest,
            }
        event_id = ''
        try:
            event_id = json.loads(event['event_key'])[1]
        except (TypeError, ValueError, IndexError):
            pass
        result.append({
            'task_id': task_id, 'event_id': event_id,
            'level': event['verification_level'],
            'conditions': json.loads(event['conditions_json'] or '{}'),
            'source': source_summary,
        })
    return result


def describe_case(payload, *, item_id,status,success_count,failure_count,sources,events,updated_at):
    verdicts=task_verdicts(events)
    level=('contradicted' if 'contradicted' in verdicts.values() else
           'repeated_verified' if success_count>=2 else 'single_verified' if success_count else 'unverified')
    return dict(payload,id=item_id,status=status,version=1,validation_level=level,
        success_count=success_count,failure_count=failure_count,sources=sources,
        verification=successful_verifications(events,sources),evidence=events,updated_at=updated_at)


def compact_verification(verification):
    source=verification.get('source') or {}
    return {'level':verification['level'],'conditions':verification['conditions'],
            'source':{'id':source.get('id'),'kind':source.get('kind')}}


def rank_cases(cases, *, project,query,constraints,semantic,vector_weight,reranker_min_score,max_chars):
    query_terms = terms(query)
    scored = []
    for case in cases:
        if case['project'] != project and not case['transferable']:
            continue
        if case['validation_level'] in {'unverified','contradicted'}:
            continue
        if any(k in constraints and constraints[k] != v for k,v in case['conditions'].items()):
            continue
        compatible_verification = [
            proof for proof in case['verification']
            if not any(k in constraints and constraints[k] != v
                       for k, v in proof['conditions'].items())]
        if not compatible_verification:
            continue
        inapplicable = [json.loads(e['conditions_json']) for e in case['evidence'] if e['outcome']=='inapplicable']
        if any(c and all(constraints.get(k)==v for k,v in c.items()) for c in inapplicable):
            continue
        text = ' '.join([case['problem'], *case['applicability'], *case['steps']])
        common = query_terms & terms(text)
        overlap = len(common) / max(1,len(query_terms))
        problem_common = common & terms(case['problem'])
        specific_common = common - _LOW_SIGNAL_TERMS
        strong_problem_term = bool(
            problem_common - _LOW_SIGNAL_TERMS)
        semantic_hit = semantic.get(case['id'])
        lexical_support = overlap >= .05 and (
            len(specific_common) >= 2
            or any(len(t) >= 5 for t in specific_common)
            or strong_problem_term)
        strong_semantic = (semantic_hit is not None and
            ContextMatchType.VECTOR in semantic_hit.match_types and
            ContextMatchType.LEXICAL not in semantic_hit.match_types and
            (semantic_hit.score_components.relevance >= vector_weight * .94
             or (semantic_hit.rerank_score is not None and semantic_hit.rerank_score >= reranker_min_score)))
        if not lexical_support and not strong_semantic:
            continue
        # Problem-statement matches carry more intent than incidental words
        # in reusable steps/applicability (for example "服务" or "恢复").
        lexical_relevance = min(
            1.0, overlap + (.15 if strong_problem_term else 0.0))
        relevance = max(
            lexical_relevance,
            (semantic_hit.rerank_score if semantic_hit.rerank_score is not None else semantic_hit.score_components.relevance) if semantic_hit else 0)
        evidence_score = (case['success_count']+1)/(case['success_count']+case['failure_count']+2)
        condition_match = sum(case['conditions'].get(k)==v for k,v in constraints.items()) / max(1,len(constraints))
        score = relevance*.55 + condition_match*.2 + evidence_score*.15 + .1*(case['project']==project)
        result = {k:v for k,v in case.items() if k != 'evidence'}
        result['verification'] = compatible_verification
        result.update(score=round(score,4), match_reason='当前项目案例' if case['project']==project else '可迁移方法：须核对新场景',
                      rerank_score=semantic_hit.rerank_score if semantic_hit else None,
                      adaptation_required=(case['project']!=project or not constraints or
                          any(k not in constraints for proof in compatible_verification
                              for k in proof['conditions'])),
                      instruction='比较机制、环境和约束；说明可复用步骤与必须改变的步骤，验证后再记录结果。')
        scored.append(result)
    scored.sort(key=lambda v:(-(v['rerank_score'] or 0),-v['score'],-v['success_count'],v['id']))
    selected = []
    remaining = max_chars
    for result in scored:
        if len(selected) == 3:
            break
        # Bound automatic content, preserving full cases behind context_read.
        preview = {
            'id': result['id'], 'project': result['project'],
            'problem': result['problem'][:240],
            'conditions': result['conditions'],
            'steps': [step[:240] for step in result['steps'][:5]],
            'applicability': [value[:120]
                              for value in result['applicability'][:3]],
            'exclusions': [value[:120] for value in result['exclusions'][:3]],
            'transferable': result['transferable'],
            'parent_experience_id': result['parent_experience_id'],
            'validation_level': result['validation_level'],
            'success_count': result['success_count'],
            'failure_count': result['failure_count'],
            'verification': [compact_verification(
                result['verification'][0])],
            'score': result['score'],
            'match_reason': result['match_reason'],
            'adaptation_required': result['adaptation_required'],
            'instruction': result['instruction'],
        }
        cost = len(_json(preview))
        if cost > remaining:
            continue
        selected.append(preview)
        remaining -= cost
    response = {'results':selected,'used_chars':max_chars-remaining}
    return response

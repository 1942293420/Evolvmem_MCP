"""Stage editors over the existing policy and collaboration sources; no shadow copies."""
import json
from evolvmem.conversation import clean_messages, render
from evolvmem.extraction_policy import redact_messages

STAGES = {
    'ownership': ('项目归属', '先判断资料属于哪个项目；不明确的交给你确认。', 'ownership_instructions', ('project_alias_matching', 'ambiguous_project_names')),
    'cleaning': ('数据清洗与需求表达', '去除噪声，保留原话；将口语整理成有依据的需求。', 'cleaning_instructions', ('cleaning_drop_lines', 'cleaning_collapse_duplicates')),
    'extraction': ('摘要与问答提炼', '历史生成摘要，经验按问题、答案、分类和条件保存。', 'extraction_instructions', ('related_memory_projects',)),
    'ingestion': ('入库判断', '明确且无冲突自动通过，推断与疑难内容待确认。', None, ('auto_min_confidence', 'require_source', 'min_chars', 'max_chars', 'ignore_keywords', 'auto_explicit_rules')),
    'collaboration': ('协作学习', '从已确认知识积累协作约定，保留适用条件和版本。', None, ()),
}


def read(service, stage):
    if stage not in STAGES:
        raise LookupError('skill_not_found')
    title, description, field, keys = STAGES[stage]
    policy = service.knowledge().rules.read()
    if stage == 'collaboration':
        learning = service.learning().settings()
        instructions, revision, settings = learning['framework'], learning['revision'], {}
    else:
        instructions = policy['settings'][field] if field else policy['instructions']
        revision, settings = policy['revision'], {k: policy['settings'][k] for k in keys}
    skill = instructions if stage == 'collaboration' else f'---\nname: evolvmem-{stage}\ndescription: {description}\n---\n\n{instructions}\n\n## 可执行条件\n\n```json\n{json.dumps(settings, ensure_ascii=False, indent=2)}\n```\n\n读取当前生效版本：GET skills/{stage}。下载文件为当时的快照，实际处理读取当前规则。\n'
    return {'id':stage, 'title':title, 'description':description, 'instructions':instructions,
            'settings':settings, 'revision':revision, 'skill':skill,
            'execution':'本地匹配条件 + AI 归属说明' if stage=='ownership' else
                        '本地去噪 + 模型整理需求' if stage=='cleaning' else
                        '模型提炼 + 原话校验' if stage=='extraction' else
                        '本地入库条件 + AI 整理说明' if stage=='ingestion' else '协作框架 + 已确认规则'}


def save(service, stage, body):
    current = read(service, stage)
    if body.get('expected_revision') != current['revision']:
        raise ValueError('revision_conflict')
    instructions = body.get('instructions', current['instructions'])
    if not isinstance(instructions, str) or not 1 <= len(instructions.strip()) <= 50000:
        raise ValueError('invalid_instructions')
    settings = body.get('settings', {})
    if not isinstance(settings, dict) or set(settings) - set(current['settings']):
        raise ValueError('invalid_rule_settings')
    if stage == 'collaboration':
        service.learning().save_framework({'expected_revision':current['revision'], 'framework':instructions})
    else:
        rules = service.knowledge().rules
        policy = rules.read()
        # Use caller revision so a concurrent change in another stage is detected.
        field = STAGES[stage][2]
        values = {**policy['settings'], **settings}
        if field:
            values[field] = instructions
        rules.save({'expected_revision':body['expected_revision'], 'settings':values,
                    'instructions':policy['instructions'] if field else instructions})
    return read(service, stage)


def preview(service, stage, body):
    if stage != 'cleaning':
        raise ValueError('unsupported_skill_preview')
    messages = body.get('messages')
    if (not isinstance(messages, list) or not 1 <= len(messages) <= 100
            or any(not isinstance(m, dict) or not isinstance(m.get('content'), str) for m in messages)
            or sum(len(m['content']) for m in messages) > 20000):
        raise ValueError('invalid_preview_messages')
    rules = service.knowledge().rules
    policy = rules.read()
    if 'settings' in body or 'instructions' in body:
        if body.get('expected_revision') != policy['revision']:
            raise ValueError('revision_conflict')
        changes = body.get('settings', {})
        if not isinstance(changes, dict) or set(changes) - set(STAGES['cleaning'][3]):
            raise ValueError('invalid_rule_settings')
        policy = rules.prepare({'expected_revision':policy['revision'], 'settings':{
            **policy['settings'], **changes, 'cleaning_instructions':body.get('instructions', policy['settings']['cleaning_instructions'])}})
    cleaned, redacted = redact_messages(clean_messages(messages, policy=policy))
    return {'messages':cleaned, 'text':render(cleaned), 'redacted':redacted,
            'input_messages':len(messages), 'dialogue_messages':len(cleaned),
            'rule_revision':policy['revision'], 'persisted':0, 'model_calls':0}


def dispatch(service, method, route, body):
    if method == 'GET' and route == 'skills':
        return {'skills':[read(service, stage) for stage in STAGES]}
    parts = route.split('/')
    if len(parts)==2:
        return read(service, parts[1]) if method=='GET' else save(service, parts[1], body)
    if len(parts)==3 and method=='POST' and parts[2]=='preview':
        return preview(service, parts[1], body)
    raise LookupError('route_not_found')

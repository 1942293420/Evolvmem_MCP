"""Three active stage editors over the existing saved policy."""
import json
from pathlib import Path
from evolvmem.conversation import clean_messages, render
from evolvmem.extraction_policy import redact_messages

STAGES = {
    'ownership': ('项目归属', '先判断资料属于哪个项目；不明确的交给你确认。', 'ownership_instructions', ('project_alias_matching', 'ambiguous_project_names')),
    'cleaning': ('数据清洗与需求表达', '去除噪声，保留原话；将口语整理成有依据的需求。', 'cleaning_instructions', ('cleaning_drop_lines', 'cleaning_collapse_duplicates')),
    'extraction': ('经验提取与验证', '历史生成项目摘要；经验提取、依据核对与准入判断在同一环节完成。', 'extraction_instructions', ('related_memory_projects', 'auto_min_confidence', 'require_source', 'min_chars', 'max_chars', 'ignore_keywords')),
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
    if stage == 'extraction':
        skill += '\n## 提取结果准入说明\n' + policy['instructions'] + '\n'
    return {'admission_instructions': policy['instructions'] if stage == 'extraction' else '', 'id':stage, 'title':title, 'description':description, 'instructions':instructions,
            'settings':settings, 'revision':revision, 'skill':skill,
            'execution':'本地匹配条件 + AI 归属说明' if stage=='ownership' else
                        '本地去噪 + 模型整理需求' if stage=='cleaning' else
                        '模型提炼 + 原话校验' if stage=='extraction' else
                        '本地入库条件 + AI 整理说明' if stage=='ingestion' else '协作框架 + 已确认规则'}


def export_skills(service):
    """Each stage snapshot is also written as its own installable SKILL.md."""
    root = Path(service.config.data_dir) / 'skills'
    for stage in ('ownership', 'cleaning', 'extraction'):
        skill = read(service, stage)['skill']
        path = root / f'evolvmem-{stage}' / 'SKILL.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text(encoding='utf-8') == skill:
            continue
        temp = path.with_suffix('.tmp')
        temp.write_text(skill, encoding='utf-8')
        temp.replace(path)

    # Retire generated entrypoints without deleting the archived rule contents.
    for stage, message in (('ingestion', '入库判断已并入经验提取与验证，请读取 skills/extraction。'),
                           ('collaboration', '协作学习已停用，不再自动生成、应用或注入协作规则。')):
        path = root / f'evolvmem-{stage}' / 'SKILL.md'
        if path.exists():
            temp = path.with_suffix('.tmp')
            temp.write_text(f'---\nname: evolvmem-{stage}\ndescription: 已停用的旧环节入口\n---\n\n{message}\n')
            temp.replace(path)
    service.learning().export_framework()


def _ensure_exported(service):
    root = Path(service.config.data_dir) / 'skills'
    if any(not (root / f'evolvmem-{stage}' / 'SKILL.md').exists() for stage in ('ownership', 'cleaning', 'extraction')):
        export_skills(service)


def save(service, stage, body):
    if stage == 'collaboration':
        raise ValueError('collaboration_disabled')
    current = read(service, stage)
    if body.get('expected_revision') != current['revision']:
        raise ValueError('revision_conflict')
    instructions = body.get('instructions', current['instructions'])
    if not isinstance(instructions, str) or not 1 <= len(instructions.strip()) <= 50000:
        raise ValueError('invalid_instructions')
    settings = body.get('settings', {})
    if not isinstance(settings, dict) or set(settings) - set(current['settings']):
        raise ValueError('invalid_rule_settings')
    if stage != 'collaboration':
        rules = service.knowledge().rules
        policy = rules.read()
        # Use caller revision so a concurrent change in another stage is detected.
        field = STAGES[stage][2]
        values = {**policy['settings'], **settings}
        if field:
            values[field] = instructions
        admission = body.get('admission_instructions', policy['instructions']) if stage == 'extraction' else policy['instructions']
        if not isinstance(admission, str) or not 1 <= len(admission.strip()) <= 50000:
            raise ValueError('invalid_instructions')
        rules.save({'expected_revision':body['expected_revision'], 'settings':values,
                    'instructions':admission if field else instructions})
    export_skills(service)
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


def verify(service, stage, body, *, llm=None):
    """Run one stage with the saved rules and park outputs in the review queues."""
    if stage not in STAGES:
        raise LookupError('skill_not_found')
    if stage == 'collaboration':
        raise ValueError('collaboration_disabled')
    if not isinstance(body, dict):
        raise ValueError('invalid_request')
    kb = service.knowledge()
    title = STAGES[stage][0]
    if stage in ('ownership', 'ingestion'):
        text = str(body.get('body') or '').strip()
        if not 1 <= len(text) <= 20000:
            raise ValueError('invalid_content')
        project = str(body.get('project') or '')
        if project:
            kb._project(project)
        decision = kb.preview({'title': text[:40], 'body': text, 'project': project, 'source': 'Skill 验证'})
        row = kb.create({'title': '验证 · ' + text.split('\n', 1)[0][:40], 'body': text,
                         'action': 'draft', 'source': f'Skill 验证 · {title}', 'tags': ['skill-verify']})
        suggestion = f"；建议归属 {decision['project']}" if decision.get('project') else ''
        with service._cutover_lock.shared(), kb.store.transaction():
            kb._stamp(row['id'], reason=f"Skill 验证：{decision['reason']}{suggestion}",
                      rule_revision=decision['rule_revision'])
        return {'stage': stage, 'created': 1, 'ids': [row['id']], 'decision': decision}
    if stage == 'cleaning':
        result = preview(service, stage, {'messages': body.get('messages')})
        row = kb.create({'title': f"验证 · 清洗结果（{result['input_messages']} 轮 → {result['dialogue_messages']} 轮）",
                         'body': result['text'], 'action': 'draft', 'source': f'Skill 验证 · {title}',
                         'tags': ['skill-verify']})
        with service._cutover_lock.shared(), kb.store.transaction():
            kb._stamp(row['id'], reason='Skill 验证：清洗结果待确认，确认后才会入库',
                      rule_revision=result['rule_revision'])
        return {'stage': stage, 'created': 1, 'ids': [row['id']], 'redacted': result['redacted']}
    if stage == 'extraction':
        from evolvmem import qa_memory
        from evolvmem.extraction_preview import preview as extract_preview
        project = str(body.get('project') or '')
        result = extract_preview(service, {'messages': body.get('messages'), 'project': project}, llm=llm)
        created = []
        for candidate in result['draft']['candidates']:
            if candidate.get('action') == 'skip':
                continue
            try:
                if candidate.get('question') and candidate.get('answer'):
                    saved = qa_memory.save(service, {'question': candidate['question'], 'answer': candidate['answer'],
                                                     'category': candidate.get('category') or 'reference',
                                                     'project': project, 'action': 'draft',
                                                     'source': f'Skill 验证 · {title}'})
                else:
                    saved = kb.create({'title': candidate['body'][:40], 'body': candidate['body'], 'project': project,
                                       'action': 'draft', 'source': f'Skill 验证 · {title}', 'tags': ['skill-verify']})
                created.append(saved['id'])
            except ValueError:
                continue
        return {'stage': stage, 'created': len(created), 'ids': created,
                'model_calls': result['model_calls'], 'redacted': result['redacted']}
    analysis = service.learning().analyze({'project': str(body.get('project') or '')}, llm=llm)
    return {'stage': stage, 'created': len(analysis.get('rules', [])), **analysis}


def dispatch(service, method, route, body):
    if method == 'GET' and route == 'skills':
        _ensure_exported(service)
        return {'skills':[read(service, stage) for stage in ('ownership', 'cleaning', 'extraction')]}
    parts = route.split('/')
    if len(parts)==2:
        return read(service, parts[1]) if method=='GET' else save(service, parts[1], body)
    if len(parts)==3 and method=='POST' and parts[2]=='preview':
        return preview(service, parts[1], body)
    if len(parts)==3 and method=='POST' and parts[2]=='verify':
        return verify(service, parts[1], body)
    raise LookupError('route_not_found')

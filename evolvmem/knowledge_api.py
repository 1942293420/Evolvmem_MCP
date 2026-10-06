"""Small shared command boundary for the Web and local knowledge skill."""
import re


def dispatch(service, method, path, body=None):
    kb = service.knowledge()
    body = body or {}
    if not isinstance(body, dict):
        raise ValueError('invalid_request')
    route = path.removeprefix('/api/knowledge').strip('/')
    if route == 'cleaning' or route.startswith('cleaning/'):
        from evolvmem.knowledge_cleaning import dispatch as cleaning_dispatch
        return cleaning_dispatch(service, method, route[len('cleaning'):], body)
    if route == 'history/organization' or route.startswith('history/organization/'):
        from evolvmem.history_organization import dispatch as organization_dispatch
        return organization_dispatch(service, method, route[len('history/organization'):], body)
    if route == 'skills' or route.startswith('skills/'):
        from evolvmem.pipeline_skills import dispatch as skills_dispatch
        return skills_dispatch(service, method, route, body)
    from evolvmem import qa_memory, history_memory, memory_recall
    if method == 'GET' and route == 'recall':
        return memory_recall.recall(service, body)
    if method == 'GET' and route == 'history':
        return {'items': history_memory.sessions(service, str(body.get('project') or ''))}
    if method == 'POST' and route == 'history/migrate':
        return history_memory.migrate(service, body)
    if route == 'qa':
        return qa_memory.list_items(service, body) if method == 'GET' else qa_memory.save(service, body)
    if re.fullmatch(r'qa/\d+', route):
        item_id = int(route.split('/')[1])
        return qa_memory.detail(service, item_id) if method == 'GET' else qa_memory.save(service, body, item_id=item_id)
    if method == 'POST' and route == 'extraction/preview':
        from evolvmem.extraction_preview import preview
        return preview(service, body)
    if route.startswith('learning'):
        learning = service.learning()
        if method == 'GET':
            if route == 'learning':
                return learning.overview()
            if route == 'learning/skill':
                return learning.skill(str(body.get('project') or ''))
            if route == 'learning/versions':
                return learning.overview()['versions']
            if re.fullmatch(r'learning/versions/\d+', route):
                return learning.version(int(route.split('/')[-1]))
        if method == 'POST':
            if not re.fullmatch(r'learning/memories/\d+', route):
                raise ValueError('collaboration_disabled')
            return learning.classify(int(route.split('/')[-1]), body)
    if method == 'GET':
        if route == 'project-memory':
            from evolvmem.project_memory import document
            return document(service, str(body.get('project') or ''))
        if re.fullmatch(r'conversations/\d+', route):
            from evolvmem.project_memory import conversation
            return conversation(service, str(body.get('project') or ''), int(route.split('/')[1]))
        if route == 'projects':
            return kb.projects()
        if route == 'items':
            return kb.list_items(body)
        if route == 'rules':
            return kb.rules.read()
        if re.fullmatch(r'items/\d+', route):
            return kb.detail(int(route.split('/')[1]))
    elif method == 'POST':
        commands = {'projects': kb.save_project, 'items': kb.create,
                    'rules': kb.rules.save, 'preview': kb.preview,
                    'organize': kb.organize, 'batch': kb.batch}
        if route in commands:
            result = commands[route](body)
            if route == 'rules':
                from evolvmem.pipeline_skills import export_skills
                export_skills(service)
            return result
        match = re.fullmatch(r'items/(\d+)/(update|assign|transition)', route)
        if match:
            return getattr(kb, match[2])(int(match[1]), body)
    raise LookupError('route_not_found')

"""Small shared command boundary for the Web and local knowledge skill."""
import re


def dispatch(service, method, path, body=None):
    kb = service.knowledge()
    body = body or {}
    if not isinstance(body, dict):
        raise ValueError('invalid_request')
    route = path.removeprefix('/api/knowledge').strip('/')
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
            commands = {'learning/analyze': learning.analyze, 'learning/framework': learning.save_framework,
                        'learning/restore': learning.restore, 'learning/family': learning.set_family}
            if route in commands:
                return commands[route](body)
            match = re.fullmatch(r'learning/(rules|memories)/(\d+)', route)
            if match:
                return (learning.review if match[1]=='rules' else learning.classify)(int(match[2]), body)
    if method == 'GET':
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
            return commands[route](body)
        match = re.fullmatch(r'items/(\d+)/(update|assign|transition)', route)
        if match:
            return getattr(kb, match[2])(int(match[1]), body)
    raise LookupError('route_not_found')

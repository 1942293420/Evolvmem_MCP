"""Explicit applicability, independent of source location and review-rule lifetime."""
import json


def projects(row):
    row = dict(row)
    raw = row.get('projects') or []
    if isinstance(raw, str):
        raw = json.loads(raw)
    return sorted(set(raw or ([row['project']] if row.get('project') else [])))


def scope(row):
    row = dict(row)
    return row.get('knowledge_scope') or row.get('scope') or 'project'


def assigned(row):
    return bool(projects(row)) or scope(row) == 'global'


def normalize(service, body, *, default=None, required=False):
    default = dict(default or {})
    explicit = 'projects' in body or 'project' in body
    values = body.get('projects', [body['project']] if body.get('project') else [] if explicit else projects(default))
    if not isinstance(values, list) or any(not isinstance(p, str) or not p.strip() for p in values):
        raise ValueError('invalid_projects')
    values = sorted(set(values))
    target = body.get('knowledge_scope') or (body.get('scope') if body.get('scope') in ('global', 'project') else None)
    target = target or ('project' if values else scope(default))
    if target not in ('global', 'project'):
        raise ValueError('invalid_knowledge_scope')
    if target == 'global' and values:
        raise ValueError('global_project_conflict')
    if required and target == 'project' and not values:
        raise ValueError('project_required')
    for project in values:
        service.knowledge()._project(project)
    return {'project': values[0] if values else '', 'projects': values, 'knowledge_scope': target}


def fields(row):
    return {'project': dict(row).get('project', ''), 'projects': projects(row), 'knowledge_scope': scope(row)}


def match_sql(alias='i'):
    # One placeholder, with legacy single-project rows remaining readable.
    return f"? IN (SELECT {alias}.project UNION SELECT value FROM json_each({alias}.projects))"


def label(row):
    return '个人通用' if scope(row) == 'global' else '、'.join(projects(row))

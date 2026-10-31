"""Business-module dispatch and actual authenticated HTTP, disposable DBs only."""
import json
from types import SimpleNamespace

import pytest

from evolvmem.capability_store import CapabilityStore
from evolvmem.project_module_web import dispatch, memory_modules
from evolvmem.project_modules import ProjectModuleStore
from tests.test_project_modules import modules, create, bind
from tests.test_capability_provenance import provenance_db, add_memory, unit_source
from tests.test_web_auth import Browser, protected_web


@pytest.fixture
def module_service(modules):
    return SimpleNamespace(store=modules.store)


def test_dispatch_explicit_crud_pending_activation_and_cas(module_service, modules):
    svc = module_service
    assert dispatch(svc, 'GET', '/api/project-modules', {'project':'alpha'})['items'] == []
    row = dispatch(svc, 'POST', '/api/project-modules', {
        'project':'alpha', 'name':'Orders', 'aliases':['订单'], 'description':'Order checks'})['item']
    path = '/api/project-modules/' + row['module_id']
    edited = dispatch(svc, 'POST', path, {'name':'Order handling', 'expected_revision':1})['item']
    assert edited['name'] == 'Order handling' and edited['revision'] == 2
    with pytest.raises(ValueError, match='revision_conflict'):
        dispatch(svc, 'POST', path, {'action':'deactivate', 'expected_revision':1})
    assert modules.get(row['module_id'])['status'] == 'active'
    inactive = dispatch(svc, 'POST', path, {'action':'deactivate', 'expected_revision':2})['item']
    assert inactive['status'] == 'inactive'
    assert dispatch(svc, 'GET', '/api/project-modules', {'project':'alpha'})['items'] == []
    assert len(dispatch(svc, 'GET', '/api/project-modules', {'project':'alpha', 'status':'all'})['items']) == 1
    assert dispatch(svc, 'POST', path, {'action':'activate', 'expected_revision':3})['item']['status'] == 'active'
    suggested = modules.suggest('alpha', 'Shipping')
    assert suggested['status'] == 'pending'
    activated = dispatch(svc, 'POST', '/api/project-modules/' + suggested['module_id'],
                         {'action':'activate', 'expected_revision':1})['item']
    assert activated['status'] == 'active'


@pytest.mark.parametrize('method,path,body,error', [
    ('GET', '/api/project-modules', {}, 'active_project_required'),
    ('POST', '/api/project-modules', [], 'invalid_arguments'),
    ('POST', '/api/project-modules', None, 'invalid_arguments'),
    ('POST', '/api/project-modules/mod_unknown', {'action':'delete'}, 'invalid_module_action'),
    ('GET', '/api/project-modules/memory/0', {}, 'unknown_module_endpoint'),
    ('GET', '/api/project-modules/memory/999', {}, 'capability_memory_not_found'),
])
def test_dispatch_rejects_malformed_inputs(module_service, method, path, body, error):
    with pytest.raises(ValueError, match=error):
        dispatch(module_service, method, path, body)


def seeded_source(modules):
    memory_id = add_memory(modules.store, source=unit_source(modules.store))
    source_id = CapabilityStore(modules.store).sources(memory_id)[0]['source_id']
    return memory_id, source_id


def test_binding_dispatch_checks_memory_identity_quote_and_revision(module_service, modules):
    memory_id, source_id = seeded_source(modules)
    module = create(modules)
    path = '/api/project-modules/' + module['module_id'] + '/bindings'
    body = {'memory_id':memory_id, 'source_id':source_id, 'quote':'user: a fact', 'expected_revision':0}
    with pytest.raises(ValueError, match='module_source_memory_mismatch'):
        dispatch(module_service, 'POST', path, {**body, 'memory_id':memory_id + 999})
    with pytest.raises(ValueError, match='quote_not_verifiable'):
        dispatch(module_service, 'POST', path, {**body, 'quote':'invented'})
    for wrong in (True, '1', 0, -1):
        with pytest.raises(ValueError, match='invalid_module_source'):
            dispatch(module_service, 'POST', path, {**body, 'source_id':wrong})
    binding = dispatch(module_service, 'POST', path, body)['binding']
    assert binding['decision'] == 'manual' and binding['revision'] == 1
    with pytest.raises(ValueError, match='revision_conflict'):
        dispatch(module_service, 'POST', path, body)
    remove = '/api/project-modules/bindings/' + str(binding['binding_id']) + '/remove'
    with pytest.raises(ValueError, match='revision_conflict'):
        dispatch(module_service, 'POST', remove, {'expected_revision':0})
    assert dispatch(module_service, 'POST', remove, {'expected_revision':1})['binding']['status'] == 'inactive'
    assert dispatch(module_service, 'POST', path, {**body, 'expected_revision':2})['binding']['revision'] == 3


def test_binding_dispatch_rejects_current_project_mismatch(module_service, modules):
    memory_id, source_id = seeded_source(modules)
    module = create(modules)
    with modules.store.transaction():
        modules.conn.execute("UPDATE organization_units SET projects='[\"beta\"]',project='beta',revision=revision+1")
    with pytest.raises(ValueError, match='module_project_mismatch'):
        dispatch(module_service, 'POST', '/api/project-modules/' + module['module_id'] + '/bindings',
                 {'memory_id':memory_id, 'source_id':source_id, 'quote':'user: a fact', 'expected_revision':0})


def test_memory_view_includes_pending_classification_and_scoped_inactive_binding_versions(module_service, modules):
    memory_id, source_id = seeded_source(modules)
    CapabilityStore(modules.store).set_status(memory_id, 'pending')
    module = create(modules)
    unit_binding = bind(modules, module, decision='manual', explicit=True)
    direct = modules.bind_source(module['module_id'], source_id, quote='user: a fact',
                                 role='applicable', decision='manual', explicit=True, expected_revision=0)
    modules.unbind(direct['binding_id'], expected_revision=1, explicit=True)
    other_memory = add_memory(modules.store, title='Another', source=unit_source(modules.store))
    other_source = CapabilityStore(modules.store).sources(other_memory)[0]['source_id']
    other = modules.bind_source(module['module_id'], other_source, quote='user: a fact',
                                role='applicable', decision='manual', explicit=True, expected_revision=0)
    result = memory_modules(module_service, memory_id)
    assert result['status'] == 'pending' and result['modules'][0]['not_recalled']
    assert result['sources'][0]['valid'] and result['sources'][0]['quote_text'] == 'user: a fact'
    assert result['sources'][0]['projects'] == ['alpha', 'beta']
    by_id = {row['binding_id']:row for row in result['bindings']}
    assert set(by_id) == {unit_binding['binding_id'], direct['binding_id']}
    assert by_id[direct['binding_id']]['status'] == 'inactive'
    assert by_id[direct['binding_id']]['revision'] == 2
    assert other['binding_id'] not in by_id
    with modules.store.transaction():
        modules.conn.execute("UPDATE session_archives SET state='purged'")
    result = memory_modules(module_service, memory_id)
    assert result['modules'] == []
    assert result['sources'][0]['valid'] is False and result['sources'][0]['reason'] == 'archive_unavailable'


def register_http_project(service):
    with service.store.transaction():
        service.store._connection().execute("INSERT INTO context_project_registry(project,created_at,updated_at) VALUES('alpha','old','old')")


def test_http_anonymous_and_viewer_cannot_mutate_any_module_route(protected_web):
    base, _, service, _, _ = protected_web
    register_http_project(service)
    module = ProjectModuleStore(service.store).create('alpha', 'Orders', explicit=True)
    path = '/api/project-modules/' + module['module_id']
    paths = ['/api/project-modules', path, path + '/bindings', '/api/project-modules/bindings/1/remove']
    anonymous = Browser(base)
    assert anonymous.request('/api/project-modules?project=alpha')[0] == 401
    for route in paths:
        assert anonymous.request(route, 'POST', {})[0] == 401
    viewer = Browser(base)
    me = viewer.login('viewer')
    assert me['can_write'] is False
    code, _, content = viewer.request('/api/project-modules?project=alpha&status=all')
    assert code == 200 and json.loads(content)['items'][0]['module_id'] == module['module_id']
    before = service.store._connection().execute('SELECT count(*) FROM project_module_audit').fetchone()[0]
    for route in paths:
        code, _, content = viewer.request(route, 'POST', {'project':'alpha','name':'Forbidden','expected_revision':1},
                                          {'X-CSRF-Token':me['csrf_token'], 'X-EvolvMem-Role':'owner'})
        assert code == 403, (route, content)
    assert service.store._connection().execute('SELECT count(*) FROM project_module_audit').fetchone()[0] == before
    assert ProjectModuleStore(service.store).get(module['module_id'])['revision'] == 1


def test_http_owner_csrf_dispatch_conflict_and_bad_body(protected_web):
    base, _, service, _, _ = protected_web
    register_http_project(service)
    owner = Browser(base)
    me = owner.login('owner')
    body = {'project':'alpha', 'name':'Orders'}
    assert owner.request('/api/project-modules', 'POST', body)[0] == 403
    headers = {'X-CSRF-Token':me['csrf_token']}
    code, _, content = owner.request('/api/project-modules', 'POST', body, headers)
    assert code == 200, content
    module = json.loads(content)['item']
    path = '/api/project-modules/' + module['module_id']
    code, _, content = owner.request(path, 'POST', {'action':'deactivate','expected_revision':0}, headers)
    assert code == 409 and json.loads(content)['error'] == 'revision_conflict'
    assert owner.request(path, 'POST', {'action':'deactivate','expected_revision':1}, headers)[0] == 200
    code, _, content = owner.request('/api/project-modules', 'POST', [], headers)
    assert code == 400 and json.loads(content)['error'] == 'invalid_arguments'
    assert ProjectModuleStore(service.store).get(module['module_id'])['revision'] == 2

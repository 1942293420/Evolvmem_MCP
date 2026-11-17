"""The optional hook retrieval mode retains authenticated LAN business gates."""
from tests.test_lan_sharing import lan, add


def test_hook_mode_reaches_business_handler_with_identity_and_schema_gates(lan):
    runtime, adapter = lan
    args = {'project':'demo', 'query':'a current observed failure', 'mode':'hook'}
    result = adapter.call_tool('kane', 'experience_recall', args)
    assert result['authenticated_user'] == 'kane'
    assert result['results'] == []
    assert 'error' not in result
    for changed in ({'mode':'other'}, {'mode':None}, {'user':'jiangli'}):
        assert 'error' in adapter.call_tool('kane', 'experience_recall', dict(args, **changed))
    item = add(adapter, 'kane')
    store = runtime.server_for('kane').context_service.store
    with store.transaction():
        store._connection().execute(
            "DELETE FROM context_layers WHERE item_id=? AND layer='l1'", (item['context_id'],))
    denied = adapter.call_tool('kane', 'experience_recall', args)
    assert denied.get('error') in {'degraded_legacy', 'unknown_tool'}
    assert not denied.get('results')

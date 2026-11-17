"""Build legacy stored histories for compatibility tests after the default changed.

These tests exercise old predecessor/derivation data, not today's public submit
or migration worker. Current defaults are covered by test_task_defaults and
complete_task_pipeline; this adapter deliberately seeds the pre-migration shape.
"""
import pytest


@pytest.fixture(autouse=True)
def legacy_stored_history(monkeypatch,request):
    if request.node.name in ('test_the_backlog_orders_same_session_batches_before_organizing','test_discovery_across_the_five_source_limit_keeps_session_order'):return
    from evolvmem import auto_organization as org, conversation_tasks
    def seed(service,keys,*,expand=False):
        return org.enqueue(service,keys)
    def old_tick(worker):
        service=worker._connect()
        from evolvmem.organization_semantic_review import tick
        if tick(service):return True
        task=worker._claim(service)
        if task is None:return False
        worker._process(service,task)
        return True
    monkeypatch.setattr(conversation_tasks,'enqueue',seed)
    monkeypatch.setattr(org.OrganizationWorker,'tick',old_tick)

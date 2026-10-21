"""The project content version must change whenever the document would change.

This exists because the version is what the console uses to decide whether its
in-memory copy of a project document is still valid. A version that fails to move
means the page silently keeps showing old content after another agent edits the
project — the exact bug it is meant to prevent. A version that moves when nothing
changed is merely wasteful, so every case here is the dangerous direction.

Covered explicitly, because each was a real trap while building it:

* a body edit lands in ``context_layers``, not in ``context_items`` — an aggregate
  over items alone misses it entirely;
* a project move has to move **both** projects' versions;
* content expiring is a change with no row mutation at all, which no version
  counter can observe — hence the separate ``next_expiry``.
"""
import sqlite3

import pytest


@pytest.fixture()
def project_db(tmp_path):
    from types import SimpleNamespace

    from evolvmem.config import Config
    from evolvmem.context_store import ContextStore

    store = ContextStore(Config(data_dir=tmp_path))
    store.initialize()
    conn = store._connection()
    now = '2026-01-01 00:00:00'
    conn.execute(
        'INSERT INTO context_items(id,identity_key,content_type,project,scope,status,'
        'created_at,updated_at) VALUES(1,?,?,?,?,?,?,?)',
        ('k1', 'session_summary', 'alpha', 'project', 'active', now, now))
    conn.execute(
        'INSERT INTO context_items(id,identity_key,content_type,project,scope,status,'
        'created_at,updated_at) VALUES(2,?,?,?,?,?,?,?)',
        ('k2', 'session_summary', 'beta', 'project', 'active', now, now))
    for item_id in (1, 2):
        conn.execute(
            'INSERT INTO context_layers(item_id,layer,content,content_hash,generator,'
            'created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
            (item_id, 'l1', 'body', 'h%d' % item_id, 'test', now, now))
    conn.commit()
    # content_version takes a service; only .store is used by the read path.
    yield SimpleNamespace(store=store, _store=store)
    store.close()


def _version(service, project):
    from evolvmem.project_memory import content_version
    return content_version(service, project)


def test_version_is_stable_without_changes(project_db):
    first = _version(project_db, 'alpha')['version']
    second = _version(project_db, 'alpha')['version']
    assert first == second


def test_body_edit_changes_the_version(project_db):
    before = _version(project_db, 'alpha')['version']
    # A real edit stamps updated_at in context_layers; context_items is untouched.
    project_db.store._connection().execute(
        "UPDATE context_layers SET updated_at='2027-06-01 00:00:00' "
        "WHERE item_id=1 AND layer='l1'")
    project_db.store._connection().commit()
    assert _version(project_db, 'alpha')['version'] != before


def test_project_move_changes_both_versions(project_db):
    before_alpha = _version(project_db, 'alpha')['version']
    before_beta = _version(project_db, 'beta')['version']
    project_db.store._connection().execute(
        "UPDATE context_items SET project='beta' WHERE id=1")
    project_db.store._connection().commit()
    assert _version(project_db, 'alpha')['version'] != before_alpha
    assert _version(project_db, 'beta')['version'] != before_beta


def test_status_change_changes_the_version(project_db):
    before = _version(project_db, 'alpha')['version']
    project_db.store._connection().execute(
        "UPDATE context_items SET status='archived' WHERE id=1")
    project_db.store._connection().commit()
    assert _version(project_db, 'alpha')['version'] != before


def test_adding_an_item_changes_the_version(project_db):
    before = _version(project_db, 'alpha')['version']
    now = '2026-02-02 00:00:00'
    project_db.store._connection().execute(
        'INSERT INTO context_items(id,identity_key,content_type,project,scope,status,'
        'created_at,updated_at) VALUES(3,?,?,?,?,?,?,?)',
        ('k3', 'session_summary', 'alpha', 'project', 'active', now, now))
    project_db.store._connection().commit()
    assert _version(project_db, 'alpha')['version'] != before


def test_next_expiry_ignores_already_expired_rows(project_db):
    """An expired row is already absent from the body, so reporting it would pin
    every cached copy permanently stale."""
    conn = project_db.store._connection()
    # Both rows belong to alpha: one already expired, one still in the future.
    conn.execute(
        "UPDATE context_items SET project='alpha' WHERE id=2")
    conn.execute(
        "UPDATE context_items SET expires_at='2020-01-01 00:00:00' WHERE id=1")
    conn.execute(
        "UPDATE context_items SET expires_at='2099-01-01 00:00:00' WHERE id=2")
    conn.commit()
    assert _version(project_db, 'alpha')['next_expiry'] == '2099-01-01 00:00:00'


def test_version_requires_a_project(project_db):
    with pytest.raises(ValueError):
        _version(project_db, '')

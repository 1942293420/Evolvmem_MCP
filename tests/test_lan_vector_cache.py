"""LAN opt-in protects real resident index caches used by native hooks."""
import numpy as np
from evolvmem.config import Config
from evolvmem.vector_index import VectorIndex


def pair(tmp_path):
    config = Config(data_dir=tmp_path, apply_environment=False)
    config.embedding_http_url = 'http://127.0.0.1:1'  # Native hook opt-in; no HTTP needed here.
    first, second = VectorIndex(config), VectorIndex(config)
    first.initialize(3)
    first.add(1, np.array([1., 0., 0.]))
    first.save()
    second.initialize(3)
    return first, second


def test_lan_save_merges_pending_ids_and_live_reads_refresh(tmp_path):
    first, second = pair(tmp_path)
    first.add(2, np.array([0., 1., 0.]))
    second.add(3, np.array([0., 0., 1.]))
    first.save()
    second.save()
    assert first.ids() == [1, 2, 3]
    assert first.count() == 3
    assert {v['id'] for v in first.search(np.array([1., 0., 0.]), k=5)} == {1, 2, 3}
    first.close()
    first.initialize(3)
    assert first.ids() == [1, 2, 3]


def test_lan_remove_and_rebuild_preserve_other_writer_and_clean_save(tmp_path):
    first, second = pair(tmp_path)
    first.add(2, np.array([0., 1., 0.]))
    first.save()
    assert second.remove(2)
    second.save()
    assert first.ids() == [1]
    # A clean stale object must never overwrite a writer's new cache.
    second.add(3, np.array([0., 0., 1.]))
    second.save()
    first.save()
    assert first.ids() == [1, 3]
    second.rebuild([4], [np.array([1., 1., 0.])])
    assert first.ids() == [4]


def test_lan_rebuild_keeps_ids_added_after_its_loaded_snapshot(tmp_path):
    first, second = pair(tmp_path)
    # A caller collected SQLite IDs while its index contained only ID 1.
    second.add(2, np.array([0., 1., 0.]))
    second.save()
    first.rebuild([1], [np.array([1., 0., 0.])])
    assert first.ids() == [1, 2]
    assert second.ids() == [1, 2]


def test_lan_rebuild_includes_new_disk_id_without_duplicate_insert(tmp_path):
    first, second = pair(tmp_path)
    second.add(2, np.array([0., 1., 0.]))
    second.save()
    first.rebuild([1, 2], [np.array([1., 0., 0.]), np.array([0., 0., 1.])])
    assert first.ids() == [1, 2]
    assert second.ids() == [1, 2]
    hit = second.search(np.array([0., 0., 1.]), k=1)[0]
    assert hit['id'] == 2
    assert abs(hit['distance']) < 1e-6
    second.close()
    second.initialize(3)
    assert second.ids() == [1, 2]

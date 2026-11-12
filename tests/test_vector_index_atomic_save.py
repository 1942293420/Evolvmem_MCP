"""Stage 0 Task 0.5 TDD tests for VectorIndex.save() sibling temporary file + os.replace atomicity."""
from __future__ import annotations

import os
import threading
from pathlib import Path

import numpy as np
import pytest

from evolvmem.config import Config
from evolvmem.vector_index import VectorIndex


def test_vector_index_save_uses_atomic_tmp_and_os_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = Config(data_dir=tmp_path / "vec_data", apply_environment=False)
    dim = 8
    idx = VectorIndex(cfg)
    idx.initialize(dim=dim)

    replace_calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        src_p, dst_p = Path(src), Path(dst)
        replace_calls.append((src_p, dst_p))
        assert src_p.parent == dst_p.parent
        assert src_p != dst_p
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)
    v1 = np.ones(dim, dtype=np.float32)
    idx.add(10, v1)
    idx.save()
    idx.close()

    assert len(replace_calls) == 1
    assert replace_calls[0][1] == idx.path
    original_bytes = idx.path.read_bytes()
    assert len(original_bytes) > 0

    # Simulate crash/disk full during os.replace: original file must remain untouched, dirty kept, zero .tmp leftovers
    def failing_replace(src, dst):
        raise OSError("simulated disk full during atomic replace")

    monkeypatch.setattr(os, "replace", failing_replace)
    idx2 = VectorIndex(cfg)
    idx2.initialize(dim=dim)
    idx2.mark_dirty()
    idx2.add(20, v1)
    with pytest.raises(OSError, match="simulated disk full"):
        idx2.save()
    idx2.close()

    assert idx.path.read_bytes() == original_bytes
    assert idx2.is_dirty()
    leftovers = [p.name for p in idx.path.parent.iterdir() if ".tmp" in p.name]
    assert leftovers == []

    # Concurrent readers never observe a truncated/corrupt index file during rapid saves
    monkeypatch.setattr(os, "replace", real_replace)
    errors: list[Exception] = []
    stop = threading.Event()

    def reader_worker() -> None:
        while not stop.is_set():
            try:
                r = VectorIndex(cfg)
                r.initialize(dim=dim)
                assert r.count() >= 1
                r.close()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
                break

    readers = [threading.Thread(target=reader_worker, daemon=True) for _ in range(3)]
    for t in readers:
        t.start()
    try:
        writer = VectorIndex(cfg)
        writer.initialize(dim=dim)
        for k in range(30, 55):
            writer.add(k, np.full(dim, float(k), dtype=np.float32))
            writer.save()
        writer.close()
    finally:
        stop.set()
        for t in readers:
            t.join(timeout=3)

    assert errors == []

"""Regression test: VectorIndex lifecycle with deleted HNSW slots.

USearch 2.26.0 restores a saved index by shrinking the vector pointer table to
``index.size()`` (live vectors only) and then copying the full slot table into
the smaller allocation.  An index that contains deleted slots therefore
overflows the native heap while loading, and the corruption aborts the process
(SIGABRT) on reset, for example when the EvolvMem index is closed and reopened.

EvolvMem's normal lifecycle - add vectors, remove vectors, save, reopen, close -
must survive that boundary.  The failure is native, so it cannot be asserted
in-process: each lifecycle runs in a child interpreter and the parent test
reports a readable assertion failure instead of being killed by the abort.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

# Geometry of the observed failure: 1456 HNSW slots, 92 of them removed,
# leaving 1364 live vectors.
SIZE = 1456
KEPT = 1364
DIM = 3
SEED = 20260920
CHILD_TIMEOUT_SECONDS = 300

_CHILD_SOURCE = '''\
"""Child interpreter: EvolvMem VectorIndex add/remove/save/reopen/close."""
import json
import sys
from pathlib import Path

try:
    import resource
except ImportError:  # Windows has no resource module.
    resource = None
else:
    # A native SIGABRT must not leave a core dump behind during the test run.
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (OSError, ValueError):
        pass

import numpy as np

from evolvmem.config import Config
from evolvmem.vector_index import VectorIndex

SIZE = 1456
KEPT = 1364
DIM = 3
SEED = 20260920


def phase(name):
    print("PHASE " + name, file=sys.stderr, flush=True)


def main():
    data_dir = Path(sys.argv[1])
    config = Config(
        data_dir=data_dir,
        lan_shared_vector_cache=sys.argv[2] == "1",
    )
    rng = np.random.default_rng(SEED)
    vectors = rng.random((SIZE, DIM), dtype=np.float32)

    phase("build")
    index = VectorIndex(config)
    index.initialize(dim=DIM)
    for key in range(SIZE):
        index.add(key, vectors[key])
    if index.count() != SIZE:
        raise AssertionError("expected %d vectors, got %d" % (SIZE, index.count()))

    phase("remove")
    for key in range(KEPT, SIZE):
        if index.remove(key) is not True:
            raise AssertionError("remove(%d) reported no removal" % key)
    if index.count() != KEPT:
        raise AssertionError("expected %d vectors, got %d" % (KEPT, index.count()))

    phase("save")
    index.save()
    phase("close")
    index.close()

    phase("reopen")
    reopened = VectorIndex(config)
    reopened.initialize(dim=DIM)
    count = reopened.count()
    ids = reopened.ids()
    if count != KEPT or ids != list(range(KEPT)):
        raise AssertionError(
            "reopened index holds %d ids (count %d)" % (len(ids), count)
        )
    hits = reopened.search(vectors[0], k=1)
    if not hits or hits[0]["id"] != 0:
        raise AssertionError("reopened search returned %r" % (hits,))

    phase("save-again")
    reopened.save()
    phase("close-again")
    reopened.close()

    phase("reopen-again")
    again = VectorIndex(config)
    again.initialize(dim=DIM)
    if again.count() != KEPT or again.ids() != list(range(KEPT)):
        raise AssertionError("second reopen lost vectors: %d" % again.count())
    phase("close-final")
    again.close()

    print(
        json.dumps(
            {
                "count": count,
                "ids": len(ids),
                "top": hits[0]["id"],
                "min_id": ids[0],
                "max_id": ids[-1],
            }
        ),
        flush=True,
    )


main()
'''


def _child_environment(data_dir: Path) -> dict:
    """Inherit the caller's PYTHONPATH (and therefore its usearch build)."""
    env = os.environ.copy()
    entries = [str(Path(__file__).resolve().parents[1])]
    inherited = env.get("PYTHONPATH")
    if inherited:
        entries.append(inherited)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["EVOLVMEM_DATA_DIR"] = str(data_dir)
    return env


def _exit_description(returncode: int) -> str:
    if returncode < 0:
        try:
            return "%s (signal %d)" % (signal.Signals(-returncode).name, -returncode)
        except ValueError:
            return "signal %d" % -returncode
    return "exit code %d" % returncode


def _last_phase(stderr: str) -> str:
    marker = "PHASE "
    phases = [
        line[len(marker):].strip()
        for line in stderr.splitlines()
        if line.startswith(marker)
    ]
    return phases[-1] if phases else "<none reported>"


def _failure_message(shared_cache: bool, completed, stderr: str) -> str:
    tail = "\n".join(stderr.splitlines()[-20:]) or "<empty>"
    return (
        "VectorIndex add/remove/save/reopen/close did not survive in a child "
        "interpreter (lan_shared_vector_cache=%r): %s after phase %r.\n"
        "stderr tail:\n%s\n"
        "USearch 2.26.0 restores an index with deleted slots by shrinking the "
        "vector pointer table to index.size() and then copying the full slot "
        "table into it; the heap-buffer-overflow aborts the child on reopen."
        % (
            shared_cache,
            _exit_description(completed.returncode),
            _last_phase(stderr),
            tail,
        )
    )


@pytest.mark.parametrize(
    "shared_cache", [False, True], ids=["cache-off", "cache-on"]
)
def test_add_remove_save_reopen_close_survives_deleted_slots(
    tmp_path, shared_cache
):
    if shared_cache and sys.platform == "win32":
        pytest.skip("SharedVectorCache uses fcntl, which is POSIX-only")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    script = tmp_path / "vector_index_lifecycle_child.py"
    script.write_text(_CHILD_SOURCE, encoding="utf-8")

    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(script),
                str(data_dir),
                "1" if shared_cache else "0",
            ],
            cwd=str(tmp_path),
            env=_child_environment(data_dir),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=CHILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            "VectorIndex lifecycle child did not finish within %d seconds "
            "(lan_shared_vector_cache=%r)" % (CHILD_TIMEOUT_SECONDS, shared_cache),
            pytrace=False,
        )

    stderr = completed.stderr or ""
    if completed.returncode != 0:
        pytest.fail(_failure_message(shared_cache, completed, stderr), pytrace=False)

    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    assert lines, "child produced no lifecycle result; stderr:\n%s" % stderr
    payload = json.loads(lines[-1])
    assert payload == {
        "count": KEPT,
        "ids": KEPT,
        "top": 0,
        "min_id": 0,
        "max_id": KEPT - 1,
    }, "unexpected lifecycle result; stderr:\n%s" % stderr

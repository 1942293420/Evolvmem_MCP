from __future__ import annotations

import os
from pathlib import Path
import pytest

from evolvmem.pg_test_guard import assert_safe_test_pg_target

STAGE5_PG_CONTAINER = "evolvmem-pg-test-stage5"
STAGE5_PG_PORT = 55434
STAGE5_PITR_CONTAINER = "evolvmem-pg-pitr-drill-stage5"
STAGE5_PITR_PORT = 25436
STAGE5_WORK_DIR = Path("/home/jiangli/evolvmem-stage5-20261115")
STAGE5_PW_FILE = Path("/tmp/evolvmem-pg-stage5.pw")


@pytest.fixture(scope="session", autouse=True)
def _guard_against_prod_pg() -> None:
    assert_safe_test_pg_target(
        port=STAGE5_PG_PORT,
        container_name=STAGE5_PG_CONTAINER,
        volumes=[STAGE5_WORK_DIR.as_posix()],
    )
    assert_safe_test_pg_target(
        port=STAGE5_PITR_PORT,
        container_name=STAGE5_PITR_CONTAINER,
    )
    if os.environ.get("PGPORT", "").strip() == "55432":
        raise RuntimeError("PGPORT=55432 forbidden in pytest")

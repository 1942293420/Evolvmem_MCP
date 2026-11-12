"""Task 1.0 TDD tests: test scripts and pytest must refuse port 55432, evolvmem_pgdata, and evolvmem-pg-prod."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from evolvmem.pg_test_guard import (
    PROD_PG_CONTAINER,
    PROD_PG_PORT,
    PROD_PG_VOLUME,
    ProductionPostgresGuardError,
    assert_safe_test_pg_target,
)


def test_guard_rejects_production_port_55432_across_all_formats() -> None:
    with pytest.raises(ProductionPostgresGuardError, match="55432"):
        assert_safe_test_pg_target(port=PROD_PG_PORT)

    with pytest.raises(ProductionPostgresGuardError, match="55432"):
        assert_safe_test_pg_target(port="55432")

    with pytest.raises(ProductionPostgresGuardError, match="55432"):
        assert_safe_test_pg_target(dsn="postgresql://tester:secret@127.0.0.1:55432/evolvmem_control")

    with pytest.raises(ProductionPostgresGuardError, match="55432"):
        assert_safe_test_pg_target(dsn="host=127.0.0.1 port=55432 dbname=postgres user=postgres")

    with pytest.raises(ProductionPostgresGuardError, match="55432"):
        assert_safe_test_pg_target(argv=["docker", "run", "-p", "127.0.0.1:55432:5432", "postgres"])


def test_guard_rejects_production_volume_and_container_name() -> None:
    with pytest.raises(ProductionPostgresGuardError, match=PROD_PG_VOLUME):
        assert_safe_test_pg_target(volumes=["evolvmem_pgdata:/var/lib/postgresql/data"])

    with pytest.raises(ProductionPostgresGuardError, match=PROD_PG_VOLUME):
        assert_safe_test_pg_target(argv=["--mount", "type=volume,source=evolvmem_pgdata,target=/var/lib/postgresql/data"])

    with pytest.raises(ProductionPostgresGuardError, match=PROD_PG_CONTAINER):
        assert_safe_test_pg_target(container_name="evolvmem-pg-prod")


def test_guard_allows_isolated_test_pg_targets() -> None:
    assert_safe_test_pg_target(
        port=55433,
        dsn="postgresql://tester:pw@127.0.0.1:55433/test_stage1",
        volumes=["evolvmem_pg_test_stage1:/var/lib/postgresql/data"],
        container_name="evolvmem-pg-test-stage1",
        argv=["--port", "55433", "--volume", "evolvmem_pg_test_stage1"],
    )


def test_manage_test_pg_cli_refuses_prod_port_and_volume() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "linux" / "manage_test_pg.py"
    p_port = subprocess.run(
        [sys.executable, str(script), "start", "--port", "55432", "--name", "evolvmem-pg-test-bad"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert p_port.returncode != 0
    assert "55432" in (p_port.stderr + p_port.stdout)

    p_vol = subprocess.run(
        [sys.executable, str(script), "start", "--port", "55439", "--volume", "evolvmem_pgdata"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert p_vol.returncode != 0
    assert "evolvmem_pgdata" in (p_vol.stderr + p_vol.stdout)


def test_pytest_autouse_psycopg_guard_blocks_port_55432() -> None:
    with pytest.raises(ProductionPostgresGuardError, match="55432"):
        psycopg.connect("postgresql://postgres:x@127.0.0.1:55432/postgres")

    with pytest.raises(ProductionPostgresGuardError, match="55432"):
        psycopg.connect(host="127.0.0.1", port=55432, dbname="postgres", user="postgres")

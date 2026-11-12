"""Shared test fixtures and hard production PostgreSQL safety guard."""

from pathlib import Path
import shutil
import sys
import tempfile

import pytest

from evolvmem.config import Config
from evolvmem.pg_test_guard import assert_safe_test_pg_target


@pytest.fixture(autouse=True)
def _block_production_postgres_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no pytest test ever connects psycopg to production port 55432."""
    assert_safe_test_pg_target()
    pg_mod = sys.modules.get("psycopg")
    if pg_mod is None:
        try:
            import psycopg as pg_mod  # type: ignore[no-redef]
        except ImportError:
            return
    orig_connect = getattr(pg_mod, "connect", None)
    if callable(orig_connect):
        def _guarded_connect(conninfo: str = "", *args, **kwargs):
            assert_safe_test_pg_target(
                port=kwargs.get("port"),
                dsn=str(conninfo) if conninfo else None,
            )
            return orig_connect(conninfo, *args, **kwargs)

        monkeypatch.setattr(pg_mod, "connect", _guarded_connect)


@pytest.fixture
def temp_dir():
    """Creates a temporary directory, auto-cleaned after test."""
    d = tempfile.mkdtemp()
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def test_config(temp_dir):
    """Returns test configuration pointing to a temporary directory."""
    return Config(data_dir=temp_dir)

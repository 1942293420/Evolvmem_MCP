"""Shared test fixtures and hard production PostgreSQL safety guard."""

from pathlib import Path
import shutil
import sys
import tempfile

import pytest

from evolvmem.config import Config
from evolvmem.pg_test_guard import assert_safe_test_pg_target


@pytest.fixture(autouse=True)
def _block_production_postgres_in_tests():
    """Ensure no pytest test ever connects psycopg to production port 55432."""
    assert_safe_test_pg_target()
    with pytest.MonkeyPatch.context() as mp:
        pg_mod = sys.modules.get("psycopg")
        if pg_mod is None:
            try:
                import psycopg as pg_mod  # type: ignore[no-redef]
            except ImportError:
                pg_mod = None
        if pg_mod is not None:
            orig_connect = getattr(pg_mod, "connect", None)
            if callable(orig_connect):
                def _guarded_connect(conninfo: str = "", *args, **kwargs):
                    assert_safe_test_pg_target(
                        port=kwargs.get("port"),
                        dsn=str(conninfo) if conninfo else None,
                    )
                    return orig_connect(conninfo, *args, **kwargs)

                mp.setattr(pg_mod, "connect", _guarded_connect)
            conn_cls = getattr(pg_mod, "Connection", None)
            if conn_cls is not None and hasattr(conn_cls, "connect"):
                orig_cls_connect = conn_cls.__dict__.get("connect")
                orig_bound = conn_cls.connect

                def _guarded_cls_connect(cls, conninfo: str = "", *args, **kwargs):
                    assert_safe_test_pg_target(
                        port=kwargs.get("port"),
                        dsn=str(conninfo) if conninfo else None,
                    )
                    if isinstance(orig_cls_connect, classmethod):
                        return orig_cls_connect.__func__(cls, conninfo, *args, **kwargs)
                    return orig_bound(conninfo, *args, **kwargs)

                mp.setattr(conn_cls, "connect", classmethod(_guarded_cls_connect))
        yield


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

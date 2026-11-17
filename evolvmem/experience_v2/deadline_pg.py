"""Dedicated libpq connections with a real absolute connect and SQL deadline."""
from __future__ import annotations
from contextlib import contextmanager
import ipaddress
import math
import re
import select
import time
import psycopg
from psycopg import pq, waiting
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.errors import _WaitTimeout
from evolvmem.library_startup import FileSecretResolver


def _remaining(deadline):
    if type(deadline) not in (float, int) or not math.isfinite(deadline):
        raise ValueError("invalid_pg_deadline")
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("experience_recall_deadline_exceeded")
    return seconds


class DeadlineConnection(psycopg.Connection):
    """Never reuse: timeout cancels safely when supported and closes this session."""
    deadline: float

    def wait(self, gen, interval=.05, timeout=None):
        seconds = _remaining(self.deadline)
        reserve = min(.03, seconds / 5)
        limit = seconds - reserve
        if timeout is not None:
            limit = min(limit, timeout)
        try:
            result = waiting.wait(gen, self.pgconn.socket, interval=min(.05, limit), timeout=limit)
            _remaining(self.deadline)
            return result
        except _WaitTimeout:
            try:
                # Never fall back to blocking PQcancel on an older libpq.
                if psycopg.capabilities.has_cancel_safe() and not self.closed:
                    self.cancel_safe(timeout=max(.001, self.deadline-time.monotonic()))
            except Exception:
                pass
            finally:
                self.close()
            raise TimeoutError("experience_recall_deadline_exceeded") from None

    def execute(self, *args, **kwargs):
        try:
            _remaining(self.deadline)
            return super().execute(*args, **kwargs)
        except TimeoutError:
            self.close()
            raise


def connect_with_deadline(conninfo="", *, deadline, **kwargs):
    """No synchronous DNS, connect_timeout rounding, or abandoned connect threads."""
    _remaining(deadline)
    params = conninfo_to_dict(conninfo, **kwargs)
    params.pop("connect_timeout", None)
    host = params.setdefault("host", "127.0.0.1")
    if host == "localhost" and not params.get("hostaddr"):
        params["host"] = host = "127.0.0.1"
    if not host.startswith("/"):
        try:
            ipaddress.ip_address(params.get("hostaddr") or host)
        except ValueError:
            raise ValueError("numeric_pg_host_required") from None
    pgconn = None
    try:
        pgconn = pq.PGconn.connect_start(make_conninfo(**params).encode())
        while True:
            remaining = _remaining(deadline)
            status = pgconn.connect_poll()
            if status == pq.PollingStatus.OK:
                conn = DeadlineConnection(pgconn)
                conn.deadline = deadline
                conn.prepare_threshold = None
                return conn
            if status == pq.PollingStatus.FAILED:
                raise psycopg.OperationalError("pg_connection_failed")
            if status == pq.PollingStatus.ACTIVE:
                continue
            read = [pgconn.socket] if status == pq.PollingStatus.READING else []
            write = [pgconn.socket] if status == pq.PollingStatus.WRITING else []
            select.select(read, write, [], remaining)
    except BaseException:
        if pgconn is not None:
            pgconn.finish()
        raise


def _connection_parameters(settings, role):
    from .pg_experience_store import PgExperienceUnavailableError, load_pg_experience_settings
    cfg = settings or load_pg_experience_settings()
    if cfg.dsn:
        params = conninfo_to_dict(cfg.dsn)
        if cfg.schema:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cfg.schema):
                raise ValueError("invalid_pg_schema")
            params["options"] = f"-c search_path={cfg.schema},public"
        return cfg, params
    if cfg.control_config is None:
        raise PgExperienceUnavailableError("pg_experience_unconfigured")
    cc = cfg.control_config
    resolver = FileSecretResolver(cc.secret_files)
    reader = role == "reader"
    params = dict(host=cc.host, port=cc.port, dbname=cc.database,
        user=cc.reader_role if reader else cc.coordinator_role,
        password=resolver(cc.reader_password_ref if reader else cc.coordinator_password_ref))
    if cc.allow_loopback_disable_ssl and cc.host in ("127.0.0.1", "::1", "localhost"):
        params["sslmode"] = "disable"
    return cfg, params


@contextmanager
def open_experience_deadline(settings=None, *, role="coordinator", deadline):
    from .pg_experience_store import PgExperienceUnavailableError
    conn = None
    try:
        cfg, params = _connection_parameters(settings, role)
        conn = connect_with_deadline(deadline=deadline, **params)
        remaining_ms = max(1, int(_remaining(deadline) * 1000))
        conn.execute("SELECT set_config('statement_timeout',%s,false), set_config('lock_timeout',%s,false)",
            (str(remaining_ms), str(min(100, remaining_ms))))
        yield conn
        conn.commit()
    except (TimeoutError, ValueError, PgExperienceUnavailableError):
        raise
    except psycopg.Error:
        raise PgExperienceUnavailableError("pg_experience_unavailable") from None
    finally:
        if conn is not None:
            conn.close()

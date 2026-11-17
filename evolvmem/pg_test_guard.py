"""Hard safety guard preventing test scripts and pytest from touching production PostgreSQL."""
from __future__ import annotations

import os
import re
from typing import Iterable, Sequence
from urllib.parse import urlparse

PROD_PG_PORT = 55432
PROD_PG_VOLUME = "evolvmem_pgdata"
PROD_PG_CONTAINER = "evolvmem-pg-prod"
PINNED_PGVECTOR_IMAGE = (
    "docker.m.daocloud.io/pgvector/pgvector@"
    "sha256:7ae6051efd0e60444282c27c7e141af07f322ce033300e727a49c3dd11075e38"
)

_LIBPQ_PORT_RE = re.compile(r"(?:^|\s)port\s*=\s*(\d+)(?:\s|$)")
_HOST_PORT_MAPPING_RE = re.compile(r"(?:^|[:=\s])55432(?:[:/\s]|$)")


class ProductionPostgresGuardError(RuntimeError):
    """Raised when a test or drill attempts to target production PostgreSQL."""


def _extract_dsn_port(dsn: str) -> int | None:
    text = dsn.strip()
    if text.startswith(("postgres://", "postgresql://")):
        parsed = urlparse(text)
        if parsed.port is not None:
            return int(parsed.port)
    m = _LIBPQ_PORT_RE.search(text)
    if m:
        return int(m.group(1))
    return None


def assert_safe_test_pg_target(
    *,
    port: int | str | None = None,
    dsn: str | None = None,
    volumes: Iterable[str] = (),
    container_name: str | None = None,
    argv: Sequence[str] = (),
) -> None:
    """Abort immediately if any parameter references production PG port, volume, or container."""
    if port is not None and str(port).strip() != "":
        try:
            parsed_port = int(str(port).strip())
        except ValueError:
            parsed_port = -1
        if parsed_port == PROD_PG_PORT or str(PROD_PG_PORT) in str(port):
            raise ProductionPostgresGuardError(
                f"Refusing test/drill operation targeting production PG port {PROD_PG_PORT}"
            )

    if dsn:
        dsn_port = _extract_dsn_port(dsn)
        if dsn_port == PROD_PG_PORT or _HOST_PORT_MAPPING_RE.search(dsn):
            raise ProductionPostgresGuardError(
                f"Refusing test/drill DSN targeting production PG port {PROD_PG_PORT}"
            )
        if PROD_PG_VOLUME in dsn or PROD_PG_CONTAINER in dsn:
            raise ProductionPostgresGuardError(
                f"Refusing test/drill DSN referencing {PROD_PG_VOLUME} or {PROD_PG_CONTAINER}"
            )

    if container_name and PROD_PG_CONTAINER in container_name:
        raise ProductionPostgresGuardError(
            f"Refusing test/drill operation on production container {PROD_PG_CONTAINER}"
        )

    for vol in volumes:
        if PROD_PG_VOLUME in str(vol):
            raise ProductionPostgresGuardError(
                f"Refusing test/drill operation mounting production volume {PROD_PG_VOLUME}"
            )

    joined_argv = " ".join(str(a) for a in argv)
    if PROD_PG_VOLUME in joined_argv:
        raise ProductionPostgresGuardError(
            f"Refusing CLI invocation referencing production volume {PROD_PG_VOLUME}"
        )
    if PROD_PG_CONTAINER in joined_argv:
        raise ProductionPostgresGuardError(
            f"Refusing CLI invocation referencing production container {PROD_PG_CONTAINER}"
        )
    if _HOST_PORT_MAPPING_RE.search(joined_argv):
        raise ProductionPostgresGuardError(
            f"Refusing CLI invocation targeting production PG port {PROD_PG_PORT}"
        )

    env_port = os.environ.get("PGPORT", "").strip()
    if env_port == str(PROD_PG_PORT):
        raise ProductionPostgresGuardError(
            f"Refusing test execution with PGPORT={PROD_PG_PORT} in environment"
        )

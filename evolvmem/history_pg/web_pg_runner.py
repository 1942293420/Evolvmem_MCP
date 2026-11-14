"""Zero-SQLite HTTP runner serving Knowledge Console & History Console backed by PostgreSQL."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
from types import SimpleNamespace

from evolvmem.web_server import make_http_server


class NullLegacyWebService:
    """Lightweight Web service facade holding zero SQLite handles."""

    def __init__(self, data_dir: Path, *, host: str = "127.0.0.1", port: int = 9377) -> None:
        dd = Path(data_dir).expanduser()
        self.config = SimpleNamespace(
            data_dir=dd,
            adapter="web",
            host=host,
            port=port,
            db_path=dd / "__unused_pg_mode.db",
            context_vector_path=dd / "__unused.vec",
            lan_shared_vector_cache=False,
            embedding_http_url="",
            lan_mcp_client_config="",
        )
        self.store = None

    def legacy_facade(self) -> SimpleNamespace:
        return SimpleNamespace()

    def knowledge(self) -> SimpleNamespace:
        return SimpleNamespace(rules=SimpleNamespace(path=Path("/nonexistent-pg-mode-rules")))


def run_pg_web_server(*, host: str = "127.0.0.1", port: int = 9377, data_dir: Path | None = None) -> None:
    dd = Path(data_dir or "~/.claude/evolvmem").expanduser()
    service = NullLegacyWebService(dd, host=host, port=port)
    srv = make_http_server(service, host=host, port=port)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="EvolvMem Zero-SQLite PostgreSQL Web Runner")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9377)
    p.add_argument("--data-dir", type=Path, default=Path("~/.claude/evolvmem"))
    args = p.parse_args(argv)
    run_pg_web_server(host=args.host, port=args.port, data_dir=args.data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Explicit, idempotent private credential setup for the trusted LAN server."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets

from evolvmem.lan_config import LanSettings


@dataclass(frozen=True)
class ProvisionResult:
    created: bool
    config_path: Path
    credentials_dir: Path


@dataclass(frozen=True)
class _CreatedFile:
    path: Path
    device: int
    inode: int


_CREDENTIAL_FILES = (
    "jiangli-token", "kane-token", "jiangli-client.json",
    "jiangli-client-instructions.txt", "kane-client-instructions.txt",
)


def _path_exists(path: Path) -> bool:
    """Return true for a filesystem entry, including a dangling symlink."""
    return os.path.lexists(path)


def _existing_setup_complete(config_path: Path, credentials_dir: Path) -> bool:
    if not credentials_dir.is_dir() or not all(
        (credentials_dir / name).is_file() for name in _CREDENTIAL_FILES
    ):
        return False
    try:
        LanSettings.from_file(config_path)
    except ValueError:
        return False
    return True


def _write_private(path: Path, contents: str, created: list[_CreatedFile] | None = None) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    stat_result = os.fstat(descriptor)
    if created is not None:
        created.append(_CreatedFile(path, stat_result.st_dev, stat_result.st_ino))
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(contents)
    os.chmod(path, 0o600)


def provision(config_path: Path, credentials_dir: Path, data_dir: Path,
              owner_data_dir: Path, host: str = "0.0.0.0", port: int = 9378,
              client_host: str = "") -> ProvisionResult:
    """Create two tokens only when no server configuration exists already."""
    config_path, credentials_dir = Path(config_path).expanduser(), Path(credentials_dir).expanduser()
    if _path_exists(config_path):
        if _existing_setup_complete(config_path, credentials_dir):
            return ProvisionResult(False, config_path, credentials_dir)
        raise FileExistsError("existing server config has incomplete credentials; refusing to overwrite")
    if _path_exists(credentials_dir):
        raise FileExistsError("credentials directory already exists; refusing to overwrite credentials")
    if not client_host or client_host == "0.0.0.0" or any(char.isspace() for char in client_host):
        raise ValueError("client_host must be the LAN hostname or address clients use")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    credentials_dir.mkdir(parents=True, mode=0o700)
    os.chmod(credentials_dir, 0o700)
    created: list[_CreatedFile] = []
    tokens = {name: secrets.token_urlsafe(32) for name in ("jiangli", "kane")}
    payload = {
        "data_dir": str(Path(data_dir).expanduser()),
        "owner_data_dir": str(Path(owner_data_dir).expanduser()),
        "token_hashes": {name: hashlib.sha256(token.encode()).hexdigest() for name, token in tokens.items()},
        "host": host, "port": port, "embedding_enabled": True,
    }
    try:
        for name, token in tokens.items():
            path = credentials_dir / f"{name}-token"
            _write_private(path, token + "\n", created)
        owner_client = {"url": f"http://127.0.0.1:{port}/owner/mcp", "token_file": str(credentials_dir / "jiangli-token")}
        path = credentials_dir / "jiangli-client.json"
        _write_private(path, json.dumps(owner_client, indent=2) + "\n", created)
        path = credentials_dir / "jiangli-client-instructions.txt"
        _write_private(path, (
            "Set config.json lan_mcp_client_config to this file:\n"
            f"{credentials_dir / 'jiangli-client.json'}\n"
            "Set lan_shared_vector_cache=true. Restart native MCP/hook processes after changing config.\n"
        ), created)
        path = credentials_dir / "kane-client-instructions.txt"
        _write_private(path, (
            "On Windows PowerShell, persist this user variable (keep this file private):\n"
            f"[Environment]::SetEnvironmentVariable('EVOLVMEM_KANE_TOKEN', '{tokens['kane']}', 'User')\n"
            f"codex mcp add evolvmem --url http://{client_host}:{port}/mcp --bearer-token-env-var EVOLVMEM_KANE_TOKEN\n"
            "Close and reopen PowerShell, then restart Codex before testing: a User environment write does not update this shell.\n"
            "Check memory_status before writing.\n"
        ), created)
        _write_private(config_path, json.dumps(payload, indent=2) + "\n", created)
    except BaseException:
        for created_file in reversed(created):
            try:
                current = created_file.path.lstat()
            except FileNotFoundError:
                continue
            if (current.st_dev, current.st_ino) == (created_file.device, created_file.inode):
                created_file.path.unlink()
        try:
            credentials_dir.rmdir()
        except OSError:
            pass
        raise
    return ProvisionResult(True, config_path, credentials_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create private credentials for the fixed jiangli/kane LAN MCP server.")
    parser.add_argument("--config", type=Path, required=True, help="new private server JSON path")
    parser.add_argument("--credentials-dir", type=Path, required=True, help="new 0700 private client instruction directory")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--owner-data-dir", type=Path, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9378)
    parser.add_argument("--client-host", required=True,
                        help="LAN hostname or address Kane's client uses; never 0.0.0.0")
    args = parser.parse_args()
    result = provision(args.config, args.credentials_dir, args.data_dir, args.owner_data_dir,
                       args.host, args.port, args.client_host)
    print("Created private LAN credentials." if result.created else "Existing private LAN credentials preserved.")
    print(f"Server config: {result.config_path}")
    print(f"Client instructions directory: {result.credentials_dir}")


if __name__ == "__main__":
    main()

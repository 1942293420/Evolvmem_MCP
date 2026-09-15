"""Explicit, idempotent private credential setup for the trusted LAN server."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets


@dataclass(frozen=True)
class ProvisionResult:
    created: bool
    config_path: Path
    credentials_dir: Path


def _write_private(path: Path, contents: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(contents)
    os.chmod(path, 0o600)


def provision(config_path: Path, credentials_dir: Path, data_dir: Path,
              owner_data_dir: Path, host: str = "0.0.0.0", port: int = 9378,
              client_host: str = "") -> ProvisionResult:
    """Create two tokens only when no server configuration exists already."""
    config_path, credentials_dir = Path(config_path).expanduser(), Path(credentials_dir).expanduser()
    if config_path.exists():
        return ProvisionResult(False, config_path, credentials_dir)
    if credentials_dir.exists():
        raise FileExistsError("credentials directory already exists; refusing to overwrite credentials")
    if not client_host or client_host == "0.0.0.0" or any(char.isspace() for char in client_host):
        raise ValueError("client_host must be the LAN hostname or address clients use")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    credentials_dir.mkdir(parents=True, mode=0o700)
    os.chmod(credentials_dir, 0o700)
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
            _write_private(path, token + "\n")
        owner_client = {"url": f"http://127.0.0.1:{port}/owner/mcp", "token_file": str(credentials_dir / "jiangli-token")}
        path = credentials_dir / "jiangli-client.json"
        _write_private(path, json.dumps(owner_client, indent=2) + "\n")
        path = credentials_dir / "jiangli-client-instructions.txt"
        _write_private(path, (
            "Set config.json lan_mcp_client_config to this file:\n"
            f"{credentials_dir / 'jiangli-client.json'}\n"
            "Set lan_shared_vector_cache=true. Restart native MCP/hook processes after changing config.\n"
        ))
        path = credentials_dir / "kane-client-instructions.txt"
        _write_private(path, (
            "On Windows PowerShell, persist this user variable (keep this file private):\n"
            f"[Environment]::SetEnvironmentVariable('EVOLVMEM_KANE_TOKEN', '{tokens['kane']}', 'User')\n"
            f"codex mcp add evolvmem --url http://{client_host}:{port}/mcp --bearer-token-env-var EVOLVMEM_KANE_TOKEN\n"
            "Close and reopen PowerShell, then restart Codex before testing: a User environment write does not update this shell.\n"
            "Check memory_status before writing.\n"
        ))
        _write_private(config_path, json.dumps(payload, indent=2) + "\n")
    except BaseException:
        for path in (config_path, credentials_dir / "kane-client-instructions.txt",
                     credentials_dir / "jiangli-client-instructions.txt",
                     credentials_dir / "jiangli-client.json", credentials_dir / "kane-token",
                     credentials_dir / "jiangli-token"):
            path.unlink(missing_ok=True)
        credentials_dir.rmdir()
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

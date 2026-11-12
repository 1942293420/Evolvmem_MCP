"""Validated identity settings for the trusted-LAN MCP runtime."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re


_USER_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z", re.ASCII)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class LanSettings:
    """Validated configuration surface for authenticated LAN MCP users."""

    data_dir: Path
    owner_data_dir: Path
    token_hashes: dict[str, str]
    host: str = "127.0.0.1"
    port: int = 9378
    embedding_enabled: bool = True
    owner_user: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir).expanduser())
        object.__setattr__(self, "owner_data_dir", Path(self.owner_data_dir).expanduser())
        if not isinstance(self.token_hashes, dict) or not self.token_hashes:
            raise ValueError("token_hashes must be a non-empty mapping")
        users = tuple(self.token_hashes.keys())
        if any(not isinstance(u, str) or u == "public" or not _USER_SLUG.fullmatch(u) for u in users):
            raise ValueError("token_hashes keys must be valid non-public user slugs")
        values = tuple(self.token_hashes[u] for u in users)
        if (
            any(not isinstance(value, str) or not _SHA256_HEX.fullmatch(value) for value in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError("token_hashes must contain distinct SHA-256 digests")
        resolved_owner = self.owner_user.strip() if isinstance(self.owner_user, str) else ""
        if not resolved_owner:
            env_owner = (os.environ.get("EVOLVMEM_CONSOLE_OWNER_ID") or "").strip()
            resolved_owner = env_owner if env_owner in self.token_hashes else users[0]
        if resolved_owner not in self.token_hashes:
            raise ValueError("owner_user must exist in token_hashes")
        object.__setattr__(self, "owner_user", resolved_owner)
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("host must be a non-empty string")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("port must be an integer between 1 and 65535")
        if type(self.embedding_enabled) is not bool:
            raise ValueError("embedding_enabled must be a boolean")

    @property
    def users(self) -> tuple[str, ...]:
        return tuple(self.token_hashes.keys())

    @classmethod
    def from_file(cls, path: Path) -> "LanSettings":
        """Load the documented JSON names without ever rendering token material."""
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("invalid LAN configuration") from exc
        if not isinstance(payload, dict):
            raise ValueError("invalid LAN configuration")
        try:
            return cls(
                data_dir=Path(payload["data_dir"]),
                owner_data_dir=Path(payload["owner_data_dir"]),
                token_hashes=payload["token_hashes"],
                host=payload.get("host", "127.0.0.1"),
                port=payload.get("port", 9378),
                embedding_enabled=payload.get("embedding_enabled", True),
                owner_user=payload.get("owner_user", ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid LAN configuration") from exc

    def authenticate(self, bearer_token: str) -> str | None:
        """Return the configured identity for a raw bearer token, if it matches."""
        if not isinstance(bearer_token, str):
            return None
        digest = hashlib.sha256(bearer_token.encode("utf-8")).hexdigest()
        matched: str | None = None
        for user, expected in self.token_hashes.items():
            if hmac.compare_digest(digest, expected):
                matched = user
        return matched

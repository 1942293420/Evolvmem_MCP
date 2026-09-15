"""Validated fixed-identity settings for the trusted-LAN MCP runtime."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
from pathlib import Path
import re


_IDENTITIES = ("jiangli", "kane")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class LanSettings:
    """Small, fixed configuration surface for two trusted LAN users."""

    data_dir: Path
    owner_data_dir: Path
    token_hashes: dict[str, str]
    host: str = "127.0.0.1"
    port: int = 9378
    embedding_enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(self.data_dir).expanduser())
        object.__setattr__(self, "owner_data_dir", Path(self.owner_data_dir).expanduser())
        if set(self.token_hashes) != set(_IDENTITIES):
            raise ValueError("token_hashes must contain exactly jiangli and kane")
        values = tuple(self.token_hashes[name] for name in _IDENTITIES)
        if (
            any(not isinstance(value, str) or not _SHA256_HEX.fullmatch(value) for value in values)
            or values[0] == values[1]
        ):
            raise ValueError("token_hashes must contain distinct SHA-256 digests")
        if not isinstance(self.host, str) or not self.host:
            raise ValueError("host must be a non-empty string")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("port must be an integer between 1 and 65535")
        if type(self.embedding_enabled) is not bool:
            raise ValueError("embedding_enabled must be a boolean")

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
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid LAN configuration") from exc

    def authenticate(self, bearer_token: str) -> str | None:
        """Return the fixed identity for a raw bearer token, if it matches."""
        if not isinstance(bearer_token, str):
            return None
        digest = hashlib.sha256(bearer_token.encode("utf-8")).hexdigest()
        matched: str | None = None
        for user in _IDENTITIES:
            if hmac.compare_digest(digest, self.token_hashes[user]):
                matched = user
        return matched

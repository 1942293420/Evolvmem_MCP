"""Comment-preserving, compare-and-swap editor for the Codex MCP config.

The editor updates only the target ``mcp_servers.<server>`` stanza of a Codex
TOML config. Every apply re-reads the current file, compares only the target
stanza hash (so concurrent unrelated edits survive), mutates the current
``tomlkit`` document (so comments, formatting, unknown fields, and other
servers are preserved), and writes through a durable same-directory atomic
replace.

Snapshots are structured and may contain secrets: they are serialized with
mode 0600, never printed or logged, and public results expose hashes only.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Callable, Mapping

import tomlkit
from tomlkit.exceptions import ParseError
from tomlkit.items import AbstractTable, Item

__all__ = [
    "DEFAULT_SERVER",
    "REQUIRED_CONTEXT_TOOLS",
    "CodexCliParseError",
    "CodexCliVerification",
    "CodexConfigApplyResult",
    "CodexConfigEditor",
    "CodexConfigError",
    "CodexConfigWriteError",
    "CodexMcpGetResult",
    "CodexMcpGetTransport",
    "CodexMcpSnapshot",
    "CodexSnapshotError",
    "CodexStanzaDriftError",
    "CodexToolPolicyError",
    "parse_mcp_get_json",
    "verify_cli_matches_stanza",
]

DEFAULT_SERVER = "evolvmem"

REQUIRED_CONTEXT_TOOLS = (
    "context_session_start",
    "context_search",
    "context_read",
    "context_status",
)

_PRIMARY_ADAPTER = "codex"
_PRIMARY_CONTEXT_MODE = "primary"
_LEGACY_CONTEXT_MODE = "legacy"
_PRIMARY_TOOLS_APPROVAL_MODE = "writes"


class CodexConfigError(Exception):
    """Base error for Codex config editing; messages never contain secrets."""


class CodexStanzaDriftError(CodexConfigError):
    """The target stanza changed since the snapshot; nothing was written."""


class CodexToolPolicyError(CodexConfigError):
    """The stanza tool policy would block required context tools."""


class CodexConfigWriteError(CodexConfigError):
    """The atomic replace failed; the original file was left in place."""


class CodexSnapshotError(CodexConfigError):
    """A snapshot file is unreadable, malformed, or tampered with."""


class CodexCliParseError(CodexConfigError):
    """``codex mcp get --json`` output is not the expected JSON shape."""


@dataclass(frozen=True, slots=True)
class CodexMcpSnapshot:
    """Private structured snapshot of one MCP stanza; may contain secrets."""

    stanza: Mapping[str, object]
    stanza_sha256: str
    source_file_sha256: str


@dataclass(frozen=True, slots=True)
class CodexConfigApplyResult:
    """Public apply result: hashes only, never stanza content."""

    before_stanza_sha256: str
    after_stanza_sha256: str


class CodexConfigEditor:
    """Edits one explicitly given Codex config; never guesses precedence."""

    def __init__(self, config_path: str | os.PathLike[str]) -> None:
        if config_path is None:
            raise TypeError("config_path is required")
        self._config_path = Path(config_path)

    def snapshot(self, server: str = DEFAULT_SERVER) -> CodexMcpSnapshot:
        """Capture the structured stanza plus stanza and source-file hashes."""
        raw = self._read_bytes()
        doc = self._parse(raw)
        stanza = self._require_stanza(doc, server)
        plain = _plain(stanza)
        return CodexMcpSnapshot(
            stanza=plain,
            stanza_sha256=_stanza_sha256(plain),
            source_file_sha256=hashlib.sha256(raw).hexdigest(),
        )

    def apply_primary(self, snapshot: CodexMcpSnapshot) -> CodexConfigApplyResult:
        """Switch the target stanza to Codex primary mode via stanza-hash CAS.

        Changes only ``env.EVOLVMEM_ADAPTER``, ``env.EVOLVMEM_CONTEXT_MODE``,
        and ``default_tools_approval_mode`` of the target stanza. Fails closed
        when the tool policy omits or blocks a required context tool.
        """

        def mutate(doc: Any, stanza: AbstractTable) -> None:
            _validate_tool_policy(stanza)
            env = _ensure_env_table(stanza)
            env["EVOLVMEM_ADAPTER"] = _PRIMARY_ADAPTER
            env["EVOLVMEM_CONTEXT_MODE"] = _PRIMARY_CONTEXT_MODE
            stanza["default_tools_approval_mode"] = _PRIMARY_TOOLS_APPROVAL_MODE

        return self._apply(snapshot.stanza_sha256, mutate)

    def apply_legacy(self, *, expected_stanza_sha256: str) -> CodexConfigApplyResult:
        """Fast operational rollback: set the target env mode explicitly to
        ``legacy``. This is not an installation-stanza restore."""

        def mutate(doc: Any, stanza: AbstractTable) -> None:
            env = _ensure_env_table(stanza)
            env["EVOLVMEM_CONTEXT_MODE"] = _LEGACY_CONTEXT_MODE

        return self._apply(expected_stanza_sha256, mutate)

    def restore_snapshot(
        self,
        snapshot: CodexMcpSnapshot,
        *,
        expected_stanza_sha256: str,
    ) -> CodexConfigApplyResult:
        """Exact installation-stanza restore from a structured snapshot.

        Distinct from :meth:`apply_legacy`: the whole stanza content is
        replaced by the snapshot, not merely switched to legacy mode.
        """
        rebuilt = tomlkit.item(dict(snapshot.stanza))
        if not isinstance(rebuilt, AbstractTable):
            raise CodexSnapshotError("snapshot stanza is not a TOML table")

        def mutate(doc: Any, stanza: AbstractTable) -> None:
            doc["mcp_servers"][DEFAULT_SERVER] = rebuilt

        return self._apply(expected_stanza_sha256, mutate)

    def save_snapshot(
        self, snapshot: CodexMcpSnapshot, path: str | os.PathLike[str]
    ) -> None:
        """Serialize a snapshot owner-only (0600); it may contain secrets."""
        if _stanza_sha256(snapshot.stanza) != snapshot.stanza_sha256:
            raise CodexSnapshotError("snapshot stanza does not match its hash")
        payload = json.dumps(
            {
                "source_file_sha256": snapshot.source_file_sha256,
                "stanza": snapshot.stanza,
                "stanza_sha256": snapshot.stanza_sha256,
            },
            indent=2,
            sort_keys=True,
        )
        self._write_private_file(Path(path), (payload + "\n").encode("utf-8"))

    @staticmethod
    def load_snapshot(path: str | os.PathLike[str]) -> CodexMcpSnapshot:
        """Load a snapshot file, verifying its recorded stanza hash."""
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            raise CodexSnapshotError("snapshot file is not readable") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexSnapshotError("snapshot file is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise CodexSnapshotError("snapshot file is malformed")
        stanza = payload.get("stanza")
        stanza_hash = payload.get("stanza_sha256")
        source_hash = payload.get("source_file_sha256")
        if (
            not isinstance(stanza, dict)
            or not _is_sha256(stanza_hash)
            or not _is_sha256(source_hash)
        ):
            raise CodexSnapshotError("snapshot file is malformed")
        if _stanza_sha256(stanza) != stanza_hash:
            raise CodexSnapshotError("snapshot stanza does not match its hash")
        return CodexMcpSnapshot(
            stanza=stanza,
            stanza_sha256=stanza_hash,
            source_file_sha256=source_hash,
        )

    def _apply(
        self,
        expected_stanza_sha256: str,
        mutate: Callable[[Any, AbstractTable], None],
    ) -> CodexConfigApplyResult:
        """Re-read, compare only the target stanza hash, mutate, replace."""
        doc = self._parse(self._read_bytes())
        stanza = self._require_stanza(doc, DEFAULT_SERVER)
        before = _stanza_sha256(_plain(stanza))
        if before != expected_stanza_sha256:
            raise CodexStanzaDriftError(
                "target MCP stanza changed since the snapshot; no write performed"
            )
        mutate(doc, stanza)
        after = _stanza_sha256(_plain(self._require_stanza(doc, DEFAULT_SERVER)))
        self._atomic_replace(tomlkit.dumps(doc), after)
        return CodexConfigApplyResult(
            before_stanza_sha256=before,
            after_stanza_sha256=after,
        )

    def _read_bytes(self) -> bytes:
        try:
            return self._config_path.read_bytes()
        except FileNotFoundError:
            raise CodexConfigError("Codex config file does not exist") from None
        except OSError as exc:
            raise CodexConfigError("Codex config file is not readable") from exc

    @staticmethod
    def _parse(raw: bytes) -> Any:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise CodexConfigError("Codex config is not valid UTF-8") from None
        try:
            return tomlkit.parse(text)
        except ParseError:
            raise CodexConfigError("Codex config is not parseable TOML") from None

    @staticmethod
    def _require_stanza(doc: Any, server: str) -> AbstractTable:
        servers = doc.get("mcp_servers")
        if not isinstance(servers, AbstractTable):
            raise CodexConfigError("Codex config has no [mcp_servers] table")
        stanza = servers.get(server)
        if not isinstance(stanza, AbstractTable):
            raise CodexConfigError(
                f"Codex config has no [mcp_servers.{server}] stanza"
            )
        return stanza

    def _atomic_replace(self, content: str, expected_after_sha256: str) -> None:
        """Unique same-dir temp, preserve owner/mode, fsync, replace, verify."""
        path = self._config_path
        tmp_name: str | None = None
        try:
            original_stat = path.stat()
            fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
            )
            os.fchmod(fd, stat.S_IMODE(original_stat.st_mode))
            with os.fdopen(fd, "wb") as handle:
                handle.write(content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            _preserve_owner(tmp_name, original_stat)
            os.replace(tmp_name, path)
            _fsync_directory(path.parent)
        except BaseException as exc:
            if tmp_name is not None:
                _remove_temp(tmp_name)
            raise CodexConfigWriteError("atomic config replace failed") from exc
        self._verify_written(expected_after_sha256)

    def _verify_written(self, expected_after_sha256: str) -> None:
        try:
            doc = self._parse(self._read_bytes())
            stanza = self._require_stanza(doc, DEFAULT_SERVER)
        except CodexConfigError as exc:
            raise CodexConfigWriteError(
                "post-write verification failed to reparse the config"
            ) from exc
        if _stanza_sha256(_plain(stanza)) != expected_after_sha256:
            raise CodexConfigWriteError(
                "post-write verification found an unexpected target stanza"
            )

    @staticmethod
    def _write_private_file(path: Path, data: bytes) -> None:
        tmp_name: str | None = None
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
            )
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            _fsync_directory(path.parent)
        except BaseException as exc:
            if tmp_name is not None:
                _remove_temp(tmp_name)
            raise CodexSnapshotError("could not write the snapshot file") from exc


def _plain(value: Any) -> Any:
    """Convert tomlkit items to JSON-safe plain Python values."""
    if isinstance(value, Item):
        value = value.unwrap()
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (bool, str, int, float)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    raise CodexConfigError(
        f"unsupported value type in MCP stanza: {type(value).__name__}"
    )


def _stanza_sha256(plain_stanza: Mapping[str, object]) -> str:
    blob = json.dumps(
        plain_stanza, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _validate_tool_policy(stanza: AbstractTable) -> None:
    """Primary requires every context tool to stay invocable."""
    enabled = _optional_str_list(stanza.get("enabled_tools"), "enabled_tools")
    if enabled is not None:
        missing = [tool for tool in REQUIRED_CONTEXT_TOOLS if tool not in enabled]
        if missing:
            raise CodexToolPolicyError(
                "enabled_tools does not allow required context tools: "
                + ", ".join(missing)
            )
    disabled = _optional_str_list(stanza.get("disabled_tools"), "disabled_tools")
    if disabled is not None:
        blocked = [tool for tool in REQUIRED_CONTEXT_TOOLS if tool in disabled]
        if blocked:
            raise CodexToolPolicyError(
                "disabled_tools blocks required context tools: " + ", ".join(blocked)
            )


def _optional_str_list(value: Any, field: str) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, Item):
        value = value.unwrap()
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise CodexToolPolicyError(f"{field} must be a list of tool names")
    return value


def _ensure_env_table(stanza: AbstractTable) -> AbstractTable:
    env = stanza.get("env")
    if env is None:
        env = tomlkit.table()
        stanza["env"] = env
        return env
    if not isinstance(env, AbstractTable):
        raise CodexConfigError("MCP server env is not a table")
    return env


def _preserve_owner(tmp_name: str, original_stat: os.stat_result) -> None:
    try:
        os.chown(tmp_name, original_stat.st_uid, original_stat.st_gid)
    except PermissionError:
        current = os.stat(tmp_name)
        if (current.st_uid, current.st_gid) != (
            original_stat.st_uid,
            original_stat.st_gid,
        ):
            raise


def _fsync_directory(directory: Path) -> None:
    dir_fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _remove_temp(tmp_name: str) -> None:
    try:
        os.unlink(tmp_name)
    except FileNotFoundError:
        pass


@dataclass(frozen=True, slots=True)
class CodexMcpGetTransport:
    """Transport block of ``codex mcp get --json``; None means not emitted."""

    type: str | None
    command: str | None
    args: tuple[str, ...] | None
    env: Mapping[str, str] | None
    cwd: str | None


@dataclass(frozen=True, slots=True)
class CodexMcpGetResult:
    """Parsed ``codex mcp get <server> --json`` output (CLI 0.147 shape).

    CLI 0.147 does not echo ``default_tools_approval_mode``; that field is
    verified by reparsing the TOML stanza instead.
    """

    name: str | None
    enabled: bool | None
    transport: CodexMcpGetTransport | None
    enabled_tools: tuple[str, ...] | None
    disabled_tools: tuple[str, ...] | None
    startup_timeout_sec: float | None
    tool_timeout_sec: float | None


@dataclass(frozen=True, slots=True)
class CodexCliVerification:
    """Field-name-only comparison outcome; never carries config values."""

    mismatches: tuple[str, ...]
    verified_fields: tuple[str, ...]
    skipped_fields: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.mismatches


def parse_mcp_get_json(text: str) -> CodexMcpGetResult:
    """Parse mocked or real ``codex mcp get --json`` stdout strictly."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise CodexCliParseError("mcp get output is not valid JSON") from None
    if not isinstance(payload, dict):
        raise CodexCliParseError("mcp get output is not a JSON object")
    return CodexMcpGetResult(
        name=_cli_optional_str(payload.get("name"), "name"),
        enabled=_cli_optional_bool(payload.get("enabled"), "enabled"),
        transport=_cli_transport(payload.get("transport")),
        enabled_tools=_cli_optional_str_tuple(
            payload.get("enabled_tools"), "enabled_tools"
        ),
        disabled_tools=_cli_optional_str_tuple(
            payload.get("disabled_tools"), "disabled_tools"
        ),
        startup_timeout_sec=_cli_optional_number(
            payload.get("startup_timeout_sec"), "startup_timeout_sec"
        ),
        tool_timeout_sec=_cli_optional_number(
            payload.get("tool_timeout_sec"), "tool_timeout_sec"
        ),
    )


def verify_cli_matches_stanza(
    cli: CodexMcpGetResult,
    stanza: Mapping[str, object],
    server: str = DEFAULT_SERVER,
) -> CodexCliVerification:
    """Compare CLI-emitted fields against the TOML stanza.

    A field is verified only when the CLI actually emitted a value for it;
    fields the CLI reported as unset are skipped (or flagged when the stanza
    sets them). ``default_tools_approval_mode`` is never part of this check
    because CLI 0.147 does not echo it.
    """
    mismatches: list[str] = []
    verified: list[str] = []
    skipped: list[str] = []

    def emitted(field: str, cli_value: Any, expected: Any) -> None:
        verified.append(field)
        if cli_value != expected:
            mismatches.append(field)

    def optional(field: str, cli_value: Any, present: bool, expected: Any) -> None:
        if cli_value is None:
            if present:
                mismatches.append(field)
            else:
                skipped.append(field)
        else:
            emitted(field, cli_value, expected)

    optional("name", cli.name, True, server)
    optional("enabled", cli.enabled, True, bool(stanza.get("enabled", True)))

    transport = cli.transport
    if transport is None:
        mismatches.append("transport")
    else:
        optional("transport.type", transport.type, True, "stdio")
        optional(
            "transport.command",
            transport.command,
            stanza.get("command") is not None,
            stanza.get("command"),
        )
        optional(
            "transport.args",
            transport.args,
            bool(stanza.get("args")),
            tuple(stanza.get("args") or ()),
        )
        stanza_env = stanza.get("env")
        optional(
            "transport.env",
            transport.env,
            bool(stanza_env),
            dict(stanza_env) if isinstance(stanza_env, Mapping) else {},
        )
        optional(
            "transport.cwd",
            transport.cwd,
            stanza.get("cwd") is not None,
            stanza.get("cwd"),
        )

    for field, cli_value in (
        ("enabled_tools", cli.enabled_tools),
        ("disabled_tools", cli.disabled_tools),
    ):
        stanza_value = stanza.get(field)
        optional(
            field,
            cli_value,
            bool(stanza_value),
            tuple(stanza_value) if isinstance(stanza_value, (list, tuple)) else (),
        )

    for field, cli_value in (
        ("startup_timeout_sec", cli.startup_timeout_sec),
        ("tool_timeout_sec", cli.tool_timeout_sec),
    ):
        stanza_value = stanza.get(field)
        if cli_value is None:
            if stanza_value is not None:
                mismatches.append(field)
            else:
                skipped.append(field)
        else:
            emitted(
                field,
                cli_value,
                float(stanza_value) if stanza_value is not None else None,
            )

    return CodexCliVerification(
        mismatches=tuple(mismatches),
        verified_fields=tuple(verified),
        skipped_fields=tuple(skipped),
    )


def _cli_optional_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CodexCliParseError(f"mcp get field {field} is not a string")
    return value


def _cli_optional_bool(value: Any, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise CodexCliParseError(f"mcp get field {field} is not a boolean")
    return value


def _cli_optional_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CodexCliParseError(f"mcp get field {field} is not a number")
    return float(value)


def _cli_optional_str_tuple(value: Any, field: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise CodexCliParseError(f"mcp get field {field} is not a string list")
    return tuple(value)


def _cli_transport(value: Any) -> CodexMcpGetTransport | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise CodexCliParseError("mcp get transport is not an object")
    env = value.get("env")
    if env is not None:
        if not isinstance(env, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in env.items()
        ):
            raise CodexCliParseError("mcp get transport env is not a string map")
        env = dict(env)
    return CodexMcpGetTransport(
        type=_cli_optional_str(value.get("type"), "transport.type"),
        command=_cli_optional_str(value.get("command"), "transport.command"),
        args=_cli_optional_str_tuple(value.get("args"), "transport.args"),
        env=env,
        cwd=_cli_optional_str(value.get("cwd"), "transport.cwd"),
    )

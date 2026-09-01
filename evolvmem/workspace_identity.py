"""Private workspace identity backed by an owner-only local HMAC key.

A workspace is identified only by an HMAC-SHA-256 fingerprint computed with
an owner-only (0600) 32-byte local key. Git workspaces share one identity
across all worktrees of a repository because the HMAC input is the resolved
git common directory; non-Git workspaces use the resolved directory itself.
The raw path is canonicalized, framed with its kind, HMACed, and then
discarded — only the fingerprint crosses the boundary.

The provider never bootstraps silently: ``resolve``/``digest_private`` fail
closed with a stable, content-free error code when the key is missing, has
unsafe permissions, or no longer matches the established fingerprint. Only
the explicitly approved maintenance action calls ``bootstrap_key``. Public
surfaces (reprs, statuses, errors) carry codes and digests only — never an
absolute path, Git remote, or key material.
"""

from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat


_WORKSPACE_IDENTITY_ERROR_CODES = frozenset(
    {
        "workspace_key_missing",
        "workspace_key_unsafe_permissions",
        "workspace_key_changed",
        "invalid_hmac_domain",
        "invalid_workspace_path",
    }
)


class WorkspaceIdentityError(RuntimeError):
    """Typed identity failure; ``str(error)`` is the stable code, nothing more."""

    def __init__(self, code: str) -> None:
        if code not in _WORKSPACE_IDENTITY_ERROR_CODES:
            raise ValueError(f"unknown workspace identity error code: {code!r}")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class WorkspaceKeyStatus:
    """Public key state. ``fingerprint`` is the SHA-256 of the key, set only
    when the key is established, owner-only, and matches expectations."""

    state: str  # missing | ready | unsafe_permissions | changed
    fingerprint: str = ""


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    """Resolved workspace identity: HMAC fingerprint plus workspace kind."""

    fingerprint: str  # "hmac-sha256:<64 hex>"
    kind: str  # git | non_git


_DOMAIN_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}")
_FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}")
_KEY_BYTES = 32
_GITDIR_MARKER_MAX_BYTES = 4096


class WorkspaceIdentityProvider:
    """Computes private workspace fingerprints from a transient path.

    Holds the key path internally but never exposes it: ``repr`` is redacted
    and every error is a stable code from ``WorkspaceIdentityError``.
    """

    WORKSPACE_DOMAIN = "workspace.identity.v1"

    def __init__(self, key_path: Path, expected_key_fingerprint: str = "") -> None:
        self._key_path = Path(key_path)
        if expected_key_fingerprint and not _FINGERPRINT_PATTERN.fullmatch(
            expected_key_fingerprint
        ):
            raise WorkspaceIdentityError("workspace_key_changed")
        self._expected_key_fingerprint = expected_key_fingerprint

    def __repr__(self) -> str:
        return "WorkspaceIdentityProvider(key_path=<redacted>)"

    def bootstrap_key(self) -> WorkspaceKeyStatus:
        """Explicitly create the owner-only key; never implicit, never overwriting."""
        self._key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return self.status()
        key = secrets.token_bytes(32)
        try:
            os.write(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)
        return self.status()

    def status(self) -> WorkspaceKeyStatus:
        """Report key state; validates owner, mode, length, and fingerprint."""
        state, key = self._inspect_key()
        fingerprint = ""
        if state == "ready":
            fingerprint = hashlib.sha256(key).hexdigest()
        return WorkspaceKeyStatus(state=state, fingerprint=fingerprint)

    def resolve(self, workspace_path: str) -> WorkspaceIdentity:
        """Fingerprint a transient workspace path; calls status, never bootstraps."""
        status = self.status()
        if status.state != "ready":
            raise WorkspaceIdentityError(f"workspace_key_{status.state}")
        kind, canonical = self._canonical_identity_bytes(workspace_path)
        fingerprint = self.digest_private(self.WORKSPACE_DOMAIN, canonical)
        # `canonical` (the only place a path existed) is discarded here.
        return WorkspaceIdentity(fingerprint=fingerprint, kind=kind)

    def digest_private(self, domain: str, payload: bytes) -> str:
        if not _DOMAIN_PATTERN.fullmatch(domain):
            raise WorkspaceIdentityError("invalid_hmac_domain")
        key = self._load_established_owner_only_key()
        domain_bytes = domain.encode("ascii")
        framed = (
            len(domain_bytes).to_bytes(2, "big")
            + domain_bytes
            + len(payload).to_bytes(8, "big")
            + payload
        )
        digest = hmac.new(key, framed, hashlib.sha256).hexdigest()
        return f"hmac-sha256:{digest}"

    def _load_established_owner_only_key(self) -> bytes:
        state, key = self._inspect_key()
        if state != "ready":
            raise WorkspaceIdentityError(f"workspace_key_{state}")
        return key

    def _inspect_key(self) -> tuple[str, bytes]:
        """Return (state, key_bytes); key bytes are empty unless state is ready."""
        try:
            fd = os.open(self._key_path, os.O_RDONLY)
        except FileNotFoundError:
            return ("missing", b"")
        except OSError:
            return ("unsafe_permissions", b"")
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                return ("unsafe_permissions", b"")
            if metadata.st_uid != os.geteuid():
                return ("unsafe_permissions", b"")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                return ("unsafe_permissions", b"")
            key = os.read(fd, _KEY_BYTES + 1)
        except OSError:
            return ("unsafe_permissions", b"")
        finally:
            os.close(fd)
        if len(key) != _KEY_BYTES:
            return ("unsafe_permissions", b"")
        if (
            self._expected_key_fingerprint
            and hashlib.sha256(key).hexdigest() != self._expected_key_fingerprint
        ):
            return ("changed", b"")
        return ("ready", key)

    def _canonical_identity_bytes(self, workspace_path: str) -> tuple[str, bytes]:
        """Resolve the path to framed canonical identity bytes, then discard it.

        Git identity is the resolved git common directory; non-Git identity is
        the resolved directory. The kind is framed into the payload so a Git
        and a non-Git identity can never collide.
        """
        if not isinstance(workspace_path, str) or not workspace_path.strip():
            raise WorkspaceIdentityError("invalid_workspace_path")
        try:
            resolved = Path(workspace_path).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise WorkspaceIdentityError("invalid_workspace_path") from None
        if not resolved.is_dir():
            raise WorkspaceIdentityError("invalid_workspace_path")
        common_dir = self._git_common_dir(resolved)
        if common_dir is not None:
            return ("git", b"git\x00" + str(common_dir).encode("utf-8"))
        return ("non_git", b"dir\x00" + str(resolved).encode("utf-8"))

    def _git_common_dir(self, directory: Path) -> Path | None:
        """Walk upwards for a .git marker, returning the resolved common dir.

        A marker only counts when it resolves to a valid git common dir (one
        containing HEAD), mirroring git's own discovery: a stray or corrupt
        ``.git`` (e.g. an empty directory left by a failed clone) is ignored
        and discovery continues upward, so junk can never fabricate or merge
        an identity.
        """
        current = directory
        while True:
            candidate = self._candidate_common_dir(current / ".git")
            if candidate is not None:
                return candidate
            parent = current.parent
            if parent == current:
                return None
            current = parent

    def _candidate_common_dir(self, marker: Path) -> Path | None:
        try:
            if marker.is_dir():
                return self._valid_common_dir(marker)
            if marker.is_file():
                line = self._read_marker_line(marker)
                if not line.startswith("gitdir:"):
                    return None
                target = line[len("gitdir:") :].strip()
                if not target:
                    return None
                git_dir = Path(target)
                if not git_dir.is_absolute():
                    git_dir = marker.parent / git_dir
                resolved = git_dir.resolve(strict=True)
                if not resolved.is_dir():
                    return None
                return self._valid_common_dir(resolved)
        except (OSError, RuntimeError, ValueError):
            return None
        return None

    def _valid_common_dir(self, git_dir: Path) -> Path | None:
        """Resolve a gitdir to its common dir, requiring a HEAD to count."""
        try:
            common = git_dir
            commondir = git_dir / "commondir"
            if commondir.is_file():
                line = self._read_marker_line(commondir)
                if not line:
                    return None
                target = Path(line)
                if not target.is_absolute():
                    target = git_dir / target
                common = target.resolve(strict=True)
            if not common.is_dir():
                return None
            if not (common / "HEAD").is_file():
                return None
            return common
        except (OSError, RuntimeError, ValueError):
            return None

    @staticmethod
    def _read_marker_line(marker: Path) -> str:
        with marker.open("rb") as handle:
            raw = handle.read(_GITDIR_MARKER_MAX_BYTES + 1)
        if len(raw) > _GITDIR_MARKER_MAX_BYTES:
            return ""
        lines = raw.decode("utf-8").splitlines()
        return lines[0].strip() if lines else ""

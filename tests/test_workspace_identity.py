"""Workspace identity: explicit owner-only key lifecycle and private fingerprints."""

import hashlib
import os
import re
import stat

import pytest

from evolvmem.workspace_identity import WorkspaceIdentityError, WorkspaceIdentityProvider


def _make_key(tmp_path, name="workspace-hmac.key", data=b"k" * 32, mode=0o600):
    key_path = tmp_path / name
    key_path.write_bytes(data)
    os.chmod(key_path, mode)
    return key_path


def _make_git_repo(root):
    """Fake a main-worktree .git directory; no git binary required."""
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "config").write_text(
        '[remote "origin"]\n\turl = git@secret.example:private/repo.git\n',
        encoding="utf-8",
    )
    return git_dir


def _make_worktree(root, main_git_dir, name="wt1"):
    """Fake a linked worktree: .git file + gitdir metadata with commondir."""
    meta = main_git_dir / "worktrees" / name
    meta.mkdir(parents=True)
    (meta / "commondir").write_text("../..\n", encoding="utf-8")
    (root / ".git").write_text(f"gitdir: {meta}\n", encoding="utf-8")
    return meta


def test_private_digest_is_domain_separated_and_missing_key_fails_closed(tmp_path):
    key_path = tmp_path / "workspace-hmac.key"
    key_path.write_bytes(b"k" * 32)
    os.chmod(key_path, 0o600)
    provider = WorkspaceIdentityProvider(key_path=key_path)

    first = provider.digest_private("workspace.identity.v1", b"same-payload")
    second = provider.digest_private("continuity.repo.worktree.v1", b"same-payload")

    assert first.startswith("hmac-sha256:")
    assert second.startswith("hmac-sha256:")
    assert first != second
    missing = WorkspaceIdentityProvider(key_path=tmp_path / "missing.key")
    with pytest.raises(WorkspaceIdentityError, match="workspace_key_missing"):
        missing.resolve(str(tmp_path))
    assert not (tmp_path / "missing.key").exists()


def test_digest_is_deterministic_and_payload_sensitive(tmp_path):
    provider = WorkspaceIdentityProvider(key_path=_make_key(tmp_path))

    first = provider.digest_private("workspace.identity.v1", b"payload-a")
    again = provider.digest_private("workspace.identity.v1", b"payload-a")
    other = provider.digest_private("workspace.identity.v1", b"payload-b")

    assert first == again
    assert first != other
    assert re.fullmatch(r"hmac-sha256:[0-9a-f]{64}", first)


@pytest.mark.parametrize("domain", ["", "x", "Up.per", "has space", "a" * 65])
def test_invalid_hmac_domain_is_rejected(tmp_path, domain):
    provider = WorkspaceIdentityProvider(key_path=_make_key(tmp_path))
    with pytest.raises(WorkspaceIdentityError, match="invalid_hmac_domain"):
        provider.digest_private(domain, b"payload")


def test_bootstrap_is_explicit_owner_only_and_idempotent(tmp_path):
    key_path = tmp_path / "keys" / "workspace-hmac.key"
    provider = WorkspaceIdentityProvider(key_path=key_path)

    assert provider.status().state == "missing"
    with pytest.raises(WorkspaceIdentityError, match="workspace_key_missing"):
        provider.digest_private("workspace.identity.v1", b"x")
    assert not key_path.exists()

    status = provider.bootstrap_key()

    assert status.state == "ready"
    assert re.fullmatch(r"[0-9a-f]{64}", status.fingerprint)
    assert len(key_path.read_bytes()) == 32
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_path.parent.stat().st_mode) == 0o700

    again = provider.bootstrap_key()
    assert again.state == "ready"
    assert again.fingerprint == status.fingerprint

    workspace = tmp_path / "ws"
    workspace.mkdir()
    identity = provider.resolve(str(workspace))
    assert identity.kind == "non_git"
    assert identity.fingerprint.startswith("hmac-sha256:")


def test_bootstrap_never_overwrites_an_existing_key(tmp_path):
    key_path = _make_key(tmp_path)
    provider = WorkspaceIdentityProvider(key_path=key_path)

    status = provider.bootstrap_key()

    assert status.state == "ready"
    assert key_path.read_bytes() == b"k" * 32


def test_unsafe_permissions_fail_closed(tmp_path):
    key_path = _make_key(tmp_path, mode=0o644)
    provider = WorkspaceIdentityProvider(key_path=key_path)

    status = provider.status()

    assert status.state == "unsafe_permissions"
    assert status.fingerprint == ""
    with pytest.raises(WorkspaceIdentityError, match="workspace_key_unsafe_permissions"):
        provider.resolve(str(tmp_path))
    with pytest.raises(WorkspaceIdentityError, match="workspace_key_unsafe_permissions"):
        provider.digest_private("workspace.identity.v1", b"x")


def test_wrong_length_key_is_not_established(tmp_path):
    provider = WorkspaceIdentityProvider(
        key_path=_make_key(tmp_path, data=b"short" * 3),
    )
    assert provider.status().state == "unsafe_permissions"
    with pytest.raises(WorkspaceIdentityError, match="workspace_key_unsafe_permissions"):
        provider.resolve(str(tmp_path))


def test_changed_key_fingerprint_refuses_and_matching_key_is_ready(tmp_path):
    key_path = _make_key(tmp_path)
    expected = hashlib.sha256(b"z" * 32).hexdigest()
    provider = WorkspaceIdentityProvider(
        key_path=key_path,
        expected_key_fingerprint=expected,
    )

    status = provider.status()

    assert status.state == "changed"
    assert status.fingerprint == ""
    with pytest.raises(WorkspaceIdentityError, match="workspace_key_changed"):
        provider.resolve(str(tmp_path))
    with pytest.raises(WorkspaceIdentityError, match="workspace_key_changed"):
        provider.digest_private("workspace.identity.v1", b"x")

    matching = WorkspaceIdentityProvider(
        key_path=key_path,
        expected_key_fingerprint=hashlib.sha256(b"k" * 32).hexdigest(),
    )
    assert matching.status().state == "ready"


def test_git_common_dir_identity_is_stable_across_worktrees(tmp_path):
    provider = WorkspaceIdentityProvider(key_path=_make_key(tmp_path))
    main = tmp_path / "main"
    main.mkdir()
    git_dir = _make_git_repo(main)
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _make_worktree(worktree, git_dir)
    nested = main / "src" / "pkg"
    nested.mkdir(parents=True)

    base = provider.resolve(str(main))

    assert base.kind == "git"
    assert provider.resolve(str(worktree)).fingerprint == base.fingerprint
    assert provider.resolve(str(worktree)).kind == "git"
    assert provider.resolve(str(nested)).fingerprint == base.fingerprint


def test_distinct_non_git_directories_have_distinct_fingerprints(tmp_path):
    provider = WorkspaceIdentityProvider(key_path=_make_key(tmp_path))
    first_dir = tmp_path / "alpha"
    second_dir = tmp_path / "beta"
    first_dir.mkdir()
    second_dir.mkdir()

    first = provider.resolve(str(first_dir))
    second = provider.resolve(str(second_dir))

    assert first.kind == "non_git"
    assert second.kind == "non_git"
    assert first.fingerprint != second.fingerprint


def test_moved_directory_produces_a_new_fingerprint(tmp_path):
    provider = WorkspaceIdentityProvider(key_path=_make_key(tmp_path))
    original = tmp_path / "original"
    original.mkdir()
    before = provider.resolve(str(original))

    moved = tmp_path / "moved"
    original.rename(moved)
    after = provider.resolve(str(moved))

    assert after.fingerprint != before.fingerprint


def test_invalid_workspace_path_is_rejected(tmp_path):
    provider = WorkspaceIdentityProvider(key_path=_make_key(tmp_path))
    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")

    for bad in ("", str(tmp_path / "does-not-exist"), str(a_file)):
        with pytest.raises(WorkspaceIdentityError, match="invalid_workspace_path"):
            provider.resolve(bad)


def test_no_path_or_remote_in_repr_result_or_public_status(tmp_path):
    provider = WorkspaceIdentityProvider(key_path=_make_key(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_repo(repo)

    identity = provider.resolve(str(repo))
    status = provider.status()
    blob = repr(provider) + repr(status) + repr(identity) + str(identity)

    assert str(tmp_path) not in blob
    assert "secret.example" not in blob
    assert str(tmp_path / "workspace-hmac.key") not in blob

    with pytest.raises(WorkspaceIdentityError) as excinfo:
        provider.resolve(str(tmp_path / "missing-dir"))
    assert str(tmp_path) not in str(excinfo.value)
    assert str(excinfo.value) == "invalid_workspace_path"

    missing = WorkspaceIdentityProvider(key_path=tmp_path / "missing.key")
    with pytest.raises(WorkspaceIdentityError) as excinfo:
        missing.digest_private("workspace.identity.v1", b"x")
    assert str(excinfo.value) == "workspace_key_missing"

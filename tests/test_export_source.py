"""The public source export preserves code and excludes local runtime files."""

from pathlib import Path
import zipfile

import pytest


def test_export_preserves_public_resources_excludes_runtime_and_never_overwrites(tmp_path):
    from scripts.export_source import export_source

    source = tmp_path / "source"
    public = {
        "README.md": b"Public setup instructions\n",
        "README.txt": b"Plain text setup instructions\n",
        ".gitignore": b"dist/\n.env\n*.db\n",
        "pyproject.toml": b'[project]\nname = "sample"\n',
        "evolvmem/__init__.py": b"# Uncommitted source is included.\n",
        "evolvmem/web_static/index.html": b'<img src="designs/demo.png">',
        "evolvmem/web_static/designs/demo.png": b"synthetic-image-bytes",
        "tests/fixtures/sample.json": b'{"example_token": "synthetic-test-value"}',
        "examples/config.example.json": b'{"api_key": "replace-me"}',
        "LICENSES/ThirdParty.txt": b"Third party notice\n",
        "THIRD_PARTY_NOTICES.md": b"Third party acknowledgements\n",
        "dsh/cordis.patch.yml": b"# Public configuration example\n",
        "scripts/helper.py": b"print('example')\n",
        "docs/github-sharing.md": b"Sharing instructions\n",
        ".github/workflows/check.yml": b"name: check\n",
    }
    private = {
        ".git/config": b"private history metadata",
        "uv.lock": b"old lockfile",
        ".env": b"FAKE_SECRET=do-not-copy",
        "docs/superpowers/plan.md": b"private work notes",
        "docs/private.md": b"private document",
        "evolvmem/config.json": b'{"api_key":"fake-local-secret"}',
        "evolvmem/credentials.json": b'{"token":"fake-local-token"}',
        "evolvmem/data/memory.db": b"fake private database",
        "evolvmem/models/model.gguf": b"fake local model",
        "evolvmem/session.jsonl": b"fake private session",
        "evolvmem/runtime.log": b"fake private log",
        "evolvmem/web_static/index.html.bak-20260904": b"old screenshot data",
        "evolvmem/__pycache__/module.pyc": b"compiled cache",
        "tests/.venv/private.txt": b"local environment",
    }
    for name, content in {**public, **private}.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("fake external secret")
    (source / "evolvmem" / "linked.txt").symlink_to(outside / "private.txt")
    (source / "evolvmem" / "linked-directory").symlink_to(outside, target_is_directory=True)

    output, archive = export_source(source, tmp_path / "release")

    actual = {
        str(path.relative_to(output)): path.read_bytes()
        for path in output.rglob("*") if path.is_file()
    }
    assert actual == public
    assert not (output / ".git").exists()
    with zipfile.ZipFile(archive) as zipped:
        archived = {
            str(Path(name).relative_to(output.name)): zipped.read(name)
            for name in zipped.namelist()
        }
    assert archived == actual
    original_zip = archive.read_bytes()

    with pytest.raises(FileExistsError):
        export_source(source, output)
    assert archive.read_bytes() == original_zip
    assert (output / "README.md").read_bytes() == public["README.md"]

    reserved = tmp_path / "reserved.zip"
    reserved.write_bytes(b"existing archive")
    with pytest.raises(FileExistsError):
        export_source(source, tmp_path / "reserved")
    assert reserved.read_bytes() == b"existing archive"
    assert not (tmp_path / "reserved").exists()

    linked_source = tmp_path / "source-with-linked-docs"
    linked_source.mkdir()
    (linked_source / "README.md").write_bytes(public["README.md"])
    (outside / "github-sharing.md").write_text("fake private external notes")
    (linked_source / "docs").symlink_to(outside, target_is_directory=True)
    linked_output, _ = export_source(linked_source, tmp_path / "linked-docs-release")
    assert not (linked_output / "docs").exists()

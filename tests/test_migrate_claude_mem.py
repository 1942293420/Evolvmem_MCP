"""Migration source selection uses temporary, read-only fixture databases."""

import sqlite3

import pytest

import migrate_claude_mem as migration


def _source_database(path, *, include_summary=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE embeddings (id INTEGER PRIMARY KEY);
            CREATE TABLE embedding_metadata (
                id INTEGER, key TEXT, string_value TEXT, int_value INTEGER
            );
        """)
        if include_summary:
            connection.execute("INSERT INTO embeddings VALUES (1)")
            connection.executemany(
                "INSERT INTO embedding_metadata VALUES (1, ?, ?, ?)",
                [
                    ("chroma:document", "Synthetic migration summary", None),
                    ("doc_type", "session_summary", None),
                    ("project", "sample-project", None),
                    ("created_at_epoch", None, 1234),
                ],
            )


def test_missing_source_is_rejected_without_creating_database(tmp_path, monkeypatch):
    source = tmp_path / "missing.sqlite3"
    monkeypatch.setattr(migration, "CHROMA_DB", source)

    with pytest.raises(FileNotFoundError):
        migration.extract_summaries()

    assert not source.exists()


def test_explicit_source_extracts_summaries_without_modifying_it(tmp_path):
    source = tmp_path / "source #1.sqlite3"
    _source_database(source, include_summary=True)
    original = source.read_bytes()

    assert migration.extract_summaries(source) == [{
        "chroma_id": 1,
        "doc": "Synthetic migration summary",
        "created_at_epoch": 1234,
        "project": "sample-project",
    }]
    assert source.read_bytes() == original


def test_cli_accepts_explicit_empty_source_without_creating_destination(
        tmp_path, monkeypatch, capsys):
    source = tmp_path / "source.sqlite3"
    _source_database(source)
    destination = tmp_path / "destination"
    monkeypatch.setenv("EVOLVMEM_DATA_DIR", str(destination))

    migration.main(["--source-db", str(source)])

    assert "无数据可迁移" in capsys.readouterr().out
    assert not destination.exists()

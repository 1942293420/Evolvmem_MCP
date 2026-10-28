"""Additive schema for capability memories and project archives."""
from evolvmem.config import Config
from evolvmem.context_store import ContextStore


def test_restructure_schema_is_additive_and_defaults_to_legacy(test_config):
    assert test_config.knowledge_structure_mode == "legacy"
    assert test_config.capability_vector_path.name == "capability_vectors.usearch"

    with ContextStore(test_config) as store:
        with store.transaction():
            store.create_schema_in_transaction()
        names = {
            row[0]
            for row in store._connection().execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    assert {
        "capability_categories",
        "capability_memories",
        "capability_memories_fts",
        "capability_category_suggestions",
        "project_archives",
        "project_events",
        "knowledge_restructure_state",
        "knowledge_restructure_map",
    } <= names
    assert Config(data_dir=test_config.data_dir).knowledge_structure_mode == "legacy"


def test_knowledge_structure_mode_round_trips_through_config_file(test_config):
    test_config.knowledge_structure_mode = "dual"
    test_config.save()

    loaded = Config.from_file(data_dir=test_config.data_dir)

    assert loaded.knowledge_structure_mode == "dual"

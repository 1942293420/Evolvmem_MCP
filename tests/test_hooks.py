"""hooks module tests."""

from evolvmem.hooks import get_session_start_block, get_stop_prompt
from evolvmem.memory_store import MemoryStore


class TestSessionStartHook:
    def test_empty_memories_returns_empty_string(self, test_config):
        result = get_session_start_block(config=test_config)
        assert result == ""

    def test_active_memories_formatted_in_block(self, test_config):
        with MemoryStore(test_config) as store:
            store.add(key="user:pref:language", value="Chinese", tags=["preference"])
            store.add(
                key="project:arch:db",
                value="PostgreSQL",
                tags=["architecture", "database"],
            )

        result = get_session_start_block(config=test_config)

        assert "Persistent Memory" in result
        assert "EvolvMem" in result
        assert "user:pref:language" in result
        assert "Chinese" in result
        assert "[preference]" in result
        assert "project:arch:db" in result
        assert "PostgreSQL" in result
        assert "[architecture,database]" in result


class TestStopHook:
    def test_stop_prompt_includes_conversation(self):
        prompt = get_stop_prompt("user: We decided to use Redis for caching\nassistant: OK, noted")

        assert "Redis" in prompt
        assert "Retention" in prompt or "extract" in prompt

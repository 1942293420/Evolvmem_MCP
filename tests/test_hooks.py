"""hooks 模块测试。"""

from hermes_memory.hooks import get_session_start_block, get_stop_prompt
from hermes_memory.memory_store import MemoryStore


class TestSessionStartHook:
    def test_empty_memories_returns_empty_string(self, test_config):
        result = get_session_start_block(config=test_config)
        assert result == ""

    def test_active_memories_formatted_in_block(self, test_config):
        with MemoryStore(test_config) as store:
            store.add(key="user:pref:language", value="中文", tags=["偏好"])
            store.add(
                key="project:arch:db",
                value="使用PostgreSQL",
                tags=["架构", "数据库"],
            )

        result = get_session_start_block(config=test_config)

        assert "持久记忆" in result
        assert "hermes-memory" in result
        assert "user:pref:language" in result
        assert "中文" in result
        assert "[偏好]" in result
        assert "project:arch:db" in result
        assert "使用PostgreSQL" in result
        assert "[架构,数据库]" in result


class TestStopHook:
    def test_stop_prompt_includes_conversation(self):
        messages = [
            {"role": "user", "content": "我们决定使用 Redis 做缓存"},
            {"role": "assistant", "content": "好的，已记录"},
        ]
        prompt = get_stop_prompt(messages)

        assert "Redis" in prompt
        assert "保留" in prompt or "提取" in prompt

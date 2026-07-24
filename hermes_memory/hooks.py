"""SessionStart and Stop Hook 集成——内存格式化和提取触发。"""

from hermes_memory.config import Config
from hermes_memory.memory_store import MemoryStore
from hermes_memory.auto_extractor import AutoExtractor


def get_session_start_block(config: Config | None = None) -> str:
    """读取所有 active 记忆并格式化为系统提示块。

    Args:
        config: 配置对象，为 None 时使用默认配置。

    Returns:
        格式化的系统提示字符串；如果没有 active 记忆则返回空字符串。
    """
    if config is None:
        config = Config.from_file()

    with MemoryStore(config) as store:
        memories = store.get_active()

    if not memories:
        return ""

    lines = [
        "## 持久记忆（来自 hermes-memory 插件）",
        "",
        "以下是从之前对话中提取的当前仍然有效的偏好、决策和约束。",
        "这些是持久化的事实，不是本次对话中的上下文。",
        "优先信任用户当前指令 > 当前代码与测试 > 以下记忆 > 历史记录。",
        "如果以下记忆与用户当前说法矛盾，以用户当前说法为准。",
        "",
    ]

    for m in memories:
        key = m["key"]
        value = m["value"]
        tags = m.get("tags", "")
        if tags:
            lines.append(f"- **{key}** [{tags}]: {value}")
        else:
            lines.append(f"- **{key}**: {value}")

    return "\n".join(lines)


def get_stop_prompt(messages_summary: str) -> str:
    """构建 Stop Hook 的提取提示词。

    Args:
        messages_summary: 对话摘要字符串（来自 hook 系统）。

    Returns:
        提取提示词字符串，可传给 Claude 进行分析。
    """
    extractor = AutoExtractor()
    return extractor.build_extraction_prompt(
        messages=[{"role": "user", "content": messages_summary}],
    )

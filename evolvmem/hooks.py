"""SessionStart and Stop Hook integration — memory formatting and extraction triggering."""

from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore
from evolvmem.auto_extractor import AutoExtractor


def get_session_start_block(config: Config | None = None) -> str:
    """Read all active memories and format as a system prompt block.

    Args:
        config: Configuration object, uses defaults when None.

    Returns:
        Formatted system prompt string; empty string if no active memories.
    """
    if config is None:
        config = Config.from_file()

    with MemoryStore(config) as store:
        memories = store.get_active()

    if not memories:
        return ""

    lines = [
        "## Persistent Memory (from EvolvMem plugin)",
        "",
        "The following are preferences, decisions, and constraints extracted from previous conversations "
        "that are still valid.",
        "These are persisted facts, not context from the current conversation.",
        "Trust priority: user's current instructions > current code and tests > the memories below > history.",
        "If any memory contradicts what the user is currently saying, follow the user's current statement.",
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
    """Build the extraction prompt for the Stop Hook.

    Args:
        messages_summary: Conversation summary string (from the hook system).

    Returns:
        Extraction prompt string, to be passed to Claude for analysis.
    """
    extractor = AutoExtractor()
    return extractor.build_extraction_prompt(
        messages=[{"role": "user", "content": messages_summary}],
    )

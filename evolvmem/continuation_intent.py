"""Continuation-intent detection: a pure function with no I/O and no state.

「继续原任务」类话语是控制意图而非检索查询：命中后 session_start 改走精确
continuity 查找，不过 FTS/HNSW。本模块只回答一个问题——这段文本是否表达
"续接上一任务"的控制意图。

判定流程（全部作用于标准化文本：折叠空白 + casefold）：

1. 反例优先，命中即 False：否定前缀（不要/别/不需要/don't/do not）、
   引号包裹（转述提及而非指令）、混合新目标标记（之外/改做/instead）。
2. 短语包含：文本包含任一完整意图短语即 True。

短语表与反例表为模块级常量：新增短语或反例必须同步
tests/test_continuation_intent.py。
"""

# 完整意图短语（标准化后的形态）。新增短语必须同步测试。
_CONTINUATION_PHRASES = frozenset(
    {
        "继续原任务",
        "继续之前的任务",
        "接着做",
        "从断点继续",
        "继续上次工作",
        "继续开发",
        "resume previous task",
        "continue previous task",
        "pick up where we left off",
    }
)

# 否定前缀：标准化文本以此开头即非续接指令。新增前缀必须同步测试。
_NEGATION_PREFIXES = ("不要", "别", "不需要", "don't", "do not")

# 引号出现即视为转述提及（引述原话），不是控制意图。只收双引号类字符；
# 不收撇号，以免误伤 "let's ..." 这类正常英文输入。
_QUOTE_CHARS = ("“", "”", "「", "」", "『", "』", "«", "»", '"')

# 混合新目标标记：短语之外又给了新目标，控制权不在续接。新增标记必须同步测试。
_MIXED_GOAL_MARKERS = ("之外", "改做", "instead")


def detect_continuation_intent(text: str) -> bool:
    """判断 text 是否为"续接上一任务"控制意图；纯函数，永不抛错。"""
    if not isinstance(text, str):
        return False
    normalized = " ".join(text.casefold().split())
    if not normalized:
        return False
    # 反例优先：否定前缀 / 引号转述 / 混合新目标，命中即 False
    if normalized.startswith(_NEGATION_PREFIXES):
        return False
    if any(char in normalized for char in _QUOTE_CHARS):
        return False
    if any(marker in normalized for marker in _MIXED_GOAL_MARKERS):
        return False
    return any(phrase in normalized for phrase in _CONTINUATION_PHRASES)

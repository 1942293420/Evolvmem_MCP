"""Continuation-intent phrase contracts: pure detection, no retrieval.

意图短语表与反例表是 ``evolvmem/continuation_intent.py`` 的模块级常量；
新增短语或反例必须同步本文件。
"""

import pytest

from evolvmem.continuation_intent import detect_continuation_intent


@pytest.mark.parametrize("text", [
    "继续原任务", "继续之前的任务", "接着做", "从断点继续", "继续上次工作",
    "resume previous task", "continue previous task", "pick up where we left off",
    "  继续开发  ",
])
def test_intent_phrases_hit(text):
    assert detect_continuation_intent(text) is True


@pytest.mark.parametrize("text", [
    "不要继续原任务",
    "文档里写着“继续原任务”四个字",
    "继续原任务之外，请改做 X",
    "帮我写个新功能",
    "",
])
def test_intent_negatives_miss(text):
    assert detect_continuation_intent(text) is False


# ---- 反例规则的逐条覆盖（模块常量的每条规则都要有测试） ----


@pytest.mark.parametrize("text", [
    "别继续原任务",
    "不需要继续之前的任务",
    "don't resume previous task",
    "do not continue previous task",
])
def test_negation_prefixes_miss(text):
    """否定前缀优先于短语命中。"""
    assert detect_continuation_intent(text) is False


@pytest.mark.parametrize("text", [
    '文档写着"继续原任务"',
    "「继续原任务」是历史原话",
    "他说 «continue previous task» 即可",
])
def test_quoted_mentions_miss(text):
    """引号包裹视为转述提及而非控制意图。"""
    assert detect_continuation_intent(text) is False


def test_mixed_goal_instead_misses():
    assert detect_continuation_intent("continue previous task instead of fixing") is False


def test_case_and_inner_whitespace_normalize():
    assert detect_continuation_intent("Resume   Previous   Task") is True
    assert detect_continuation_intent("CONTINUE PREVIOUS TASK") is True


def test_non_string_input_is_not_intent():
    assert detect_continuation_intent(None) is False
    assert detect_continuation_intent(42) is False

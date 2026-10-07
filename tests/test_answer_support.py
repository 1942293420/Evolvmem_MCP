"""Independent answer-support checks: a user quote must not license a wider answer.

Every line is synthetic. Each test names the production change it catches:
a real user quote with an answer that widened the scope, made an optional
route mandatory, or dropped a "generally/usually" limit must stay a
candidate, while a faithful extractive narrowing may replace the answer.
"""
import json

import pytest

from evolvmem import answer_support
from evolvmem.auto_extractor import CandidateMemory
from evolvmem.context_models import ContextMode
from evolvmem.learning_extraction import plan
from evolvmem.legacy_models import LegacyExtractionItem
from tests.test_web_server import _make_service


@pytest.fixture
def service(test_config):
    svc = _make_service(test_config, mode=ContextMode.SHADOW)
    svc.knowledge().save_project({'project': 'demo'})
    yield svc
    svc.close()


def candidate(value, *, question, answer, quote, basis='explicit', key='project:demo:constraint:cards',
              attribute='constraint', category='project_convention', **extra):
    """A fresh candidate stub mirroring the extractor's payload shape."""
    learning = {'category': category, 'basis': basis, 'question': question, 'answer': answer,
                'quote': quote, 'normalization': {'requirement': answer, 'acceptance': [], 'questions': []}}
    learning.update(extra)
    return CandidateMemory(key=key, value=value, attribute=attribute, confidence=.95, learning=learning)


def verdicts(*entries):
    """A reviewer reply as plain JSON, one entry per numbered candidate."""
    return json.dumps([dict(entry) for entry in entries], ensure_ascii=False)


def reviewed(*entries):
    return lambda _prompt: verdicts(*entries)


CARD_LINE = '设置入口在卡片本身，退款入口也在卡片上。'
SHORTCUT_LINE = '可以用快捷键或者其他办法区分这两个入口。'
REPAIR_LINE = '接口报错一般回服务站维修，具体情况要看错误码。'


def test_answer_that_expands_the_scope_of_a_real_quote_stays_a_candidate(service):
    """Catch: 用户只说卡片入口，答案却加上退款、删除、导出全部放在卡片。"""
    value = '退款、删除、导出均放在卡片，设置入口在卡片本身。'
    item = candidate(value, question='这些入口放在哪里？', answer=value, quote=CARD_LINE)
    llm = reviewed({'id': 1, 'verdict': 'review', 'reason': '答案增加了用户未说的入口范围',
                    'quote': CARD_LINE})
    answer_support.support([{'role': 'user', 'content': CARD_LINE}], [item], llm)
    assert item.learning['answer_support']['verdict'] == 'review'
    assert item.learning['answer_support']['original_answer'] == value
    messages = [{'role': 'user', 'content': CARD_LINE}]
    result = plan(service, LegacyExtractionItem(key=item.key, value=item.value, attribute=item.attribute,
                                                confidence=.95, learning=item.learning), messages)
    assert result['status'] == 'candidate'


def test_option_becoming_a_mandatory_rule_stays_a_candidate(service):
    """Catch: 用户说“可以用快捷键或者其他办法”，答案强行固定为必须快捷键。"""
    value = '必须使用快捷键区分入口，禁止其他区分办法。'
    item = candidate(value, question='入口如何区分？', answer=value, quote=SHORTCUT_LINE)
    llm = reviewed({'id': 1, 'verdict': 'review', 'reason': '把可选办法写成必须，删除了或者',
                    'quote': SHORTCUT_LINE})
    answer_support.support([{'role': 'user', 'content': SHORTCUT_LINE}], [item], llm)
    assert item.learning['answer_support']['verdict'] == 'review'
    assert plan(service, LegacyExtractionItem(key=item.key, value=item.value, attribute=item.attribute,
                                              confidence=.95, learning=item.learning),
                [{'role': 'user', 'content': SHORTCUT_LINE}])['status'] == 'candidate'


def test_general_statement_becoming_a_prohibition_stays_a_candidate(service):
    """Catch: 用户说“一般回服务站维修”，答案写成必须并且禁止其他位置。"""
    value = '接口报错必须回服务站维修，禁止在其他位置处理。'
    item = candidate(value, question='接口报错在哪里维修？', answer=value, quote=REPAIR_LINE)
    llm = reviewed({'id': 1, 'verdict': 'review', 'reason': '删除了一般并扩大到禁止其他位置',
                    'quote': REPAIR_LINE})
    answer_support.support([{'role': 'user', 'content': REPAIR_LINE}], [item], llm)
    assert item.learning['answer_support']['verdict'] == 'review'
    assert plan(service, LegacyExtractionItem(key=item.key, value=item.value, attribute=item.attribute,
                                              confidence=.95, learning=item.learning),
                [{'role': 'user', 'content': REPAIR_LINE}])['status'] == 'candidate'


def test_verbatim_answer_is_also_reviewed_and_a_later_correction_blocks_it(service):
    """Catch: 取消逐字快路——末尾用户“纠正：禁止上传”必须能推翻 supported。"""
    line = '以后上传前先确认目标项目。'
    correction = '纠正：禁止上传任何资料。'
    messages = [{'role': 'user', 'content': line},
                {'role': 'assistant', 'content': '我会在上传前确认项目。'},
                {'role': 'user', 'content': correction}]
    calls = []

    def llm(prompt):
        calls.append(prompt)
        assert correction in prompt, '完整批次消息必须整体送审，末尾纠正不能被截掉'
        return verdicts({'id': 1, 'verdict': 'review',
                         'reason': '用户随后明确禁止上传', 'quote': line})

    item = candidate(line, question='上传前要做什么？', answer=line, quote=line)
    answer_support.support(messages, [item], llm)
    assert len(calls) == 1, '所有具备依据的 explicit 候选统一走一次批量独立审核'
    assert item.learning['answer_support']['verdict'] == 'review'
    assert answer_support.check(item.learning).startswith(answer_support.REVIEW_REASON)
    assert plan(service, LegacyExtractionItem(key=item.key, value=item.value, attribute=item.attribute,
                                              confidence=.95, learning=item.learning),
                messages)['status'] == 'candidate'


def test_a_verbatim_answer_without_any_review_never_promotes(service):
    """Catch: 缺核对的逐字改写不得因为逐字相等就直接 active。"""
    item = candidate(CARD_LINE, question='入口设置在哪里？', answer=CARD_LINE, quote=CARD_LINE)
    assert answer_support.check(item.learning) != ''
    assert plan(service, LegacyExtractionItem(key=item.key, value=item.value, attribute=item.attribute,
                                              confidence=.95, learning=item.learning),
                [{'role': 'user', 'content': CARD_LINE}])['status'] == 'candidate'


def test_a_quote_that_cuts_off_the_limit_is_not_a_fast_path(service):
    """Catch: 引用只取“接口报错一般回服务站维修”时不能走逐字快路，必须独立核对。"""
    quote = '接口报错一般回服务站维修'
    item = candidate(quote, question='接口报错在哪里维修？', answer=quote, quote=quote)
    calls = []

    def llm(prompt):
        calls.append(prompt)
        return verdicts({'id': 1, 'verdict': 'review',
                         'reason': '引用没有覆盖“具体情况要看错误码”的条件', 'quote': quote})

    answer_support.support([{'role': 'user', 'content': REPAIR_LINE}], [item], llm)
    assert calls, '截断原句的引用不得走逐字快路，必须交给独立核对'
    assert item.learning['answer_support']['verdict'] == 'review'
    assert plan(service, LegacyExtractionItem(key=item.key, value=item.value, attribute=item.attribute,
                                              confidence=.95, learning=item.learning),
                [{'role': 'user', 'content': REPAIR_LINE}])['status'] == 'candidate'


def test_supported_rewrite_may_promote_with_the_original_quote(service):
    """Catch: 忠实改写可以放行，但引用仍必须是真实用户原话。"""
    value = '设置入口和退款入口都放在卡片上。'
    item = candidate(value, question='入口放在哪里？', answer=value, quote=CARD_LINE)
    llm = reviewed({'id': 1, 'verdict': 'supported', 'reason': '改写忠于原话，未扩大范围',
                    'quote': CARD_LINE})
    answer_support.support([{'role': 'user', 'content': CARD_LINE}], [item], llm)
    assert item.learning['answer_support']['verdict'] == 'supported'
    assert item.learning['quote'] == CARD_LINE
    assert plan(service, LegacyExtractionItem(key=item.key, value=item.value, attribute=item.attribute,
                                              confidence=.95, learning=item.learning),
                [{'role': 'user', 'content': CARD_LINE}])['status'] == 'active'


def test_supported_keeps_a_faithful_summary_that_drops_a_background_state_word(service):
    """Catch: 虚构用户说“仪表盘现在没有层次，需要分组颜色和明显的标题”，
    问“仪表盘展示有什么要求？”；忠实归纳“使用分组颜色与明显标题”省略的是描述
    现状的背景词，不是规范否定，不得被程序硬拒。"""
    line = '仪表盘现在没有层次，需要分组颜色和明显的标题。'
    value = '仪表盘使用分组颜色与明显的标题。'
    item = candidate(value, question='仪表盘展示有什么要求？', answer=value, quote=line)
    messages = [{'role': 'user', 'content': line}]
    answer_support.support(messages, [item], reviewed(
        {'id': 1, 'verdict': 'supported', 'reason': '忠实归纳需求，只省略了现状描述', 'quote': line}))
    assert item.learning['answer_support']['verdict'] == 'supported'
    assert answer_support.check(item.learning) == ''
    assert plan(service, LegacyExtractionItem(key=item.key, value=item.value, attribute=item.attribute,
                                              confidence=.95, learning=item.learning),
                messages)['status'] == 'active'


@pytest.mark.parametrize('line,value', [
    ('保存草稿时禁止上传到云端。', '保存草稿时可以上传到云端。'),
    ('删除记录时不得跳过二次确认。', '删除记录时跳过二次确认。'),
    ('浮窗不允许覆盖主操作按钮。', '浮窗覆盖主操作按钮。'),
])
def test_supported_still_blocks_a_dropped_normative_prohibition(service, line, value):
    """Catch: 明确禁止上传/不得跳过/不允许覆盖被答成允许，supported 仍必须硬拦截。"""
    item = candidate(value, question='这条规则是什么？', answer=value, quote=line)
    messages = [{'role': 'user', 'content': line}]
    answer_support.support(messages, [item], reviewed(
        {'id': 1, 'verdict': 'supported', 'reason': '模型说含义一致', 'quote': line}))
    assert item.learning['answer_support']['verdict'] == 'review'
    assert '否定' in item.learning['answer_support']['reason']
    assert plan(service, LegacyExtractionItem(key=item.key, value=item.value, attribute=item.attribute,
                                              confidence=.95, learning=item.learning),
                messages)['status'] == 'candidate'


def test_narrow_keeps_a_valid_extractive_fragment_with_its_limit(service):
    """Catch: 合规的抽取式收窄要保留限定词并同步 answer/quote/normalization。"""
    value = '接口报错一般回服务站维修。'
    item = candidate(value, question='接口报错在哪里维修？', answer=value, quote=REPAIR_LINE)
    corrected = '接口报错一般回服务站维修'
    llm = reviewed({'id': 1, 'verdict': 'narrow', 'reason': '只有前半句能直接引用',
                    'quote': corrected, 'corrected_quote': corrected})
    _, replacements = answer_support.support([{'role': 'user', 'content': REPAIR_LINE}], [item], llm)
    assert item.learning['answer_support']['verdict'] == 'narrow'
    assert item.learning['answer'] == corrected and item.learning['quote'] == corrected
    assert item.learning['normalization']['requirement'] == corrected
    assert item.learning['answer_support']['original_answer'] == value
    assert replacements[id(item)].value == corrected
    assert answer_support.check(item.learning) == ''


def test_narrow_that_drops_a_limit_is_refused(service):
    """Catch: 模型若用删除限定的片段放行，程序必须改判 review，不复制其结论。"""
    line = '如果接口报错，可以用快捷键或者重新加载来处理。'
    value = '接口报错时可以用快捷键或者重新加载处理，不必回服务站。'
    item = candidate(value, question='接口报错怎么办？', answer=value, quote=line)
    fragment = '可以用快捷键'
    llm = reviewed({'id': 1, 'verdict': 'supported', 'reason': '只保留快捷键即可',
                    'quote': fragment})
    answer_support.support([{'role': 'user', 'content': line}], [item], llm)
    assert item.learning['answer_support']['verdict'] == 'review'
    # The locating quote is not the candidate's own quote: refused before any
    # narrowing may be trusted, and the original answer stays visible.
    assert '不一致' in item.learning['answer_support']['reason']
    assert item.learning['answer'] == value
    assert item.learning['answer_support']['original_answer'] == value


def test_narrow_fragment_from_another_user_message_is_refused(service):
    """Catch: 修正片段必须与原引用来自同一条用户消息，不能换人换话题。"""
    original = '设置入口在卡片本身。'
    other = '快捷键可以让老用户更快打开设置入口在卡片本身。'
    messages = [{'role': 'user', 'content': original}, {'role': 'user', 'content': other}]
    item = candidate(original, question='入口在哪？', answer=original, quote=original)
    fragment = '快捷键可以让老用户更快打开'
    llm = reviewed({'id': 1, 'verdict': 'narrow', 'reason': '换到另一条消息',
                    'quote': fragment, 'corrected_quote': fragment})
    answer_support.support(messages, [item], llm)
    assert item.learning['answer_support']['verdict'] == 'review'


@pytest.mark.parametrize('field,new_value', [
    ('question', '入口到底应该放在哪个页面上？'),
    ('answer', '设置入口只在卡片本身。'),
    ('quote', '设置入口在卡片本身,退款入口也在卡片上。'),
])
def test_editing_question_answer_or_quote_invalidates_the_old_check(service, field, new_value):
    """Catch: 问答或引用改变后旧核对结论不得复用。"""
    value = '设置入口和退款入口都放在卡片上。'
    item = candidate(value, question='入口放在哪里？', answer=value, quote=CARD_LINE)
    llm = reviewed({'id': 1, 'verdict': 'supported', 'reason': '忠于原话', 'quote': CARD_LINE})
    answer_support.support([{'role': 'user', 'content': CARD_LINE}], [item], llm)
    assert answer_support.check(item.learning) == ''
    item.learning[field] = new_value
    reason = answer_support.check(item.learning)
    assert reason and '重新核对' in reason
    assert answer_support.needs_review(item.learning)


def test_assistant_only_quote_and_false_quote_fail_closed(service):
    """Catch: 助手消息或虚假引用不能成为核对依据。"""
    value = '设置入口和退款入口都放在卡片上。'
    item = candidate(value, question='入口放在哪里？', answer=value, quote=CARD_LINE)
    llm = reviewed({'id': 1, 'verdict': 'supported', 'reason': '助手说过', 'quote': CARD_LINE})
    answer_support.support([{'role': 'assistant', 'content': CARD_LINE}], [item], llm)
    assert item.learning['answer_support']['verdict'] == 'review'

    item = candidate(value, question='入口放在哪里？', answer=value, quote='用户从没说过这句话。')
    answer_support.support([{'role': 'user', 'content': CARD_LINE}], [item], llm)
    assert item.learning['answer_support']['verdict'] == 'review'


def test_supported_verdict_cannot_point_at_a_different_user_quote(service):
    """Catch: 引用只能证明引用存在，不能用另一条用户原话支持答案细节。"""
    value = '必须使用快捷键区分入口。'
    item = candidate(value, question='入口如何区分？', answer=value, quote=SHORTCUT_LINE)
    llm = reviewed({'id': 1, 'verdict': 'supported', 'reason': '确实提到快捷键',
                    'quote': '设置入口在卡片本身。'})
    messages = [{'role': 'user', 'content': SHORTCUT_LINE},
                {'role': 'user', 'content': '设置入口在卡片本身。'}]
    answer_support.support(messages, [item], llm)
    assert item.learning['answer_support']['verdict'] == 'review'
    assert '原话' in item.learning['answer_support']['reason']


def test_broken_ids_and_bad_json_fail_closed_per_candidate(service):
    """Catch: 坏 JSON、缺号、重复号、额外号一律 review，不是默认 supported。"""
    messages = [{'role': 'user', 'content': SHORTCUT_LINE}]
    good = verdicts({'id': 1, 'verdict': 'supported', 'reason': 'x', 'quote': SHORTCUT_LINE})
    cases = [
        ('不是 JSON', (True, True)),
        (good, (False, True)),                      # 漏掉第 2 条
        (verdicts(                                  # 重复第 1 条
            {'id': 1, 'verdict': 'supported', 'reason': 'x', 'quote': SHORTCUT_LINE},
            {'id': 1, 'verdict': 'supported', 'reason': 'y', 'quote': SHORTCUT_LINE}), (True, True)),
        (verdicts({'id': 9, 'verdict': 'supported', 'reason': 'z', 'quote': SHORTCUT_LINE}), (True, True)),
        (verdicts({'id': 1, 'verdict': 'supported', 'reason': 'x', 'quote': SHORTCUT_LINE},
                  {'id': 2, 'verdict': 'supported', 'reason': 'y', 'quote': SHORTCUT_LINE},
                  {'id': 3, 'verdict': 'supported', 'reason': 'z', 'quote': SHORTCUT_LINE}), (True, True)),
    ]
    for reply, (first_bad, second_bad) in cases:
        first = candidate(SHORTCUT_LINE, question='入口如何区分？', answer=SHORTCUT_LINE,
                          quote=SHORTCUT_LINE)
        second = candidate(SHORTCUT_LINE, question='入口如何区分？', answer=SHORTCUT_LINE,
                           quote=SHORTCUT_LINE, key='project:demo:habit:shortcut')
        answer_support.support(messages, [first, second], lambda _p, reply=reply: reply)
        assert (first.learning['answer_support']['verdict'] == 'review') is first_bad, reply
        assert (second.learning['answer_support']['verdict'] == 'review') is second_bad, reply


def test_a_model_exception_never_drops_the_batch_or_promotes(service):
    """Catch: 接口异常按条 fail-closed，摘要与历史候选照常保留。"""
    def boom(_prompt):
        raise TimeoutError('review provider down')

    summary = CandidateMemory(key='SESSION_SUMMARY', value='本次讨论了卡片入口的设置方式。')
    item = candidate('设置入口在卡片本身。', question='入口在哪？', answer='设置入口在卡片本身。',
                     quote=SHORTCUT_LINE)
    applied, _ = answer_support.support([{'role': 'user', 'content': SHORTCUT_LINE}], [summary, item], boom)
    assert item.learning['answer_support']['verdict'] == 'review'
    assert 'answer_support' not in (summary.learning or {})
    assert summary.value == '本次讨论了卡片入口的设置方式。'


def test_a_promotion_without_any_verdict_is_reviewed_not_promoted(service):
    """Catch: 其他路径直接 persist 也不能绕过 basis_gate 的答案核对。"""
    value = '设置入口和退款入口都放在卡片上。'
    item = candidate(value, question='入口放在哪里？', answer=value, quote=CARD_LINE)
    result = plan(service, LegacyExtractionItem(key=item.key, value=value, attribute=item.attribute,
                                                confidence=.95, learning=item.learning),
                  [{'role': 'user', 'content': CARD_LINE}])
    assert result['status'] == 'candidate'
    assert '独立核对' in result['reason']


def test_one_batch_review_covers_the_batch_and_skips_the_summary():
    """Catch: 每批只做一次有界核对，摘要不参与；编号映射唯一。"""
    summary = CandidateMemory(key='SESSION_SUMMARY', value='本次讨论了卡片入口的设置方式。')
    first = candidate(SHORTCUT_LINE, question='入口如何区分？', answer=SHORTCUT_LINE,
                      quote=SHORTCUT_LINE)
    second = candidate(SHORTCUT_LINE, question='入口如何区分？', answer=SHORTCUT_LINE,
                       quote=SHORTCUT_LINE, key='project:demo:habit:shortcut')
    prompts = []

    def llm(prompt):
        prompts.append(prompt)
        return verdicts({'id': 1, 'verdict': 'supported', 'reason': '忠于原话', 'quote': SHORTCUT_LINE},
                        {'id': 2, 'verdict': 'review', 'reason': '缺少条件', 'quote': SHORTCUT_LINE})

    answer_support.support([{'role': 'user', 'content': SHORTCUT_LINE}], [summary, first, second], llm)
    assert len(prompts) == 1
    assert first.learning['answer_support']['verdict'] == 'supported'
    assert second.learning['answer_support']['verdict'] == 'review'
    assert 'answer_support' not in (summary.learning or {})
    assert '编号' in prompts[0] and '独立核对员' in prompts[0]


def test_model_support_check_field_is_cleared_and_regenerated():
    """Catch: 提取模型自带的 support_check 不能当作核对结论。"""
    value = '设置入口和退款入口都放在卡片上。'
    item = candidate(value, question='入口放在哪里？', answer=value, quote=CARD_LINE,
                     support_check={'verdict': 'supported'})
    llm = reviewed({'id': 1, 'verdict': 'review', 'reason': '扩大入口范围', 'quote': CARD_LINE})
    answer_support.support([{'role': 'user', 'content': CARD_LINE}], [item], llm)
    assert 'support_check' not in item.learning
    assert item.learning['answer_support']['verdict'] == 'review'


EXTRACTION_REPLY = json.dumps({'memories': [
    {'key': 'project:demo:constraint:cards', 'value': '设置入口和退款入口都放在卡片上。',
     'attribute': 'constraint', 'tags': [], 'confidence': .95, 'importance': 6, 'tier': 'normal',
     'learning': {'category': 'project_convention', 'basis': 'explicit', 'quote': CARD_LINE,
                  'question': '入口放在哪里？', 'answer': '设置入口和退款入口都放在卡片上。',
                  'normalization': {'requirement': '设置入口和退款入口都放在卡片上。',
                                    'acceptance': [], 'questions': []}}},
    {'key': 'SESSION_SUMMARY', 'value': '本次讨论了卡片入口的设置方式。', 'attribute': 'fact',
     'tags': ['日志'], 'confidence': .9, 'importance': 5, 'tier': 'normal'},
]}, ensure_ascii=False)


def test_preview_reports_the_same_support_verdict_as_ingestion(service):
    """Catch: 提取预览与正式入库必须走同一核对路径、给出同一结论。"""
    from evolvmem.extraction_preview import preview

    def llm(prompt):
        return EXTRACTION_REPLY if '独立核对员' not in prompt else verdicts(
            {'id': 1, 'verdict': 'review', 'reason': '答案扩大了入口范围', 'quote': CARD_LINE})

    body = {'project': 'demo', 'messages': [{'role': 'user', 'content': CARD_LINE},
                                            {'role': 'assistant', 'content': '我会按入口整理。'}]}
    result = preview(service, body, llm=llm)['current']['candidates'][0]
    assert result['action'] == 'review'
    assert result['answer_support']['verdict'] == 'review'
    assert result['answer_support']['reason'].endswith('答案扩大了入口范围')

    stub = candidate('设置入口和退款入口都放在卡片上。', question='入口放在哪里？',
                     answer='设置入口和退款入口都放在卡片上。', quote=CARD_LINE)
    _, _ = answer_support.support([{'role': 'user', 'content': CARD_LINE}], [stub], llm)
    direct = plan(service, LegacyExtractionItem(key=stub.key, value=stub.value, attribute=stub.attribute,
                                                confidence=.95, learning=stub.learning),
                  [{'role': 'user', 'content': CARD_LINE}])
    assert direct['status'] == 'candidate'
    assert direct['answer_support']['verdict'] == 'review'


def test_the_whole_batch_reaches_the_reviewer_even_with_a_late_correction():
    """Catch: probe 复现——末尾“纠正：禁止上传”必须出现在送审消息里。"""
    early = '上传资料前先确认目标项目。'
    correction = '纠正：禁止上传任何资料。'
    filler = [{'role': 'user', 'content': f'第 {i} 轮补充：先说明当前进展，再继续讨论。'} for i in range(40)]
    messages = [{'role': 'user', 'content': early}, *filler,
                {'role': 'assistant', 'content': '收到。'}, {'role': 'user', 'content': correction}]
    seen = []

    def llm(prompt):
        seen.append(prompt)
        assert correction in prompt and early in prompt
        return verdicts({'id': 1, 'verdict': 'review', 'reason': '随后禁止上传', 'quote': early})

    item = candidate(early, question='上传前要做什么？', answer=early, quote=early)
    answer_support.support(messages, [item], llm)
    assert seen and item.learning['answer_support']['verdict'] == 'review'


def test_an_over_budget_batch_is_reviewed_as_unverifiable_not_truncated():
    """Catch: probe 复现——16940 字符的消息不能静默截断后仍判 supported。"""
    early = '上传资料前先确认目标项目。'
    correction = '纠正：禁止上传任何资料。'
    long_tail = '补充说明：' + '这是一段很长的过程描述，用来超过单条消息上限。' * 400
    messages = [{'role': 'user', 'content': early + long_tail + correction}]
    assert len(messages[0]['content']) > answer_support.MAX_MESSAGE_CHARS
    calls = []
    item = candidate(early, question='上传前要做什么？', answer=early, quote=early)
    answer_support.support(messages, [item], lambda p: calls.append(p) or verdicts(
        {'id': 1, 'verdict': 'supported', 'reason': '模型只看开头', 'quote': early}))
    assert calls == [], '超出预算时不得发送被截断的批次'
    assert item.learning['answer_support']['verdict'] == 'review'
    assert '超过' in item.learning['answer_support']['reason']


def test_a_same_quote_verdict_does_not_survive_a_later_user_correction(service):
    """Catch: 同一引用换上下文/后续纠正后旧结论不得复用。"""
    item = candidate(CARD_LINE, question='入口设置在哪里？', answer=CARD_LINE, quote=CARD_LINE)
    messages = [{'role': 'user', 'content': CARD_LINE}]
    answer_support.support(messages, [item], reviewed(
        {'id': 1, 'verdict': 'supported', 'reason': '忠于原话', 'quote': CARD_LINE}))
    assert answer_support.check_binding(item.learning, messages, key=item.key.casefold()) == ''
    corrected = [*messages, {'role': 'user', 'content': '纠正：入口不放在卡片上了。'}]
    assert answer_support.check_binding(item.learning, corrected, key=item.key.casefold()).endswith('需要重新核对')
    assert answer_support.check_binding(item.learning, messages,
                                        key='project:demo:habit:other').endswith('候选标识已变化，需要重新核对')


def test_the_answer_may_not_drop_a_negation_the_quote_carries(service):
    """Catch: 模型说 supported，但答案删掉了原话里的否定词，仍要保守拦截。"""
    line = '修改界面时不要新建独立页面。'
    value = '修改界面时新建独立页面。'
    item = candidate(value, question='修改界面时应该怎么做？', answer=value, quote=line)
    messages = [{'role': 'user', 'content': line}]
    answer_support.support(messages, [item], reviewed(
        {'id': 1, 'verdict': 'supported', 'reason': '模型看漏了否定', 'quote': line}))
    assert item.learning['answer_support']['verdict'] == 'review'
    assert '否定' in item.learning['answer_support']['reason']


def test_review_failure_keeps_the_summary_and_the_original_text():
    """Catch: 核对失败/解析失败不能丢摘要，且原答案作为元数据保留。"""
    summary = CandidateMemory(key='SESSION_SUMMARY', value='本次讨论了卡片入口的设置方式。')
    value = '设置入口和退款入口都放在卡片上。'
    item = candidate(value, question='入口放在哪里？', answer=value, quote=CARD_LINE)
    answer_support.support([{'role': 'user', 'content': CARD_LINE}], [summary, item],
                           lambda _p: '不是 JSON')
    assert summary.value == '本次讨论了卡片入口的设置方式。'
    assert 'answer_support' not in (summary.learning or {})
    assert item.learning['answer_support']['verdict'] == 'review'
    assert item.learning['answer_support']['original_answer'] == value


def test_preview_counts_each_real_provider_callback(service):
    """Catch: model_calls 必须统计实际调用（提炼+核对），不是写死 1/2。"""
    from evolvmem.extraction_preview import preview

    calls = []

    def llm(prompt):
        calls.append(prompt)
        if '独立核对员' in prompt:
            return verdicts({'id': 1, 'verdict': 'review', 'reason': '扩大范围', 'quote': CARD_LINE})
        return EXTRACTION_REPLY

    body = {'project': 'demo', 'messages': [{'role': 'user', 'content': CARD_LINE}]}
    result = preview(service, body, llm=llm)
    assert result['model_calls'] == len(calls) == 2
    assert result['current']['model_calls'] == 2
    assert result['current']['candidates'][0]['status'] == 'candidate'
    assert result['current']['candidates'][0]['answer_support']['verdict'] == 'review'


def test_prompt_prefers_narrow_over_review():
    """Catch: 原 prompt 把 review 定义过宽，模型干脆不收窄。"""
    item = candidate('退款、删除、导出均放在卡片。', question='入口放在哪里？',
                     answer='退款、删除、导出均放在卡片。', quote='设置入口在卡片本身，不要放侧栏；')
    prompt = answer_support.build_prompt([{'role': 'user', 'content': '设置入口在卡片本身，不要放侧栏；'}], [item])
    assert '优先收窄' in prompt and 'narrow' in prompt
    assert '没有任何片段能忠实回答' in prompt


def test_the_card_example_is_narrowed_to_the_user_fragment():
    """Catch: 合成样本——错误答案额外列退款删除导出，应能收窄为用户原话片段。"""
    user = '设置入口在卡片本身，不要放侧栏；'
    wrong = '退款、删除、导出均放在卡片，设置入口在卡片本身。'
    corrected = '设置入口在卡片本身，不要放侧栏'
    item = candidate(wrong, question='入口放在哪里？', answer=wrong, quote=user)
    messages = [{'role': 'user', 'content': user}]
    _, replacements = answer_support.support(messages, [item], reviewed(
        {'id': 1, 'verdict': 'narrow', 'reason': '只有这条原话片段能忠实回答',
         'quote': user, 'corrected_quote': corrected}))
    assert item.learning['answer_support']['verdict'] == 'narrow'
    assert item.learning['answer'] == corrected and item.learning['quote'] == corrected
    assert item.learning['normalization']['requirement'] == corrected
    assert replacements[id(item)].value == corrected
    assert answer_support.check(item.learning) == ''
    # The program guard is unchanged: dropping the negation from the fragment fails.
    dropped = candidate(wrong, question='入口放在哪里？', answer=wrong, quote=user)
    answer_support.support(messages, [dropped], reviewed(
        {'id': 1, 'verdict': 'narrow', 'reason': '只留前半句', 'quote': '设置入口在卡片本身',
         'corrected_quote': '设置入口在卡片本身'}))
    assert dropped.learning['answer_support']['verdict'] == 'review'
    assert '否定' in dropped.learning['answer_support']['reason']


def test_prompt_requires_the_narrow_fragment_to_answer_and_be_confirmed():
    """Catch: 虚构“浮窗可以右键或者别的办法打开”回答不了“左右键各自如何分工？”；
    虚构“设备怎么保养，通常是不是要返厂？”仍在提问。prompt 必须要求 narrow 片段
    已经正面回答原问题、且表达用户已确认的事实/需求，未采纳备选与疑问保持 review，
    同时不得只凭句末问号一刀切。"""
    panel = '浮窗可以右键或者别的办法打开。'
    upkeep = '设备怎么保养，通常是不是要返厂？'
    item = candidate(panel, question='左右键各自如何分工？', answer=panel, quote=panel)
    prompt = answer_support.build_prompt([{'role': 'user', 'content': panel},
                                          {'role': 'user', 'content': upkeep}], [item])
    assert '正面回答' in prompt
    assert '已确认' in prompt and '备选' in prompt
    assert '疑问' in prompt and '复述' in prompt
    assert '问号' in prompt
    # 明确已回答、同条原话连续片段仍可 narrow：原有措辞与判定分支保持。
    assert '优先收窄' in prompt and '没有任何片段能忠实回答' in prompt
    assert '连续原话片段' in prompt


def test_preview_normalizes_a_wrong_model_project_before_the_review(service):
    """Catch: 模型给错项目、用户选定正确项目时，不得在核对后判 stale。"""
    from evolvmem.extraction_preview import preview

    text = '设置入口在卡片本身，不要放侧栏。'
    reply = json.dumps({'memories': [
        {'key': 'SESSION_SUMMARY', 'value': '本次讨论了入口位置。'},
        {'key': 'project:other:constraint:cards', 'value': text, 'attribute': 'constraint',
         'confidence': .95, 'learning': {'category': 'project_convention', 'basis': 'explicit',
                                         'quote': text, 'question': '入口放在哪里？', 'answer': text}}]},
        ensure_ascii=False)

    def llm(prompt):
        if '独立核对员' in prompt:
            return verdicts({'id': 1, 'verdict': 'supported', 'reason': '与用户原话一致', 'quote': text})
        return reply

    result = preview(service, {'project': 'demo', 'messages': [{'role': 'user', 'content': text}]},
                     llm=llm)['current']['candidates'][0]
    assert result['status'] == 'active', result.get('reason')
    assert '标识已变化' not in str(result.get('reason'))


def test_prepare_and_persist_accept_a_wrong_model_project_key(service, monkeypatch):
    """Catch: prepare_extraction 解析后写库的行必须能复验同一核对结论。"""
    from evolvmem import kimi_hooks
    from evolvmem.session_extraction import prepare_extraction

    text = '设置入口在卡片本身，不要放侧栏。'
    extraction = json.dumps({'memories': [
        {'key': 'SESSION_SUMMARY', 'value': '本次讨论了入口位置。'},
        {'key': 'project:wrong:request:auto', 'value': text, 'confidence': .9, 'attribute': 'constraint',
         'learning': {'category': 'task_requirement', 'basis': 'explicit', 'quote': text,
                      'question': '入口放在哪里？', 'answer': text}}]}, ensure_ascii=False)

    def model(prompt, *args, **kwargs):
        if '独立核对员' in prompt:
            return verdicts({'id': 1, 'verdict': 'supported', 'reason': '与用户原话一致', 'quote': text})
        return extraction

    monkeypatch.setattr(kimi_hooks, '_load_llm_config', lambda **kw: object())
    monkeypatch.setattr(kimi_hooks, '_call_llm_with_retry', model)
    messages = [{'role': 'user', 'content': text}]
    request = prepare_extraction(service.config, 'demo', 'session-x', messages, object())
    assert request.candidates[0].key.startswith('project:demo:')
    result = service.persist_legacy_extraction(request, source_messages=messages)
    row = service.knowledge().detail(result.candidates[0].context_id)
    assert row['status'] == 'active', row['ingestion_reason']
    assert row['learning']['answer_support']['verdict'] == 'supported'


def test_a_failed_review_never_reuses_the_same_body_old_active_row(service):
    """Catch: 同一正文的旧 active 不得让本轮核对失败/被纠正的来源变成有效来源。"""
    from evolvmem.legacy_models import LegacyExtractionItem, LegacyExtractionRequest

    text = '上传资料前先确认目标项目。'
    messages = [{'role': 'user', 'content': text}]
    first = candidate(text, question='上传前要做什么？', answer=text, quote=text)
    answer_support.support(messages, [first], reviewed(
        {'id': 1, 'verdict': 'supported', 'reason': '与用户原话一致', 'quote': text}))
    result = service.persist_legacy_extraction(LegacyExtractionRequest(
        summary=LegacyExtractionItem(key='project:demo:progress:log:dup', value='本次讨论上传前置条件。', attribute='fact'),
        candidates=(LegacyExtractionItem(key=first.key, value=text, attribute=first.attribute,
                                         confidence=.95, learning=first.learning),),
        source_session='dup-old'), source_messages=messages)
    old_id = result.candidates[0].context_id
    assert service.knowledge().detail(old_id)['status'] == 'active'

    corrected = [{'role': 'user', 'content': text}, {'role': 'user', 'content': '纠正：禁止上传任何资料。'}]
    second = candidate(text, question='上传前要做什么？', answer=text, quote=text)
    answer_support.support(corrected, [second], reviewed(
        {'id': 1, 'verdict': 'review', 'reason': '用户随后明确禁止上传', 'quote': text}))
    gate = plan(service, LegacyExtractionItem(key=second.key, value=text, attribute=second.attribute,
                                              confidence=.95, learning=second.learning), corrected)
    assert gate['status'] == 'candidate'
    assert gate['action'] != 'skip', '核对失败时不得进入复用旧 active 的快捷路径'
    assert str(gate['reason']).startswith(answer_support.REVIEW_REASON)
    assert gate['target_id'] is None
    assert service.knowledge().detail(old_id)['status'] == 'active', '旧知识原状态不被降级'

    # A supported duplicate still reuses the existing row and keeps both sources.
    third = candidate(text, question='上传前要做什么？', answer=text, quote=text)
    answer_support.support(messages, [third], reviewed(
        {'id': 1, 'verdict': 'supported', 'reason': '与用户原话一致', 'quote': text}))
    reuse = service.persist_legacy_extraction(LegacyExtractionRequest(
        summary=LegacyExtractionItem(key='project:demo:progress:log:dup2', value='本次讨论上传前置条件。', attribute='fact'),
        candidates=(LegacyExtractionItem(key=third.key, value=text, attribute=third.attribute,
                                         confidence=.95, learning=third.learning),),
        source_session='dup-new'), source_messages=messages)
    assert reuse.candidates == ()
    assert reuse.reused and reuse.reused[0].context_id == old_id
    assert reuse.reused[0].changed is False, '合法去重只复用，不新增写入'
    assert service.knowledge().detail(old_id)['status'] == 'active'


# ---- assistant suggestions stay in history only ----

AGREEMENT_LINE = '星图验收项目的明确约定：导出清单时保留原始编号，日期统一使用 YYYY-MM-DD 格式。'
SUGGESTION_LINE = '已了解。我另建议每次导出后自动删除原始清单，这只是建议，尚未得到你的确认。'
SUGGESTION_ANSWER = '助手建议每次导出后自动删除原始清单，该建议尚未得到用户确认。'
SUGGESTION_QUESTION = '助手对星图验收项目导出清单提出了什么尚未确认的建议？'
FACT_LINE = '接口默认超时时间是 30 秒，超过后客户端会重试一次。'
FACT_ANSWER = '该接口默认超时时间为 30 秒，超时后客户端自动重试一次。'


def inferred(value, *, question, answer, key='project:demo:reference:assistant-suggestion',
             category='reference', attribute='fact', **extra):
    """A model-marked inference with no user quote, as the extractor emits it."""
    learning = {'category': category, 'basis': 'inferred', 'quote': '', 'question': question,
                'answer': answer, **extra}
    return CandidateMemory(key=key, value=value, attribute=attribute, confidence=.8, learning=learning)


def suggestion_reply(*, suggestion, verdicts_=None, reason='助手单方面提出且用户未采纳'):
    payload = {'verdicts': list(verdicts_ or []),
               'assistant_suggestions': [{'id': 1, 'suggestion': suggestion, 'reason': reason}]}
    return json.dumps(payload, ensure_ascii=False)


MIXED_MESSAGES = [{'role': 'user', 'content': AGREEMENT_LINE},
                  {'role': 'assistant', 'content': SUGGESTION_LINE}]


def test_an_unadopted_assistant_suggestion_is_kept_only_in_history(service):
    """Catch: 真实复现——助手“另建议…尚未确认”只保留历史，不生成 active 也不生成 candidate。"""
    explicit = candidate('星图验收项目导出清单时保留原始编号。',
                         question='星图验收项目导出清单有什么约定？',
                         answer='星图验收项目导出清单时保留原始编号。', quote=AGREEMENT_LINE)
    suggestion = inferred(SUGGESTION_ANSWER, question=SUGGESTION_QUESTION, answer=SUGGESTION_ANSWER)
    calls = []

    def llm(prompt):
        calls.append(prompt)
        assert '<助手建议候选>' in prompt, '助手来源的推断候选必须进入同一次独立复核'
        return suggestion_reply(
            suggestion=True,
            verdicts_=[{'id': 1, 'verdict': 'supported', 'reason': '与用户原话一致',
                        'quote': AGREEMENT_LINE}])

    answer_support.support(MIXED_MESSAGES, [explicit, suggestion], llm)
    assert len(calls) == 1, '明确候选与助手建议共用一次有界批量核对'
    assert explicit.learning['answer_support']['verdict'] == 'supported'
    assert suggestion.learning['answer_support']['verdict'] == answer_support.HISTORY_VERDICT
    assert answer_support.check(suggestion.learning).startswith(answer_support.HISTORY_REASON)
    assert answer_support.history_only(suggestion.learning)
    assert not answer_support.history_only(explicit.learning)
    assert answer_support.check(explicit.learning) == ''
    assert [item.key for item in
            answer_support.drop_history_only([explicit, suggestion])] == [explicit.key]


def test_a_plain_assistant_fact_is_reviewed_but_never_dropped():
    """Catch: 助手普通事实回答（非建议）不得因同源于助手被程序删除。"""
    fact = inferred(FACT_ANSWER, question='该接口的默认超时是多少？', answer=FACT_ANSWER)
    messages = [{'role': 'user', 'content': '这个接口超时怎么算？'},
                {'role': 'assistant', 'content': FACT_LINE}]
    assert answer_support.suggestion_reviewable(fact.learning, fact, messages), \
        '同源于助手的推断候选应交给独立复核判断，而不是程序直接删'
    answer_support.support(messages, [fact], lambda _p: suggestion_reply(
        suggestion=False, reason='回答用户提问的事实'))
    assert not answer_support.history_only(fact.learning)
    assert answer_support.drop_history_only([fact]) == [fact]


def test_a_missing_suggestion_judgment_keeps_the_candidate():
    """Catch: 复核未给出建议判断（旧数组格式/坏输出）时 fail-closed 保留候选。"""
    suggestion = inferred(SUGGESTION_ANSWER, question=SUGGESTION_QUESTION, answer=SUGGESTION_ANSWER)
    answer_support.support(MIXED_MESSAGES, [suggestion],
                           reviewed({'id': 1, 'verdict': 'review', 'reason': '模型未判断建议',
                                     'quote': AGREEMENT_LINE}))
    assert not answer_support.history_only(suggestion.learning)
    assert answer_support.drop_history_only([suggestion]) == [suggestion]


def test_a_reviewer_failure_never_marks_history_only():
    """Catch: 复核异常不得把候选误判为“只留历史”。"""
    suggestion = inferred(SUGGESTION_ANSWER, question=SUGGESTION_QUESTION, answer=SUGGESTION_ANSWER)

    def boom(_prompt):
        raise TimeoutError('review provider down')

    answer_support.support(MIXED_MESSAGES, [suggestion], boom)
    assert not answer_support.history_only(suggestion.learning)
    assert answer_support.drop_history_only([suggestion]) == [suggestion]


def test_content_the_user_also_said_is_never_screened_as_a_suggestion():
    """Catch: 含“建议”但为用户要求/明确决策的内容不得进入助手建议通道。"""
    line = '我要求：以后每次导出都自动删除原始清单，这是明确要求而不是建议。'
    requirement = inferred('每次导出后自动删除原始清单。', question='导出后要做什么？',
                           answer='每次导出后自动删除原始清单。',
                           key='project:demo:task:export-cleanup')
    messages = [{'role': 'user', 'content': line},
                {'role': 'assistant', 'content': '已了解，我会按这个要求执行。'}]
    assert not answer_support.suggestion_reviewable(requirement.learning, requirement, messages)
    calls = []
    answer_support.support(messages, [requirement], lambda p: calls.append(p) or verdicts(
        {'id': 1, 'verdict': 'review', 'reason': '推断待确认', 'quote': line}))
    assert calls == [], '用户自己也说过的内容不得进入助手建议通道，也不该占用核对调用'
    assert answer_support.drop_history_only([requirement]) == [requirement]


def test_an_adopted_assistant_suggestion_stays_eligible():
    """Catch: 用户随后明确采纳的建议不能再被当作“未采纳建议”删除。"""
    adoption = '好，就按你说的，导出后自动删除原始清单。'
    plan_candidate = inferred('导出后自动删除原始清单。', question='导出后要做什么？',
                              answer='导出后自动删除原始清单。',
                              key='project:demo:task:export-cleanup')
    messages = [*MIXED_MESSAGES, {'role': 'user', 'content': adoption}]
    assert not answer_support.suggestion_reviewable(plan_candidate.learning, plan_candidate, messages)
    answer_support.support(messages, [plan_candidate], lambda _p: suggestion_reply(
        suggestion=False, reason='用户已明确采纳该建议'))
    assert answer_support.drop_history_only([plan_candidate]) == [plan_candidate]


def test_experience_and_explicit_user_decisions_are_never_screened():
    """Catch: 有证据经验与用户明确决策不能被助手建议筛选误删。"""
    experience = inferred('导出后用校验和核对编号。', question='怎样核对导出编号？',
                          answer='导出后用校验和核对编号。', category='experience',
                          attribute='experience', key='project:demo:experience:checksum')
    assert not answer_support.suggestion_reviewable(experience.learning, experience, MIXED_MESSAGES)
    explicit = candidate(AGREEMENT_LINE, question='导出清单有什么约定？', answer=AGREEMENT_LINE,
                         quote=AGREEMENT_LINE)
    assert not answer_support.suggestion_reviewable(explicit.learning, explicit, MIXED_MESSAGES)
    # Even a wrong true verdict cannot drop them: the screen keeps them out of the pass.
    answer_support.support(MIXED_MESSAGES, [experience],
                           lambda _p: suggestion_reply(suggestion=True))
    assert answer_support.drop_history_only([experience]) == [experience]
    assert not answer_support.history_only(experience.learning)

    # A real conflict is a review item, not an assistant suggestion: the prompt says so.
    prompt = answer_support.build_prompt(MIXED_MESSAGES, [], [inferred(
        SUGGESTION_ANSWER, question=SUGGESTION_QUESTION, answer=SUGGESTION_ANSWER)])
    assert '冲突' in prompt and '经验' in prompt and '事实' in prompt
    assert '未明确采纳' in prompt


def test_the_screen_needs_the_assistant_wording_not_a_keyword():
    """Catch: 仅凭“建议”一词不得触发删除；来源证据要求助手原话的连续片段。"""
    keyword_only = inferred('助手建议把导出编号保留下来。', question='导出编号怎么处理？',
                            answer='助手建议把导出编号保留下来。')
    messages = [{'role': 'user', 'content': AGREEMENT_LINE},
                {'role': 'assistant', 'content': '好的，我会整理这份清单。'}]
    assert not answer_support.suggestion_reviewable(keyword_only.learning, keyword_only, messages)
    assert not answer_support.history_only(keyword_only.learning)


def test_the_history_verdict_can_never_promote_through_the_shared_gate():
    """Catch: 即使调用方仍把条目送去入库，history 结论也不得放行成 active。"""
    suggestion = inferred(SUGGESTION_ANSWER, question=SUGGESTION_QUESTION, answer=SUGGESTION_ANSWER)
    answer_support.support(MIXED_MESSAGES, [suggestion],
                           lambda _p: suggestion_reply(suggestion=True))
    reason = answer_support.check_binding(suggestion.learning, MIXED_MESSAGES,
                                          key=suggestion.key.casefold())
    assert reason.startswith(answer_support.HISTORY_REASON)


def test_preview_drops_an_unadopted_assistant_suggestion_like_ingestion(service):
    """Catch: 预览与正式入库一致——未采纳建议在两处都不作为候选展示。"""
    from evolvmem.extraction_preview import preview

    extraction = json.dumps({'memories': [
        {'key': 'SESSION_SUMMARY', 'value': '星图验收项目确认了导出约定，助手另提了一条建议。'},
        {'key': 'project:demo:convention:export', 'value': '导出清单时保留原始编号。',
         'attribute': 'constraint', 'confidence': .95,
         'learning': {'category': 'project_convention', 'basis': 'explicit', 'quote': AGREEMENT_LINE,
                      'question': '导出清单有什么约定？', 'answer': '导出清单时保留原始编号。'}},
        {'key': 'project:demo:reference:assistant_suggestion', 'value': SUGGESTION_ANSWER,
         'attribute': 'fact', 'confidence': .8,
         'learning': {'category': 'reference', 'basis': 'inferred', 'quote': '',
                      'question': SUGGESTION_QUESTION, 'answer': SUGGESTION_ANSWER}}]},
        ensure_ascii=False)

    def llm(prompt):
        if '独立核对员' in prompt:
            assert '<助手建议候选>' in prompt
            return suggestion_reply(suggestion=True, verdicts_=[
                {'id': 1, 'verdict': 'supported', 'reason': '与用户原话一致', 'quote': AGREEMENT_LINE}])
        return extraction

    result = preview(service, {'project': 'demo', 'messages': MIXED_MESSAGES}, llm=llm)['current']
    bodies = [candidate['body'] for candidate in result['candidates']]
    assert SUGGESTION_ANSWER not in bodies, '预览不得显示未采纳建议'
    assert '导出清单时保留原始编号。' in bodies


MALFORMED_SUGGESTIONS = [
    # 同一 id 先 true 后 false：矛盾
    [{'id': 1, 'suggestion': True, 'reason': '助手建议'},
     {'id': 1, 'suggestion': False, 'reason': '其实是回答用户提问'}],
    # 同一 id 先 false 后 true：矛盾（取最后一条会误删候选）
    [{'id': 1, 'suggestion': False, 'reason': '回答用户提问'},
     {'id': 1, 'suggestion': True, 'reason': '助手建议'}],
    # 同一 id 重复且取值相同：仍然是不可寻址的编号接口
    [{'id': 1, 'suggestion': True, 'reason': '助手建议'},
     {'id': 1, 'suggestion': True, 'reason': '助手建议'}],
    # 非正 id
    [{'id': 0, 'suggestion': True, 'reason': '助手建议'}],
    [{'id': -1, 'suggestion': True, 'reason': '助手建议'}],
    # 合法 id 旁边夹带非正 id：整组编号不可信，不得只采纳 id=1
    [{'id': 0, 'suggestion': True, 'reason': '助手建议'},
     {'id': 1, 'suggestion': True, 'reason': '助手建议'}],
    # suggestion=true 但理由缺失、空白或非字符串
    [{'id': 1, 'suggestion': True}],
    [{'id': 1, 'suggestion': True, 'reason': '   '}],
    [{'id': 1, 'suggestion': True, 'reason': None}],
    [{'id': 1, 'suggestion': True, 'reason': 7}],
]


@pytest.mark.parametrize('entries', MALFORMED_SUGGESTIONS)
def test_a_malformed_suggestion_judgment_keeps_every_candidate(entries):
    """Catch: 重复/矛盾 id、非正 id、true 缺理由等坏输出必须整体保留候选。"""
    assert answer_support._parse_suggestions(entries) is None, '坏输出必须整体判为不可用'
    suggestion = inferred(SUGGESTION_ANSWER, question=SUGGESTION_QUESTION, answer=SUGGESTION_ANSWER)
    reply = json.dumps({'verdicts': [], 'assistant_suggestions': entries}, ensure_ascii=False)
    answer_support.support(MIXED_MESSAGES, [suggestion], lambda _p: reply)
    assert not answer_support.history_only(suggestion.learning)
    assert answer_support.drop_history_only([suggestion]) == [suggestion]


def test_a_well_formed_suggestion_judgment_still_applies():
    """Catch: 严格校验不得改变合法结果——带理由的 true 仍剔除，false 仍保留。"""
    def judge(entries):
        item = inferred(SUGGESTION_ANSWER, question=SUGGESTION_QUESTION, answer=SUGGESTION_ANSWER)
        reply = json.dumps({'verdicts': [], 'assistant_suggestions': entries}, ensure_ascii=False)
        answer_support.support(MIXED_MESSAGES, [item], lambda _p: reply)
        return answer_support.history_only(item.learning)

    assert answer_support._parse_suggestions(
        [{'id': 1, 'suggestion': True, 'reason': '助手单方面建议，用户未采纳'}]) == {
            1: {'suggestion': True, 'reason': '助手单方面建议，用户未采纳'}}
    assert judge([{'id': 1, 'suggestion': True, 'reason': '助手单方面建议，用户未采纳'}]) is True
    assert judge([{'id': 1, 'suggestion': False, 'reason': '回答用户提问的事实'}]) is False
    # false 只会保留候选，没有理由也无需作废整组判断
    assert judge([{'id': 1, 'suggestion': False}]) is False

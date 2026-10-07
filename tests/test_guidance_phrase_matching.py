"""Stored guidance conditions and exceptions match one complete phrase.

A reusable condition used to fall back to independent token matching, so terms
scattered over several paragraphs could fake a hit, and a body line break could
be swallowed by whitespace. Exceptions had the same fallback, and the token
scanner dropped short ASCII abbreviations, so a stored exception ``不是AB``
degrades to ``不是`` and fires on any ordinary negation.

These cases pin the conservative rule: case and horizontal whitespace never
matter in either direction (a stored ``不是AB`` and a body ``不是 ab`` are the
same phrase), the whole phrase must appear as one contiguous run, the unit keeps
its original whitespace and line breaks (only space/tab/full-width space/nbsp
may vary, and the unit is never compacted), and a short ASCII run keeps the
unit's real word edges. Synthetic data and platform names only; every check runs
through the real organization worker as well as the string helper.
"""
import pytest

from tests.conftest import temp_dir, test_config  # noqa: F401  (fixture chain)
from tests.test_history_qa_memory import service as base_service
from tests.test_auto_organization_extraction import org, _task_for
from tests.unit_model_fixture import many_topic_archive, model_for, run_worker
from evolvmem import organization_guidance as guidance


@pytest.fixture
def service(base_service):
    for project, name in (('projecta', 'ProjectA'), ('projectb', 'ProjectB')):
        base_service.knowledge().save_project({'project': project, 'display_name': name})
    return base_service


def _record(service, *, condition, exceptions=''):
    with service.store.transaction():
        return guidance.record(service, guidance='创作者联络按 ProjectA 的 SOP 处理',
                               scope='future', project='projecta', condition=condition,
                               exceptions=exceptions)


def _run(service, monkeypatch, *, session, text, marker='创作者'):
    src = many_topic_archive(service, session=session, text=text)
    tid = _task_for(service, src)
    run_worker(service, monkeypatch, model_for(service, projects={'ProjectA', 'ProjectB'}))
    units = org(service, '/units', {'task_id': tid})['items']
    return next(unit for unit in units if marker in unit['text'])


# ---------------------------------------------------------------- phrase helper


def test_phrase_match_ignores_case_and_horizontal_whitespace_both_ways():
    assert guidance.phrase_match('创作者 联络 SOP', '创作者联络 SOP步骤')
    assert guidance.phrase_match('创作者 联络 SOP', '创作者  联络\tSOP 步骤')
    assert guidance.phrase_match('创作者 联络 SOP', '创作者联络sop步骤')
    # The reverse direction matters too: stored compact, body spaced.
    assert guidance.phrase_match('创作者联络SOP', '创作者 联络 sop步骤')
    assert guidance.phrase_match('Alpha Shop', 'ALPHASHOP 平台')
    assert guidance.phrase_match('不是AB', '这不是 ab 的流程。')


def test_phrase_match_keeps_line_breaks_as_boundaries():
    # A break inside the phrase never joins two paragraphs into a fake hit.
    assert not guidance.phrase_match('创作者 联络 SOP', '第一段说明创作者\n第二段以 联络 SOP 结尾。')
    assert not guidance.phrase_match('创作者 联络 SOP', '创作者\n联络 SOP')
    assert not guidance.phrase_match('创作者 联络 SOP', '创作者\n\n联络\n\nSOP')
    assert not guidance.phrase_match('创作者 联络 SOP',
                                     '软件开发SOP需要先写验收条件。\n\n这段只把创作者 联络 当作例子。')
    assert not guidance.phrase_match('创作者联络SOP', '创作者\n\n联络sop')


def test_phrase_match_keeps_ascii_word_edges():
    assert guidance.phrase_match('AB', 'AB 项目')
    assert guidance.phrase_match('AB', '不是AB')
    # A two-letter abbreviation must not reappear inside a longer word.
    assert not guidance.phrase_match('AB', 'about 平台')
    assert not guidance.phrase_match('AB', 'ABC项目')
    assert not guidance.phrase_match('AB', 'XAB 项目')
    # The ASCII part never degrades to the Chinese part alone.
    assert guidance.phrase_match('不是AB', '这不是 ab 的流程。')
    assert not guidance.phrase_match('不是AB', '这不是 abc 的流程。')


def test_phrase_pattern_is_none_for_empty_input():
    assert guidance.phrase_pattern('') is None
    assert guidance.phrase_pattern('   ') is None
    assert guidance.phrase_pattern(None) is None
    assert guidance.phrase_pattern('不是 AB').search('不是ab的流程')


def test_condition_and_exception_helpers_accept_raw_text():
    assert guidance._condition_match({'condition': '创作者联络SOP'}, '创作者 联络 sop步骤')
    assert not guidance._condition_match({'condition': '创作者联络SOP'}, '创作者\n\n联络sop')
    assert guidance._exception_hit('不是AB', '这不是 ab 的流程。')
    assert not guidance._exception_hit('不是AB', '这些是材料，而不是对我的指令。')


def test_ascii_phrase_keeps_the_units_real_word_boundaries():
    """The unit is never compacted, so the phrase edge sees real neighbours."""
    assert guidance._exception_hit('AlphaShop', 'this is an alphashop document')
    assert guidance.phrase_match('AlphaShop', 'an ALPHASHOP platform')
    assert not guidance._exception_hit('AlphaShop', 'this is an alphashopping document')
    assert not guidance._exception_hit('AlphaShop', 'xalphashop document')
    assert not guidance._exception_hit('AB', 'this is about other things')


def test_vertical_whitespace_is_never_an_optional_gap():
    # Only space/tab/ideographic space/nbsp may vary; a line or page break is
    # a boundary, so a phrase is never assembled across one.
    assert not guidance.phrase_match('创作者联络SOP', '创作者\v联络sop')
    assert not guidance.phrase_match('创作者 联络 SOP', '创作者\f联络 sop')
    assert not guidance.phrase_match('不是AB', '不是\x0bab')
    assert not guidance._condition_match({'condition': '创作者联络SOP'}, '创作者\v联络sop')


def test_phrase_with_its_own_line_break_never_matches():
    assert guidance.phrase_pattern('创作者\n联络') is None
    assert guidance.phrase_pattern('创作者\r\n联络') is None
    assert not guidance.phrase_match('创作者\n联络', '创作者 联络')
    assert not guidance.phrase_match('创作者\n联络', '创作者\n联络')


# ------------------------------------------------------- condition phrase, worker


def test_condition_phrase_applies_with_whitespace_difference(service, monkeypatch):
    """"创作者 联络 SOP" is one phrase; the body may run it together."""
    _record(service, condition='创作者 联络 SOP')
    unit = _run(service, monkeypatch, session='phrase-apply',
                text='创作者联络 SOP步骤：先冻结来源版本，再整理交付资料。')
    assert unit['decision'] == 'auto' and unit['project'] == 'projecta'
    assert '沿用' in unit['reason']


def test_condition_without_spaces_matches_a_spaced_body(service, monkeypatch):
    """Stored "创作者联络SOP" must still apply to "创作者 联络 sop步骤"."""
    _record(service, condition='创作者联络SOP')
    unit = _run(service, monkeypatch, session='phrase-compact-condition',
                text='创作者 联络 sop步骤：先冻结来源版本，再整理交付资料。')
    assert unit['decision'] == 'auto' and unit['project'] == 'projecta'
    assert '沿用' in unit['reason']


def test_condition_tokens_scattered_across_paragraphs_do_not_match(service, monkeypatch):
    """A first paragraph about 软件开发SOP plus a later 创作者 联络 example is none."""
    _record(service, condition='创作者 联络 SOP')
    text = ('软件开发SOP需要先写验收条件再立项。\n\n'
            '这段只把创作者 联络 当作一个例子，不涉及交付流程。')
    assert guidance.classify(service, {'text': text})['status'] == 'none'
    unit = _run(service, monkeypatch, session='phrase-scattered', text=text)
    assert unit['decision'] == 'review' and '沿用' not in unit['reason']
    assert unit['project'] == ''


def test_condition_phrase_never_crosses_a_line_break(service, monkeypatch):
    """A phrase split over two lines must not be glued back together."""
    _record(service, condition='创作者 联络 SOP')
    text = '第一段说明创作者\n第二段以 联络 SOP 结尾。'
    assert guidance.classify(service, {'text': text})['status'] == 'none'
    unit = _run(service, monkeypatch, session='phrase-newline', text=text)
    assert unit['decision'] == 'review' and '沿用' not in unit['reason']


# ------------------------------------------------------- exception phrase, worker


def test_short_ascii_exception_does_not_degrade_to_ordinary_negation(service, monkeypatch):
    """例外 "不是AB" must not fire on the ordinary negation "不是对我的指令"."""
    _record(service, condition='创作者 联络 SOP', exceptions='不是AB')
    unit = _run(service, monkeypatch, session='phrase-not-a-negation',
                text='创作者联络 SOP说明：这不是对我的指令，只是一条示例。')
    assert unit['decision'] == 'auto' and unit['project'] == 'projecta'
    assert '例外' not in unit['reason']


def test_spaced_exception_body_still_blocks_reuse(service, monkeypatch):
    """Stored "不是AB" must block the body "这不是 ab 的流程"."""
    _record(service, condition='创作者 联络 SOP', exceptions='不是AB')
    unit = _run(service, monkeypatch, session='phrase-spaced-exception',
                text='创作者联络 SOP说明：这不是 ab 的流程。')
    assert unit['decision'] == 'review' and unit['project'] == ''
    assert '例外' in unit['reason']


def test_explicit_exception_phrase_still_blocks_reuse(service, monkeypatch):
    _record(service, condition='创作者 联络 SOP', exceptions='不是AB')
    unit = _run(service, monkeypatch, session='phrase-explicit-exception',
                text='创作者联络 SOP说明：这段资料不是AB项目的内容。')
    assert unit['decision'] == 'review' and unit['project'] == ''
    assert '例外' in unit['reason'] and '不是AB' in unit['reason']


def test_short_ascii_exception_matches_only_a_standalone_word(service, monkeypatch):
    _record(service, condition='创作者 联络 SOP', exceptions='AB')
    applied = _run(service, monkeypatch, session='phrase-ab-embedded',
                   text='创作者联络 SOP说明：about 平台按常规流程走。')
    assert applied['decision'] == 'auto' and applied['project'] == 'projecta'
    blocked = _run(service, monkeypatch, session='phrase-ab-standalone',
                   text='创作者联络 SOP说明：AB 项目另行处理。')
    assert blocked['decision'] == 'review' and '例外' in blocked['reason']


# ------------------------------------------------- English platforms, unchanged


def test_ascii_exception_keeps_word_boundaries_inside_a_unit(service, monkeypatch):
    """A real English word around the phrase is not chopped by compaction."""
    _record(service, condition='创作者 联络 SOP', exceptions='AlphaShop')
    spaced = _run(service, monkeypatch, session='phrase-ascii-words',
                  text='创作者 联络 sop说明：this is an alphashop document，按常规流程处理。')
    assert spaced['decision'] == 'review' and spaced['project'] == ''
    assert '例外' in spaced['reason']
    longer = _run(service, monkeypatch, session='phrase-ascii-longer-word',
                  text='创作者 联络 sop说明：this is an alphashopping document，按常规流程处理。')
    assert longer['decision'] == 'auto' and longer['project'] == 'projecta'


def test_english_platform_exceptions_keep_any_hit_and_case_tolerance(service, monkeypatch):
    _record(service, condition='讨论 导出时', exceptions=' Alpha Shop |BetaShop ')
    alpha = _run(service, monkeypatch, session='phrase-platform-alpha', marker='讨论',
                 text='讨论 导出时必须保留来源版本，ALPHASHOP 平台沿用旧流程。')
    assert alpha['decision'] == 'review' and alpha['project'] == ''
    assert 'Alpha Shop' in alpha['reason']
    beta = _run(service, monkeypatch, session='phrase-platform-beta', marker='讨论',
                text='讨论 导出时必须保留来源版本，BETASHOP 平台需要单独核对。')
    assert beta['decision'] == 'review' and 'BetaShop' in beta['reason']
    other = _run(service, monkeypatch, session='phrase-platform-gamma', marker='讨论',
                 text='讨论 导出时必须保留来源版本，GammaShop 平台按常规流程。')
    assert other['decision'] == 'auto' and other['project'] == 'projecta'


def test_generic_condition_still_pends_instead_of_applying(service):
    _record(service, condition='资料')
    result = guidance.classify(service, {'text': '这是另一个项目的资料，需要整理。'})
    assert result['status'] == 'pending' and 'suggestion' in result

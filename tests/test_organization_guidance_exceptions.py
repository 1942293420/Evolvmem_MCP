"""Guidance exceptions: several items in one stored field, any hit blocks reuse.

Synthetic project/platform names only (ProjectA/ProjectB, AlphaShop/BetaShop).
Every check runs through the real organization worker, not only the string
helper, so the stored guidance is exercised end to end.
"""
import pytest

from tests.conftest import temp_dir, test_config  # noqa: F401  (fixture chain)
from tests.test_history_qa_memory import service as base_service
from tests.test_auto_organization_extraction import org, _task_for  # noqa: F401
from tests.unit_model_fixture import many_topic_archive, model_for, run_worker
from evolvmem import organization_guidance as guidance


@pytest.fixture
def service(base_service):
    for project, name in (('projecta', 'ProjectA'), ('projectb', 'ProjectB')):
        base_service.knowledge().save_project({'project': project, 'display_name': name})
    return base_service


def _record(service, *, exceptions):
    with service.store.transaction():
        return guidance.record(service, guidance='讨论导出时归 ProjectA', scope='future',
                               project='projecta', condition='讨论导出时', exceptions=exceptions)


def _run(service, monkeypatch, *, session, text):
    src = many_topic_archive(service, session=session, text=text)
    tid = _task_for(service, src)
    run_worker(service, monkeypatch, model_for(service, projects={'ProjectA', 'ProjectB'}))
    units = org(service, '/units', {'task_id': tid})['items']
    return next(u for u in units if '讨论导出时' in u['text'])


def test_exception_list_any_hit_forces_review_and_names_the_hit(service, monkeypatch):
    _record(service, exceptions='AlphaShop|BetaShop')
    unit = _run(service, monkeypatch, session='exc-alpha',
                text='讨论导出时必须保留来源版本，AlphaShop 平台沿用旧流程。')
    assert unit['decision'] == 'review' and unit['project'] == ''
    assert '例外' in unit['reason'] and 'AlphaShop' in unit['reason']


def test_exception_list_second_platform_also_blocks(service, monkeypatch):
    _record(service, exceptions='AlphaShop|BetaShop')
    unit = _run(service, monkeypatch, session='exc-beta',
                text='讨论导出时必须保留来源版本，BetaShop 平台需要单独核对。')
    assert unit['decision'] == 'review' and unit['project'] == ''
    assert 'BetaShop' in unit['reason']


def test_exception_list_non_matching_platform_still_applies(service, monkeypatch):
    _record(service, exceptions='AlphaShop|BetaShop')
    unit = _run(service, monkeypatch, session='exc-gamma',
                text='讨论导出时必须保留来源版本，GammaShop 平台按常规流程。')
    assert unit['decision'] == 'auto' and unit['project'] == 'projecta'


def test_newline_separated_exceptions_block(service, monkeypatch):
    _record(service, exceptions='AlphaShop\nBetaShop')
    unit = _run(service, monkeypatch, session='exc-newline',
                text='讨论导出时必须保留来源版本，BetaShop 平台需要单独核对。')
    assert unit['decision'] == 'review' and 'BetaShop' in unit['reason']


def test_exception_case_and_whitespace_are_normalized(service, monkeypatch):
    _record(service, exceptions='  AlphaShop  |  BetaShop  ')
    unit = _run(service, monkeypatch, session='exc-case',
                text='讨论导出时必须保留来源版本，ALPHASHOP 平台沿用旧流程。')
    assert unit['decision'] == 'review' and unit['project'] == ''
    assert 'AlphaShop' in unit['reason']


def test_empty_exception_segments_are_ignored(service, monkeypatch):
    _record(service, exceptions=' |  | \n ')
    unit = _run(service, monkeypatch, session='exc-empty',
                text='讨论导出时必须保留来源版本，AlphaShop 平台沿用旧流程。')
    assert unit['decision'] == 'auto' and unit['project'] == 'projecta'


def test_single_exception_value_stays_backward_compatible(service, monkeypatch):
    _record(service, exceptions='AlphaShop')
    blocked = _run(service, monkeypatch, session='exc-single-hit',
                   text='讨论导出时必须保留来源版本，AlphaShop 平台沿用旧流程。')
    assert blocked['decision'] == 'review' and 'AlphaShop' in blocked['reason']
    applied = _run(service, monkeypatch, session='exc-single-miss',
                   text='讨论导出时必须保留来源版本，项目按常规流程。')
    assert applied['decision'] == 'auto' and applied['project'] == 'projecta'


def test_exception_items_split_ignores_empty_segments():
    assert guidance.exception_items('AlphaShop|BetaShop') == ['AlphaShop', 'BetaShop']
    assert guidance.exception_items('AlphaShop\nBetaShop') == ['AlphaShop', 'BetaShop']
    assert guidance.exception_items('  | \n ') == []
    assert guidance.exception_items('') == []

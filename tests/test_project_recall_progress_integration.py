"""The public project recall must include current, dated task progress."""
import pytest
from evolvmem.project_mention_recall import recall_mentioned_projects, detect_mentioned_projects
from tests.test_project_progress_recall import env  # real isolated store fixture


@pytest.mark.parametrize('name', ['记忆\u3000插件', 'a\u00a0i'])
def test_nonboundary_unicode_whitespace_keeps_literal_project_name(name):
    matches = detect_mentioned_projects(name+' 的更新', projects=(name,))
    assert [match.project for match in matches] == [name]


def test_public_recall_keeps_newest_checkpoint_and_original_query(env):
    started = env.begin('ai_purchase', '统一采购应用身份')
    completed = env.checkpoint(
        started, project='ai_purchase', action='complete',
        expected_focus_revision=started.focus_revision,
        completed_steps=('采购流程已统一使用采购应用，切换验证完成',),
    )
    with env.store.transaction():
        env.store._connection().execute(
            "INSERT INTO context_project_aliases(alias,project,created_at,updated_at) "
            "VALUES ('AI采购','ai_purchase','2026-09-21','2026-09-21')"
        )
        env.store._connection().execute(
            "UPDATE context_items SET created_at='2026-09-18 13:21:38' WHERE id=?",
            (completed.context_id,),
        )
    result = recall_mentioned_projects(
        env.service,
        query='查询一下 我的AI 采购项目最后一次更新是啥时候 更新了啥',
        max_chars=4000,
    )
    assert result.matched_projects == ('ai_purchase',)
    assert completed.context_id in result.selected_ids
    assert started.context_id not in result.selected_ids
    assert '2026-09-18 13:21:38' in result.block
    assert '采购流程已统一使用采购应用' in result.block
    assert '不代表 Git 提交' in result.block
    assert result.used_chars == len(result.block) <= 4000


def test_public_recall_shares_one_budget_and_does_not_change_focus(env):
    env.begin('alpha', '最新进展甲', completed_steps=('甲已完成' * 150,))
    env.begin('beta', '最新进展乙', workspace='second')
    env.begin('unmentioned', '无关秘密项目', workspace='third')
    conn = env.store._connection()
    before = [tuple(row) for row in conn.execute('SELECT * FROM continuity_focus ORDER BY project')]
    for budget in (220, 600, 1200, 4000):
        result = recall_mentioned_projects(env.service, query='对比 alpha 和 beta 的更新', max_chars=budget)
        assert result.used_chars == len(result.block) <= budget
        assert result.matched_projects == ('alpha', 'beta')
        assert '无关秘密项目' not in result.block
    assert '最新进展甲' in result.block and '最新进展乙' in result.block
    assert before == [tuple(row) for row in conn.execute('SELECT * FROM continuity_focus ORDER BY project')]

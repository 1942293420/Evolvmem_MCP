"""Exercise the actual JSON stdin/stdout hook contract without a model or store."""
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / 'scripts/linux/codex-memory-intent.py'


def run_hook(prompt, **fields):
    event = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'fixture-session',
             'turn_id': 'fixture-turn', 'prompt': prompt, **fields}
    process = subprocess.run([sys.executable, '-B', str(HOOK)],
                             input=json.dumps(event, ensure_ascii=False).encode(),
                             capture_output=True, check=True)
    assert not process.stderr
    return json.loads(process.stdout)


@pytest.mark.parametrize(('prompt', 'scope'), [
    ('请记住：SYNTHETIC-DO-NOT-ECHO', 'experience'),
    ('这次已核对。记住：月底跨月费用按入账日核对。', 'experience'),
    ('你以后都按同一口径核算。', 'experience'),
    ('以后你不要上传密钥。', 'experience'),
    ('写入全局.md：以后核对实际账号。', 'global_rule'),
    ('请把这条写入项目.md', 'project_rule'),
    ('请写入 `项目.md`：保留原始单号。', 'project_rule'),
])
def test_candidate_event_emits_only_a_hint(prompt, scope):
    output = run_hook(prompt)
    assert set(output) == {'hookSpecificOutput'}
    assert output['hookSpecificOutput']['hookEventName'] == 'UserPromptSubmit'
    hint = output['hookSpecificOutput']['additionalContext']
    assert f'scope={scope}' in hint
    assert re.search(r'request_id=manual-hook-[a-f0-9]{64}', hint)
    assert 'manual_experience_save' in hint and 'tool confirms' in hint
    assert prompt not in hint
    if scope != 'experience':
        assert 'rule file' in hint and 'file only' in hint


@pytest.mark.parametrize('prompt', [
    '按现在的口径核对数据。',
    '你以后会怎么做？',
    '你以后需要做什么',
    '记住这个词什么意思？',
    '写入项目.md可以吗',
    '不要记住：这条规则',
    '以后不要记住这条',
    '“记住：这条规则”',
    '「\n记住：这条规则\n」',
    '> 记住：这条规则',
    '`记住：这条规则`',
    '```text\n记住：这条规则\n```',
    '~~~text\n记住：这条规则\n~~~',
    '    记住：这条规则',
    '例如：\n记住：这条规则',
    '<blockquote>\n记住：这条规则\n</blockquote>',
    '能不能通过类似\n记住\n写入项目.md\n你以后\n这些词触发？',
])
def test_ordinary_quoted_or_discussion_prompts_emit_nothing(prompt):
    assert run_hook(prompt) == {}


def test_unidentified_or_non_user_event_emits_nothing():
    prompt = '请记住：保留原始单号。'
    assert run_hook(prompt, session_id='') == {}
    assert run_hook(prompt, hook_event_name='Stop') == {}
    assert run_hook(prompt, hook_event_name=None) == {}


def test_request_id_is_stable_per_event_and_distinct_by_turn():
    prompt = '请记住：保留原始单号。'
    assert run_hook(prompt) == run_hook(prompt)
    assert run_hook(prompt) != run_hook(prompt, turn_id='next-turn')
    assert run_hook(prompt, turn_id='', prompt_id='alternate') != run_hook(prompt, turn_id='')
    assert run_hook(prompt, turn_id='') != run_hook(prompt, turn_id='', session_id='other-session')

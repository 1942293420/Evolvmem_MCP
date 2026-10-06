"""Deterministic provider for the numbered segmentation protocol and real extraction.

All data is synthetic. The provider speaks the same contracts the product uses:
the segmentation request returns record ids (never a copy of the source text),
and the extraction request returns the existing memory contract with a
SESSION_SUMMARY plus atomic knowledge (short question/answer, quote evidence).
"""
from __future__ import annotations

import json
import re
import time

ENVELOPE = re.compile(r'^\[(\d+)\] ([\w]+)：(.*)$', re.M)
EXTRACTION_LINE = re.compile(r'^\[(user|assistant|tool|unknown)\]: (.*)$', re.M)
TOPICS = [
    ('用户要求', 'evo', 'task_requirement'),
    ('中间这段', 'dsh', 'project_convention'),
    ('最后用户确认', '', 'habit'),
]
MIXED_TEXT = ('用户要求：Evo 演示项目先明确验收条件。\n'
              '中间这段是 DSH 项目导出的要求，不能只看开头结尾。\n'
              '最后用户确认：批量删除必须逐条确认。')


class UnitProvider:
    """Handles both provider contracts used by the organization worker."""

    def __init__(self, *, projects=('evo', 'dsh'), topics=None, summary=None,
                 answer_chars=200, fail_extraction=None, extraction=None):
        self.projects = set(projects)
        self.topics = topics or TOPICS
        self.summary = summary or '本次对话围绕整理与验收条件展开，供项目历史核对。'
        self.answer_chars = answer_chars
        self.fail_extraction = fail_extraction
        self.extraction = extraction
        self.calls = []
        self.extract_calls = []

    # -- segmentation --

    def segment(self, prompt):
        records = [(int(pos), role, content) for pos, role, content in ENVELOPE.findall(prompt)]
        if not records:
            raise AssertionError('segmentation prompt has no numbered records')
        units, current = [], None
        for pos, role, content in records:
            marker = next(((text, hint, category) for text, hint, category in self.topics
                           if text in content), None)
            if marker is not None:
                if current:
                    units.append(current)
                current = {'start_id': pos, 'end_id': pos, 'title': content[:40],
                           'cleaned_summary': content[:180], 'category': marker[2],
                           'project_hint': marker[1] if marker[1] in self.projects else '',
                           'evidence_quote': marker[0], 'disposition': 'keep',
                           'disposition_reason': ''}
            elif current is None:
                current = {'start_id': pos, 'end_id': pos, 'title': content[:40],
                           'cleaned_summary': content[:180], 'category': 'reference',
                           'project_hint': '', 'evidence_quote': '', 'disposition': 'keep',
                           'disposition_reason': ''}
            else:
                current['end_id'] = pos
        if current:
            units.append(current)
        return json.dumps({'units': units}, ensure_ascii=False)

    # -- extraction --

    def extract(self, prompt):
        if self.fail_extraction:
            raise RuntimeError(self.fail_extraction)
        lines = [(role, content.strip()) for role, content in EXTRACTION_LINE.findall(prompt)]
        user_lines = [content for role, content in lines if role == 'user']
        tool_lines = [content for role, content in lines if role == 'tool']
        source = (user_lines or [content for _, content in lines] or [''])[0]
        value = source[:self.answer_chars].strip() or '资料内容不足，保留待核对。'
        if len(value) < 10:
            value = (value + '：这是一段需要整理的业务资料。')[:self.answer_chars]
        learning = {'category': 'task_requirement', 'basis': 'explicit', 'quote': value,
                    'question': '这段对话明确了什么要求？', 'answer': value, 'trigger': '',
                    'rationale': '提炼自本次对话', 'topic': 'auto', 'instruction': '按对话内容执行'}
        memories = [{'key': 'SESSION_SUMMARY', 'value': self.summary},
                    {'key': 'project:x:request:auto', 'value': value, 'confidence': .9,
                     'attribute': 'constraint', 'learning': learning}]
        if tool_lines:
            proof = tool_lines[0][:self.answer_chars]
            case = {'project': 'x', 'problem': '工具结果说明了什么？', 'conditions': {},
                    'steps': ['在原始对话中核对工具结果'], 'rationale': '直接读取工具结果',
                    'result': proof, 'applicability': [], 'exclusions': [], 'transferable': False}
            memories.append({'key': 'project:x:experience:auto', 'value': proof, 'attribute': 'experience',
                             'confidence': .9, 'case': case,
                             'learning': {'category': 'experience', 'basis': 'explicit', 'quote': proof,
                                          'question': '工具结果验证了什么？', 'answer': proof,
                                          'trigger': '出现同类工具结果时', 'rationale': '来自工具结果',
                                          'topic': 'evidence'}})
        if self.extraction is not None:
            memories = self.extraction(prompt, memories) or memories
        return json.dumps({'memories': memories}, ensure_ascii=False)

    def __call__(self, prompt, *args, **kwargs):
        self.calls.append(prompt)
        if '整理分段助手' in prompt:
            return self.segment(prompt)
        if '编号消息' not in prompt and EXTRACTION_LINE.search(prompt):
            self.extract_calls.append(prompt)
            return self.extract(prompt)
        raise AssertionError('unexpected prompt: ' + prompt[:120])


def many_topic_archive(service, session='auto-session', text=None):
    """Archive one synthetic multi-topic source and confirm its cleaning draft."""
    from tests.test_history_qa_memory import archive
    from tests.test_knowledge_cleaning import ready
    source = archive(service, session=session, project='', text=text or MIXED_TEXT)
    ready(service)
    return source


def long_topic_archive(service, session='long-session'):
    """Confirm the FULL cleaning draft: the list preview is truncated on purpose."""
    from tests.test_history_qa_memory import archive
    from evolvmem.knowledge_api import dispatch
    head = '甲' * 200 + '开头要求：Evo 演示项目先明确验收条件。\n'
    middle = '中段要求：DSH 导出必须保留来源版本，并核对缓存。\n'
    tail = '收尾要求：批量删除必须逐条确认。\n' + '乙' * 14000
    source = archive(service, session=session, project='', text=head + middle + tail)
    full = dispatch(service, 'GET', 'cleaning/detail', {'key': f'archive:{source.id}'})
    dispatch(service, 'POST', 'cleaning/save', {'items': [
        {'key': full['key'], 'expected_revision': full['expected_revision'],
         'cleaned_text': full['body'], 'category': 'reference'}]})
    return source


def model_for(service, *, projects=None, topics=None, **kwargs):
    return UnitProvider(projects=projects or ('evo', 'dsh'), topics=topics, **kwargs)


def use_model(monkeypatch, model):
    from evolvmem import kimi_hooks
    monkeypatch.setattr(kimi_hooks, '_load_llm_config', lambda **kw: object())
    monkeypatch.setattr(kimi_hooks, '_call_llm_with_retry', model)


def run_worker(service, monkeypatch, model, *, timeout=15):
    from evolvmem.auto_organization import OrganizationWorker
    from evolvmem.context_models import ContextMode
    use_model(monkeypatch, model)
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    worker.start()
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            tasks = worker.tasks()
            if tasks and all(t['status'] in ('completed', 'review', 'failed', 'superseded') for t in tasks):
                break
            time.sleep(.05)
        else:
            raise AssertionError('organization worker did not settle: ' + str(worker.tasks()))
        return worker.tasks()
    finally:
        worker.stop()

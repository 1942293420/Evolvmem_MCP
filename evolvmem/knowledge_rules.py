"""One editable SKILL.md is the source of ingestion settings and AI guidance."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

DEFAULT_SETTINGS = {
    'auto_min_confidence': 0.8,
    'require_source': True,
    'min_chars': 10,
    'max_chars': 20000,
    'ignore_keywords': [],
    'ambiguous_project_names': ['设计', 'design', '测试', 'test', '项目', 'project', '系统', 'system', '平台', 'platform', '开发', 'development', '文档', 'docs', '采购'],
    'project_overrides': {},
}
DEFAULT_INSTRUCTIONS = """# 知识库入库与整理

明确资料自动入库，疑难资料待确认。

## 项目归属
- 从资料正文、来源会话、项目名称和别名核对业务项目。
- 主目录名、运行程序目录不代表业务项目；不要将 jiangli、home、general 当作项目。
- 路径中的程序目录和“设计、测试、平台”等通用词不独立构成业务归属；不确定时待确认。
- 优先尊重用户已确认的归属。资料同时涉及多个项目、线索冲突或仅有含糊指代时进入待确认。
- 知识 key 使用 project:<已登记项目>:<领域>:<主题>；全局用户偏好用 user: 前缀。

## 入库逻辑
- 保留可复用业务规则、决策及原因、参考资料、项目进展和有来源的经验。
- 普通知识符合下方条件且归属明确时自动入库；疑难资料保存到待确认队列，并说明原因。
- 跨项目资料按内容判断，可拆分时分别归属；不要为了减少待确认数量硬猜。
- 密钥、临时密码和访问凭据不进入资料正文。经验入库不等于经验已验证成功，沿用证据要求。
- 已有人工作出的决定不由自动整理覆盖。

## 知识用途与协作学习
- 项目归属与用途分别判断：长期习惯、项目约定、任务要求、环境事实、决策依据、技术经验、参考资料。不要因内容类型是偏好就扩大到全局。
- 提炼保留用户原话、来源、适用条件及纠正原因；明确表达与 AI 推断分开。一次性任务要求不成为永久协作规则。
- 协作框架与学习成果使用 GET learning；实际项目适用 Skill 使用 GET learning/skill 并传 project，或 MCP collaboration_recall。
- 明确原话、来源可核对且没有冲突的规则自动更新；归纳、冲突和范围扩大待确认。用户否决及人工编辑优先，来源被纠正后旧衍生规则停止使用。
- POST learning/analyze 按 project 或 family 分析记忆；POST learning/family 设置项目类型。分类修正使用 POST learning/memories/ID，包含 expected_revision、category、trigger。
- 协作框架编辑使用 POST learning/framework（expected_revision、framework）；规则确认或否决使用 POST learning/rules/ID（expected_revision、action）。替换已有同主题规则需明确 replace_conflicts。版本比较与恢复在知识库原位操作。

## 管理操作
- 先读取当前规则，再查询项目与资料，核对来源后执行用户要求的操作。
- 编辑和改归属使用返回的 revision；版本冲突时重新读取，不覆盖新内容。
- 批量整理先查看建议与原因。已明确的资料按规则自动处理，疑难资料保留待确认。
- 可通过 evolvmem.knowledge_cli 读取/保存规则、查询、编辑、入库或整理；Web 与 CLI 使用同一服务核心。

## 本地操作入口

在 EvolvMem 源码目录使用项目 Python 环境执行；先运行 `python -m evolvmem.knowledge_cli GET rules` 读取当前规则。
- 查询项目：`python -m evolvmem.knowledge_cli GET projects`
- 查询资料：`python -m evolvmem.knowledge_cli GET items --json '{"project":"evolvmem","q":"入库"}'`
- 读取详情：`python -m evolvmem.knowledge_cli GET items/123`
- 写入使用 UTF-8 JSON 文件：`python -m evolvmem.knowledge_cli POST items/123/assign --file request.json`
- 改归属请求包含 `project` 和详情返回的 `expected_revision`；全局知识使用空 project。任务改归属还需 `move_workstream: true`，由用户确认整组迁移。
- 新增：POST items，字段 title、body、project、source、content_type，action 为 auto（按规则）或 draft（待确认）。
- 编辑：POST items/ID/update，字段 expected_revision、title、body、tags。系统生成内容通过新增补充资料更正。
- 入库/归档：POST items/ID/transition，字段 expected_revision、action（publish、archive、restore、reject、delete）。
- 整理预览：POST organize，字段 ids（最多100个）；核对后同请求加 apply: true 处理明确资料。
- 保存规则：POST rules，字段 expected_revision 和 skill（完整文件），或 settings 与 instructions；先获取版本，再保存。
- 所有写入仅限用户当前授权。经验确认入库不构成成功证据，不得据此记录验证成功。
"""
_FRONT = '---\nname: evolvmem-knowledge-manager\ndescription: 管理 EvolvMem 项目知识库、资料归属和入库；编辑知识资料时先读取当前入库规则。\n---\n\n'
_SETTINGS = re.compile(r'```json\s*\n(.*?)\n```', re.S)


def render_skill(settings, instructions):
    return _FRONT + instructions.strip() + '\n\n## 可执行入库条件\n\n```json\n' + json.dumps(settings, ensure_ascii=False, indent=2) + '\n```\n'


def validate_settings(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_SETTINGS):
        raise ValueError('invalid_rule_settings')
    settings = {**DEFAULT_SETTINGS, **value}
    if type(settings['auto_min_confidence']) not in (float, int) or not 0 <= settings['auto_min_confidence'] <= 1:
        raise ValueError('invalid_confidence')
    if type(settings['require_source']) is not bool:
        raise ValueError('invalid_require_source')
    if any(type(settings[k]) is not int for k in ('min_chars', 'max_chars')) or not 1 <= settings['min_chars'] <= settings['max_chars'] <= 100000:
        raise ValueError('invalid_content_limits')
    for name in ('ignore_keywords', 'ambiguous_project_names'):
        if not isinstance(settings[name], list) or any(not isinstance(x, str) or not x.strip() or len(x) > 200 for x in settings[name]):
            raise ValueError('invalid_' + name)
    if not isinstance(settings['project_overrides'], dict):
        raise ValueError('invalid_project_overrides')
    for project, overrides in settings['project_overrides'].items():
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', project) or not isinstance(overrides, dict) or 'project_overrides' in overrides:
            raise ValueError('invalid_project_overrides')
        validate_settings({**settings, **overrides, 'project_overrides': {}})
    return settings


class KnowledgeRules:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'knowledge-management' / 'SKILL.md'

    def read(self):
        skill = self.path.read_text() if self.path.exists() else render_skill(DEFAULT_SETTINGS, DEFAULT_INSTRUCTIONS)
        match = _SETTINGS.search(skill)
        if not skill.startswith('---\n') or not match or len(skill) > 60000:
            raise ValueError('invalid_skill_format')
        settings = validate_settings(json.loads(match.group(1)))
        body = skill.split('---', 2)[-1].strip()
        instructions = _SETTINGS.sub('', body).replace('## 可执行入库条件', '').strip()
        return {'skill': skill, 'settings': settings, 'instructions': instructions,
                'revision': hashlib.sha256(skill.encode()).hexdigest(), 'saved': self.path.exists()}

    def save(self, body):
        current = self.read()
        if body.get('expected_revision') != current['revision']:
            raise ValueError('revision_conflict')
        if body.get('reset'):
            skill = render_skill(DEFAULT_SETTINGS, DEFAULT_INSTRUCTIONS)
        elif 'skill' in body:
            skill = body['skill']
            if not isinstance(skill, str) or len(skill) > 60000 or not skill.startswith('---\n'):
                raise ValueError('invalid_skill_format')
            front = skill.split('---', 2)
            if len(front) != 3 or 'name: evolvmem-knowledge-manager' not in front[1] or 'description:' not in front[1]:
                raise ValueError('invalid_skill_frontmatter')
            match = _SETTINGS.search(skill)
            if not match:
                raise ValueError('missing_rule_settings')
            validate_settings(json.loads(match.group(1)))
        else:
            settings = validate_settings(body.get('settings', current['settings']))
            instructions = body.get('instructions', current['instructions'])
            if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 50000:
                raise ValueError('invalid_instructions')
            skill = render_skill(settings, instructions)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Same lock as writers in the application; this file is the only source,
        # so updating prose and executable conditions cannot partially succeed.
        import fcntl
        with (self.path.parent / '.rules.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.read()['revision'] != current['revision']:
                raise ValueError('revision_conflict')
            fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix='.skill-')
            try:
                with os.fdopen(fd, 'w') as stream:
                    stream.write(skill)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        return self.read()

    def prompt(self):
        return '\n\n当前用户维护的知识库入库规则（适用于本次资料提炼与归属）：\n' + self.read()['skill']

    def evaluate(self, sample, registry):
        policy = self.read()
        text = str(sample.get('title', '')) + '\n' + str(sample.get('body', ''))
        # Runtime paths and URLs often mention an unrelated hosting project.
        # Keep the original content for quality checks, exclude paths only
        # from project-name matching.
        project_text = re.sub(r'(?:https?://|(?:~|[A-Za-z]:)?[/\\]|[A-Za-z0-9_.~-]+[/\\])[^\s`，。；、）)]+', ' ', text)
        project_text = re.sub(r'(?<![\w])[.][A-Za-z][A-Za-z0-9_-]*', ' ', project_text)
        ambiguous = {name.casefold() for name in policy['settings']['ambiguous_project_names']}
        canonical = {r['project'] for r in registry}
        names = {}
        for row in registry:
            for name in (row['project'], row.get('display_name', ''), *row.get('aliases', [])):
                if len(name.strip()) >= 2 and name.casefold() not in ambiguous:
                    names.setdefault(name.casefold(), set()).add(row['project'])
        matches = set()
        for name, projects in names.items():
            pattern = re.escape(name)
            if name.isascii():
                pattern = r'(?<![a-z0-9_-])' + pattern + r'(?![a-z0-9_-])'
            if re.search(pattern, project_text, re.I):
                matches.update(projects)
        explicit = str(sample.get('project') or '')
        key = str(sample.get('key') or '')
        segments = key.split(':')
        key_project = segments[1] if len(segments) > 2 and segments[0] == 'project' else segments[0]
        if explicit in canonical:
            matches.add(explicit)
        elif key_project in canonical:
            matches.add(key_project)
        global_scope = sample.get('scope') == 'global' or key.startswith('user:')
        project = '' if global_scope or len(matches) != 1 else next(iter(matches))
        settings = {**policy['settings'], **policy['settings']['project_overrides'].get(project, {})}
        confidence = sample.get('confidence', .9)
        if type(confidence) not in (float, int) or not 0 <= confidence <= 1:
            raise ValueError('invalid_confidence')
        from evolvmem.extraction_policy import contains_sensitive_text
        action, reason = 'auto', '明确资料，符合入库规则'
        if contains_sensitive_text(text):
            action, reason = 'ignore', '内容含有凭据信息'
        elif any(word.casefold() in text.casefold() for word in settings['ignore_keywords']):
            action, reason = 'ignore', '匹配忽略关键词'
        elif not settings['min_chars'] <= len(str(sample.get('body', ''))) <= settings['max_chars']:
            action, reason = 'review', '内容长度不符合入库条件'
        elif any(marker in text for marker in ('另回答', '另讨论', '跨项目', '多个项目', '同时涉及')):
            action, reason = 'review', '资料包含多个话题或项目，需要拆分或确认'
        elif not global_scope and len(matches) > 1:
            action, reason = 'review', '存在多个项目线索，需要确认归属'
        elif not global_scope and not project:
            action, reason = 'review', '缺少明确的业务项目线索'
        elif settings['require_source'] and not sample.get('source'):
            action, reason = 'review', '缺少可核对的来源'
        elif confidence < settings['auto_min_confidence']:
            action, reason = 'review', '置信度低于自动入库门槛'
        elif sample.get('content_type') in ('experience', 'playbook'):
            action, reason = 'review', '经验与操作方法需要证据确认'
        return {'action': action, 'reason': reason, 'project': project,
                'candidates': sorted(matches), 'confidence': confidence,
                'rule_revision': policy['revision'], 'scope': 'global' if global_scope else 'project'}

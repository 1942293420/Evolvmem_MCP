"""Evidence-linked memory learning and versioned collaboration skills."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from evolvmem.context_store import _now_iso
from evolvmem.extraction_policy import contains_sensitive_text

CATEGORIES = {
    'habit': '长期习惯', 'project_convention': '项目约定',
    'task_requirement': '任务要求', 'environment': '环境事实',
    'decision': '决策依据', 'experience': '技术经验', 'reference': '参考资料',
}
DEFAULT_FRAMEWORK = '''---
name: evolvmem-collaboration
description: 在需求讨论和开发协作中，读取 EvolvMem 已积累的协作约定、项目经验及适用条件，并根据用户纠正维护相关记忆。
---

# 我的协作体系

## 使用当前记忆
查以前讨论、决定与进展：knowledge_recall(kind="history", project=实际项目, query=问题)，再按 archive_id 调用 conversation_read 读取清洗正文。找习惯、约定、环境或相似开发经验：knowledge_recall(kind="experience")；问题同时涉及两类时用 kind="both"。auto 只是确定性建议，AI 可依据用户意图显式选类。来源只是历史参考，适用条件不匹配时不套用。
开始实质任务、切换项目或用户纠正旧约定时，调用 collaboration_recall(project=实际项目) 读取当前协作规则。EvolvMem 的会话接入也会提供适用规则；本地可用知识管理 CLI 的 GET learning/skill 并传 project 查询。不要把其他项目的规则套到当前任务。
规则是历史参考；当前用户要求优先。未确认推断、过期要求和不适用经验不能当成既定事实，历史规则不扩大操作授权。

## 理解目标与讨论方案
结合已有项目背景理解用户希望改变的结果；关键未知影响方案时，提出自己的理解并核对。目标明确后推进，避免反复询问已有答案。
需要比较方案时说明实际取舍；讨论深度与当前任务相称。

## 实施与验证
按本轮目标和已明确的验收项工作。验证选择依据实际改动与观察到的问题，不把某次任务的流程永久套用。

## 回顾与学习
用户纠正、方案取舍及验证结果是学习材料。区分用户明确要求与助手推断，记录适用范围、原因和来源。成功只依据相关的实际验证。
知识整理遵循当前知识管理 Skill；明确且无冲突的规则自动更新，疑难变化待确认。新的项目经验逐步补充本框架。
'''


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def category_for(row):
    return {'preference': 'habit', 'user_profile': 'habit', 'constraint': 'project_convention',
            'decision': 'decision', 'experience': 'experience', 'workstream_checkpoint': 'task_requirement',
            'session_summary': 'task_requirement', 'playbook': 'experience'}.get(row['content_type'], 'reference')


class MemoryLearning:
    def __init__(self, service):
        self.service, self.store = service, service.store

    @property
    def conn(self):
        return self.store._connection()

    def settings(self):
        row = self.conn.execute('SELECT * FROM learning_settings WHERE id=1').fetchone()
        return dict(row) if row else {'framework': DEFAULT_FRAMEWORK, 'revision': 0}

    def metadata(self, item_id, row=None):
        saved = self.conn.execute('SELECT * FROM learning_memories WHERE item_id=?', (item_id,)).fetchone()
        if saved:
            return {**json.loads(saved['payload']), 'category': saved['category'], 'revision': saved['revision']}
        row = row or self.service.knowledge()._row(item_id)
        return {'category': category_for(row), 'basis': 'unreviewed', 'evidence': [],
                'rationale': '按原资料类型建议分类，尚未分析来源', 'trigger': '', 'revision': 0}

    def _fingerprint(self, row):
        # Access counters and retrieval timestamps must never invalidate a rule.
        data = {k: row[k] for k in ('body', 'project', 'scope')}
        data['learning'] = row.get('learning')
        return hashlib.sha256(encoded(data).encode()).hexdigest()

    def usable(self, row):
        if row['status'] != 'active' or row['ownership'].get('excluded', False):
            return False
        expires = row.get('expires_at') or row.get('effective_until')
        if expires:
            try:
                deadline = datetime.fromisoformat(expires.replace('Z','+00:00'))
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
                if deadline <= datetime.now(timezone.utc):
                    return False
            except ValueError:
                return False
        return True

    def capture(self, item_id, metadata, *, messages=(), source_session='', archive_id=None):
        if not isinstance(metadata, dict):
            return
        row = self.service.knowledge().detail(item_id)
        previous = self.metadata(item_id, row)
        if previous.get('basis') == 'manual':
            return
        category = metadata.get('category', category_for(row))
        if category not in CATEGORIES:
            category = category_for(row)
        quote = str(metadata.get('quote') or '').strip()[:3000]
        evidence = []
        if quote and not contains_sensitive_text(quote):
            for index, message in enumerate(messages):
                if quote in str(message.get('content', '')):
                    evidence.append({'quote': quote, 'role': message.get('role', 'unknown'),
                        'message_index': index, 'session': source_session, 'archive_id': archive_id})
                    if message.get('role') == 'user':
                        evidence = [evidence[-1]]
                        break
        basis = 'explicit' if metadata.get('basis') == 'explicit' and any(e['role'] == 'user' for e in evidence) else 'inferred'
        payload = {'basis': basis, 'evidence': evidence, 'trigger': str(metadata.get('trigger') or '')[:500],
                   'rationale': str(metadata.get('rationale') or '')[:1500], 'source_session': source_session}
        from evolvmem.learning_extraction import process_evidence
        payload['process'], errors = process_evidence(metadata.get('process'), messages)
        payload['process_errors'] = metadata.get('process_errors', errors)
        from evolvmem.learning_extraction import normalization
        normalized, normalization_errors = normalization(metadata, messages, row['body'])
        if normalized is not None or normalization_errors:
            payload['normalization'] = normalized
            payload['normalization_errors'] = normalization_errors
        for field in ('relation', 'intake', 'rule_revision'):
            if field in metadata:
                payload[field] = metadata[field]
        with self.store.transaction():
            self.conn.execute('INSERT INTO learning_memories(item_id,category,payload) VALUES(?,?,?) '
                'ON CONFLICT(item_id) DO UPDATE SET category=excluded.category,payload=excluded.payload,revision=revision+1',
                (item_id, category, encoded(payload)))
            # A preference about a named project is not automatically a global preference.
            if row['project']:
                self.conn.execute("UPDATE context_items SET scope='project' WHERE id=?", (item_id,))
            if category == 'task_requirement':
                self.conn.execute("UPDATE context_items SET tier='normal' WHERE id=?", (item_id,))
            from evolvmem.qa_memory import record
            record(self.service, item_id, metadata)
            instruction = str(metadata.get('instruction') or '').strip()
            if not instruction or category in ('task_requirement', 'environment', 'reference', 'experience'):
                return
            try:
                self.propose({'topic': metadata.get('topic') or row['identity_key'], 'instruction': instruction,
                    'trigger': payload['trigger'], 'rationale': payload['rationale'],
                    'scope': 'project' if row['project'] else 'global', 'target': row['project'],
                    'source_ids': [item_id], 'exceptions': metadata.get('exceptions', '')},
                    explicit=basis == 'explicit' and instruction == quote and metadata.get('auto_explicit_rules', True), origin='extraction')
            except ValueError:
                # Bad model rule fields must not discard otherwise valid memories.
                payload['rule_error'] = 'invalid_learning_rule'
                self.conn.execute('UPDATE learning_memories SET payload=? WHERE item_id=?', (encoded(payload), item_id))

    def classify(self, item_id, body):
        kb = self.service.knowledge()
        with self.store.transaction():
            row = kb._check(item_id, body)
            category = body.get('category')
            if category not in CATEGORIES:
                raise ValueError('invalid_learning_category')
            payload = {**row['learning'], 'basis': 'manual', 'rationale': str(body.get('rationale') or '用户修正分类'),
                       'trigger': str(body.get('trigger', row['learning'].get('trigger', '')))[:500]}
            payload.pop('category', None)
            payload.pop('revision', None)
            self.conn.execute('INSERT INTO learning_memories(item_id,category,payload) VALUES(?,?,?) '
                'ON CONFLICT(item_id) DO UPDATE SET category=excluded.category,payload=excluded.payload,revision=revision+1',
                (item_id, category, encoded(payload)))
            if row['project']:
                self.conn.execute("UPDATE context_items SET scope='project' WHERE id=?", (item_id,))
        kb._sync([item_id])
        return kb.detail(item_id)

    def families(self):
        return [dict(r) for r in self.conn.execute('SELECT project,family FROM learning_project_types ORDER BY project')]

    def set_family(self, body):
        project, family = str(body.get('project') or ''), str(body.get('family') or '').strip()
        self.service.knowledge()._project(project)
        if not project or len(family) > 80:
            raise ValueError('invalid_project_type')
        with self.store.transaction():
            self.conn.execute('INSERT INTO learning_project_types VALUES(?,?) ON CONFLICT(project) DO UPDATE SET family=excluded.family', (project, family))
        return {'project': project, 'family': family}

    def _source_rows(self, source_ids):
        if not isinstance(source_ids, list) or not 1 <= len(source_ids) <= 30 or any(type(i) is not int for i in source_ids):
            raise ValueError('learning_sources_required')
        return [self.service.knowledge().detail(i) for i in dict.fromkeys(source_ids)]

    def _in_scope(self, row, scope, target):
        if scope == 'global':
            return row['scope'] == 'global' and not row['project']
        if scope == 'project':
            return row['project'] == target
        return row['project'] in [r['project'] for r in self.families() if r['family'] == target]

    def propose(self, data, *, explicit=False, origin='analysis'):
        instruction = str(data.get('instruction') or '').strip()
        if not 5 <= len(instruction) <= 2000 or contains_sensitive_text(instruction):
            raise ValueError('invalid_learning_rule')
        scope, target = data.get('scope', 'project'), str(data.get('target') or '')
        if scope not in ('global', 'project', 'family') or (scope != 'global' and not target):
            raise ValueError('invalid_learning_scope')
        rows = self._source_rows(data.get('source_ids'))
        if any(not self._in_scope(row, scope, target) for row in rows):
            raise ValueError('learning_source_scope_mismatch')
        topic = str(data.get('topic') or 'collaboration')[:150]
        trigger = str(data.get('trigger') or '')[:500]
        exceptions = str(data.get('exceptions') or '')[:1000]
        fingerprint = hashlib.sha256(encoded([scope, target, topic, instruction, trigger, exceptions]).encode()).hexdigest()
        # Match the semantic fields as well as supporting pre-P1 fingerprints.
        previous = self.conn.execute('SELECT * FROM learning_rules WHERE scope=? AND target=? AND topic=? AND instruction=? AND trigger_text=? AND exceptions=?',
                                     (scope, target, topic, instruction, trigger, exceptions)).fetchone()
        if previous:
            return self.rule(previous['id'])
        reason = '归纳或范围变化，需要确认'
        eligible = explicit and all(self.usable(row) for row in rows)
        if re.search(r'这次|本次|当前会话|暂时|仅此|本规则待确认|可能|是否|[？?]', instruction):
            eligible, reason = False, '临时、疑问或不确定表达，需要确认'
        if scope == 'global' and not re.search(r'长期|所有项目|以后|始终|默认', instruction):
            eligible, reason = False, '缺少长期或通用适用范围的明确依据'
        conflict = any(self.rule(r[0])['effective'] for r in self.conn.execute(
            "SELECT id FROM learning_rules WHERE scope=? AND target=? AND topic=? AND status='active'", (scope, target, topic)).fetchall())
        if conflict:
            eligible, reason = False, '同一主题已有生效规则，需核对冲突'
        sources = [{'id': row['id'], 'fingerprint': self._fingerprint(row)} for row in rows]
        with self.store.transaction():
            now = _now_iso()
            cur = self.conn.execute('INSERT INTO learning_rules(fingerprint,topic,instruction,trigger_text,rationale,exceptions,scope,target,sources,status,origin,reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (fingerprint, topic, instruction, trigger, str(data.get('rationale') or '')[:1500],
                 exceptions, scope, target, encoded(sources), 'active' if eligible else 'candidate', origin,
                 '用户明确原话且适用范围清楚' if eligible else reason, now, now))
            rule_id = cur.lastrowid
            self._snapshot('明确规则自动更新' if eligible else '新增待确认学习结果')
        return self.rule(rule_id)

    def rule(self, rule_id):
        saved = self.conn.execute('SELECT * FROM learning_rules WHERE id=?', (rule_id,)).fetchone()
        if saved is None:
            raise ValueError('learning_rule_not_found')
        row = dict(saved)
        row['sources'] = json.loads(row['sources'])
        row['trigger'] = row.pop('trigger_text')
        effective, reason = row['status'] == 'active', row['reason']
        details = []
        for source in row['sources']:
            try:
                item = self.service.knowledge().detail(source['id'])
                details.append({'id': item['id'], 'title': item['title'], 'project': item['project'], 'learning': item['learning']})
                if not self.usable(item):
                    effective, reason = False, '来源尚未确认、已过期、已归档或已被替代'
                elif self._fingerprint(item) != source['fingerprint']:
                    effective, reason = False, '来源已被纠正，请重新分析或确认修订'
                elif not self._in_scope(item, row['scope'], row['target']):
                    effective, reason = False, '项目归属或类型已改变'
            except ValueError:
                effective, reason = False, '来源已移除'
        conflicts = [r[0] for r in self.conn.execute("SELECT id FROM learning_rules WHERE scope=? AND target=? AND topic=? AND status='active' AND id!=?", (row['scope'],row['target'],row['topic'],row['id']))]
        row.update(effective=effective, effective_reason=reason, source_details=details, conflicting_ids=conflicts)
        return row

    def _snapshot(self, reason):
        settings = self.settings()
        rules = [dict(r) for r in self.conn.execute('SELECT * FROM learning_rules ORDER BY id')]
        self.conn.execute('INSERT INTO learning_versions(framework,rules,reason,created_at) VALUES(?,?,?,?)',
                          (settings['framework'], encoded(rules), reason, _now_iso()))
        self.conn.execute('INSERT INTO learning_settings(id,framework,revision) VALUES(1,?,1) '
                          'ON CONFLICT(id) DO UPDATE SET revision=revision+1', (settings['framework'],))

    def overview(self):
        settings = self.settings()
        rules = [self.rule(r[0]) for r in self.conn.execute('SELECT id FROM learning_rules ORDER BY id DESC')]
        return {**settings, 'categories': CATEGORIES, 'families': self.families(), 'rules': rules,
            'versions': [dict(r) for r in self.conn.execute('SELECT id,reason,created_at FROM learning_versions ORDER BY id DESC LIMIT 30')],
            'runs': [dict(r) for r in self.conn.execute('SELECT * FROM learning_runs ORDER BY id DESC LIMIT 10')],
            'counts': {'active': sum(r['effective'] for r in rules), 'candidate': sum(r['status'] == 'candidate' for r in rules),
                       'stale': sum(r['status'] == 'active' and not r['effective'] for r in rules)}}

    def review(self, rule_id, body):
        with self.store.transaction():
            row = self.rule(rule_id)
            if row['revision'] != body.get('expected_revision'):
                raise ValueError('revision_conflict')
            action = body.get('action')
            if action not in ('accept', 'reject'):
                raise ValueError('invalid_learning_action')
            instruction = str(body.get('instruction', row['instruction'])).strip()
            if not 5 <= len(instruction) <= 2000 or contains_sensitive_text(instruction):
                raise ValueError('invalid_learning_rule')
            sources = self._source_rows([s['id'] for s in row['sources']])
            if action == 'accept' and any(not self.usable(s) or not self._in_scope(s,row['scope'],row['target']) for s in sources):
                raise ValueError('learning_sources_not_ready')
            if action == 'accept':
                if row['conflicting_ids'] and not body.get('replace_conflicts'):
                    raise ValueError('learning_conflict_confirmation_required')
                self.conn.execute("UPDATE learning_rules SET status='superseded',revision=revision+1 WHERE scope=? AND target=? AND topic=? AND status='active' AND id!=?", (row['scope'],row['target'],row['topic'],rule_id))
            sources = [{'id': s['id'], 'fingerprint': self._fingerprint(s)} for s in sources]
            fingerprint = hashlib.sha256(encoded([row['scope'], row['target'], row['topic'], instruction, row['trigger'], row['exceptions']]).encode()).hexdigest()
            other = self.conn.execute('SELECT id FROM learning_rules WHERE scope=? AND target=? AND topic=? AND instruction=? AND trigger_text=? AND exceptions=? AND id!=?',
                (row['scope'], row['target'], row['topic'], instruction, row['trigger'], row['exceptions'], rule_id)).fetchone()
            if other:
                raise ValueError('learning_rule_duplicate')
            self.conn.execute('UPDATE learning_rules SET instruction=?,fingerprint=?,sources=?,status=?,origin=?,reason=?,revision=revision+1,updated_at=? WHERE id=?',
                (instruction,fingerprint,encoded(sources),'active' if action=='accept' else 'rejected','manual','用户确认修订' if action=='accept' else '用户否决',_now_iso(),rule_id))
            self._snapshot('确认规则修订' if action=='accept' else '否决学习结果')
        return self.rule(rule_id)

    def save_framework(self, body):
        framework = str(body.get('framework') or '').strip() + '\n'
        if len(framework) > 20000 or not framework.startswith('---\n') or 'name: evolvmem-collaboration' not in framework or 'description:' not in framework:
            raise ValueError('invalid_collaboration_skill')
        with self.store.transaction():
            if body.get('expected_revision') != self.settings()['revision']:
                raise ValueError('revision_conflict')
            if not self.conn.execute('SELECT 1 FROM learning_versions LIMIT 1').fetchone():
                self._snapshot('初始协作框架')
            self.conn.execute('UPDATE learning_settings SET framework=? WHERE id=1', (framework,))
            self._snapshot('编辑协作框架')
        self.export_framework()
        return self.overview()

    def export_framework(self):
        from pathlib import Path
        path = Path(self.service.config.data_dir) / 'collaboration' / 'SKILL.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(self.settings()['framework'], encoding='utf-8')
        temp.replace(path)
        return path

    def restore(self, body):
        with self.store.transaction():
            if body.get('expected_revision') != self.settings()['revision']:
                raise ValueError('revision_conflict')
            old = self.conn.execute('SELECT * FROM learning_versions WHERE id=?', (body.get('version_id'),)).fetchone()
            if old is None:
                raise ValueError('learning_version_not_found')
            self.conn.execute("UPDATE learning_rules SET status='rejected',origin='manual',revision=revision+1")
            for rule in json.loads(old['rules']):
                self.conn.execute("UPDATE learning_rules SET fingerprint='restored-duplicate:'||id WHERE fingerprint=? AND id!=?", (rule['fingerprint'],rule['id']))
                self.conn.execute('UPDATE learning_rules SET instruction=?,fingerprint=?,sources=?,status=?,origin=?,reason=?,revision=revision+1 WHERE id=?',
                    (rule['instruction'],rule['fingerprint'],rule['sources'],rule['status'],'manual','从历史版本恢复',rule['id']))
            self.conn.execute('UPDATE learning_settings SET framework=? WHERE id=1', (old['framework'],))
            self._snapshot('恢复版本 ' + str(old['id']))
        self.export_framework()
        return self.overview()

    def version(self, version_id):
        saved = self.conn.execute('SELECT * FROM learning_versions WHERE id=?', (version_id,)).fetchone()
        if saved is None:
            raise ValueError('learning_version_not_found')
        row = dict(saved)
        row['rules'] = json.loads(row['rules'])
        return row

    def skill(self, project='', *, max_chars=12000):
        family = next((x['family'] for x in self.families() if x['project']==project), '')
        settings = self.settings()
        applicable = []
        for saved in self.conn.execute("SELECT id FROM learning_rules WHERE status='active' AND (scope='global' OR (scope='project' AND target=?) OR (scope='family' AND target=? AND target!='')) ORDER BY CASE scope WHEN 'project' THEN 0 WHEN 'family' THEN 1 ELSE 2 END,id DESC", (project,family)):
            rule = self.rule(saved['id'])
            if rule['effective']:
                applicable.append(rule)
        text = settings['framework'] + '\n## 已学习的适用规则\n'
        included = []
        for r in applicable:
            part = f"\n- [规则 #{r['id']} · {r['scope']}:{r['target'] or '通用'}] {r['instruction']}\n  适用：{r['trigger'] or '遵循来源范围'}；例外：{r['exceptions'] or '无额外声明'}；依据：" + ', '.join('#'+str(s['id']) for s in r['sources']) + '\n'
            if len(text) + len(part) > max_chars:
                continue
            text += part
            included.append(r['id'])
        from evolvmem.memory_recall import recall
        qa_ids = []
        try:
            knowledge = recall(self.service, {'project':project, 'query':'开发协作习惯约定', 'kind':'experience',
                                              'max_chars':max(1, max_chars-len(text))})
        except ValueError:
            knowledge = {'qa':[]}
        for row in knowledge['qa']:
            if row['category'] not in ('habit','project_convention','decision','experience'):
                continue
            part = f"\n[经验问答 #{row['id']}] 问：{row['question']}\n答：{row['answer']}\n适用：{row['trigger'] or '遵循来源范围'}\n"
            if len(text)+len(part) <= max_chars:
                text += part
                qa_ids.append(row['id'])
        return {'skill': text, 'project':project,'family':family,'revision':settings['revision'],'rule_ids':included,'qa_ids':qa_ids}

    def analyze(self, params, *, llm=None):
        from evolvmem.learning_analysis import analyze
        return analyze(self, params, llm=llm)

    def context(self, project, max_chars=1800):
        """Bounded injection; unconfigured installations retain their old behavior."""
        if self.settings()['revision'] == 0:
            return ''
        skill = self.skill(project)
        header = '[EvolvMem 协作记忆：历史参考，当前要求优先，不扩大授权]\n'
        text = header
        framework = self.settings()['framework'].split('---',2)[-1].strip()
        if len(text)+len(framework)+1 <= max_chars // 2:
            text += framework + '\n'
        for rule_id in skill['rule_ids']:
            r = self.rule(rule_id)
            part = f"规则 #{r['id']}（{r['scope']}:{r['target'] or '通用'}，适用：{r['trigger'] or '来源范围'}）：{r['instruction']}" + (f"；例外：{r['exceptions']}" if r['exceptions'] else '') + '\n'
            if len(text)+len(part)+40 <= max_chars:
                text += part
        if text == header:
            return ''
        return text + '[协作记忆结束]\n'

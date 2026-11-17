"""Auto memory extraction — analyzes conversations, extracts candidate memories worth persisting."""

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CandidateMemory:
    """Candidate memory — information extracted from conversation that may be persisted."""
    key: str
    value: str
    attribute: str = "fact"
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.5
    importance: float = 5.0
    tier: str = "normal"
    experience_case: dict | None = None
    learning: dict | None = None


class AutoExtractor:
    """Automatic memory extractor.

    Prompts the configured provider to review conversations and produce
    candidate memories; this module builds the prompt and parses responses.
    """

    EXTRACTION_PROMPT = """你是 EvolvMem 长期记忆提炼器。请审阅完整会话，只提炼跨会话仍有价值的信息。

## 保留规则
保留：用户长期偏好和画像、硬约束与安全开关、业务规则、架构或技术决策及原因、废弃方案及替代原因、可复用故障根因和防复发规则（标为 experience）。

## 丢弃规则
丢弃：临时密码、等待输入或稍后确认、一次性测试、单次测试通过或测试数量、单次部署完成、纯提交号、已完成且没有长期决策或原因的待办、可直接从代码或 git 获得的事实。

所有 value 必须使用中文；说明性 tags 也使用中文。稳定 key、代码标识符、产品名和必要缩写可以保留英文。

## 稳定 key 格式
项目资料使用格式：`project:{{已登记项目名}}:{{领域}}:{{主题}}`，必须保留字面量 project: 前缀。用户偏好使用 user: 前缀。不要将通用主目录名称作为业务项目。
示例：
- `project:shop:decision:after_sales` — 售后决策
- `user:preference:communication:language` — 语言偏好
- `project:evolvmem:arch:embedding_model` — 架构选择

## 输出格式
只返回一个 JSON 对象，顶层字段必须且只能为 "memories"。
"memories" 的值必须是数组；数组条目包含：
- key：稳定标识符
- value：记忆内容，必须为单句且最多 200 个字符；更长内容必须拆分或压缩。
- attribute：decision | preference | fact | constraint | user_profile | experience | playbook
  - experience：可复用的排查/操作经验——验证过的方法步骤、故障根因与修复路径；新提炼的 experience 先进入候选隔离，经确认或自动晋升后才生效。
  - playbook：多经验汇总的标准操作流程；通常不由提炼直接产出（由系统自动生成），但合约允许标记。
- tags：相关标签列表
- confidence：0.0-1.0 的置信度
- importance：1-10 的整数。9-10 为硬约束或成败关键决策；7-8 为重要架构或业务决策；5-6 为普通偏好和事实；3-4 为边缘参考资料。
- tier：若该记忆必须在每个会话可见（约束、长期用户偏好、用户画像）则为 "pinned"；若为只应在相关时通过 memory_search 获取、绝不注入的长参考资料则为 "reference"；否则为 "normal"。
- case：仅 experience 条目使用的结构化对象（其 value 仍为短摘要）。对象字段为 project、problem、conditions（字符串键值）、steps（步骤数组）、rationale、result、applicability（数组）、exclusions（数组）、transferable（布尔）、parent_experience_id（已知时填写）。case 总长最多 6000 字，保留机制、条件和步骤，不受 value 的 200 字限制。仅据会话提炼，推测原因注明推测；助手自称成功、无回复或无关测试不能证明方法有效。提炼结果只成为候选，实际成功必须另由真实工具结果/用户确认绑定来源。

## 会话摘要条目
必须包含且只包含一个 key 为 SESSION_SUMMARY 的会话摘要；即使没有原子记忆也不能省略。SESSION_SUMMARY 不占 8 条原子记忆配额。
- key：字面量 `SESSION_SUMMARY`（调用方会重写）
- value：最多 200 个字符，叙述会话涉及的项目、完成的事项和当前状态。
- attribute："fact"；importance：5-6；tier："normal"；tags：["日志", "分类:<project>"]

## 记忆学习元数据
原子记忆必须带 learning 对象（必填字段见后续"原子知识问答与依据合约"，缺失即视为无效候选）：category 为 habit（长期习惯）、project_convention（项目约定）、task_requirement（任务要求）、environment（环境事实）、decision（决策依据）、experience（技术经验）或 reference（参考资料）；basis 为 explicit（用户明确）或 inferred（推断）。
learning.quote 必须逐字引用本次会话的一段原话；trigger 写适用时机，rationale 保留纠正或选择的原因；不生成自动执行的协作规则；用户要求作为有范围和来源的知识记录。
用户直接表达的要求保留原话、条件和范围；推断或归纳明确标为 inferred，不能把助手建议写成用户要求。用户纠正时保留被纠正的理解及原因。单一项目约定仍用 project: key；只有明确长期通用要求才用 user: key。
task_requirement 不进入长期协作规则；一次性任务进展由摘要和任务断点保留。不要为填满字段臆造规则。SESSION_SUMMARY 不需要 learning。
只提炼用户明确要求或资料本身可核对的内容；助手自己的建议、计划或自称完成一律 basis=inferred，不能写成用户决定。

## 会话内容
{conversation}

## 输出
只返回 JSON 对象，不要输出其他内容："""

    def build_extraction_prompt(self,
                                messages: list[dict[str, str]], *, policy=None, related=()) -> str:
        """Build the extraction prompt."""
        conversation = "\n".join(
            f"[{m.get('role', 'unknown')}]: {m.get('content', '')}"
            for m in messages
        )
        prompt = self.EXTRACTION_PROMPT.format(conversation=conversation)
        prompt += ('\n\n原子知识问答与依据合约：\n'
                   '历史对话由系统清洗后单独入库；SESSION_SUMMARY 只概括本次历史，不需要 learning。\n'
                   '其余每条原子知识必须同时给出 learning.basis、learning.quote、learning.question、'
                   'learning.answer，缺一项即视为无效候选：\n'
                   '1. quote：从下方会话中复制一段**原始子串**，逐字一致且必须取自 [user] 消息；'
                   '助手说的话、你自己的改写、清洗稿都不算用户原话。找不到就写 ""。\n'
                   '2. answer：与 value 完全相同（逐字一致，不是同义改写）。\n'
                   '3. question：自然、具体、可检索，最多 160 字；一个问题一条短答案，复杂内容拆成多条。\n'
                   '4. basis 与逐条归属（最重要）：\n'
                   '   - basis=explicit 时，answer 里**每一个实质断言**都必须来自 [user] 消息里说过的内容，'
                   '并且能被 quote 支持；quote 存在只证明说过这句话，不证明 answer 的其余内容由用户说过。\n'
                   '   - 一条记忆只能有一个角色：用户要求与助手补充必须**拆成多条**。'
                   '助手替你列出的清单、标题、编号、下一步计划、文档名、链接、路径、截图内容，都属于助手补充，'
                   '必须单独成条并写 basis=inferred，不得并入 explicit 的 answer。\n'
                   '   - 典型例子：用户问“还需要哪些数据”只说明一项需求“执行前先列明待补数据”；'
                   '助手随后列出的商品、佣金、寄样等具体清单是助手补充 → 单独 basis=inferred，'
                   '不能写成“用户要求补齐商品、佣金…”。用户泛指资料的一句话，不能为助手产出的文档标题或流程背书。\n'
                   '   - 参考/资料类内容来自助手转述或文档本身时，basis=inferred，不要凭用户一句泛指写 explicit。\n'
                   '5. normalization：只有 category=task_requirement（用户明确需求）时才给 '
                   '{"requirement":"必须与 answer 逐字相同的整理后需求","acceptance":["用户逐字验收要求，无则 []"],'
                   '"questions":["尚需用户澄清的问题，无则 []"]}；'
                   'requirement 必须直接复制 answer（整理后的一句话），**不要**把 quote 原样搬进 requirement——'
                   'quote 可以有口语和空格，requirement 必须是 answer 的逐字副本，两者不一致会被判为无效并留在待确认；'
                   'reference、fact、experience 等其他分类不要写 normalization，也不要为参考资料编造用户需求。\n'
                   '6. 范围：task_requirement 的 trigger 写**本次任务范围**（例如整理这份 SOP 时），'
                   '不把一次性要求扩大成永久偏好；用户明确的长期习惯和项目约定保留原有范围。一次性进展、助手自称成功不算已验证经验，'
                   'experience 必须是可复用方法且绑定真实验证结果。派生摘要、清洗稿和助手的话都不得作为用户原话依据。\n'
                   '7. 输出保持前述 memories 对象格式与 SESSION_SUMMARY。避免重复叙述；保留必要的 case 经验结构、'
                   '适用条件，以及纠正、补充或替代旧知识所需的 action、target_id、target_revision、process 等字段。\n'
                   '短示例（合法 memories 对象，含摘要、用户要求和助手补充）：\n'
                   '{"memories":[{"key":"SESSION_SUMMARY","value":"演示项目讨论了只读测试边界，助手列出了待补参数，未做实质操作。",'
                   '"attribute":"fact"},'
                   '{"key":"project:demo:constraint:readonly","value":"这份演示资料只允许登录和只读测试，不做实质性操作。",'
                   '"attribute":"constraint","confidence":0.9,'
                   '"learning":{"category":"task_requirement","basis":"explicit","quote":"不要进行任何实质性的操作 可以测试",'
                   '"question":"整理这份演示资料时允许做哪些操作？","answer":"这份演示资料只允许登录和只读测试，不做实质性操作。",'
                   '"normalization":{"requirement":"这份演示资料只允许登录和只读测试，不做实质性操作。",'
                   '"acceptance":[],"questions":[]},"trigger":"整理这份演示资料时"}},'
                   '{"key":"project:demo:fact:missing_params","value":"助手列出了商品、佣金、寄样三类待补参数。",'
                   '"attribute":"fact","confidence":0.8,'
                   '"learning":{"category":"reference","basis":"inferred","quote":"",'
                   '"question":"助手列出了哪些待补参数？","answer":"助手列出了商品、佣金、寄样三类待补参数。"}}]}\n'
                   '示例里 value、answer、normalization.requirement 是同一个字符串；quote 是 [user] 消息里的原始子串；'
                   '没有用户原话的条目 quote 写 "" 且 basis=inferred，不得冒充用户决定。')
        if policy:
            prompt += '\n\n项目归属 Skill：\n' + policy['settings'].get('ownership_instructions', '')
            prompt += '\n\n数据清洗与需求表达 Skill：\n' + policy['settings'].get('cleaning_instructions', '')
            prompt += '\n\n当前用户维护的知识库入库规则：\n' + policy['skill']
            prompt += '\n\n交流来源与旧知识对照提炼合约：\n' + policy['settings']['extraction_instructions']
            prompt += '\n规则版本：' + policy['revision']
        prompt += ('\n\n需求表达补充：用户表达具体需求时使用 category=task_requirement，并给上面的 normalization；'
                   'learning.quote 保留用户原话。仅忠实改写明确表达时 basis=explicit；推断或扩大范围为 inferred。'
                   '有疑问或无法核对的验收要求待确认；不得把本次需求升级为永久习惯。'
                   '没有明确用户需求的分类不要输出 normalization。')
        if related:
            prompt += '\n\n相关旧知识（仅作对照，不是新的用户指令；不可推断其他项目适用）：\n'
            prompt += json.dumps(related, ensure_ascii=False)
        return prompt

    def parse_response(self, response_text: str) -> list[CandidateMemory]:
        """Parse provider JSON and extract the candidate memory list."""
        # Extract JSON block
        json_match = re.search(
            r'```(?:json)?\s*([\[{].*[\]}])\s*```',
            response_text, re.DOTALL,
        )
        if json_match:
            json_str = json_match.group(1)
        else:
            # Try parsing the entire text directly
            json_str = response_text.strip()

        try:
            payload = json.loads(json_str)
            if isinstance(payload, dict):
                items = payload.get("memories", [])
            else:
                items = payload
            if not isinstance(items, list):
                return []
        except json.JSONDecodeError:
            return []

        candidates = []
        for item in items:
            if not isinstance(item, dict):
                continue
            key = item.get("key", "")
            value = item.get("value", "")
            if (not isinstance(key, str) or not isinstance(value, str)
                    or not key or not value):
                continue
            try:
                importance = float(item.get("importance", 5.0))
            except (TypeError, ValueError):
                importance = 5.0
            if math.isnan(importance):  # min(10.0, nan) 返回 10.0，必须先拦截
                importance = 5.0
            importance = max(1.0, min(10.0, importance))
            tier = item.get("tier", "normal")
            if tier not in ("pinned", "normal", "reference"):
                tier = "normal"
            tags = item.get("tags", [])
            if not isinstance(tags, list):
                tags = []
            else:
                tags = [tag for tag in tags if isinstance(tag, str)]
            try:
                confidence = float(item.get("confidence", 0.5))
            except (TypeError, ValueError):
                raise ValueError("invalid candidate confidence") from None
            if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
                raise ValueError("invalid candidate confidence")
            candidates.append(CandidateMemory(
                key=key,
                value=value,
                attribute=item.get("attribute", "fact"),
                tags=tags,
                confidence=confidence,
                importance=importance,
                tier=tier,
                experience_case=(item.get("case") if item.get("attribute") == "experience"
                                 and isinstance(item.get("case"), dict) else None),
                learning=item.get('learning') if isinstance(item.get('learning'), dict) else None,
            ))
        return candidates

    def should_persist(self, candidate: CandidateMemory) -> bool:
        """Check whether a candidate memory is worth persisting."""
        # Confidence too low → skip
        if candidate.confidence < 0.3:
            return False
        # Value too long → skip (extraction prompt requires <= 200 chars; hard cap 500)
        if len(candidate.value) > 500:
            return False
        # Casual chat type → skip
        if candidate.attribute in ("chat", "greeting", "small_talk"):
            return False
        # Key or value too short → skip
        if len(candidate.key) < 5 or len(candidate.value) < 5:
            return False
        return True

    def build_key(self, project: str, domain: str, attribute: str,
                  topic: str) -> str:
        """Build a standards-compliant stable key."""
        parts = [project, domain, attribute, topic]
        # Lowercase, replace spaces with underscores, keep only alphanumeric and underscores
        sanitized = []
        for p in parts:
            p = p.lower().strip()
            p = re.sub(r'[^\w一-鿿-]', '_', p)
            p = re.sub(r'_+', '_', p)
            sanitized.append(p.strip('_'))
        return ":".join(sanitized)

# EvolvMem 2.0 Context Core 设计

日期：2026-08-17
状态：用户已确认总体方向；等待本文档审阅后编写实施计划

## 一句话目标

将 EvolvMem 从单层 `key/value` 个人记忆插件升级为本地优先、跨 Agent 适配的研发经验记忆系统：它能在后续编码会话中可靠召回项目决策和经验证的方法，并把重复成功的处理经验巩固为可复用 Playbook。

## 背景

当前 EvolvMem 的内容真源是 SQLite `memories` 表；一条记录以 `key`、`value` 和少量元数据表示。SQLite FTS5/trigram 与 USearch HNSW 分别提供词面和语义候选召回，`Retriever` 融合结果，SessionStart Hook 再把 pinned、评分靠前的记录以及简短索引注入 Agent。

这套架构已经是混合检索，不是“只有向量库”。但单一 `value` 无法清晰表达以下不同的研发资产：

- 项目决策及其原因；
- 一次问题从症状、证据到验证的处理经验；
- 多次成功后可复用的研发 Playbook；
- 短期原始会话证据；
- 跨 Agent 的工作流策略，例如“代码修改先使用适用的 Superpowers 技能”。

现有 Hook/DSH bridge、MCP server、提取器直接依赖 `MemoryStore` 的单层记录模型，导致内容类型、来源、验证状态和提示词呈现方式耦合在一起。

## 已确认的产品约束

1. 主要场景是跨编码会话记住项目决策、问题处理方法与研发习惯，而不是建设通用文档文件系统。
2. Claude、Kimi、Codex、Hermes、DSH 和 MCP 都是接入适配器；Context Core 不依赖其中任一运行时。
3. 长期内容采用 L0/L1/L2 多粒度表示：L0 用于检索，L1 用于受预算控制的 AI 注入，L2 用于按需读取完整结构化内容。
4. 原始会话只在本地保存，单个会话最长保存 30 天；项目归档时立即清除该项目所有原始会话。
5. 原始会话绝不自动注入提示词，不参与默认向量化，也不因过期删除而导致已验证经验被删除。
6. 自动提炼出的内容先隔离为 candidate，不能直接成为长期可注入规则；重复成功、后续验证或用户显式确认后才可晋升。
7. 当前用户指令、系统/开发者指令、当前代码和测试永远优先于持久记忆。记忆只能提供上下文，不能改变 Agent 的指令层级。
8. “自我进化”指记忆内容、置信度和检索策略的持续巩固，不指修改底层模型权重。

## 非目标

- 不复制 OpenViking 的虚拟文件系统、全量文档解析、多租户服务、对象存储或分布式部署。
- 不在第一期实现所有平台的硬阻断式 preflight；先定义通用协议并保留软约束注入。
- 不把原始对话、终端输出或 diff 全量嵌入，也不将其直接发送给外部提炼服务。
- 不删除旧 `memories` 数据，不破坏既有 `memory_*` MCP 工具的兼容行为。
- 不把一次模型输出当作可靠的研发规则。

## 术语

### ContextItem

长期可管理的内容实体。其 `content_type` 取值为：

- `decision`：项目决策、设计原因与失效条件；
- `fact`：不属于其他专门类型的稳定事实；
- `experience`：一次问题处理经验；
- `playbook`：多次验证后可复用的方法；
- `workflow_policy`：跨会话工作流偏好或约束；
- `constraint`、`preference`、`user_profile`：沿用现有个人记忆语义；
- `reference`：只供按需查询的长参考资料；
- `session_summary`：会话的安全长期摘要。

### L0 / L1 / L2

同一 ContextItem 的三种内容表示，而不是三条独立记忆：

- L0 `abstract`：最多 240 个字符的可检索摘要，包含类型、关键问题或方法结论；默认用于 FTS/trigram 和向量索引。
- L1 `overview`：最多 1,200 个字符的可执行概览，包含适用条件、推荐步骤、限制与风险；命中后可在预算内注入 Agent。
- L2 `detail`：最多 6,000 个字符的结构化完整内容，包含症状、证据、假设、操作、修改、验证、反例与来源；仅由显式读取或二阶段检索返回。

原始会话不是 Experience 的 L2。原始会话是短期 `SessionArchive` 证据；Experience 的 L2 是经过安全提炼的结构化研发轨迹。

### 生命周期

`candidate → active → superseded | archived` 是长期 ContextItem 的主路径。`candidate`、`superseded`、`archived` 和 `deleted` 默认不可注入。原始会话有独立状态 `available → purged`。

## 选定架构

采用“轻量 Context Core”路线：保留 Python、SQLite、FTS5/trigram、USearch HNSW 和现有本地数据目录；在其上引入独立的领域模型、分层内容、证据与生命周期服务。现有 Hook、MCP、DSH bridge 和未来的运行时插件只通过明确的 Core 接口访问数据。

```text
Claude / Kimi / Codex / Hermes / DSH / MCP
                 │
                 ▼
             Adapter 层
                 │
                 ▼
             Context Core
  ┌──────────────┼───────────────────────────────────┐
  │ ContextStore │ LayerService │ LifecycleService    │
  │ ContextRecall│ EvidenceService │ PromotionService │
  └──────────────┼───────────────────────────────────┘
                 │
                 ▼
       SQLite + FTS5/trigram + HNSW
                 │
                 ▼
       本地加密 SessionArchive（30 天 TTL）
```

`MemoryStore` 不会被一次性删除。它先作为旧数据导入与兼容门面，逐步把 `memory_*` 请求映射到 Context Core。

## 数据模型

### `context_items`

```text
id INTEGER PRIMARY KEY
identity_key TEXT NOT NULL
content_type TEXT NOT NULL
project TEXT NOT NULL DEFAULT ''
scope TEXT NOT NULL DEFAULT 'project'       -- global | project
status TEXT NOT NULL DEFAULT 'candidate'    -- candidate | active | superseded | archived | deleted
tier TEXT NOT NULL DEFAULT 'normal'         -- pinned | normal | reference
tags TEXT NOT NULL DEFAULT ''
importance REAL NOT NULL DEFAULT 5.0
confidence REAL NOT NULL DEFAULT 0.5
source_state TEXT NOT NULL DEFAULT 'none'    -- none | available | partial_purged | purged
source_count INTEGER NOT NULL DEFAULT 0
success_count INTEGER NOT NULL DEFAULT 0
failure_count INTEGER NOT NULL DEFAULT 0
access_count INTEGER NOT NULL DEFAULT 0
last_accessed TEXT
last_verified_at TEXT
expires_at TEXT
supersedes INTEGER REFERENCES context_items(id)
superseded_by INTEGER REFERENCES context_items(id)
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
```

`identity_key` 是稳定逻辑标识，例如 `workflow:development:superpowers`、`project:evolvmem:decision:context-core` 或 `experience:mcp:stdio-hang`。它用于替代、去重、项目相关性和兼容旧 key；它不是向量内容的唯一来源。

数据库建立 partial unique index：在同一 `(identity_key, project, scope)` 下最多只能有一个 `status='active'` 的 ContextItem。candidate 可以并存，由 LifecycleService 在事务内决定保留、合并、supersede 或归档哪一项。

### `context_layers`

```text
item_id INTEGER NOT NULL REFERENCES context_items(id)
layer TEXT NOT NULL                            -- l0 | l1 | l2
content TEXT NOT NULL
content_hash TEXT NOT NULL
generator TEXT NOT NULL                        -- migrated | deterministic | extractor:<provider> | user
created_at TEXT NOT NULL
updated_at TEXT NOT NULL
PRIMARY KEY (item_id, layer)
```

`context_layers` 是所有 L0/L1/L2 正文的唯一位置。默认 FTS/trigram 索引 L0 和 L1，不索引 L2；L2 仅在已选中项目、显式 `context_read` 或扩展检索时搜索，避免详细过程与历史噪声污染默认召回。

### `context_sources`

```text
id INTEGER PRIMARY KEY
item_id INTEGER NOT NULL REFERENCES context_items(id)
archive_id INTEGER REFERENCES session_archives(id)
source_kind TEXT NOT NULL                       -- session | user | migration | import
extraction_version TEXT NOT NULL
created_at TEXT NOT NULL
UNIQUE(item_id, archive_id, source_kind)
```

它只保存来源关系和提炼版本，不复制原始内容。原始会话 purge 后，关联 ContextItem 的 `source_state` 更新为 `purged`，但不会影响其已验证的 L0/L1/L2。

当一个 ContextItem 同时有 available 和 purged 来源时，`source_state='partial_purged'`；只有全部来源都 purge 后才变为 `purged`。

### `legacy_memory_migrations`

```text
legacy_memory_id INTEGER PRIMARY KEY
context_item_id INTEGER NOT NULL REFERENCES context_items(id)
migrated_at TEXT NOT NULL
```

它使旧库迁移可重复执行：已经映射的旧 `memories.id` 不会再次创建 ContextItem。

### `context_evidence`

```text
id INTEGER PRIMARY KEY
item_id INTEGER NOT NULL REFERENCES context_items(id)
source_id INTEGER REFERENCES context_sources(id)
outcome TEXT NOT NULL                           -- success | failure | confirmed | contradicted
note TEXT NOT NULL DEFAULT ''
observed_at TEXT NOT NULL
created_at TEXT NOT NULL
```

它记录经验是否在后续会话中被复用、成功、失败或被用户确认。`note` 必须经过现有敏感内容策略；不存储原始终端输出。

### `session_archives`

```text
id INTEGER PRIMARY KEY
project TEXT NOT NULL
adapter TEXT NOT NULL
external_session_id TEXT NOT NULL
payload_path TEXT NOT NULL
payload_sha256 TEXT NOT NULL
state TEXT NOT NULL DEFAULT 'available'        -- available | purged
expires_at TEXT NOT NULL
purged_at TEXT
created_at TEXT NOT NULL
UNIQUE(adapter, external_session_id)
```

加密 payload 存放在 `${data_dir}/session_archives/`，不保存为 SQLite BLOB。文件采用本地 AES-GCM 加密；密钥存于 `${data_dir}/archive.key`，创建时限制为 owner read/write。没有可用加密库时，归档写入失败并记录无正文告警，但会话提炼仍可继续；绝不降级为明文落盘。

## 检索与渐进式披露

L0/L1/L2 与向量索引承担不同责任：向量索引负责语义候选召回，层级负责命中后的信息披露深度。

```text
新任务 / 当前项目
  → pinned 的 active workflow_policy、constraint 直接候选
  → L0 FTS/trigram + L0 HNSW 向量检索
  → 合并、去重、按项目/类型/置信度/证据/时效性重排
  → 返回少量 L0；将预算内、可信的 L1 注入
  → Agent 显式 context_read(id, layer='l2') 读取完整经验
  → 低置信或无命中：扩大 L1 检索，必要时读取未过期原始会话
  → 仍无答案：Agent 正常检查代码、运行工具或询问用户；本次结果成为候选经验
```

### 默认召回规则

1. `active` 且 `tier='pinned'` 的 `workflow_policy`、`constraint`、`preference` 先按 scope 注入 L1；全局策略仅在与当前任务类型兼容时进入上下文。
2. 普通候选必须通过 `status='active'`、未过期、项目匹配与最小置信度过滤。
3. FTS/trigram 和 HNSW 的得分沿用可配置的混合权重；结果在取 L1 前用 `confidence`、`success_count - failure_count`、项目匹配和既有 recency/frequency 分数重新排序。
4. `candidate` 只在用户显式审阅/确认 API 中可见，绝不进入 SessionStart block。
5. `reference` 与 L2 不进入默认注入，只提供可发现的 L0 索引和按需读取路径。
6. 命中 L1 仍不能替代当前用户、系统、开发者指令或当前代码/测试。

### `superpowers` 工作流示例

全局 `workflow_policy` 的 `identity_key` 为 `workflow:development:superpowers`，`scope='global'`、`tier='pinned'`。

- L0：涉及代码修改时使用适用的 Superpowers 技能。
- L1：说明何时先 brainstorm、何时写计划、何时 TDD、何时先系统化调试，以及当前指令优先。
- L2：包含完整流程、例外、失败案例和证据链接。

所有 Adapter 都可将 L1 作为软约束注入。拥有任务/工具事件的 Adapter 可在以后实现 `preflight(task)`：它只报告缺失的工作流步骤或要求用户确认，不能伪造某个运行时不存在的技能，也不能越过系统指令强制执行。

## 内容构建、候选隔离与经验巩固

### 提取结果

SessionEnd 提取器输出结构化候选，而不是直接输出单层 `CandidateMemory`：

```json
{
  "items": [
    {
      "identity_key": "experience:mcp:stdio-hang",
      "content_type": "experience",
      "project": "evolvmem",
      "scope": "project",
      "l0": "MCP stdio 握手卡住时，优先检查 stdin 预读竞争。",
      "l1": "症状是 initialize 无响应；先检查是否有并发读取 stdin；改为单一读取路径后用真实握手回归验证。",
      "l2": "症状、证据、假设、排查、修改、验证、适用条件和反例的结构化内容。",
      "tags": ["mcp", "stdio"],
      "importance": 7,
      "confidence": 0.7
    }
  ]
}
```

现有安全门控扩展到所有三层：敏感内容、低信息、短期状态、非中文合约和长度限制任一层不合格时，该候选不进入 `candidate`。外部模型只接收现有脱敏后的消息；原始 archive 从不外发。

### 晋升规则

- 新提炼的 `experience` 与 `playbook` 默认 `candidate`。
- 用户通过 `context_confirm(id)` 可直接将 candidate 置为 active，并新增一条 `confirmed` evidence。
- 自动晋升 Experience 的条件是：至少两个不同 `session_archives` 的 `success` evidence，且 `failure_count=0`；晋升后状态为 active。
- 自动生成或更新 Playbook 的条件是：同一 `project` 或 `scope='global'` 下至少三个 active Experience 的 L0 语义相似度达到 `promotion_similarity_threshold`，每项至少有两个 success evidence，且没有未解决的 contradicted evidence。
- 自动生成 Playbook 时保留原 Experience，不覆盖其 L2；新 Playbook 引用三个以上 Source。若生成失败或模型输出不合格，保持 Experience 状态不变。
- 任一 `failure` 或 `contradicted` evidence 会降低置信度。`failure_count >= success_count` 的 active Experience 进入 archived，已注入的 Playbook 若依赖它则进入 candidate review，不再默认注入。

默认阈值：`promotion_min_successes=2`、`playbook_min_experiences=3`、`promotion_similarity_threshold=0.95`。相似度沿用 EvolvMem 当前的归一化口径 `(1 + cosine) / 2`；所有阈值在 `config.json` 中可调。

## 原始会话归档与删除

1. Adapter 在 SessionEnd 将原始消息写入加密 archive，`expires_at = session_end + 30 days`。
2. 提取器从内存中的原始消息构建脱敏副本给外部 LLM；archive 的存在不是提炼成功的前置条件。
3. 每次 SessionStart 与显式 `context_sweep` 都会运行轻量 purge：删除已到期 archive payload，置 `session_archives.state='purged'`、填 `purged_at`，并更新关联 ContextItem 的 `source_state`。
4. `context_archive_project(project)` 立即 purge 该项目全部 available archive payload；不删除 active ContextItem。
5. purge 是不可逆操作。失败时保持 `available` 状态，下一次 sweep 重试；不会伪造已删除状态。

## 兼容与迁移

### 旧数据映射

迁移是幂等、可恢复、事务化的：

| 旧 `memories` 字段 | 新 Context Core 映射 |
|---|---|
| `id` | `legacy_memory_id` 迁移映射表中的源 ID |
| `key` | `identity_key` |
| `value` | L1 与 L2；L0 为不超过 240 字符的确定性摘要 |
| `attribute` | `constraint`、`preference`、`user_profile`、`decision` 同名映射；`fact` 且 key 含 `:progress:log:` 映射为 `session_summary`，其他 `fact` 映射为 `fact`；未知值映射为 `reference` |
| `tier`、`importance`、`tags` | 同名元数据 |
| `status` | `active`、`superseded`、`archived`、`deleted` 同义映射 |
| `source_session` | `context_sources.source_kind='migration'` 的来源标记 |
| `expires_at`、访问统计、替代关系 | 同名/对应字段 |

迁移层内容的 `generator='migrated'`，不会调用外部 LLM。旧 `value` 不足以生成高质量 L0/L1/L2 时，L0 是截断摘要、L1/L2 都是原值；后续显式 `context_relayer` 或新的会话提炼才可替换为高质量层。

### 旧 API

`memory_search`、`memory_add`、`memory_replace`、`memory_remove`、`memory_status` 和 `memory_consolidate` 保持可用：

- `memory_add` 默认创建 `content_type='fact'`；调用者提供可识别 attribute 时映射为对应类型；将输入 value 作为 L1/L2，并生成确定性 L0。
- `memory_search` 返回兼容的 `key`、`value` 字段，其中 `value` 是 L1；返回 `context_id` 和 `available_layers` 供新客户端展开。
- `memory_replace` 创建 superseding ContextItem，旧项置 `superseded`。
- 新 API 为 `context_search`、`context_read`、`context_confirm`、`context_record_outcome`、`context_archive_project` 与 `context_status`。

迁移完成后，旧 `memories` 表保留为只读回滚数据，直到显式的未来清理版本；HNSW 在 Context Core 启用后从 active ContextItem 的 L0 重建，不能复用旧 vector ID。

## 模块边界

| 新模块 | 职责 | 不负责 |
|---|---|---|
| `context_models.py` | dataclass、枚举、输入/输出验证 | SQLite、网络、提示词 |
| `context_store.py` | schema、迁移、事务、CRUD、FTS | embedding、LLM、Adapter 语义 |
| `context_layers.py` | 确定性 L0 生成、层验证、层读取 | 直接写数据库 |
| `context_retriever.py` | FTS/HNSW 融合、排序、递进读取 | 提炼和 archive 写入 |
| `context_lifecycle.py` | candidate/active/archive/supersede、evidence、晋升 | 传输协议 |
| `session_archive.py` | 加密 archive、TTL purge、项目 purge | LLM 提炼、prompt 注入 |
| `context_service.py` | 协调写入、提取候选、检索和兼容门面 | JSON-RPC/Hook 细节 |
| Adapter 文件 | 将平台事件映射到 ContextService | 直接 SQL 或向量索引操作 |

现有 `vector_index.py`、`embedding.py` 继续作为基础设施；`extraction_policy.py` 继续作为纯安全与质量策略模块。

## 分期实施

该重构不能作为一个不可验证的大提交。按以下可独立交付阶段推进：

### Phase 0：运行时契约修复

- 统一安装器、README、`Config.model_path` 与 `embedding_dim`；安装后必须能加载实际下载的 embedding 模型。
- 增加 `Config.validate_runtime()`，在 MCP/Hook 启动时给出无敏感信息的明确配置错误。
- 交付：新安装的语义检索可用；旧配置有明确迁移/诊断路径。

### Phase 1：Context Core 与无损迁移

- 新建 ContextItem/Layers/Store/LayerService；创建 schema、FTS 与迁移记录。
- 将旧 active/superseded/archived 数据幂等迁移；保持旧 MCP API 的只读检索兼容。
- 从 active L0 重建向量索引，失败时保留 FTS 降级和 dirty marker 语义。
- 交付：旧库可升级，所有迁移项都有 L0/L1/L2，现有查询不丢数据。

### Phase 2：分层检索与上下文注入

- 实现 `ContextRetriever`、`context_search`、`context_read` 和 SessionStart L1 注入。
- 把 pinned workflow policy/constraint 与普通经验分开预算；candidate 永不注入。
- 交付：当前项目的 L1、全局工作流策略和相关经验可以可靠进入下一次会话；L2 只按需读取。

### Phase 3：会话 archive、候选与经验巩固

- 实现 AES-GCM archive、30 天 TTL、项目立即 purge、来源关系与 evidence。
- 扩展提取 JSON 合约、层质量门控、candidate 隔离、确认/成功/失败 API 以及 Experience→Playbook 晋升。
- 交付：新会话可形成候选经验，经重复成功后形成 Playbook；原始会话到期后不可再读且不会污染注入。

### Phase 4：适配器、兼容 API 与可观测性

- 让 Claude/Kimi/DSH/MCP 全部经 ContextService；定义通用 Adapter 接口。
- 增加 Context Core 状态、purge、promotion、低置信召回的统计与无正文日志。
- 为有事件能力的平台加入可选 preflight 协议；不改变不支持它的平台的 fail-open 行为。
- 交付：多运行时复用同一 Context Core，并可观察记忆为何被注入、晋升或归档。

## 故障处理与安全边界

- 所有 SQLite 多表写入使用同一外层事务；失败时不留下半个 ContextItem、半套层或半条 evidence。
- SQLite 成功、HNSW 写入失败时保留 ContextItem 提交并设置 dirty marker；下一次启动或显式修复重建向量索引。
- archive 加密、写入和 purge 失败均 fail-open，不阻塞用户会话；状态必须反映真实结果。
- 外部模型调用继续沿用脱敏、超时、重试、响应大小限制与安全重定向规则。原始 archive 不得作为模型请求体。
- L0/L1/L2 中任何一层含敏感内容、越过长度限制或无法满足语言/低信息规则时，整个自动候选进入拒绝统计，不落 candidate。
- `context_read` 对 L2 和 archive 要求精确 ID；普通向量检索不返回 raw payload。

## 测试与验收

每个 Phase 都采用先写失败测试、再最小实现、再回归全套测试的方式。

### Phase 0

- 安装配置与运行时模型路径/维度一致性。
- 缺少模型、维度不匹配、配置无效的诊断。

### Phase 1

- 新 schema 的约束、事务回滚、FTS trigger 与 L0/L1/L2 唯一性。
- 空库、旧库、部分迁移、重复启动后的幂等迁移。
- 所有旧状态、metadata、supersede 关系和 expires_at 的映射。
- HNSW 重建成功/失败与 FTS fallback。

### Phase 2

- L0 混合召回、项目/类型/状态过滤、L1 预算和 L2 显式读取。
- candidate/reference/L2 不进入默认注入。
- `workflow_policy` 的 global scope 和当前用户指令优先提示。
- 旧 `memory_search` 返回兼容 shape。

### Phase 3

- archive AES-GCM 往返、无加密库时拒绝明文写入、30 天 TTL、项目 purge 幂等性。
- 脱敏外发与 archive 分离。
- candidate 不注入、用户确认晋升、两会话成功晋升 Experience、三经验生成 Playbook、失败证据降级。
- 原始 source purge 后长期经验仍可用且标记为 `purged`。

### Phase 4

- Claude/Kimi/DSH/MCP 适配器均只通过 ContextService。
- fail-open、无正文日志、状态统计和 preflight 协议回归。

全量验收要求：已有 313 项基线测试全部通过；新增迁移、分层、TTL、污染防护和兼容 API 测试全部通过；真实模型只用于人工验收，不作为确定性自动化断言。

## 成功标准

1. 新会话可以在不注入原始历史的前提下，自动得到当前项目决策、相关经验和 L1 级工作流策略。
2. AI 能用 `context_read` 获取选中经验的 L2，而非把所有历史塞进 prompt。
3. 原始会话在 30 天后或项目归档后真实删除；已验证的经验保留且来源状态可追溯。
4. 一次提炼不会污染长期上下文；candidate 必须经明确晋升规则才可注入。
5. 多次验证成功的 Experience 可形成或更新 Playbook，失败/冲突经验会降级。
6. 旧数据库、旧 MCP 工具和现有 Claude/Kimi/DSH 行为保持兼容；向量故障仍能退化为 FTS 检索。
7. EvolvMem 不依赖 Hermes，任何未来 Adapter 只需实现统一 ContextService 协议。

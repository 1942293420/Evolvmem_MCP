# EvolvMem 项目记忆清洗与跨 Agent 续接设计

**日期：** 2026-09-01

**状态：** 已完成交互式设计确认与文档自审，待用户文档评审

**前置设计：**

- `2026-08-17-evolvmem-context-core-design.md`
- `2026-08-18-evolvmem-context-core-codex-cutover-design.md`

## 目标

本次改造把“项目记忆清洗”和“正在执行的工作续接”合并成一条可靠链路：

1. 修复 legacy → Context Core 迁移和新写入中的空 `project`。
2. 对历史项目归属进行确定性回填；信号冲突或缺失时进入待审，不让模型自动猜测。
3. 把无限增长的 `*:progress:log:*` 会话摘要收敛为每项目一条版本化滚动知识摘要，同时保留可追溯的短期原始日志。
4. 让 Web 记忆图书馆默认展示 Context Core 的真实 `project/content_type/L0/L1/L2`，不再把 legacy 投影视为主数据。
5. 增加版本化工作流检查点和每个项目工作区身份唯一的焦点指针，使新会话只说“继续原任务”即可从精确断点继续。
6. 让连接同一 EvolvMem 数据目录、遵循同一 MCP 合约的不同 Agent 安全交接，避免并发覆盖和跨项目串线。

## 当前事实

2026-09-01 的只读审计确认：

- EvolvMem 运行态仍为 `legacy`，`context_ready=false`。
- legacy 表共有 1477 条记录，Context Core 和 migration mapping 各 1026 条，仍有 451 条未映射。
- 已迁移的 1026 个 ContextItem 都有完整 L0/L1/L2，但 `project` 全为空。
- 历史共有 197 条 `progress:log`，其中 96 条当前仍为 active、66 条尚未映射；active 记录都没有到期时间。当前生成器每次使用带分钟时间戳的新 key，因此不会覆盖旧摘要。
- Web `/api/memories` 和页面列表仍读取 legacy projection 的 `key/value`。
- `context_session_start` 已存在，但它把第一条用户消息作为普通检索 query；“继续原任务”会进入 FTS/HNSW，而不是读取确定性任务指针。
- 当前工作区存在另一项未提交开发，且与 `context_service.py`、`context_store.py`、`mcp_server.py` 等文件重叠。实施必须隔离并保留这些改动。

## 非目标

本次不做：

- 用正文语义或 LLM 自动决定历史记忆属于哪个项目。
- 物理删除历史会话日志、superseded 版本或原始会话证据。
- 把 L2、完整终端输出、补丁正文、环境变量值或凭据位置注入新会话。
- 依靠 cross-encoder、LLM reranker 或更换向量索引来解决数据清洗问题。
- 在项目无法识别或存在多个无焦点候选时静默选择“最新任务”。
- 承诺不遵循 MCP instructions、未连接同一 EvolvMem 数据目录的任意第三方 Agent 也能自动接管。

## 方案比较与决策

### 方案 A：继续使用语义搜索

把“继续原任务”直接交给 `memory_search/context_search`。改动最少，但短查询缺乏项目和主题信息，已实测会召回无关项目。该方案不能提供精确断点、并发保护或完成状态。

### 方案 B：一条可覆盖的 current-task 记忆

使用稳定 key，例如 `project:<p>:workstream:current`，每次 `memory_replace`。它能改善单 Agent 续接，但缺少工作流身份、revision、租约和比较并交换；两个 Agent 会形成最后写入者覆盖。

### 方案 C：版本化检查点 + 项目工作区唯一焦点

检查点使用 ContextItem 的 L0/L1/L2 和 supersession 链保存，独立结构化表保存工作流身份、精确当前指针、revision、焦点和租约。续接直接读取精确指针，不经过相似搜索。

**决策：采用方案 C。** 会话摘要和加密 archive 只作为崩溃后的待确认恢复候选，不可覆盖权威检查点。

## 总体架构

```text
新写入 / 历史记录
        │
        v
ProjectResolver ── resolved ──> ContextItem.project
        │
        ├─ conflict ───────────> project resolution 待审队列
        └─ unresolved ─────────> project=''，不进入普通项目注入

SessionEnd 原子记忆 + session summary
        │
        v
ProjectRollupGenerator
        ├─ 成功 ─> project:<p>:knowledge:current 新版本
        │           └─ 旧滚动摘要 superseded，旧日志按策略 archived
        └─ 失败 ─> 保留旧摘要和全部新证据，状态可计算为 pending

Agent 首条消息
        │
        v
context_session_start(workspace_path, project_hint, query)
        ├─ continuation intent ─> ContinuityService.resume 精确读取 focus/checkpoint
        └─ normal task ────────> ContextRetriever + ContextRenderer

Agent 里程碑
        │
        v
continuity_checkpoint(action, expected versions)
        ├─ CAS 成功 ─> 新 ContextItem 版本或原子更新 lease/focus
        └─ revision/lease 冲突 ─> 拒绝覆盖，要求重新 resume
```

## 一、规范项目识别

### 统一解析器

新增单一 `ProjectResolver`，供以下路径共同使用：

- Kimi、DSH、Codex 和后续 adapter 的新写入；
- legacy → Core 初次迁移；
- 已映射 ContextItem 的历史 project 回填；
- Web 人工审核；
- Continuity 的 workspace → project 解析。

解析器不读取 `value`、L1 或 L2。项目注册表、alias、通用目录黑名单和 resolver 自身都带版本；它们的版本与 canonical digest 必须进入每次 migration plan。解析器只消费结构化信号：

1. 已登记的 workspace → project 绑定，或 adapter 在新版本 typed request 中显式提供的 project；
2. 由已知可信生成器版本写入的 session archive project；
3. 规范 key `project:{name}:...`；
4. `分类:{name}` tag；
5. 已登记项目名下的 legacy key `{name}:progress:log:...`；
6. 与已登记项目名完全相等的 bare tag。

第 1、2 项在来源版本可信且值不是通用目录名时是强信号；第 3 至 6 项默认是中等信号，至少两个独立来源一致才可自动解决。历史上由粗粒度 cwd basename 生成的 `project:jiangli` 一类已知错误模式、用户主目录名以及 `home/workspace/project/src` 等通用名一律降为不可单独采信。语法正确本身不是可信度证明。

所有候选先经过同一 alias 归一化。新写入的 typed request 增加 `project_hint` 和仅在本地服务调用期间存在的 `workspace_path`；adapter 必须传当前工作目录，服务端立即解析并丢弃原始路径。Core draft 的 `project` 只取 ProjectResolver 结果，不能继续由 legacy projection 的空字段继承。`project_summary`、`workstream_checkpoint` 等内部类型必须显式提供并校验 project，不参与历史启发式推断。

工作区另计算 `workspace_fingerprint`：Git 仓库使用 owner-only 本地密钥，对规范化 git common-dir 身份做 HMAC-SHA-256，使同一仓库的多个 worktree 共享身份；非 Git 工作区对规范化目录身份做相同 HMAC。只保存 HMAC，不在 MCP 响应、日志或 Context 正文暴露密钥、绝对路径或 remote URL。密钥进入 owner-only rollback bundle，公开 manifest 只记 key fingerprint；丢失或意外变化时 `continuity_ready=false`，不得静默生成新 key 使所有 pointer 失联。目录移动后需要显式重新登记，不静默把新 fingerprint 合并到旧工作区。

### 项目注册表与工作区绑定

DB 中的版本化 registry 是运行时权威源；现有配置 aliases 只作为首次 migration plan 的 seed，导入后若与 registry 不同只报告 drift，不在运行时形成第二套真相：

```text
context_project_registry
project                 TEXT PRIMARY KEY
status                  active | archived
revision                INTEGER NOT NULL
created_at              TEXT NOT NULL
updated_at              TEXT NOT NULL

context_project_aliases
alias                   TEXT PRIMARY KEY
project                 TEXT NOT NULL -> context_project_registry.project
revision                INTEGER NOT NULL
created_at              TEXT NOT NULL
updated_at              TEXT NOT NULL

context_project_workspace_bindings
workspace_fingerprint   TEXT NOT NULL
project                 TEXT NOT NULL -> context_project_registry.project
state                   candidate | active | revoked
is_default              INTEGER NOT NULL
method                  TEXT NOT NULL
revision                INTEGER NOT NULL
created_at              TEXT NOT NULL
updated_at              TEXT NOT NULL
PRIMARY KEY(workspace_fingerprint, project)

context_project_registry_meta
singleton_id            INTEGER PRIMARY KEY CHECK(singleton_id=1)
revision                INTEGER NOT NULL
updated_at              TEXT NOT NULL
```

部分唯一索引保证一个 workspace fingerprint 至多一个 active default binding；alias 在全 registry 唯一。没有 project hint 时，resolver 只采用 active default；给出 hint 时必须精确命中该 workspace 的 active binding。存在多个 active binding 且无 default、hint 不匹配、alias 冲突或只有 candidate 时一律返回 `ambiguous_project`，绝不以最近 workstream 反推。

CLI 与 Web v2 提供 project/alias/binding 的 list/register/bind/revoke/set-default 操作，全部使用 registry meta revision 和 row revision 双 CAS，并写 append-only registry events。adapter 信号只能自动创建 candidate；用户已明确确认新项目目标时，`continuity_checkpoint(create)` 可携带 `confirm_project_binding=true` 和 expected registry revision，在同一事务中把唯一 candidate 提升为 active default。其他情况必须由 operator 审核。每个 active binding 在同一事务中预建一条空 focus row。

### 判定规则

- 至少一个强信号，且所有可信非空信号归一化后相同：`resolved`。
- 没有强信号，但至少两个独立中等信号一致：`resolved`，method 记录具体信号组合。
- 两个可信非空信号不一致：`conflict`。
- 没有可用信号：`unresolved`。
- global 类型（constraint、preference、user_profile）按现有语义保持 global，不强制 project。

LLM 可以在 Web 待审页生成建议，但建议不得自动写入 project、alias 或 resolution 状态。

### 归属证据表

新增 `context_project_resolutions` 当前状态表：

```text
item_id             INTEGER PRIMARY KEY -> context_items.id
resolution_state    resolved | conflict | unresolved | global | ignored
decision_source     automatic | human | none
review_state        not_required | pending | accepted | rejected
proposed_project    TEXT
resolved_project    TEXT
previous_project    TEXT
confidence          high | medium | none
method              TEXT
evidence_json       canonical bounded JSON
resolver_version    TEXT
run_id              TEXT
revision             INTEGER NOT NULL
reviewed_by_hash    TEXT NULL
reviewed_at         TEXT NULL
created_at          TEXT
updated_at          TEXT
```

外键、状态 CHECK 和 `(review_state, resolution_state)` 待审索引由 schema migration 创建。另建 append-only `context_project_resolution_events`，记录 item ID、前后 revision/state、动作、actor hash、run ID、证据 digest 和时间，不保存正文。

`evidence_json` 只保存 `source/type/source_version/normalized_value`，不保存正文和绝对路径。人工确认使用精确 item ID 和 `expected_revision` 做 CAS；接受、拒绝、确认 global 与明确忽略是不同动作，不能用一个笼统的 `reviewed` 状态覆盖。

冲突或未知记录不改成 `candidate`，否则 legacy/Core 状态会分歧。它们保留原 status，project 为空，默认项目检索和注入继续排除；Web 待审视图和显式管理员跨项目查询可以看到。

## 二、滚动项目知识摘要

### 身份与内容

新增 `ContextContentType.PROJECT_SUMMARY`。每项目的稳定身份为：

```text
project:{project}:knowledge:current
```

每次更新创建新 ContextItem，旧版本变为 `superseded`。唯一 active identity 索引继续保证同一项目只有一条当前摘要。

- L0：项目名称、当前阶段、主要阻塞和下一步的一句话摘要。
- L1：稳定知识、最近变化、关键决策、已知问题、当前工作流和主要检索关键词。
- L2：结构化来源映射；每个陈述关联 Context ID，不复制原始会话或完整终端输出。

滚动摘要是导航和检索路由，不替代原子事实、决策、经验和证据。

### 更新流程

在一个 session 的原子记忆和 session summary 成功提交后，运行 best-effort rollup：

1. 读取上一版项目摘要 L1；
2. 读取本次已落库且项目已确定的原子 Context ID 和 session summary；
3. 排除 candidate、deleted、过期和其他项目内容；
4. 调用已配置 LLM 生成结构化候选；
5. 验证项目、字数、来源 ID、敏感信息和结构；
6. 在单事务中先 supersede 旧摘要，再写入新 L0/L1/L2 和 sources；
7. 提交后同步 Context 向量。

LLM 调用、解析、验证或 SQLite 事务失败时不得覆盖旧摘要。SQLite 提交成功但向量同步失败时，新摘要仍是权威版本，设置 `vector_dirty`，普通检索降级到 SQLite FTS；不能把已提交数据描述为回滚。cron/session miner 可幂等重试向量同步。

新增 `context_project_rollups` 水位表，至少保存 `project/current_context_id/source_set_hash/covered_through/generator_version/run_id/status/revision/updated_at`。`status` 为 `pending|ready|failed|vector_dirty`。相同 source set 和 generator version 必须跳过 LLM；归档判断逐项核对 session summary Context ID 已出现在成功 rollup 的 relational source closure 中，不能只比较时间戳。非确定性的 LLM 输出不进入 migration plan digest。

### 冲突策略

新内容与 active 事实或决策冲突时：

- 两边原子记忆和来源都保留；
- 摘要不得把冲突项写进“稳定知识”；
- 写入“待确认变化”，列出双方 Context ID；
- 只有用户确认、可信外部证据或既有 lifecycle 晋升规则通过后，新版才替代旧版；
- 旧内容转为 superseded，不做物理删除。

### 会话日志生命周期

新 session summary 写入时默认 `expires_at = created_at + 30 days`。每项目目标是只保留最近 10 条 active 原始摘要，但数量上限和 TTL 归档都必须满足同一个前置条件：该 summary Context ID 已被成功 rollup 覆盖。

未覆盖记录到期时不归档；lifecycle 保持其 active、延长一个有限重试窗口，并在 rollup 水位表派生 `rollup_pending` 告警，直到 rollup 成功或管理员明确处理。新增 `session_archive_holds(archive_id, source_context_id, reason, created_at)`，使相关 encrypted archive 不能先于待汇总证据被 purge；覆盖成功或人工处置后才在事务中释放 hold。归档不是删除：它保留来源、supersession 和管理员历史读取能力，但不进入默认注入、普通搜索和 Core 首页列表。健康 rollup 下该机制稳定收敛；持续失败时宁可暴露 backlog，也不静默丢失尚未汇总的断点证据。

## 三、历史回填与切换

### 工具形态

新增专用维护 CLI，提供四个阶段：

```text
plan    只读分析，输出统计、逐项 action 和 deterministic digest
apply   只接受对应 plan digest，在备份和独占锁后执行
verify  验证数据库、映射、三层、项目、滚动摘要和向量不变量
rollback 仅使用本次 run_id 对应的 manifest/backup 恢复
```

plan 报告至少包含：

- legacy 总数、已映射数、未映射数；
- resolved/conflict/unresolved/global 数量；
- 每项目 session summary 数量及计划归档数；
- 将创建或更新的滚动摘要；
- project mismatch、duplicate active identity 和 projection lag；
- 预计向量文档数；
- 不含正文、绝对路径和敏感配置的逐项 reason code。

plan digest 覆盖数据库只读指纹、schema/resolver/generator 版本、项目注册表 digest、alias/通用目录配置 digest 和 canonical actions。`apply` 获得排他锁后必须重新生成 plan，只有数据库指纹和完整 digest 都与用户批准的输入一致才可继续。回填前预先检测目标 `(identity_key, project, scope)` 的 active 唯一键碰撞；碰撞项进入 conflict 待审，不依赖事务执行到一半才报错。

新增 `context_maintenance_runs`，持久化 `run_id/kind/stage/status/failed_stage/attempt/plan_digest/database_fingerprint/resolver_version/registry_digest/pre_epoch/post_epoch/started_at/updated_at/error_code`。`stage` 表示最后成功完成的阶段，`status` 为 `running|failed|completed|rolled_back`；`failed_stage` 只在 failed 时保存刚才尝试的下一阶段。迁移阶段严格为：

```text
planned -> backfilled -> rolling_up -> archived -> vector_synced -> verified
```

每次阶段跃迁单独持久化。失败时保持最后成功 `stage`、置 `status=failed` 并记录 `failed_stage`；同一 run 只有在重新核对锁、plan/config digest 和该阶段前置不变量后，才允许 `failed -> running` 重试同一个 `failed_stage`，同时 `attempt+1`。不能跳阶段或把 failed 直接标 completed。只有 `stage=verified,status=completed` 才叫整体成功；任何其他组合都必须在 Web/status/CLI 中显示为 incomplete，不能以“主事务已提交”冒充完成。

为给 rollback 提供可证明的漂移门禁，新增：

```text
context_state_meta(singleton_id=1, mutation_epoch, schema_version, updated_at)
context_mutation_journal(epoch PRIMARY KEY, owner_run_id NULL, kind, state_digest, created_at)
```

所有会改变 legacy projection、Context Core、resolution、registry、rollup、continuity 或 archive lifecycle 语义状态的生产事务，都必须通过同一个 transaction helper 恰好增加一次 `mutation_epoch` 并写 journal；纯 maintenance 状态/诊断读取不增加。maintenance 自身的业务 mutation 写当前 run ID。manifest 保存 pre epoch/digest，run 持续记录自身拥有的 epoch 集合和 post epoch/digest。配置文件另保存 pre/post canonical digest 与 owner-only rollback snapshot，因为它不属于 SQLite 事务。

### 安全执行顺序

1. 代码侧先统一新写入 project 和 session summary TTL，停止继续制造空 project 和永久日志。
2. plan 只读运行，复跑 digest 必须一致。
3. 获得 cutover 独占锁并阻止生产写入。
4. 在锁内重算并核对 plan；使用 SQLite Backup API 备份含 WAL 的一致性数据库，写 owner-only manifest 并重新打开执行 `quick_check`。
5. 一个最外层事务内：把 451 条未映射记录全部迁入 Core；resolved 项写 project，conflict/unresolved 项保持 `project=''`；对已映射项在碰撞检查后做受审计的 project 回填；同时写 resolution 当前行、event journal 和确定性结构。LLM rollup 不在长事务内调用。
6. 提交后生成项目 rollup，再按成功来源归档旧 session summaries。
7. 重建 Context HNSW，并验证 active L0 文档数量。
8. 第二次 plan/apply 必须产生零业务变化。
9. 先进入 shadow，对比项目过滤和召回；所有门禁通过后再进入 primary。

排他 maintenance 模式保持到 `verified`、显式 rollback 或明确选择保留 incomplete run 后才释放；LLM 不占用 SQLite 长事务，但该期间生产 writer 仍被拒绝。阶段崩溃后只能按 manifest 续跑同一 run 或执行受保护 rollback。

任何一步失败都保留 manifest、run_id 和报告；不把部分结果描述为完成。rollback 只针对本次 run ID，不能选择“最新备份”猜测。rollback 前必须停止 writer、重新取得排他锁，并确认 pre epoch 之后的每条 mutation journal 都属于该 run、当前 canonical state digest 等于 run 的 post digest，且当前配置 digest 等于 run 记录的 post config digest；任一不符都说明出现后续或旁路写入，自动恢复必须拒绝，转为显式 forward repair/人工合并。恢复使用 SQLite Backup API，不复制数据库/WAL 文件；恢复后重建向量、恢复 owner-only pre config snapshot 并重新验证。

### 新的 primary 门禁

除既有 cutover 条件外，增加：

- 每条可确定归属的 project-scope active item 的 project 非空且与 resolution 证据一致；
- conflict/unresolved 项默认不可项目注入；
- 每项目至多一条 active `project_summary`；对存在合格来源且 rollup 成功的 resolved 项目必须恰有一条；
- 每个 workstream 的 `current_context_id` 都指向 active `WORKSTREAM_CHECKPOINT`，其 identity 必须精确等于 `project:{project}:workstream:{id}:checkpoint`，且 L2 workstream ID/checkpoint revision/status 与表一致；每个非空 focus 指向同 project/workspace 的 unfinished workstream；
- Core project 的权威来源是当前已确认 resolution；`projection_lag` 比较该 resolution 与 Core。legacy metadata 重新推断的差异单列为 `resolver_drift`，不得把人工裁定永久判成 projection lag；
- mapping lag 为零；
- Context 向量与 active L0 文档一致。

## 四、版本化工作流检查点

### 领域模型

新增 `ContextContentType.WORKSTREAM_CHECKPOINT`。检查点稳定 identity 为：

```text
project:{project}:workstream:{workstream_id}:checkpoint
```

每次 checkpoint 产生新 ContextItem 并 supersede 前一版。它是 project scope、normal tier；ContextRetriever 的默认 content type 集合与自动注入必须显式排除 `WORKSTREAM_CHECKPOINT/WORKSTREAM_RECOVERY`，只能由 Continuity 精确读取或管理员显式按类型搜索。即使 L0/L1 存在 FTS/HNSW 文档，也不能因向量命中绕过该过滤。

检查点各层：

- L0：任务标题、状态和下一动作的一句话。
- L1：目标、已确认方案、已完成事项、当前步骤、下一步、阻塞、验证状态。
- L2：canonical JSON，供跨 Agent 精确恢复。

L2 schema version 1：

```json
{
  "schema_version": 1,
  "workstream_id": "ws_<opaque>",
  "parent_workstream_id": null,
  "project": "string",
  "workspace_fingerprint": "hmac-sha256:<hex>",
  "checkpoint_revision": 1,
  "objective": "string",
  "accepted_decisions": ["string"],
  "completed_steps": ["string"],
  "current_step": "string",
  "next_action": "string",
  "blockers": ["string"],
  "status": "open",
  "repo": {
    "kind": "git|non_git",
    "branch": "string",
    "root_commit": "hex-or-empty",
    "head_commit": "hex-or-empty",
    "worktree_state_hash": "hmac-or-empty"
  },
  "artifacts": [
    {"path": "repo-relative/path", "role": "spec|plan|code|report"}
  ],
  "verification": [
    {"program": "bounded executable name", "args_digest": "sha256-or-empty", "outcome": "pass|fail|not_run", "summary": "bounded text", "at": "UTC timestamp"}
  ],
  "source_context_ids": [1]
}
```

表中的 project、workspace fingerprint、parent、status、workstream ID、checkpoint revision 和 repo 字段是权威值；服务端根据数据库状态与当前 workspace 生成并写回 L2。客户端只提交 objective、decisions、steps、next action、blockers、相对 artifacts、verification 摘要和 source IDs；若重复提交权威字段或与服务端值不一致，整次请求拒绝。这样同一状态不会在 action、表和 L2 之间各自漂移。

每个 `source_context_id` 必须存在、未 deleted、对调用者可见，并属于同一 project 或是允许引用的 global item。服务在同一事务中写 `context_sources(source_kind='context_reference', source_ref='<id>')`；不存在、跨项目、不可见或已删除的来源使整次 checkpoint 回滚。parent 也必须在同一 project/workspace，禁止 self-parent 和父链循环。

禁止字段：绝对路径、环境变量值、token、凭据路径、patch 正文、完整终端输出、任意 shell command 和模型隐藏推理。字符串和数组均有配置化硬上限，写入前复用敏感信息检测与边界转义。

### 工作流状态表

新增 `continuity_workstreams`：

```text
id                     TEXT PRIMARY KEY
project                TEXT NOT NULL
workspace_fingerprint  TEXT NOT NULL
parent_id              TEXT NULL -> continuity_workstreams.id
current_context_id     INTEGER NOT NULL -> context_items.id
checkpoint_revision    INTEGER NOT NULL
state_version          INTEGER NOT NULL
status                 open | paused | blocked | completed | cancelled
repo_anchor_json       canonical bounded JSON
lease_token_hash       TEXT NULL
lease_writer_hash      TEXT NULL
lease_until            TEXT NULL
lease_epoch            INTEGER NOT NULL
created_at             TEXT
updated_at             TEXT
completed_at           TEXT NULL
```

`checkpoint_revision` 只在产生新 checkpoint 内容时增加；`state_version` 对 checkpoint、租约和状态的任意 mutation 都增加；`lease_epoch` 在 claim/renew/release 时增加。`lease_token_hash` 才是授权依据；`lease_writer_hash` 只用于审计，不授予权限。

焦点使用独立精确指针表，避免在多个 workstream 行上模拟单例：

```text
continuity_focus
project                TEXT NOT NULL
workspace_fingerprint  TEXT NOT NULL
workstream_id          TEXT NULL -> continuity_workstreams.id
revision               INTEGER NOT NULL
updated_at             TEXT NOT NULL
PRIMARY KEY(project, workspace_fingerprint)
```

每个 active workspace binding 在登记事务中都创建 focus row：初始 `workstream_id=NULL, revision=0`。clear 只把 workstream ID 置 NULL 并使 revision+1，永不删除行；即使当前无 focus，resume/list 也必须返回可供下次 CAS 的 revision。binding revoke/archive 前先清指针，空 row 作为审计状态保留。

因此唯一性是“每个 `(project, workspace_fingerprint)` 恰有零或一个 focus”，不是整个 project 跨所有 clone 只有一个。Git common-dir 相同的 worktree 共享 fingerprint。所有 focus 设置、切换和清除都对 `expected_focus_revision` 做 CAS；原子 switch 先验证旧指针和目标 workstream 的 project/workspace/非终态，再在一条事务中改指针。失败时旧 focus 保持不变。

另建 append-only `continuity_events`，只记录 workstream ID、事件类型、前后 revision/version、writer hash、bounded error code 和时间，用于并发与交接审计，不记录 L1/L2 正文。

### 原子 checkpoint 与 CAS

`continuity_checkpoint` 是一个带 action discriminator 的写工具。每个 action 都显式携带 transient `workspace_path`，不能读取 MCP 服务进程 cwd。MCP transport/session 建立时由服务端生成随机、不可复用的 writer instance 和 256-bit secret；handler 把它们作为内部 `LeaseCredentials` 传给 ContinuityService，不进入模型可见 JSON schema。DB 只保存 HMAC/token hash，writer hash 仅供审计。

`create/claim/accept_recovery_new` 成功时，服务端把 workstream/lease epoch 对应的 capability 保存在该 transport session 内存；后续内容 mutation、renew/release 和 focus switch 由 handler 自动附带 capability，并做常量时间验证。模型既不提交也看不到 lease token。stdio server 一进程一 session；可复用网络 transport 必须提供隔离的 authenticated session storage，否则不得宣告 `evolvmem.continuity.v1` capability。`make_focus=true` 的 create 必须同时提供 `expected_focus_revision`。动作契约如下：

| action | 必需 CAS | 是否写新 ContextItem | 结果 |
|---|---|---:|---|
| `create` | checkpoint/state 为 0；设 focus 时需 focus revision | 是 | 生成 ID，首个 checkpoint revision=1、state version=1，绑定当前 session capability；可原子设 focus |
| `update/pause/block/resume/unblock/complete/cancel` | checkpoint revision + state version + 当前 session lease capability；清 focus 时还需 focus revision | 是 | 按状态表写新断点；update/resume/unblock 续租，pause/complete/cancel 清 focus 和 lease，block 保持 focus 但释放 lease |
| `claim/renew/release` | state version；renew/release 还需当前 session capability | 否 | 只更新 lease、state version 和 lease epoch，current Context ID/checkpoint revision 不变 |
| `switch_focus` | focus revision + target state version + target capability；当前 focus 有 active lease 时还需同 session 持有其 capability | 否 | 原子更新 focus 指针，不伪造 checkpoint；foreign active lease 时拒绝 |
| `clear_focus` | focus revision + 当前 focus state version/capability | 否 | 把指针置 NULL、revision+1；管理员修复走独立 operator 路径 |
| `accept_recovery_new` | candidate revision + focus revision | 是 | 新建 open workstream/revision=1、关闭候选并绑定当前 session capability；可原子设 focus |
| `accept_recovery_existing` | candidate + target checkpoint/state revision + target capability；设 focus 时还需 focus revision | 是 | 更新同 project/workspace unfinished workstream，关闭候选 |
| `reject_recovery` | candidate revision | 否 | 只关闭候选，不依赖 focus |

`create` 使用独立事务路径：生成 opaque ID，插入首个 ContextItem/layers/sources，再 `INSERT continuity_workstreams(... checkpoint_revision=1, state_version=1 ...)`；ID、parent 或 focus 冲突导致整体回滚。它不执行不存在行的 `UPDATE ... revision=0`。

状态迁移是封闭白名单：

| 当前状态 | 允许 action → 下一状态 |
|---|---|
| 不存在 | `create → open` |
| `open` | `update → open`、`pause → paused`、`block → blocked`、`complete → completed`、`cancel → cancelled` |
| `paused` | `update → paused`、`resume → open`、`complete → completed`、`cancel → cancelled` |
| `blocked` | `update → blocked`、`unblock → open`、`pause → paused`、`complete → completed`、`cancel → cancelled` |
| `completed/cancelled` | 无；所有内容与租约 action 都拒绝 |

claim/renew/release 和 focus action 不改变 workflow status。任何表外迁移都返回 `invalid_transition`；`update` 永不暗中 resume/unblock。

内容更新在同一事务中按以下顺序执行：

1. 从 transient workspace path 重新计算 project/fingerprint/repo anchor，校验 parent/source、`expected_checkpoint_revision`、`expected_state_version`、状态迁移和 transport lease capability/epoch；
2. 若旧 checkpoint 仍 active，先把它标为 superseded；
3. 再插入同一稳定 identity 的新 active ContextItem、L0/L1/L2 和 relational sources；
4. 条件更新 workstream 的 current Context ID/revision/state version/status/repo anchor；
5. 必要时以 CAS 清除 focus；
6. 任一条件更新 rowcount 不为 1 时整体回滚，旧 checkpoint 重新保持 active，且不残留 layer/source/event。

默认租约 15 分钟，以服务端 UTC 比较；`lease_until > now` 才算 active，等于或早于 now 都算过期。claim 仅在无租约或已过期时成功并绑定新 capability；active owner session 可 renew；未过期 release 也仅允许持有 capability 的 session。过期 capability 永久失效，旧进程与其他 writer 一样必须用最新 state version 重新 claim。终态 workstream 拒绝 claim/update。内容 mutation 必须由持有当前 capability 的 transport session 发起，租约为空或过期时先 claim，不能边写边静默夺取。其他 session 可以 read-only resume，但必须得到 `lease_held_by_other`，不得执行或写回。任何 CAS/lease 冲突都不提供 last-write-wins 分支。

父 Agent 保持项目 focus；子 Agent 可创建带 `parent_id` 的非 focus 子工作流。子 Agent 完成后，父 Agent把其 commit、测试和报告作为来源合并进父检查点，避免多个子 Agent 争写同一 revision。

### 仓库锚点与陈旧判定

repo anchor 由服务端从 transient workspace path 采集，而不是信任客户端：Git 仓库记录 root commit、当前 branch、HEAD 和经 HMAC 的 worktree status 摘要；非 Git 工作区只记录 fingerprint 和 `kind=non_git`。resume 返回正交 `staleness_flags`，同时按固定优先级选唯一 `primary_staleness_code`：

- fingerprint 不同：flag `wrong_workspace`；
- 无法采集或非 Git：flag `unknown`；
- 历史分叉：flag `head_diverged`；
- branch 改变：flag `branch_changed`；
- 当前 HEAD 是 checkpoint HEAD 的后代：flag `head_advanced`；
- dirty 摘要变化：flag `worktree_changed`；
- Git HEAD、branch 和 worktree hash 全相同：flag `fresh`。

primary code 的优先级严格为 `wrong_workspace > unknown > head_diverged > branch_changed > head_advanced > worktree_changed > fresh`。多 flag 不改变此顺序。

只有 `fresh` 可直接按 next action 执行。其他结果仍返回有界 checkpoint，但 Agent 必须先检查代码与测试，并在新 checkpoint 中记录重新验证结果；`wrong_workspace` 不返回 L1/L2。

## 五、MCP 与 Agent 续接协议

### 工具

新增三个 schema 相同的 MCP 工具。这里的 adapter-independent 指 Codex、Kimi 及 generic MCP client 看到相同工具名、JSON schema、错误码和 `evolvmem.continuity.v1` contract version；自动调用仍要求对应 Agent 遵循初始化 instructions。`memory_status` 增加 capabilities、contract version 和 `continuity_ready`，未知/不支持的客户端不在保证范围内。

`continuity_ready` 使用独立门禁：continuity/registry schema 版本正确、HMAC key 权限安全、active binding 都有 focus row、pointer/checkpoint 不变量通过、数据目录可读写且未处于 maintenance/rollback。它不复用当前会拒绝 compat 的普通 `_require_serving`。因此工具可在 compat/shadow/primary 注册；legacy-only 且尚未建 continuity schema 时返回稳定 `continuity_not_ready`，不能假装通过普通 Context retrieval gate。

MCP handler 必须在普通 `_context_gate_error` 之前完成 continuation intent 分类：continuation 分支只走 `continuity_ready`，普通 query 分支仍走既有 Context serving gate。否则 compat 模式会在抵达精确 pointer 前被旧门禁提前拒绝。

#### `continuity_resume`

只读。输入 transient `workspace_path` 和可选 project hint；handler 使用当前 transport session 的内部 capability 判断 `owned_by_caller`。服务端解析 project/fingerprint 后，返回精确 focus/workstream ID、Context ID、checkpoint revision、state/focus version、状态、调用者视角的 lease state、staleness 和有界 checkpoint 结构。不运行 FTS/HNSW，不返回 archive payload、绝对路径或 lease token/writer hash。

#### `continuity_checkpoint`

写工具。每次调用都要求 transient `workspace_path`；执行上述 action matrix、CAS、focus 和租约协议，不标 `readOnlyHint`。所有冲突返回稳定、无正文错误码。

#### `continuity_list`

只读。输入 transient `workspace_path` 和可选 project hint，只列当前 project/workspace 的未完成工作流元数据和 L0；不跨项目，不返回 L1/L2。仅在 focus 缺失或用户显式查看任务时使用。

### continuation intent

新增纯规则、可测试的 `ContinuationIntentDetector`。标准化空白和大小写后识别完整意图，包括：

```text
继续原任务
继续之前的任务
接着做
从断点继续
继续上次工作
resume previous task
continue previous task
pick up where we left off
```

这些短语被视为控制意图，不作为相似度查询。若用户同时提供明确新目标，则以当前用户目标为准，并把 checkpoint 只作为历史背景。

检测器还必须覆盖否定、引用和混合输入：`不要继续原任务`、讨论字符串“继续原任务”、以及“继续原任务之外，请改做 X”都不能误触发自动 resume；后者按明确新目标处理。

### `context_session_start` 行为

1. `ContextSessionStartRequest` 增加 transient `workspace_path`；Agent 传实际 cwd，服务端规范化 project/fingerprint 后立即丢弃原始路径。
2. 若命中 continuation intent，先调用 ContinuityService 的精确 resume。
3. 唯一有效 focus：渲染 checkpoint L1 加必要的 project summary L1；原子证据按 checkpoint 中的精确 Context ID 读取，仍受总预算限制。预算再小时也必须保留 objective、current step、next action、blockers、checkpoint revision 和 state version；L2 不自动披露。
4. focus 缺失但只有一个 unfinished workstream（open/paused/blocked）：返回 `needs_focus_confirmation`，不得隐式改变 focus；用户确认后先 claim 目标，再通过 `switch_focus` 真正写入指针。
5. 多候选或 project 不可识别：返回 `ambiguous` 和有界候选 L0，要求用户选择。
6. checkpoint 的 repo anchor 与当前仓库不一致：返回具体 staleness code；Agent 必须先检查当前代码和测试，不能宣称已直接续接。
7. 普通新任务继续使用现有 ContextRetriever/Renderer。`context_session_start` 保持只读；用户确认新目标后，Agent 才通过 `continuity_checkpoint(create)` 建立 workstream。已有 focus 时不得自动暂停或替换，除非用户明确切换并完成原子 focus switch。

续接块继续带“不可信历史、当前系统/开发者/用户指令与代码测试优先”的固定边界。

resume 只返回一个 primary result code，并严格按以下顺序短路：先解析 project/binding；再处理无 focus 分支；有 focus 时先校验 pointer/Context identity/L2 不变量和终态，再检查 foreign active lease，再检查 primary staleness，最后解释 workflow status。附加 flags 可用于诊断，但不得改变此安全顺序。因此 paused + foreign lease + repo changed 的 primary code 必定是 `lease_held_by_other`；lease 可用但 repo changed 时才返回 stale code；两者都通过后才返回 paused/blocked/open。

resume 决策矩阵：

| pointer/workstream | lease/repo | 结果与允许动作 |
|---|---|---|
| focus → open | lease available + fresh | 返回 checkpoint，先 claim，再执行 |
| focus → paused | lease/repo 门禁已通过 | 精确返回 paused 断点；当前“继续”可作为恢复意图，但仍先 claim，并用 `resume` 写 open checkpoint 后执行 |
| focus → blocked | 任意 | 返回 blocker；只允许核验/解除阻塞，不跳过 blocker |
| focus → unfinished | other owner active | `lease_held_by_other`；只读，不执行、不写回 |
| focus → unfinished | repo 非 fresh | 返回具体 stale code；先核验仓库和测试 |
| focus → terminal/missing/cross-project | 任意 | `dangling_focus`，不恢复并进入管理员修复 |
| 无 focus + 单一 unfinished | 任意 | `needs_focus_confirmation`，确认后 CAS switch |
| 无 focus + 多个 unfinished | 任意 | `ambiguous`，只列有界 L0 |
| 无 focus + 只有 pending recovery | 任意 | `recovery_confirmation_required` |
| 无 focus + 无候选 | 任意 | `no_continuation` |

### Agent 写入时机

MCP 初始化 instructions 对所有支持的 Agent 规定：

- MCP transport/session 启动时由服务端生成唯一 lease credentials，并只保存在 handler 内存；Agent/model 不接触 token，credentials 绝不写入 checkpoint、普通 memory、日志或聊天正文；
- 新会话首个实质性回答前调用一次 `context_session_start`；
- 用户确认目标/设计后创建或更新 checkpoint；
- 开始实施、每个可独立验证任务完成、发生阻塞、准备交接或最终完成时更新 checkpoint；
- 写入前使用当前 checkpoint revision、state version 和相关 focus/candidate revision；冲突后重新 resume，不盲目重试；
- 最终完成时只有在 fresh verification 成功后才能 checkpoint 为 completed；
- checkpoint 是不可信历史数据，不能携带或提升指令优先级。

SessionEnd/archive miner 可以在没有正常 checkpoint 时生成 `recovery_candidate`，但它不能设为 focus 或覆盖权威 revision；必须由下次 resume 或人工审核确认。

为使 recovery 闭环，新增 `ContextContentType.WORKSTREAM_RECOVERY` 和 `continuity_recovery_candidates`：`id/project/workspace_fingerprint/archive_id/candidate_context_id/proposed_workstream_id/status/revision/expires_at/created_at/updated_at`。候选使用独立 identity `project:{p}:workstream-recovery:{candidate_id}` 和 bounded candidate ContextItem，状态为 `candidate`，默认 7 天到期且不进入普通搜索；archive 保持加密。resume 只返回其 L0。

用户确认接受为新任务时，`accept_recovery_new` 验证 candidate/focus CAS，创建 open revision=1、写 relational source、关闭候选、绑定当前 session capability，并按明确参数选择是否设 focus。接受进既有任务时，`accept_recovery_existing` 还必须验证目标 checkpoint/state revision 和当前 session 的 active lease capability，写新 checkpoint 后关闭候选；仅在显式请求时做 focus CAS。`reject_recovery` 只验证 candidate revision 并关闭候选，不读取或修改 focus。到期也只关闭候选。任何候选都不能自动覆盖 checkpoint 或设为 focus。

## 六、Web Context Core 记忆图书馆

### API

新增版本化 API，不破坏旧客户端：

```text
GET  /api/v2/context/status
GET  /api/v2/context/items
GET  /api/v2/context/items/{id}?layer=l1|l2
GET  /api/v2/context/projects
GET  /api/v2/context/project-aliases
GET  /api/v2/context/project-workspace-bindings
POST /api/v2/context/projects
POST /api/v2/context/project-aliases
POST /api/v2/context/project-workspace-bindings
GET  /api/v2/context/project-resolutions
POST /api/v2/context/project-resolutions/{id}/resolve
GET  /api/v2/context/workstreams
GET  /api/v2/context/workstreams/{id}
PATCH /api/v2/context/items/{id}
POST /api/v2/context/items/{id}/archive
POST /api/v2/context/items/{id}/restore
POST /api/v2/context/items/{id}/delete
```

status 返回 schema/readiness、maintenance stage、mapping/resolution/vector/continuity 门禁，不含路径。列表使用有上限的 cursor pagination 和稳定 `(updated_at DESC, id DESC)` 排序，默认只返回 ID、identity、project、scope、content type、status、tier、confidence、importance、L0、mapping/source 状态和时间。L1 只在详情读取，L2 必须由精确 ID 和显式操作获取；sources/evidence/resolution 也按页、有硬大小上限。错误面不返回 traceback、正文或数据目录。

schema migration 为 `context_items` 增加递增 `row_version`；metadata/lifecycle mutation 每次增加它。所有 v2 mutation 要求对应的 `expected_revision/row_version` CAS，并经过 ContextService typed boundary。服务只监听 loopback 时可使用本地 operator token；若绑定非 loopback，写接口必须同时启用 operator token 与 Origin/CSRF 校验，否则启动失败。默认 UI 不暴露 physical hard delete。

### 页面

默认页改为 Core 视图：

- 按 project/content type/status/tier/resolution state 筛选；
- 明确把 tier 与 L0/L1/L2 分开；
- 列表展示 L0，不展示 legacy 原文；
- 详情按需读取 L1/L2，并显示 sources、supersession 和 project resolution evidence；
- 单独的“项目归属待审”和“当前工作流”页；
- rolling project summary 显示来源覆盖时间和 pending/stale 状态；
- legacy 数据保留在“兼容投影”页，明确标注非主存储；默认 UI 只读展示该页，不把 legacy value 当作 Core L0/L1/L2。

所有 mutation 继续经过 ContextService typed boundary，Web 不直接持有任意 SQL 写权限。

兼容承诺明确为：旧 `GET /api/memories` 及现有 `/api/memory/{id}/*` mutation endpoint 在本次版本继续保留。旧 mutation 在 compat/shadow 继续通过 typed facade 双写，在 primary 通过 ContextService 写 Core 后更新 projection；不会静默退化为直接写 legacy，也不会无公告返回 410。后续若废弃另行版本化，不与本次默认页面切换捆绑。

## 七、失败、安全与并发策略

### 失败行为

- project resolver 异常：保持原 project，记录 bounded reason，不能猜测。
- rollup 在 SQLite 提交前异常：保留旧摘要和新原子证据；提交后仅向量失败时按 `vector_dirty` 语义处理。
- checkpoint revision/lease 冲突：整体回滚并要求重新 resume。
- workspace/writer HMAC key 缺失、权限不安全或 fingerprint 变化：`continuity_ready=false`，只提供无正文诊断，不自动换 key。
- Context 向量失败：SQLite 已提交结果保留，设置 dirty marker；精确 continuity 仍可读，普通召回降级为 SQLite FTS。
- primary 不满足新门禁：保持 shadow/compat 或回退 legacy，不部分切换；continuity 是否可用由独立 `continuity_ready` 决定。
- Web v2 不可用：旧兼容页仍可诊断；旧 mutation endpoint 只能经过 typed service/facade，不允许绕过服务边界写旧库。

### 项目与权限边界

- Continuity 的 resume/list 永不跨 project/workspace fingerprint。
- `continuity_resume/list` 标 `readOnlyHint=true`；checkpoint 是写工具。
- project resolution 人工修改和生产 apply 留审计时间、reviewer hash、run ID。
- 所有来源内容视为不可信历史，当前代码、测试和当前用户要求优先。
- 任何公开报告、MCP 返回和日志不包含原始 archive、绝对路径、token 或完整正文。

### 一句话续接的保证边界

系统保证：当前 workspace 能唯一解析到项目、该 `(project, workspace_fingerprint)` 存在有效 focus、repo 为 fresh 且 lease 可取得时，用户只说“继续原任务”即可得到精确 checkpoint，并从 `next_action` 继续，不需要 Codex 本地历史。lease 被占用、repo 已变化或任务 blocked 时仍能精确召回，但会停在对应安全门禁，不假装已经继续执行。

系统不伪造保证：项目无法识别、focus 损坏或真实存在多个无焦点任务时，必须返回歧义而不是猜测。这是防止跨项目泄漏和错误续做的安全边界。

## 八、测试与验收

### 确定性单元/集成测试

- ProjectResolver 的每种信号、可信/已知错误生成器版本、通用目录、alias、冲突、未知、global 和敏感输入。
- registry/alias/workspace binding 的唯一约束、meta/row 双 CAS、candidate → active、default 选择、多绑定无 default 歧义及空 focus row revision=0。
- 新写入不再产生可解析但 `project=''` 的项目项。
- 历史 dry-run 零写入、锁内重算与 digest/配置漂移门禁、apply 幂等；单阶段故障事务全回滚，多阶段崩溃留下准确 stage 并可重入或受保护 rollback。
- maintenance failed 时保留最后成功 stage/failed_stage，重试只能回到同一阶段；所有 typed 生产写事务恰增一次 mutation epoch，旁路写由 canonical digest 检出。
- 已映射 project 回填和 451 条补迁移保持 L0/L1/L2、状态、时间、来源和 supersession。
- 每项目唯一 active project summary；相同 source set 跳过 LLM；生成失败保旧，向量失败保新并 dirty；来源覆盖后才归档日志。
- session summary 默认 30 天、已覆盖时最多 10 条 active；未覆盖到期项保持 `rollup_pending` 和 archive hold。
- Web v2 列表不泄露 L1/L2，精确详情逐层披露，待审操作使用 revision CAS；分页排序稳定，非 loopback 写接口缺 token/Origin/CSRF 时启动失败。
- continuation intent 不调用 FTS/HNSW；否定句、引用文本和混合新目标不误触发。
- create CAS 成功路径、ID/parent/source/focus 冲突和两个并发 create-focus 只有一个成功。
- focus 空指针永不删行、clear 后 revision 增加、无 focus resume 返回 revision；原子 switch 失败保持旧指针。
- update CAS 失败后不残留 ContextItem/layer/source/event，且旧 checkpoint 仍 active。
- checkpoint revision 与 lease state version 独立；完整 workflow 状态迁移表和非法迁移拒绝。
- transport session credentials 由服务端生成且不出现在工具 schema/output/log；两个 session 即使客户端标签相同也不能共用 lease；owner claim/renew/release、非 owner 拒绝、UTC 过期边界和进程崩溃后接管。
- 所有 continuity mutation 缺 transient workspace path 都拒绝，服务进程 cwd 不参与 fingerprint/anchor。
- 原子 focus switch/clear；候选确认后 focus 真正写入；失败时旧 focus 不变。
- pause/block/complete/cancel、focus 清理和 parent/child 工作流；跨项目 parent、self-parent 和父链循环被拒绝。
- source ID 不存在、deleted、跨项目或不可见时整笔拒绝。
- completed/cancelled 不会被恢复。
- repo anchor 的正交 flags、固定 primary-code 优先级和 fresh/head advanced/branch changed/diverged/dirty changed/unknown/wrong workspace 判定。
- paused、blocked、terminal、悬空 focus、无 focus、多候选、空 project 和他人 lease 的完整 resume matrix，不跨项目。
- current Context pointer 指错 workstream identity/type、L2 ID/revision/status 不一致时 readiness 失败且 resume 返回 dangling/corrupt，不得串任务。
- recovery candidate 的生成、7 天到期、accept-new/accept-existing/reject 三条独立 CAS 流程和永不自动设 focus。
- 普通 ContextRetriever/renderer 即使 FTS/HNSW 命中也不自动返回 checkpoint/recovery content types。
- 极小注入预算仍保留 objective/current/next/blockers/revisions，且不自动泄露 L2。
- 写权限被拒绝时 Agent 明确报告 checkpoint 未保存，旧断点保持权威。
- MCP tools/list、annotations、capability discovery、schema、错误码和 instructions 在 compat/shadow/primary 及所有承诺 adapter 上一致。

### 迁移验收

- SQLite backup 可独立打开且 `quick_check=ok`。
- maintenance run 从 planned 到 verified 的阶段记录完整；锁内数据库/config digest 与批准 plan 一致。
- pre epoch 后的 mutation journal 全属当前 run；post state/config digest 与 manifest 一致。
- mapping lag 为零，每个 mapped item 恰有 L0/L1/L2。
- 可解析的 active project-scope item project 不为空。
- conflict/unresolved 数量与 plan 完全一致并全部进入待审。
- 有合格来源且 rollup 成功的 resolved 项目恰有一条 active project summary；日志归档只包含已覆盖 ID。
- Context 向量数与 active L0 文档数一致。
- 第二次 plan/apply 零变化。
- rollback 在 mutation epoch 漂移时拒绝；无漂移时通过 SQLite Backup API 恢复并重建向量/config。

### 两 Agent 一句话验收

在唯一临时数据目录中运行两个相互独立、无共享 Codex transcript、无法读取 `.codex` session history 的 ephemeral Agent 进程：

1. Agent A 创建唯一 canary workstream，写入已确认设计、当前步骤、下一动作和一条通过验证；repo anchor 由服务端采集，然后 Agent A 释放 lease。
2. 结束 Agent A；不向 Agent B 提供 A 的 prompt、转录或手工摘要。
3. Agent B 的唯一用户输入是“继续原任务”。
4. 事件流必须显示先调用 `context_session_start`，其 continuation 分支精确读取同一 workstream/context ID/revision。
5. 事件流不得调用 `memory_search` 或 `context_search` 来猜任务。
6. Agent B 必须复述 objective/current step/next action，核对 repo anchor，使用当前 state version claim lease，然后从 next action 执行一个无副作用 canary 步骤。
7. Agent B 使用 expected checkpoint revision 和 state version 写下一 checkpoint；checkpoint revision 恰加一。
8. 另一个并发 writer 使用旧 revision/version 必须得到稳定 conflict，且数据库无残留。
9. 清理只针对 canary ID 和临时目录。

上述流程先执行 Codex → fresh Codex，再执行 Codex → 另一个声明支持 `evolvmem.continuity.v1` 的 Agent/generic MCP harness，证明交接依赖 EvolvMem 合约而不是某一客户端历史。

临时验收通过后，再在真实库备份和迁移门禁通过的前提下创建一个授权 canary，使用全新真实 Agent 会话重复同样流程。事件审计必须证明唯一恢复来源是 EvolvMem continuity tools；失败时不宣称切换完成，并保留可恢复备份和无正文诊断报告。

## 九、实施顺序

1. 隔离当前 dirty 工作区并建立可恢复开发分支。
2. ProjectResolver、resolution schema 和新写入 project 修复。
3. 历史 plan/apply/verify/rollback 与 project 回填。
4. project summary、session summary TTL/保留策略和 adapter 接入。
5. Web v2 Core API 与默认页面。
6. continuity workstream/checkpoint/focus/CAS/lease 与 recovery candidate 领域层。
7. MCP 工具、capability/readiness、session-start continuation 路由和 Agent instructions。
8. 全量自动测试、shadow 对比和临时两 Agent 验收。
9. 真实库 backup、dry-run、apply、verify、向量重建和渐进 cutover。
10. 真实新会话一句话验收、Web 检查、修复记录与最终代码审查。

每一步以独立 TDD 任务实施和提交。任何真实数据 mutation 都晚于代码测试、backup 和 dry-run；历史内容只归档、不物理删除。

# EvolvMem 项目归属、滚动摘要与极简续接设计（Lite 范围）

**日期：** 2026-09-01

**状态：** 用户已评审并确认本范围（B 路线），取代原三计划套件

**前置文档：**

- `2026-09-01-evolvmem-project-continuity-design.md`（完整版设计，本文档为其裁剪子集）
- 原三份实施计划（`2026-09-01-evolvmem-project-memory-cleanup.md`、`-context-web-v2.md`、`-continuity-protocol.md`）**作废**，仅作历史参考，不再执行。

## 范围决策记录

用户评审完整版设计后确认：诊断与核心思路（确定性项目归属、精确指针续接、滚动摘要收敛）全部保留；以下机制按"单用户、可信内网、写入并发极低"的实际威胁模型裁掉：

- **裁掉** transport session 租约与 256-bit capability（用 revision CAS 防丢失更新即可；并发冲突最坏结果是后写者收到 `revision_conflict` 后重新 resume，无静默覆盖）。
- **裁掉** mutation epoch、mutation journal、canonical state digest 全局记账（一次性迁移的回滚安全由"apply 前 SQLite 一致性备份 + 锁内 plan digest 复算"承担，不给每个写路径永久加税）。
- **裁掉** maintenance 六阶段状态机与 staged run 记录（plan/apply/verify 三个 CLI + 幂等性验证足够覆盖一次性回填）。
- **裁掉** recovery candidate 子系统（崩溃会话无 checkpoint 时，`continuity_resume` 返回 `no_continuation`，退回普通检索）。
- **裁掉** worktree dirty 摘要的 HMAC（repo 锚点只记 branch/root/head commit；staleness 不含 `worktree_changed`）。
- **推迟** Web v2 整块（17 个端点与新页面）到核心链路跑通之后；项目归属待审先用 CLI 处理。
- **保留** Task 1 已交付的 ProjectResolver 与 WorkspaceIdentityProvider（HMAC 工作区指纹已建成、有测试，继续作为 focus/bindings 的工作区身份；key 缺失时对应功能 fail-closed，不影响其余能力）。

已完成的旧计划 Task 2（mutation epoch）WIP 已作废，补丁存档于 `~/.local/state/evolvmem-project-continuity/task2-epoch-wip-abandoned.patch`。

## 目标

1. 新写入与历史记录都有确定性的项目归属；冲突或信号不足进待审队列，不让模型猜。
2. 每项目一条版本化滚动知识摘要收敛无限增长的 `progress:log`；原始日志默认 30 天 TTL，被摘要覆盖后才归档。
3. 用户只说"继续原任务"即可从精确断点继续：每 `(project, workspace_fingerprint)` 至多一个 focus 指针，指向版本化 workstream checkpoint，续接不过语义搜索。

## 非目标

- 不做多租户级并发控制（lease/capability）。
- 不做 Web v2 页面与 v2 API。
- 不做崩溃会话的自动恢复候选。
- 不物理删除任何历史记录；不向 MCP 响应、日志、报告写入正文、绝对路径、token 或凭据位置。

## 一、项目归属

### 组件复用

直接使用已提交的 `evolvmem/project_resolver.py`（ProjectResolver）、`evolvmem/workspace_identity.py`（WorkspaceIdentityProvider）、`evolvmem/project_models.py`。判定规则与信号优先级完全沿用完整版设计第一章：强信号单一采信，两个独立中等信号一致方可自动 resolved，冲突/无信号分别进入 conflict/unresolved，global 类型（constraint/preference/user_profile）不强制 project。

### Schema（一次迁移，PRAGMA user_version 递进）

```text
context_project_registry(project PK, status active|archived, revision, created_at, updated_at)
context_project_aliases(alias PK, project -> registry, revision, created_at, updated_at)
context_project_workspace_bindings(
    workspace_fingerprint, project -> registry,
    state candidate|active|revoked, is_default, method, revision, created_at, updated_at,
    PK(workspace_fingerprint, project))
  -- 部分唯一索引：同一 fingerprint 至多一条 active 且 is_default=1
context_project_resolutions(
    item_id PK -> context_items.id,
    resolution_state resolved|conflict|unresolved|global|ignored,
    decision_source automatic|human|none,
    review_state not_required|pending|accepted|rejected,
    proposed_project, resolved_project, confidence high|medium|none,
    method, evidence_json(canonical bounded), resolver_version,
    revision, reviewed_at, created_at, updated_at)
```

不建 registry meta 表与 append-only events 表（revision CAS 已在行内；审计需求低）。alias 全库唯一；registry 为运行时权威，config 的 `context_project_aliases` 只在迁移时 seed 一次，之后漂移仅诊断。

### 新写入接线

ContextService 的 typed 写入口（含 legacy facade 双写路径）接受可选 transient `workspace_path` 与 `project_hint`；服务端立即用 WorkspaceIdentityProvider 计算 fingerprint 并丢弃原始路径。Core draft 的 `project` 只取 ProjectResolver 结果；resolved 写 project 并落 resolution 行（review_state=not_required），conflict/unresolved 保持 `project=''` 并落 resolution 行（review_state=pending）。`project_summary`、`workstream_checkpoint` 内部类型必须显式携带合法 project，不参与启发式。

adapter（kimi_hooks、dsh_bridge、MCP typed 请求）必须传当前工作目录；未传时按无 workspace 信号处理，不报错。

### 待审与 CLI

CLI 提供：`projects list/register/archive`、`aliases list/add/remove`、`bindings list/bind/revoke/set-default`、`resolutions list-pending/accept/reject`。审核动作用行内 revision CAS；accept 把 `resolved_project` 写入 resolution 与对应 ContextItem（同事务），reject 保持 project 为空。binding 的 candidate→active 只能经 CLI `bind` 确认。

## 二、滚动项目摘要与会话日志 TTL

沿用完整版设计第二章，不裁剪：

- `ContextContentType.PROJECT_SUMMARY`，identity `project:{project}:knowledge:current`，每次更新新 ContextItem、旧版 superseded，唯一 active identity 索引保证每项目一条当前摘要。L0/L1/L2 内容契约不变（L2 只存结构化来源映射）。
- rollup 触发点：session summary 成功提交后 best-effort 执行；LLM 生成在 SQLite 事务外；验证失败保旧摘要；提交后向量失败置 `vector_dirty`。
- `context_project_rollups(project PK, current_context_id, source_set_hash, covered_through, generator_version, status pending|ready|failed|vector_dirty, revision, updated_at)`；相同 source set + generator version 跳过 LLM。
- 新 session summary 写入默认 `expires_at = created_at + 30 days`（config 可调）；每项目最多保留最近 10 条 active 原始摘要；TTL 归档与数量归档都以"该 summary Context ID 已出现在 ready rollup 的 relational source closure"为前置。
- `session_archive_holds(archive_id, source_context_id, reason, created_at)`：相关 encrypted archive 在覆盖成功或人工处置前不得 purge。
- 未覆盖到期记录保持 active 并派生 `rollup_pending` 状态（在 rollup 水位表与 status 输出可见），不静默丢失。

冲突策略不变：摘要不得把冲突项写进"稳定知识"，写入"待确认变化"并列出双方 Context ID。

## 三、极简续接（continuity lite）

### 检查点

`ContextContentType.WORKSTREAM_CHECKPOINT`，identity `project:{project}:workstream:{workstream_id}:checkpoint`，版本化 supersession 链。L0/L1/L2 契约沿用完整版设计（L2 canonical JSON schema_version=1，含 workstream_id/project/workspace_fingerprint/checkpoint_revision/objective/accepted_decisions/completed_steps/current_step/next_action/blockers/status/repo/source_context_ids）。

- ContextRetriever 默认 content type 集合与自动注入**显式排除** WORKSTREAM_CHECKPOINT；FTS/HNSW 命中也不得绕过。
- 禁止字段不变：绝对路径、环境变量值、token、patch 正文、完整终端输出；字符串/数组硬上限；写入前过敏感信息检测。
- `source_context_ids` 必须存在、未删除、同 project 或 global，否则整次回滚；parent 同 project/workspace，禁止 self-parent 与父链循环。

### 表结构（无 lease 列）

```text
continuity_workstreams(
    id PK, project, workspace_fingerprint, parent_id NULL -> continuity_workstreams.id,
    current_context_id -> context_items.id, checkpoint_revision, state_version,
    status open|paused|blocked|completed|cancelled,
    repo_kind git|non_git, repo_branch, repo_root_commit, repo_head_commit,
    created_at, updated_at, completed_at NULL)
continuity_focus(
    project, workspace_fingerprint, workstream_id NULL -> continuity_workstreams.id,
    revision, updated_at, PK(project, workspace_fingerprint))
continuity_events(
    id PK, workstream_id, event_type, before_revision, after_revision,
    before_state_version, after_state_version, error_code, created_at)
```

`checkpoint_revision` 只在新 checkpoint 内容时 +1；`state_version` 对任何 mutation +1。focus 行随 active binding 预建（`workstream_id=NULL, revision=0`），clear 只置 NULL 并 revision+1，永不删行。

### CAS 与动作矩阵

所有写 action 携带 `expected_checkpoint_revision` 与 `expected_state_version`（create 时两者为 0）；focus 相关另带 `expected_focus_revision`。任一条件更新 rowcount≠1 → 整体回滚 + 稳定错误码 `revision_conflict`，调用方重新 resume 后重试。并发双 create 由唯一 active identity 索引与 focus CAS 保证只有一个成功。

动作：`create / update / pause / resume / block / unblock / complete / cancel / switch_focus / clear_focus`。状态迁移白名单与完整版设计一致（completed/cancelled 终态拒绝一切 mutation；update 不暗中 resume/unblock）。create/update 写新 ContextItem；pause/block/resume/unblock/complete/cancel 也写新 checkpoint（内容与状态同步演进）；switch_focus/clear_focus 只动指针。

### 仓库锚点与 staleness

服务端从 transient workspace_path 采集：Git 仓库记 root commit、branch、HEAD；非 Git 只记 `kind=non_git`。resume 返回正交 flags 与唯一 primary code，优先级：

`wrong_workspace > unknown > head_diverged > branch_changed > head_advanced > fresh`

（无 `worktree_changed`。）只有 `fresh` 可直接执行 next action；`wrong_workspace` 不返回 L1/L2。

### MCP 工具与 session_start 路由

新增三个工具（compat/shadow/primary 均可注册；continuity schema 未建时返回稳定 `continuity_not_ready`）：

- `continuity_resume(workspace_path, project_hint?)` — readOnly；返回精确 focus/workstream ID/Context ID/revisions/status/staleness 与有界 checkpoint 结构；不过 FTS/HNSW；不返回绝对路径与正文外数据。
- `continuity_checkpoint(action, workspace_path, ...)` — 写工具；上述 action 矩阵 + CAS。
- `continuity_list(workspace_path, project_hint?)` — readOnly；只列当前 project/workspace 未完成 workstream 的元数据与 L0。

`context_session_start` 增加 transient `workspace_path`；handler 在普通 Context serving gate 之前先做 continuation intent 分类。`ContinuationIntentDetector` 为纯规则实现，覆盖完整版设计列出的中英文意图短语与否定/引用/混合反例。resume 结果矩阵（裁剪 lease 分支后）：

| 场景 | 结果 |
|---|---|
| focus → open/paused + fresh | 返回 checkpoint，直接续做（paused 先按 resume 语义更新状态） |
| focus → blocked | 返回 blocker，不跳过 |
| focus → 非 fresh | 返回具体 stale code，先核验代码与测试 |
| focus → terminal/缺失/跨项目 | `dangling_focus`，管理员修复 |
| 无 focus + 单一 unfinished | `needs_focus_confirmation`，用户确认后 switch_focus |
| 无 focus + 多个 unfinished | `ambiguous`，只列有界 L0 |
| 无 focus + 无候选 | `no_continuation`，消息的其余部分走普通检索 |

续接块继续带"不可信历史，当前指令与代码测试优先"的固定边界；极小注入预算也必须保留 objective/current_step/next_action/blockers 与 revisions，L2 不自动披露。

MCP 初始化 instructions 增加：首个实质性回答前调用 `context_session_start`；目标确认后 create checkpoint；里程碑/阻塞/完成时 update；写前用最新 revision，冲突后重新 resume；最终完成须有新鲜验证才允许 complete。

## 四、一次性历史回填（maintenance CLI）

三个子命令，不要状态机：

- `evolvmem maintenance plan`：只读。输出 legacy 总数/已映射/未映射、resolver 四态计数、逐项目摘要与日志归档预览、collision 预检、以及覆盖（数据库只读指纹 + schema/resolver/generator 版本 + registry/alias digest + canonical actions）的 plan digest。复跑 digest 必须一致。
- `evolvmem maintenance apply --plan-digest <d>`：取得 cutover 独占锁 → 锁内重算 plan 并核对 digest → SQLite Backup API 一致性备份 + 独立打开 `quick_check` → 单一外层事务：未映射 legacy 全部迁入 Core、resolved 回填 project、写 resolution 行、碰撞项进 conflict 待审 → 提交后逐项目生成 rollup → 按覆盖归档日志 → 重建 Context 向量。任一步失败保留备份路径与无正文诊断；回滚 = 用该次备份经 Backup API 恢复 + 重建向量（手动确认后执行，不做自动 drift 门禁）。
- `evolvmem maintenance verify`：mapping lag 为零、每个 mapped item 恰有 L0/L1/L2、resolved 项 project 非空、conflict/unresolved 计数与 plan 一致、每 resolved 项目恰有一条 active project_summary、向量文档数与 active L0 一致、第二次 plan/apply 零业务变化。

**真实库执行门禁**：apply 真实 EvolvMem 数据库前必须取得用户显式批准，且先验证备份可独立打开。

## 五、测试与验收

确定性测试覆盖：

- resolver 信号矩阵沿用 Task 1 已有测试；registry/alias/binding 唯一约束、candidate→active、default 选择、多 active 无 default 歧义、focus 空行预建。
- 新写入不再产生可解析但 `project=''` 的项目项；conflict/unresolved 进 pending。
- rollup：唯一 active、相同 source set 跳过 LLM、失败保旧、向量失败 dirty、覆盖后才归档、未覆盖到期保持 rollup_pending。
- checkpoint：create/update CAS、并发 create-focus 唯一成功、非法迁移拒绝、跨项目/自父/循环 parent 拒绝、source ID 校验、终态拒绝、focus switch/clear 原子性、空 focus 行 revision 语义。
- intent 检测：意图短语、否定、引用、混合新目标不误触发；continuation 分支不调用 FTS/HNSW。
- staleness 五码与优先级；`wrong_workspace` 不泄露 L1/L2。
- retriever/renderer 即使向量命中也不返回 checkpoint 类型。
- maintenance：plan 只读且 digest 稳定、apply 幂等（二次零变化）、单阶段失败事务全回滚、备份 quick_check=ok。
- 验收收尾：全量 pytest、ruff、主 checkout 脏 WIP 哈希不变、修复记录按 `~/fix-records/README.md` 如实写入。

## 六、实施顺序

1. B1 项目归属（schema + 写入接线 + CLI 待审）。
2. B2 滚动摘要 + 日志 TTL。
3. B3 极简续接（域层 + MCP 工具 + session_start 路由）。
4. B4 maintenance CLI 与真实库回填（末步，用户批准后执行）。

每步独立 TDD 任务、单独提交；全部在 worktree `.worktrees/evolvmem-project-continuity`（分支 `feat/evolvmem-project-continuity`）实施；主 checkout 的 skill 蒸馏/会话挖掘 WIP 保持原样不触碰。

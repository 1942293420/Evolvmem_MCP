# EvolvMem 记忆信任更新（2026-10-02）

用户已批准本轮五项改进。本文是本轮唯一设计/说明文档，不新增 plan/spec/review
文档。第一工作包（归属可信、决策时间有效性、验收 runner）与第二工作包（只读
同步链可观测性、有来源的项目知识页）均已落地。

边界：可信内网、单机 SQLite；复用现有 store/retriever/service/recall/Web API
与飞书登录，不新增依赖、数据库服务或遥测协议。不从自由文本推断项目，不自动
迁移或改写历史业务数据，不自动调用模型生成知识页。测试只用合成夹具。

## 1. 项目归属与召回可信

- `evolvmem/project_ownership.py`：只读既有 `context_project_resolutions`，
  条目分为 `confirmed`（resolved/人工接受）、`excluded`（pending/conflict/
  unresolved/rejected/无 resolution 行）、`unverified`（global/ignored）。
- 默认注入面（`session_start`）与项目进展召回排除 `excluded`；无 resolution
  行的项目条目被扣留（`project_ownership_unverified`），确认后才回到默认面。
  只对声明了 `project` 的条目设闸门；空归属历史条目不因此被压制。
- 结构化有界诊断：`context_project_recall` 返回 `diagnostics`（命中表面、匹配
  项目、selected_ids、unverified_ids、excluded=[{id,reason}]、ownership_detail）。
  只含 id、registry 名称与稳定原因码，不含正文、路径、query 原文。
- 纠正入口：`GET /api/resolutions?state=unreviewed` 列出无 resolution 行；
  `POST /api/resolutions/<id>/accept|reject` 走 CAS。跨项目移动被绑定工作流的
  checkpoint 以 `workstream_project_mismatch` 拒绝，不产生半移动。

## 2. 决策的时间有效性

- `evolvmem/context_temporal.py`：ISO 8601 统一归一化为 naive UTC；缺省/`unknown`
  保持 UNKNOWN，**绝不从 `created_at` 推断事件时间**；`effective_until` 排他。
- `context_items` 增量列 `effective_from/effective_until/occurred_at/mentioned_at`；
  `ContextItemDraft`/`ContextItem`/`ContextSearchResult` 暴露字段与 `temporal_state`。
- 默认召回排除已知窗口已过期或尚未生效的记录。**继任边界**：同一身份上已排定
  但尚未生效的继任者不抹掉当前生效的前任（`current_predecessor`）；继任者到期
  后由其优先。
- **同身份优先（2026-10-02 评审收紧）**：适用性按完整的同身份 family（同
  `identity_key` + project + scope）解析，与哪条文字恰好命中查询或 `top_k`
  无关。同一身份同时落在各自窗口内时只呈现最新可适用的一条——active 继任者
  优先于 superseded 前任，否则比较已知时间 rank；命中的过期历史不会因为新记录
  文字不同而复现（此时返回空，而不是旧行）。被取代行及其显式 `effective_until`
  原样保留在存储中（历史可查），但不再作为第二条“当前有效”记录返回；`as_of`
  历史读取同样适用。未知日期的 superseded 与非 decision 的 superseded 仍不参与；
  已排定但未生效的继任者仍不抹掉当前生效的前任。知识页当前规则使用同一解析。
- 迟到补录：新记录以 `superseded` 历史行保留（`superseded_by` 指向当时 active 行），
  现有 active 行零改写。
- 暴露：`ContextService.set_decision_window/decision_window`、MCP
  `context_decision_window`（读写）与 `context_search(as_of=...)`、Web
  `GET/POST /api/decision-window`。GET 只读，POST 部分更新。

## 3. 只读同步链可观测性

- `evolvmem/trust_views.py`：`sync_chain()` 只按服务端已持久化的回执组装
  `client_capture → upload_archive → extraction → recall → hook_delivery →
  model_adoption`，每阶段给出 `state/time/reason/backlog/evidence`。
- 服务端只能证明归档与提炼：上传/归档读 `lan_session_uploads` 与
  `session_archives`（`evidence_at` 取归档创建时间；分片接收中取最近上传回执
  时间并标注依据）；提炼分别保留 `extracted/queued/failed/not_requested/
  superseded` 计数与 `last_success_at/last_failure_at`，混合状态不互相掩盖。
- 不可观测即如实标注：本机采集 `unknown`、召回 `unknown`
  （`retrieved_for_hook=unknown`）、钩子投递 `unknown`、模型采纳 `unverified`。
  绝不从连接、access_count、提炼成功或已配置钩子推断。
- 原生 Windows `-Action status` 的 `sync_chain` 只取本机既有 worker/launcher/
  upload 回执（计数、状态、时间），不含消息正文、路径、session ID 或令牌。
  `worker_status=idle` 不再单独证明采集成功：只有观察到 `captured_versions>0`
  且无采集/发现错误才报 success 并给出时间；零采集如实标 `no_capture_receipt`
  的 unknown，采集/发现失败按其自身计数报 error，不占用上传队列 pending 计数。
- 接口：`GET /api/sync-chain`；页面：`GET /trust`（首页导航“信任视图”）。

## 4. 有来源的项目知识页

- `GET /api/knowledge?project=<name>`：读时组合，不调用模型、不写库。三段：
  当前规则、最新进展、未决事项。
- 覆盖与刷新：复用 rollup 水位（`covered_through`）与来源集合；新来源或
  **已覆盖来源被修改**（`updated_at` 超过水位）→ `needs_refresh`。摘要失败时
  保留上一次成功内容并标注 `failed`，绝不显示为最新。
- 来源可信闸门（`knowledge_snapshot`）：对来源应用归属 resolution、最低置信度、
  窗口有效性与行状态；被扣留来源以 `held_sources` 报告，不计入可信覆盖；来源
  被扣留时整体标注 `source_hold`，不把摘要显示为新鲜可信。
- 默认面闸门：`latest_progress` 指向的摘要行必须 active、同项目、未过期、在窗口
  内、归属未扣留；否则扣留内容并在未决事项列出原因。`current_rules` 额外检查
  最低置信度、过期、窗口，且只有 DECISION 允许 future-successor 前任规则。
- 当前工作流指针：读取 `continuity_workstreams.current_context_id` 精确断点，
  同一批闸门后给出 status/current_step/next_action/blockers；成功测试不推断完成。
- 未决事项：归属评审队列 + 进行中（open/paused/blocked）任务断点 + 被扣留来源/
  断点；删除、pending、conflict 归属不进入当前规则与最新进展。知识页队列按当前
  项目过滤（不混入其他项目）；独立评审 API `GET /api/resolutions?state=unreviewed`
  仍列出全部无 resolution 的非删除项目条目（不限 content type）。

## 5. UI

- 新增 `GET /trust`（`web_static/designs/trust.html` + `web_static/trust.js/css`），
  沿用 signal 首页/insights 的浅灰底、白面板与既有飞书登录/CSRF；新增控件使用
  紫色强调。首页 `signal.html` 导航加入“信任视图”。
- 默认文案为简短中文（项目知识、来源覆盖时间、归属待确认、生效时间、等待提炼、
  最近刷新失败等）；原始字段名、原因码、revision 等只出现在折叠的“技术细节”。
- 归属评审：每条可选择实际目标项目后确认；无 resolution 行（revision 0）同样
  可以拒绝，写入一条 rejected 行（安全 CAS，重复/过期拒绝返回
  `revision_conflict`），不动项目、身份键与正文。UI 使用
  `/api/resolutions/<id>/accept|reject`，服务端同时保留旧
  `/api/resolution/<id>/...` 单数路径，两类调用者都可用。
- 时间：所有时间以 UTC 保存、显示，并在界面明确标注 UTC（不是北京时间）；
  输入可带时区偏移（如 `+08:00`）或 `Z`，保存时统一换算为 UTC。

## 6. 验收与边界

- `evolvmem/trust_acceptance.py` + `scripts/memory_trust_acceptance.py`：13 个
  真实边界用例（临时数据目录、纯合成夹具、无外部模型/网络），含
  `failed-knowledge-refresh`（失败 rollup 保留旧内容 + needs_refresh）与
  `knowledge-needs-refresh`（新来源 → stale）。命令：
  `python scripts/memory_trust_acceptance.py`（`--json` 可机器读取）。
- 逐例断言固化在 `tests/test_memory_trust.py`、`test_memory_trust_repair.py`、
  `test_memory_trust_views.py`（只读 API、阶段状态、知识来源边界、revision 0
  拒绝、LAN 修订触发器）。
- **未验证**：真实桌面模型是否采纳召回内容（`model_adoption=UNVERIFIED`）；
  真实 Windows 本机采集/钩子投递是否发生；真实 Codex/Kimi 客户端回环。
- 本机 Windows 客户端不可达时，服务端如实显示 unknown，不绕过、不推断。

## 本轮实际验证

- 主助手独立全量回归：2248 passed、21 failed、54 skipped；21 个失败 ID 与
  修改前基线一致，无新增失败；新增 92 项通过，原生 Windows 新用例未执行。
- 离线验收 13/13；Chrome 实际完成确认/拒绝归属及保存时间，运行异常为 0。
- 图册 12 个专题均 9/9、0 错误/警告；相关四个专题已检查明暗主题，页面已检查
  桌面和手机显示。视觉状态记录在既有图册 receipt，不代表 Windows 实机验收。

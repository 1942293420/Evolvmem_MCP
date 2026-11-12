# 项目权限接入现有 Host 与 Writer

当前状态：候选代码准备阶段，默认关闭 PG；没有切换生产，没有执行本差量的 SQL。当前只有 memory_add、memory_replace、memory_remove、context_read 四个业务工具；库与项目 ACL 管理是另两个独立工具，不代表完整召回已接入。

## 请求路径

现有 HTTP handler → LibraryHost 的 request_scope/serve_rpc → AuthorityRouter、SessionManager、AuthorityLease → resolve_project_scope → executor/core → 正文、revision、receipt、vector_outbox → 租约内发布。入站凭据仍须匹配注册 principal/credential/device；获得跨设备库授权不允许冒充该库的设备身份。

业务请求必须传 library_id 和已注册 project_key；服务端使用当前内容连接、实际 session_user、当前 epoch 与 allows 验证，并解析稳定 UUID。拒绝 caller project_id、身份和连接参数。写入还需稳定 request_id。显式 project_key:null 仅用于 context_read，且只让库的实际 owner 读取未归类数据；缺省项目、NULL 写入和未授权项目都拒绝。

core 的所有库内查询/更新有参数化 project_id 条件，项目同时进入业务 key 锁、typed identity、请求 hash、receipt 和 outbox，防止获准多个项目的用户用 A 项目的同名 key 替换 B 的内容。global 语义不绕过项目 ACL。历史数据未完成证明和 promotion 时，replace/remove 保持 historical_project_scope_not_promoted 拒绝，不在在线请求内重分类历史。

## 管理操作

配置可信 coordinator/projector 后，现有 host 才发布 library_acl_update 和 library_project_acl_update。两者统一 ProjectAclCoordinator → v2 AclProjector → confirm → finish → 当前 actor 回执。库级变更也投影完整的库 ACL、项目 ACL、凭据和新 epoch；失败保持 updating/拒绝状态，不能用旧投影继续访问。

共同参数：library_id、target_principal_id、permission（read/write/admin/null）、expected_policy_epoch、operation_id、request_id。项目操作另需 project_key。null permission 表示撤销；重试必须保持原 request_id、operation_id 和 expected epoch，不能另开操作绕过 payload/replay 校验。管理员更新不包入自身共享业务 lease，以免与撤权独占锁自锁。

## SQL 顺序与权限边界

先有 control v5 / content v3 项目 ACL 基础，再应用 additive `004_content_project_scope.sql`。未来新增 runtime login，先执行既有 provision_runtime.sql，再执行 provision_project_scope.sql。窄 SECURITY DEFINER resolver 只在 session_context/allows 均允许时返回映射，不开放元数据表。迁移脚本纳入既有 package data，不新增服务。本次源代码合入不等于 SQL 已执行。

历史 physical 表分离决定保持：runtime em_content.lan_write_requests 使用运行时 receipt 结构；同名历史 ledger 保留于 history_lan_write_requests；历史 aliases/workspacebindings 同理。历史源 table_code、原列、ID/hash 和 SQLite 原件不改。

## 证据和剩余工作

原候选 9 项本地检查和 35 个 SQL 参数绑定检查曾通过；使用数据库记录器及 DTO facade 替身，不是 PostgreSQL/RLS/完整旧 DTO 或并发验收。本集成包只做哈希、文本差量和 AST，没有重复测试或运行服务。移入仓库的测试为 tests/library_project_acl_wiring/test_wiring.py，只调整 loader；其检查主体不变。

仍需真实 PG 检查 resolver、权限拒绝、撤权 epoch 与失败关闭；确认真实项目注册映射；完成历史 promotion gate、完整召回、向量 outbox 消费/删除、授权跨库召回、10 多人并发容量和生产切换/回退。不能通过直接移除历史 gate 或公开 NULL 项目来完成这些目标。

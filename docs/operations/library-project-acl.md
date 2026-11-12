# 项目 ACL 授权基础与完整投影链候选

状态：**本地候选，未合入 canonical、未部署、未执行 PostgreSQL SQL。** 没有启动 PG、服务或旧验收 suite，没有改动历史记录/源 SQLite，没有增加 Linux 数据快照。

输出目录：`C:\Users\ASUS\Documents\ChatGPT\Evolvmem\artifacts\pg-project-acl-20261101`。

## 已实现

| 层 | 文件 | 已实现的行为 |
| --- | --- | --- |
| 项目标识与意图 | `candidate/evolvmem/library_project_acl/coordinator.py` | `project_uuid` 由已核实的全局 canonical project key 确定稳定 UUID；`ProjectAclIntent` 的版本化 hash 绑定身份、项目 UUID/key、库、对象、权限、epoch、operation、request_id；不可用同一 request_id 改成另一项目/库级操作。 |
| 控制库 | `sql/005_control_project_acl.sql` | 新增全局 `project_registry`、库内 `library_projects`、`library_project_acl`；项目注册与首次项目授权共用一个事务。新 `begin_project_acl_change` 与已有 `begin_acl_change` 共用操作锁、路由隔离和幂等账本，保留旧 operations/receipts/plans。 |
| 权限管理 | 同上 | 实际 owner 可管理本库；其他人管理项目时需库 admin 上限和该项目 admin 授权。项目 grant 不得超过库 ACL 的当前上限。owner 的库 admin 连接权限不能通过该 API 撤销或降级；owner 转移需要另行实现专门流程。 |
| 统一 epoch | 同上 | 库 ACL 与项目 ACL 的变更都使用同一 `policy_epoch`、`acl_state`、`_lock_acl_operation`、confirm/finish/fail 链。库级变更也投影完整项目集，并把所有控制库项目 ACL 更新到目标 epoch，避免旧投影继续有效。 |
| 完整计划 | 同上 | `get_acl_projection_plan` 返回 v2、17 个顶层字段：原 15 项加 `projects`、`project_acls`。保留所有 active/inactive ACL 和凭据，计划 hash、原 payload hash、registry hash 不可变。上限为库 ACL 256、凭据 1024、项目 2048、项目 ACL 8192；超限拒绝，不截断。 |
| 内容库/RLS | `sql/003_content_project_acl.sql` | 新增项目注册与项目 ACL 投影；所有现有调用 `em_security.allows(library_id,project_id,permission)` 的 RLS 自动使用两层权限。库 ACL 只给连接与权限上限；非 owner 需要显式项目 grant。`project_id IS NULL` 只允许真实 owner；缺失/未知项目不作为全局授权。 |
| 实际 owner | 同上 | 从可信 control 库记录核实后，由受信任部署参数写入 `library_state.owner_principal_id`。迁移立即把内容 `acl_state` 置为 updating；v2 计划 owner 与内容 owner 不一致则拒绝，不能在首次完整投影前开放。 |
| 跨设备 | SQL + worker | 去除“被访问库 device == 来访凭据 device”及“设备库只能 owner 有 ACL”的旧限制。保留 credential→principal/device/epoch/expiry→真实 `session_user` login binding 的精确校验；入站 device 校验仍由现有 auth/catalog/session 层执行。没有使用 caller GUC。 |
| 投影与读回 | `worker.py` | v2 projector 同时核对完整项目集、项目 ACL、两层 epoch、全部 login bindings 及实际 owner；提交后用新连接读回、hash receipt 后才 confirm。库 ACL 更新与项目 ACL 更新都必须使用这个 worker。 |
| 撤权/未知提交 | coordinator + worker + SQL | begin 先关闭 control 路由；内容投影异常或提交结果未知，原有 coordinator 会 mark failed，不能 finish/reopen。receipt 缺失/不匹配、旧 epoch、缺失项目项、registry identity 漂移、重放 hash 不同均拒绝。旧请求仍需现有 AuthorityLease 贯穿业务结果返回。 |

这不是业务后端已全部接通，也不是生产 PostgreSQL 已迁移。`PgEligibilityRead` 的历史域推广拒绝没有被简单移除。

## 关键语义

- 库 ACL grant 本身看不到项目行。项目 ACL grant 也不能绕过库 ACL 被撤销或降级后的权限上限。
- 撤销库 ACL 会保留该人的项目授权历史，但其有效权限为零；以后重新给予库权限，会恢复仍有效的显式项目 grant。若产品需要永久取消，需同时撤销相应项目 ACL。
- NULL 历史行不会为了让检索有结果而暴露给非 owner；`scope='global'` 也不是权限。历史项目需经证据映射后，在派生运行投影里填写明确 project UUID，源原列/ID/hash 保持原状。
- UUID key 必须是核实后的全局项目注册 key，不是模糊中文名称、临时 alias 或自行猜测的工作区。已存在的旧 UUID/source project ID 需显式映射到稳定 key；不能批量重写源原件。
- 项目注册属于项目 grant 事务；没有独立绕开 epoch 的注册写接口，也没有在此候选实现项目重命名/所有权转移/物理删除。
- owner 管理项目仍受注册有效性约束：非 NULL 项目要存在于当前 epoch 的 active 项目投影；NULL 只为该库实际 owner 保留。

## 已做验证及证据界限

新增 `tests/test_project_acl.py`：16 个纯本地检查通过，覆盖稳定 UUID、项目与库级 payload 区分、跨设备有效凭据、伪造 device binding 拒绝、缺失/重复/NULL 项目拒绝、错误 key 的稳定拒绝、无库 ceiling 的 grant 拒绝、失效 device 不启用、库级变更后的项目 epoch 读回、稳定 request/hash 参数、成功 confirm→finish 以及未知提交失败关闭。

先写检查时因候选 API 尚不存在失败，添加实现后通过。随后增加 SQL/边界代码时只运行这些新检查；没有运行旧 suite。依赖使用本地已有 `pg-library-host-20261101-v2` 中真实 foundation/catalog 类型，跳过重型应用初始化，不创建数据库。`validation.json` 列出结果与限制，`manifest.json` 记录源与输出 SHA-256。

**没有 PostgreSQL 语法/执行/RLS/锁并发的实测证据。** 本机没有 `pglast` / `sqlparse`，未为此安装新依赖或启动服务。静态检查与 Python 单测不能替代下一次集中 PG 验证。

## 根代理下一步接线顺序

1. 检查总保留空间；维护既定可用空间红线、完整历史原件与一个完整恢复副本。确认控制库 v4、内容库 v2、owner 来源、对应 database/library/connection_ref、所有 runtime binding。禁止未完成的 v1 ACL 操作跨版本迁移；候选 preflight 会拒绝。
2. 在已有集中迁移/验证环境中应用 additive control v5/content v3；不改冻结历史迁移源。content v3 默认关闭，直到 v2 全量投影成功。不应先切 host 或替换生产后端。
3. 把**库级和项目级**管理员操作都接到 `ProjectAclCoordinator` 和此包 `worker.AclProjector`。保留现有库级 `AclIntent`；项目级用 `ProjectAclIntent`。原 `lookup/confirm/finish/fail` API 共用；旧 v1 projector 遇到 v2 计划会拒绝，不能继续使用。可信 provisioning 需适配新增 owner 字段，并为显式获准跨库的凭据提供独立目标库 login binding。
4. Host / source reads / writer：接已核实 canonical project key→stable UUID 注册映射，把请求项目交集与行上的项目权限落到 SQL。保留 router 对 **credential.device 与 authenticated identity.device** 的检查；该比较正确，不应删除。`AuthorizationRouter` 不是旧跨设备阻碍，真正旧阻碍在 v1 投影。读写必须继续持 AuthorityLease，逐库并发使用独立 session/lease/guard，不能把同一线程绑定 guard 共享给工作线程。
5. 96 历史域的验证/推广证据与 runtime gate 按审计报告接通；只有通过分类证据的派生记录才有 project UUID，NULL 仍 owner-only。writer 同事务写正文、revision、outbox、runtime 幂等 receipt，向量投影保留 library/project/source/revision；ACL 更改推进权限 epoch，不要求重新 embedding。未接增删改、tombstone 和回退 journal 的环节仍需要完成，不能以 ACL 候选替代。

下次集中 PG 验证应包含：同库不同人不同项目、跨设备已授权成功/未授权拒绝、库 grant 不能读 NULL、owner NULL 访问、错误凭据/device/epoch/expiry 拒绝、旧 epoch 的长读在撤权开始前 drain 或取消、撤权 commit 后无旧结果返回、库级降权后所有项目投影 epoch 一致、项目撤权失败/未知提交保持关闭、同 request 重放幂等/不同 payload 拒绝，以及全套 source/rowhash 保真。不需为本候选另起一套长期验收服务。

## 与历史表冲突方案一致

本候选不改变父任务已经选定的物理分离：runtime `em_content.lan_write_requests` 保留现有 receipt 结构；历史同名源表映射为 `history_lan_write_requests`；历史 aliases/workspace bindings 也采用 `history_` 物理前缀，逻辑 source table_code、原字段、ID/rowid/hash 与源 SQLite 保留。本候选的 ACL operation/receipt 与业务请求 receipt 是不同账本。

## 文件用途

- `candidate/`：可审查、待集成的代码与两个 additive SQL。
- `baseline/`：只读审计时取得的原文件快照，用于证明来源；不部署覆盖这些旧版本。
- `build_candidate.py` 及 `*_prefix.sql` / `content_project_*.sql`：本地可重现生成材料，输出范围仅本 artifact 的 candidate。
- `tests/`：新增轻量检查，无 PG。
- `manifest.json`、`validation.json`：哈希与验证范围。

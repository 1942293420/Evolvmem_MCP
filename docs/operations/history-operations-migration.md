# B7+B8 operations 固定历史迁移候选

这是可集成的 9 表实现，包含映射、封印源读取、计划、目标事务导入、序列恢复、完成证明、固定 SQL 和 operator 适配。未接入生产、未迁移真实历史、未启动 PostgreSQL 或服务、未修改 canonical。新增 15 项 Windows 小型 SQLite / 内存检查通过；不能视作 PostgreSQL 实库、RLS、锁、目录结构或序列行为验证。

## 真实结构与范围

唯一结构输入是 `../pg-schema-metadata-v3-20261101/schema-metadata-live-v3.json`，SHA-256 为 `f36051b843be978baa56b78c2ca7fee98a8d10bdfc4442832cb4dc711f6eb924`。生成前核对了各 namespace 的 DDL、列类型、PK/rowid、AUTO、FK、索引及本表触发器。复用 B4 固定框架与 B3 冻结依赖，不生成运行时通用 DDL。

| 表 | owner | kane | public | 保留语义 |
| --- | --- | --- | --- | --- |
| context_health_generation | 有 | 有 | 有 | id=1、version、generation、schema_version 原值 |
| lan_memory_revision | 有 | 有 | 有 | revision 原值，不自动加版本 |
| lan_publications | 无 | 无 | 有 | context_id、author、active、title、summary、project |
| lan_workspace_bindings | 有 | 有 | 无 | remote_key 可 NULL，fingerprint/workstream_id 原值 |
| lan_write_requests | 有 | 有 | 无 | user/tool/request_id、payload_hash、result 原文或 NULL |
| local_capture_files | 有 | 无 | 无 | 原 session/path/device/inode/offset/line/state/计数/时间/错误 |
| local_capture_scans | 有 | 无 | 无 | id=1、扫描统计和历史状态 |
| organization_review_vectors | 有 | 无 | 无 | cache_key 可 NULL，vector 为 BLOB 原字节 |
| round_metrics | 有 | 无 | 无 | round 原主键、NULL 指标、REAL 数值与测量时间 |

缺表进入 manifest 的 `not_applicable`，不能伪装为空表。源表出现/消失、DDL/索引/FK 改变会拒绝。只有 `local_capture_files.id` 是 AUTOINCREMENT；`round_metrics.round` 和 `lan_publications.context_id` 都是 rowid alias，但不是 AUTO。

唯一真实 FK 是 public 的 `lan_publications.context_id -> context_items.id`（NO ACTION），目标以 `(library_id, context_id)` 指向 `(library_id, id)`。没有自关联，也不因名字相似给其他字段添加外键。所有表保留 `source_rowid`，源 nullable 主键以 namespace 内 UNIQUE 保留，避免合并多条 NULL。

## 原语义与无业务重放

这些表仅作为隔离的历史记录导入。不会执行历史工具请求、重新发布、触发扫描或归档、调整捕获游标、重新计算 health generation/revision，也不会调用向量索引构建。原 Linux 路径、设备号、inode、offset 和 session_id 是来源机器的历史事实，绝不宣称它们在当前 PostgreSQL 服务器可用；重新启用捕获必须由另行设计的身份迁移完成。

导入器仅允许本域的安全范围触发器和迁移进度 generation 触发器；发现额外业务触发器即拒绝。迁移进度 generation 与原 `context_health_generation.generation` 是两种数据，互不混用。

审查修正：不再仅凭 `immutable_security_scope` 名称判断安全。导入前还检查完整事件（BEFORE UPDATE、逐行）、启用方式、WHEN/列限制/参数/约束/transition table、固定函数绑定、所有者、语言、SECURITY INVOKER、search_path、返回类型和完整函数正文等 30 项条件。正文与已核对的 `pg_business_core/sql/002_business_core.sql` 完全一致，正文 SHA-256 为 `cf36b752d459da92ff4492d6ece616a15a1129c8481e639888c4903a7a315731`。同名触发器改成 INSERT、改绑业务函数或修改函数正文都应被拒绝；真实 PG 目录查询仍待后续验证。

BLOB 映射为 bytea，并以 Python bytes 参数化写入。NUL、非 UTF-8 字节、空 BLOB 都不解码成文本或浮点数组。含二进制的散列使用不可与标准 JSON 混淆的前缀和逐节点类型编码；十六进制展开也计入 4 MiB 导出预算。元数据仍为标准 JSON。TEXT 字段（包括 result）原样保存，不 parse/reserialize JSON；非预期 SQLite 存储类型、无效目标 TEXT、非有限 REAL 会拒绝，绝不静默转换/丢弃。

审查修正：在 `chunk_arguments` 导出前逐列核对源页 `storage_types` 与固定声明类型及可空性。SQLite 的无效 UTF-8 TEXT 可能由 text_factory 返回 Python bytes，因此只有实际 `typeof(...)='blob'` 才允许进入 bytea；`CAST(X'00FF' AS TEXT)` 已有拒绝回归。拒绝会保留原始封印源，不改写原值或自动转换类型。

## 边界、事务与证明

- 每源页最多 128 行 / 4 MiB；使用已核对的冻结 Snapshot 读取代码、typeof/native digest、只读不可变小快照、截止时限以及同一事务中的 rowid 绑定。
- 源计划绑定同一封印文件、namespace、父 sources/core 计划、目标、run 与完整页面；首尾封印检查完成前不宣称域完成。
- 目标 TEXT/bytea、manifest、loaded_counts、cursor、row_keys 在取正文前执行长度预检；参数化写入后逐页按原散列回读。
- 数据、chunk receipt、loaded totals 与 frontier 在同一目标事务内；未知提交结果不自动重试。显式续跑先核验回执，不重复请求或插入。
- 每目标事务继承 core/source 安全门和父链完成/generation 检查，并检查父 generation 触发器条件。本域进度通过不可跳页 frontier、逐页回读、目标逐页计数及最终 receipt 集合验收。
- AUTO 缺失证据与显式 seq=0 分开保存；删除后的高水位保留；INT64_MAX 耗尽禁止复用。仅为实际 AUTO 恢复 identity 序列，普通 INTEGER PK 不凭空创建序列。
- operator 的完成证明额外绑定 manifest、源 seal、当前 generation、完整退出以及两次源 hash；`history_complete` 与 `route_activated` 仍为 false。

## 文件与集成

`candidate/evolvmem/history_migration/` 下的 operations 文件均为新增。依赖现有 canonical 的固定 core/source/organization gate 与 operator runtime。`integration/operator.patch` 是对 B4 `integration/merged_operator.py` 的增量补丁，基础 SHA 见 `integration/base.json`；`integration/operator.py` 是供审阅/测试的整合版本。B5/B6 若已改 operator，应合并补丁与独立 `operator_operations.py`，不要整文件覆盖其他域。operations 父链是 `('core','sources')`，保留 governance 的 organization 父链。

全局只新增固定域 `operations`，支持原 CLI 的 prepare/plan/step/status/verify/finalize 路径。helper 使用既有受信 `OperatorRuntime`，没有独立 DSN、凭据、服务、路由或后台任务。

本地测试（仅本域新增测试，不运行旧全套）：

```powershell
python -B artifacts/pg-history-operations-20261101/tests/native_runner.py --tmpdir D:/EvolvMem-test-tmp --receipt artifacts/pg-history-operations-20261101/verification.json
```

集成后的新增测试可用 `--package-root <absolute-canonical-or-review-tree> --operator <absolute-operator.py>`。测试只创建带 `b78-synthetic-` 前缀的小型临时 SQLite；cleanup 只处理本次临时目录。

已验证 15 项：namespace/alias/FK 结构、BLOB 和 raw JSON 保真、nullable key/rowid、固定页边界、捕获身份、AUTO 缺失与零/高水位/耗尽计算、源漂移与超限拒绝、原子回滚、未知提交显式续跑、回读前预算、父链/frontier/generation 拒绝、public 完整 runner 与 operator 完成证明。

本次审查修正仅执行新增 `test_fidelity.py` 的 3 项定向检查，0.188 秒通过，原 15 项未重跑。检查涵盖真实合成 SQLite 的无效 UTF-8 TEXT、全部固定列存储类型/NULL、受信触发器目录谓词缺项/篡改拒绝。后者使用目录响应模拟，不能宣称 PostgreSQL 门禁已实库验证。详情见 `review-fixes.md`。

实 PostgreSQL 的 DDL/CHECK 目录表达式、RLS、锁、真实 FK/bytea 适配和序列事务行为仍需后续受控验证。CHECK gate 当前按标准 PostgreSQL `pg_get_constraintdef` 输出做严格比较，任何渲染差异均 fail closed；本地效应端测试没有模拟这些 PostgreSQL 安全性质。没有因为缓存名称而丢弃任何表，也没有宣称新向量索引已经完成。

# Capture prepare 参数渲染修复

唯一生产源差异为 `operator_scope.py` 一行：psql 值变量的正则由 `:'([a-z_]+)'` 改为 `:'([a-z_][a-z0-9_]*)'`，使 `parent_manifest_sha256` 被正确展开。变量仍仅从既有可信 target 和同 source seal 的 sources Plan 取值，保留原 SQL 字符串引号转义。

`001_history_capture.sql` 是原字节副本，无修改。root 的实际 PG 只读复核已确认 history_source_runs.manifest.source 存放 SourceRun.manifest，键为 source_namespace；磁盘 source_plan.Plan.document.source 的键 namespace 属于另一层对象。SQL 继续读取 source_namespace 并限制 owner/kane，没有放宽任何 owner/RLS/epoch/parent generation/persisted-binding 门禁。

新增两项定向回归：

- 调用实际 OperatorRuntime.prepare、实际父计划校验和 SQL 拆分渲染，数据库端使用 fake leases；确认无任何未展开的 psql 值变量，parent hash/run ID 与实际经过验证的 SourceRun/Plan 一致，两个短连接均 commit 并关闭。
- 用实际 SourceRun、source_plan.Plan 和 CompleteImporter.bound_manifest 验证 SQL JSON 路径；区分磁盘计划 namespace 与持久化 run source_namespace，并保留 public 不适用的 namespace 限制。

首轮失败为未展开列表 `['parent_manifest_sha256']`；修复后两项均通过。Windows 测试没有连接 PostgreSQL，没有 SSH、服务操作或旧全套测试。测试不写临时业务数据，也不执行数据库 SQL。root 负责 native 新检查、真实 PG 正例和父门禁负例；此前因 SQL 语法错误而失败的负例不构成父门禁验证证据。

候选源哈希：

| 文件 | 原 SHA256 | 新 SHA256 |
|---|---|---|
| operator_scope.py | e93b7569b74cde476e8841126239960b562cf18c7ecf26f817da85ab3b5cdf7d | ee9b0a047511d120d3615c221095012c61290e617b2d1513aa2a1004aae442c7 |
| sql/001_history_capture.sql | 1a306857819dad18ec0bae6cfc138f50167a2f59ef30109433a00e1229f14eb1 | 1a306857819dad18ec0bae6cfc138f50167a2f59ef30109433a00e1229f14eb1 |

测试可迁至 canonical tests 目录，默认从已集成 package 导入；仅在这个候选目录内存在 sparse candidate 时，动态加载本候选 operator_scope 以复现/验证唯一差异。无旧 artifact 日期路径依赖。

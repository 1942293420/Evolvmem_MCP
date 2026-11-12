# Capture 三表与 PG 来源资格读取连接点

2026-11-01。仅核对本地保留源码、当前集成清单与候选 DDL；未 SSH、未运行测试、未读取业务正文、未修改源代码。

## 本轮结论

**没有发现新增 capture 三表造成的现有 PG 资格 SQL 列名兼容错误。** 当前 `eligibility_sql.SQL` 完全不读取 `lan_session_uploads`、`lan_session_heads`、`lan_session_backfills`，所以三表迁移接入不会自动形成当前来源证明。

本轮应只完成 capture 包和 operator 分支适配，**不开放 runtime、不删除历史域拒绝条件、不追加普通 runtime GRANT**。迁移完成不等于原文可读、角色有效、当前归属有效或完整召回已通。

## 核对基线与文件

以 `artifacts/pg-history-operator-20261101/canonical-integration.json` 的 152 路径清单为基准，以下本地副本 SHA256 均与集成记录相符：

| 集成文件 | 已核对的本地来源 |
|---|---|
| `evolvmem/pg_business_core/historical_validation.py` | `artifacts/pg-eligibility-query-reuse-20261101/candidate/evolvmem/pg_business_core/historical_validation.py` |
| `evolvmem/pg_business_core/eligibility_read.py`、`eligibility_sql.py` | `artifacts/pg-source-eligibility-20261101/candidate/evolvmem/pg_business_core/` |
| `evolvmem/source_qualification.py` | `artifacts/pg-source-eligibility-20261101/candidate/evolvmem/source_qualification.py` |
| `evolvmem/pg_business_core/recall_store.py` | `artifacts/pg-context-read-slice-20261101/candidate/evolvmem/pg_business_core/recall_store.py` |
| `evolvmem/pg_business_core/vector_recall.py` | `artifacts/pg-context-vector-read-20261101/candidate/evolvmem/pg_business_core/vector_recall.py` |
| `evolvmem/pg_business_core/sql/grant_runtime.sql` | `artifacts/recall-batch-integration-20261101/pg-wip-source-20261101/evolvmem/pg_business_core/sql/grant_runtime.sql` |

旧 `artifacts/recall-batch-integration-20261101/pg-wip-source-20261101/evolvmem/pg_business_core/executor.py` 的哈希**不匹配**当前清单，本报告不采用它判断当前 HTTP/工具路由。

capture DDL依据是冻结候选 `artifacts/pg-history-capture-20261101/001_history_capture.sql`；本轮新的打包/绑定改动由接入任务另行核验，本报告不把尚在编辑的新候选视为已验证代码。

## 具体连接点与需要修的内容

| 文件与函数 | 已确认情况 | 后续需要处理 |
|---|---|---|
| `pg_business_core/eligibility_read.py::_EligibilityQueries.currently_backed/eligible` | 只读取 derivation、basis、conversation、sources、learning/human/manual；没有 capture/head 当前性或原文角色证明。 | 接通历史来源时增加真正的来源证明，不能把现有布尔资格函数当作 `OriginalRead`。 |
| `pg_business_core/eligibility_read.py::PgEligibilityRead.__init__` | 固定抛出 `historical_domain_not_promoted`。 | 继续关闭。三表保存成功不能作为移除拒绝条件的依据。 |
| `pg_business_core/historical_validation.py::_QuarantineRead.gate`、`PARENTS_SQL`、`TABLES` | 当前锁及策略检查覆盖 20 表；父链只绑定 core/source，组织域只检查建表标记；没有 capture 进度、generation、manifest，也没有组织迁移进度证明。 | 用于完整迁移数据之前，绑定 capture/organization 的完成状态与同源 manifest、generation；不能以表存在或建表标记代替完整迁移证明。 |
| 同文件 `FixtureBinding.checked`、`validate_quarantined_candidates` | 仅接受 `em_pilot_*`；要求工厂提供 owner、REPEATABLE READ 的隔离事务。 | 新正式 operator 目标和默认 READ COMMITTED READ WRITE 迁移事务不能直接复用。不得只放宽名称正则或把 `operator.factory` 硬接过去。 |
| `capability_assessment_migration.py::OriginalRead._heads`、`history_organization.py::_current_archives` 的 PG 对应读取 | 保留的 SQLite 实现使用 `sqlite_master`、裸表名、SQLite `IS NOT` 和逻辑 family 去重。现有 PG 资格 SQL 尚无对应完整读取。 | 使用固定 `em_content` SQL，所有 join 加同库 `library_id`，保留 NULL-safe head mismatch 与 family 去重；涉及旧 rowid 的选取使用 `source_rowid`，不能改成 PG 插入顺序或 max(source_order)。 |
| capture DDL 与 `pg_business_core/sql/grant_runtime.sql` | capture 三表只有 owner policy 和对 PUBLIC 的 restrictive owner quarantine；普通 runtime 授权清单不含三表。 | 单加 SELECT GRANT 不足以允许读取。需要后续独立 domain promotion/ACL 发布；迁移 ledger 仍只限维护身份。本轮保留现状。 |
| `pg_business_core/recall_store.py::PgRecallStore.__init__/_load` | 发现 unit_derivations、learning_memories、context_evidence 等历史资格域便拒绝；普通路径另要求新写入 receipt、legacy-write.v1 migration 来源及空 archive。 | 历史适配仍缺，capture 不解决这些条件，不能移除既有检查来假装支持。 |
| `pg_business_core/vector_recall.py::PgVectorContextRecall.search` | 仅支持普通 fact/reference 单库向量分支，拒绝历史时间及其他类型；最终仍使用普通 PgRecallStore。 | 经验、capability、项目摘要与跨库混合召回仍需各自真实接入及最终复核。 |

上述 SQLite 当前来源实现的精确依赖与原保留源码哈希另见 `artifacts/pg-history-capture-design-20261101/DEPENDENCIES.md`、`evidence.json`；本报告没有声称重新审计了当前生产 SQLite 源码。

## 下一步的实际小范围

1. 先实现并验证 **PG 固定当前 archive/head 读取**，保持无 head、NULL head、显式 head mismatch、逻辑 family、排除记录与同库关系的原语义，不引入通用 SQL 转换层。
2. 在私有历史读取入口同时绑定 **capture 与 organization 父链/完成证明**，包含同源 manifest 和 generation；继续独立执行末尾版本及选中候选复核。
3. 保持产品 runtime 关闭，直到 B3 人工排除、反馈、规则、governance enrollment、模块归属以及后续知识与经验资格链得到完整实现。不要把新三表直接认定为全部来源权限依据。

仍阻断完整来源资格的域包括：B3 人工排除/反馈/规则、governance enrollment、项目模块；知识原文/事实决策；capability 最新 assessment；经验 outcome；rollup 与 continuity。现有 `basis_reconcile.reconcile_quarantined_basis` 也明确返回 `task_refresh_complete=False` 与 `product_ready=False`，不能代替这些在线更新。

新 capture 包是必要的数据保留依赖，不是完整召回或生产切换的完成证明。

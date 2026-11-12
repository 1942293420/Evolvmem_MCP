# B4 知识事实、经验与学习规则：固定迁移候选

2026-11-01。本目录只新增可集成代码及合成检查；没有修改 canonical，没有读真实历史正文或凭据，没有启动 PostgreSQL、服务或测试环境。只通过 `ssh codex-linux` 读取了指定 canonical 的源码标识、文件哈希和依赖方法定位。Linux 30 GiB 容量底线由已有 `OperatorRuntime.guard` 继承，本轮没有 Linux 数据写入。

## 交付与接入

将 `candidate/evolvmem/history_migration/` 的 11 个文件作为新增文件接入现有框架，并将 **`integration/merged_operator.py`** 安装为 `evolvmem/history_migration/operator.py`。`integration/merged_operator.patch` 是相对于已经集成的 B3 operator 的可审查补丁。不要用较早的 `integration/operator.py` 覆盖 B3；该文件仅保留 B4 单域接入证据。

B3 基准 operator SHA256 为 `8c8f37687b1e3fbcb55289025ae3c10496d1d453ebbe14962720ad98c0faaba6`；合并后为 `91b77926eb7fbf4074b64e04ff255d333e61d78f3f7a1c82646f5bb469420001`。保留 `governance` 域和它的 `core/sources/organization` 父链、完成 proof。B4 的 `knowledge` 域使用 `core/sources` 父链；不把同目录 helper 的存在当作正式 CLI 已部署。

`operator_scope.py` 无须修改。新增 `operator_knowledge.py` 使用现有受信 `OperatorRuntime`，实现 prepare/preflight/plan/status/batch。CLI 补丁覆盖 domain choices、父计划加载、prepare、plan、load_plan、importer、step、status、finalize 和 `_knowledge_proof`。没有新增 DSN、身份、服务、公开路由或自动重试。

`manifest.json` 给出每个目标路径的源文件与 SHA256。root 集成时应基于最新 B3 176 路径清单更新版本绑定，而非退回旧 165 路径清单。

## 权威结构与保真

权威证据为 `pg-schema-metadata-v3-20261101/schema-metadata-live-v3.json`，SHA256 `f36051b843be978baa56b78c2ca7fee98a8d10bdfc4442832cb4dc711f6eb924`；表集合另与 `pg-schema-coverage-v2-20261101/table-coverage.json` 对照。结构是此前采集的固定收据，不能声称是本轮跨库一致快照。

| namespace | B4 实际表数 | 原列数 | 原 FK 数 | AUTOINCREMENT |
|---|---:|---:|---:|---|
| owner | 23 | 148 | 15 | learning_rules、learning_runs、learning_versions、skill_proposals |
| kane | 22 | 138 | 15 | learning_rules、learning_runs、learning_versions |
| public | 22 | 138 | 15 | learning_rules、learning_runs、learning_versions |

仅 `skill_proposals` 在 kane/public 缺失；计划明确写入 `not_applicable`，不伪造为空表源。目标保留全部 23 个固定表，缺失表要求目标始终为空。`knowledge_fact_relations` 的一个索引 DDL 在 namespace 间只有空白差异，也分别封存并按原文核验；不拿 owner 索引文本代替其他库。

固定表：knowledge_decisions、knowledge_equivalences、knowledge_fact_actions、knowledge_fact_choices、knowledge_fact_conditions、knowledge_fact_groups、knowledge_fact_issues、knowledge_fact_relations、knowledge_fact_separations、knowledge_fact_sources、knowledge_fact_versions、knowledge_metadata、knowledge_objects、knowledge_qa、knowledge_restructure_map、knowledge_restructure_state、knowledge_source_bindings、learning_project_types、learning_rules、learning_runs、learning_settings、learning_versions、skill_proposals。

原列逐列映射，TEXT 保持原文：不解析/重写 JSON，不补默认值，不修正状态，不归并重复知识对象，不重算提取、学习、冲突或人工选择。SQLite 的 nullable TEXT PK 在目标保留 NULL；以 `(library_id, source_rowid)` 为迁移主键，原业务 key 保留为 NULL-distinct UNIQUE，原 INTEGER PRIMARY KEY 的 rowid alias 必须相等。所有 15 个原 FK 转为 library_id 范围约束，保留 CASCADE/NO ACTION；`learning_settings CHECK(id=1)` 也保留。`project`、旧 item ID、task ID、source key、版本与条件不自动转换成新授权。

## 流式与完成条件

复用 frozen `sqlite_source`、`static_wal`、core/source transaction gate：受审查的 sealed source、源入口/出口哈希、短生命周期只读 Snapshot、128 行/4 MiB 分页、同一 SQLite 事务绑定隐藏 rowid；不使用 OFFSET，不生成全量正文 JSON/CSV 副本。

行、chunk receipt、loaded totals 和 frontier 在同一目标事务。回读核验原行摘要和原字节长度；最终分页计数还会发现额外/异库行。所有 B4 表与 receipt 的 generation trigger 是 ALWAYS，要求无 WHEN 条件；父 core/source 的 generation trigger 也额外检查 `tgqual IS NULL`，避免被条件触发器绕过。每次事务仍复核父链当前代际、RLS/owner/列/约束/触发器。

读取目标正文前先查询 TEXT 未压缩总字节，限定 4 MiB/页和 129 行上限；读取 manifest/counts/cursor/receipt JSONB 前分别限制 `::text` 大小（16 MiB/16 KiB/128 B/8 KiB）。超预算、原列漂移、缺表变化或不支持的 SQLite storage type 会停止，不截断、不丢行后报完成。

目标 verified 仅是暂时证据。runner 需要成功关闭源 phase 并完成至少两次源哈希，再 finalize。正式 operator 额外保存和检查 manifest/config/source seal/current generation 绑定的完成 proof。所有输出保持 `history_complete=false`、`route_activated=false`；B4 完成不等于全部历史或业务读写切换完成。

## 原序列为空、被删除高水位与耗尽

完整 `sqlite_sequence` evidence 被写入 manifest，`present=false,value=null` 与 `present=true,value=0` 始终不同。不存在序列行时，按 SQLite 当前最大 ID 的分配语义求目标下一值；保留“缺行”证据，不伪造来源 0。已删除高 ID 的序列高水位仍生效。

`INT64_MAX` 历史行和序列值均允许迁入。达到 MAX 的目标序列通过事务性 `ALTER SEQUENCE ... RESTART WITH MAX` 后消费最后一值，保持 `NO CYCLE`、`last_value=MAX,is_called=true`；后续自动分配必须失败。verified/status 也重新检查序列，防止完成后序列回退导致重用 ID。完成结果包含每表 source_present/source_value/exhausted/next_value 证据。

语义依据：[SQLite AUTOINCREMENT](https://www.sqlite.org/autoinc.html)、[PostgreSQL 17 ALTER SEQUENCE](https://www.postgresql.org/docs/17/sql-altersequence.html)。这些文档支持实现选择，**不是实际 PG rollback/unknown-commit/nextval 耗尽测试证据**。

## 应用依赖核对

canonical HEAD 为 `979859145adcdf0ce7742de861a0b236b0659688`。只读源码定位确认：`knowledge_identity` 写 knowledge_objects/source_bindings/decisions 并更新 organization_units；`knowledge_facts` 保留版本、sources、人工 choices/conditions/issues/actions/separations；`knowledge_equivalence` 人工 confirm/撤销会更新 active/revision；`memory_learning` 保存规则、框架、快照和人工恢复；`experience_service.record/outcome/recall` 还依赖 context_items/context_sources/context_evidence 的版本和验证资格。详细源码哈希在 `source-evidence.json`。

本候选只迁移保存这些历史状态，没有把 SQLite 业务读写端口改为 PostgreSQL，也不自动运行上述方法。跨库授权、实际资格读取、更新/删除追平、并发切换及切换后新增写入回退仍需整体实施。

## 已测与未测

`test-results.txt`：仅 B4 的 15 项 Windows 本地合成检查，9.673 秒，全部通过；另在 B3+B4 合并 operator 上执行 1 项定向 proof/CLI 检查，0.020 秒通过。没有重跑旧 core/source/capture 完整套件。scratch 固定在 `D:/EvolvMem-test-tmp`。

覆盖 nullable key/raw JSON/人工选择摘要、rowid alias、23/22/22 缺表差异、精确结构与索引、130 行分页、MIN/MAX ID、缺 sequence 与 MAX 已删除高水位、超大行/错误 storage type 拒绝、manifest 与父绑定拒绝、真实 SQLite effect-port 下的事务前回执故障回滚、提交后未知结果显式恢复、回读篡改/预算拒绝，以及完成 proof/代际检查。

effect-port 只验证新 importer 的行/回执/进度行为，故意不模拟 PG RLS/catalog/FK/locks。源计划测试的 parent 是受格式校验的合成元数据，不代表真实父域已完成。未执行 PG DDL、真实 catalog/FK/RLS、真实序列耗尽/事务回退、真实全量历史导入、canonical CLI 部署、生产切换或容量峰值验证。只读独立审查确认新增 B4 核心目前无未修复可操作发现；实库验证仍是下一道门槛。

## Linux 原生路径适配

`tests/native_runner.py --package-root /absolute/canonical --tmpdir /absolute/scratch --receipt /absolute/scratch/b4-native.json` 会直接导入已安装 B4 的包与 operator；不会复制旧依赖。可用 `--pattern test_transaction.py --filter completion` 只跑新增差异。`B4_NATIVE_PACKAGE_ROOT`、`B4_NATIVE_OPERATOR`、`B4_TEST_TMPDIR` 仅选择执行路径，原有业务断言不变；Windows 默认 scratch 仍为 D 盘。测试内容与 native adapter 的哈希在 manifest 中分列。该 runner 尚未在本代理下于 Linux 执行；root 可据此开展授权的有界原生核验。

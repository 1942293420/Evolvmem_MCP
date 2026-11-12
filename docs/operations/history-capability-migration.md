# B5 能力来源与价值评估：固定历史迁移候选

本目录新增可集成代码，不修改 canonical、不启动 PostgreSQL/服务、不读取真实历史正文。仅通过 `ssh codex-linux` 只读核对指定 canonical 的源码与哈希。Linux 未写入；现有 `OperatorRuntime.guard` 的 30 GiB 底线继续生效。

## 集成

将 `manifest.json` 中 11 个新增文件放入 `evolvmem/history_migration/`，再审查应用 `integration/operator.patch`。`integration/merged_operator.py` 基于 B4 merged operator（SHA256 `91b77926eb7fbf4074b64e04ff255d333e61d78f3f7a1c82646f5bb469420001`），保留 B3 governance 与 B4 knowledge；合并后 SHA256 `6b40c3ca3c1f56809b8b725fbe275cef9cfe104fed1d5e5324f37c007f7534cb`。如果其他批次已改 operator，应机械合并新增 capability 分支，不覆盖它们。

当前集成清单基准为 `pg-knowledge-integration-20261101/canonical-integration.json`（B4 完成后的 193 路径）；不是早期 176 路径。

新增 domain `capability` 使用 `core/sources` 父链。复用现有受信 OperatorRuntime、私有连接工厂、容量守卫、冻结 source/sqlite/WAL 模块；不添加独立 DSN、身份、服务、路由或自动重试。`operator_scope.py` 无改动。父 source 当前代际与同源绑定在每个事务重新核对；无条件 generation trigger 资格也检查父 core/source。

源 schema 取自 metadata-v3 SHA256 `f36051b843be978baa56b78c2ca7fee98a8d10bdfc4442832cb4dc711f6eb924`，7 表集合另与 coverage-v2 的 `B5_capability` 精确核对。这是此前结构收据，不是本轮跨库一致快照。

| namespace | 表数 | 原列数 | 原 FK 数 | 缺席 |
|---|---:|---:|---:|---|
| owner | 7 | 80 | 3 | 无 |
| kane | 6 | 71 | 3 | existing_capability_assessment_runs |
| public | 6 | 71 | 3 | existing_capability_assessment_runs |

固定拓扑顺序：capability_categories、capability_memories、capability_sources、capability_assessments、capability_category_suggestions、capability_intake、existing_capability_assessment_runs。每库精确核对 DDL、列序/table_xinfo、FK、索引与缺席表；缺席不伪造为空表 source，计划保存 `not_applicable`，目标该表仍必须为空。

## 保存原值与原语义

全部原列逐列保存，原 NULL、raw JSON/非法 JSON 文本、历史 ID、隐藏 rowid、人工状态、revision、原因、时间、评估 details、legacy_active_preserved、source 绑定和 audit lease/result_unknown 均不重写。`capability_categories.id` 的 nullable TEXT PRIMARY KEY 保留 NULL，迁移主键为 `(library_id,source_rowid)`；原 PK/UNIQUE 转为库范围且 NULL-distinct 的 UNIQUE。

3 条 FK 是 memories.category→categories.id、sources.memory_id→memories.memory_id、assessments.memory_id→memories.memory_id，均按原 NO ACTION 保留。不会猜造 assessments.source_id、audit 或来源 task/context 字段的 FK。6 个源 CHECK（包括 verification_status 只能 unverified）写入固定 SQL 并在 gate 核对，旧状态不会由迁移提升。

只读应用源码确认当前评估使用 assessment_id DESC / 每 source_id 的 MAX(assessment_id)，最新评估不合格时不能退回较旧合格评估；第一条评估包含冻结 legacy 来源白名单。保存全部原 assessment_id、revision、details 与 source 绑定足以保留这些历史输入。此次不重跑评估、不调用持久化或状态修复、不把向量相似或迁移 verified 当作知识验证成功。业务读端切换及来源资格判断仍未迁移。

5 个 AUTOINCREMENT 主键名分别是 memory_id、source_id、assessment_id、suggestion_id、intake_id。源适配复用 frozen Snapshot 的私有代码对象，但关闭其 `_page` 的硬编码 `row['id']` 分支，再按经过 schema 验证的真实 alias 更新高水位。冻结模块本体未改。目标 sequence 与最大 ID 查询同样按各表 alias 取值。

`sqlite_sequence` 缺行与显式 0 的证据不同；删除高 ID 后的高水位仍保留。INT64_MAX 可迁入，目标保持 NO CYCLE 耗尽：事务性 RESTART 到 MAX 后消费最后一值，后续分配失败。status/verified 会复核 sequence 未回退。具体 PostgreSQL RESTART/nextval/rollback 行为尚未实库验证，不能据此宣称 PG 耗尽测试通过。

## 边界与完成证明

source 每页最多 128 行/4 MiB；隐藏 rowid 在同一只读 SQLite 事务绑定，使用 keyset，没有 OFFSET 或全量原文中转副本。sealed source 的入口/出口哈希、侧文件身份、事务时限继续约束读取。

目标行、回执、loaded totals、frontier 同一事务。回读先查 TEXT 未压缩字节，再读取正文；读取 JSONB manifest/count/cursor/receipt 前分别执行 16 MiB/16 KiB/128 B/8 KiB 预算检查。回读校验全部列摘要、行数与编码字节量，最终分页计数拒绝多余行和跨库行；超预算停止，不截断后报成功。

runner 退出 source phase 且至少两次源哈希成功后才能 finalize。operator 保存 manifest/config/source seal/current generation 绑定的 proof，单纯目标 verified 不能算 domain_complete。所有结果仍为 `history_complete=false`、`route_activated=false`。

## 新增验证及限制

`test-results.txt`：17 项 B5 Windows 本地合成检查，4.478 秒，0 失败；只运行本批新检查，没有重跑旧验收。scratch 固定为 `D:/EvolvMem-test-tmp`。初始 7 项先观察到实现缺失失败，再实现；新增流式测试又暴露 frozen `_page` 对 id 的假设，修复后通过。

覆盖：nullable category、raw 人工详情、各种 alias/INT64 边界、lease REAL 与 NULL、每库精确结构/缺席、最新 reject 保持最新、6 CHECK 资格、130 行分页、缺 sequence/删除高水位/INT64MAX、超大行和 storage type 拒绝、父源/manifest 篡改拒绝、SQLite effect-port 的提交前回滚/提交后未知结果显式恢复/回读篡改/预算拒绝、完整 7 表 load→verify→count、operator CLI 与出口 proof/代际绑定。

SQLite effect-port 仅验证新 importer 的行/回执/frontier 事务效果，明确跳过 PG catalog/RLS/FK/sequence gate；CHECK rendering 测试只核对固定期望与拒绝逻辑，未连接 PG。未执行 PostgreSQL DDL、真实 FK/RLS/catalog/锁、真实 sequence 耗尽与事务回退、真实历史导入、canonical 部署、切换、并发追平或写回退。`source-evidence.json`、`verification.json` 与 `manifest.json` 提供可核对边界和文件哈希。

Linux 仅路径适配命令：`python -B tests/native_runner.py --package-root /absolute/canonical --tmpdir /absolute/scratch --receipt /absolute/receipt.json`。runner 要求绝对 package-root/tmpdir，并设置 `B5_NATIVE_PACKAGE_ROOT`、`B5_NATIVE_OPERATOR`、`B5_TEST_TMPDIR`、`TMPDIR`；`--operator` 可选，不传则检查安装后的 operator。不得直接在 Linux 使用 Windows 默认测试路径。

独立审查后 P2 修复：原 CHECK 规范化错误地删除单引号常量内部空白等字符；现按 SQL 单引号字面量（含双写单引号转义）分段，只在字面量之外清理格式和 `::text`，内部内容逐字保存。仅新增运行 1 项聚焦回归，0.143 秒通过，涵盖外部空白可接受、内部空白/括号/双引号/cast 文本/转义引号/大小写变化拒绝。原 17 项未重跑。见 `check-literal-fix-test-results.txt` 和 verification.json 的 post_review_delta。

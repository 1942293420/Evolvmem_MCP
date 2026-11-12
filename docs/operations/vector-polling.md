# 向量投影空闲探测调度（本地候选）

状态：**仅隔离候选；未合入 canonical，未部署，未验证 PostgreSQL 或实际 pending 排空。**
范围：`evolvmem/pg_vector_port/worker.py`、`evolvmem/library_host.py` 及其离线测试。
本次没有启动 PostgreSQL、Docker、systemd、Host、真实模型或生产数据库，没有改动认证、读取就绪、SQL、队列或迁移策略。

## 问题

Host 的 `start_projection` 每次 `context_factory()` 按库轮转，一次只选下一个原生项目。
当库 A（shared + hidden）与库 B（shared 为空）并存时，A 的 shared 服务可能每隔若干次调用才出现一次。
旧 worker 对每次 `None` 或 `result.job_id is None` 都等待完整的 `interval_seconds`，导致大量空探测也按完整轮询间隔付费，实际有 job 的轮次被不必要地推迟。

## 修复语义

`ProjectionWorker` 新增可信整数参数 `idle_probe_budget`，范围 1..16，默认 1。
默认值保持现有调用者的行为：第一次空结果后立即回到完整轮询间隔。

- 空结果（factory 返回 `None`，或 `ProjectionResult.job_id is None`）在请求和 cleanup 全部退出后，最多连续探测 `idle_probe_budget` 次；每次短探测之间只等待 `0.01s`。
- 达到预算后等待配置的 `interval_seconds` 完整间隔，并重新开始一个不超过预算的空探测批次。
- 成功完成一个真实 job（非空 `job_id`）会清零空探测计数。
- `ProjectionDeferred` 始终等待完整间隔，并清零空探测计数；它不会在短探测批次中重试。
- 每次探测仍然重新调用 `context_factory()`，因此身份、授权、route、guard 和 deadline 都是新的；没有跳过认证或按库特判。
- 停止信号、epoch deadline、真实 job 上限和 cooperative yield 语义不变。
- SQL、未知提交及 cleanup 不完整等错误仍直接终止 worker，不重试、不重启。

Host 创建的 `ProjectionService` 显式传入 `idle_probe_budget=8`；普通 `ProjectionWorker` 不传时仍为 1。

## 调度形状

设预算为 `N`，轮询间隔为 `I`：

```text
空结果 ... N 次:  0.01s, 0.01s, ..., 0.01s, I,  0.01s, ...
                     └──── 一个批次 ────┘  └─ 下一批 ─┘
成功真实 job:      清零批次计数，按 0.01s cooperative yield 继续
ProjectionDeferred: 立即 I，并清零批次计数
```

这减少了空探测的固定成本，但没有承诺某个具体积压能在固定时限内排空；实际吞吐仍取决于每个 job 的模型/数据库延迟、身份和库轮转。

## 离线验证

新增 `tests/pg_history_vectors/test_worker_idle_probe.py`，用真实 `ProjectionWorker` 生命周期和现有测试缝（伪 clock、stop event、context factory）覆盖：

- `None` 与 `result.job_id is None` 的有界短探测批次及完整间隔；
- 真实 job 后预算重置（空探测 → job → 空探测，预算 2 时必须重新计数）；
- `ProjectionDeferred` 完整睡眠并重置（defer → 空探测，预算 2 时必须重新计数）；
- 全部为空、预算 8 时 7 次短探测后恰好一次完整间隔；
- `commit_result_unknown` / `write_committed_delivery_failed` 在预算 8 下只尝试一次并停止；
- `close()` 停止带预算的真实线程；
- 预算必须是 1..16 的整数。

`tests/library_phase2_host/test_projection_deferral.py` 增加 Host 传入 `idle_probe_budget=8` 的检查，并增加真实 Host factory/worker epoch 集成测试：A=`[hidden, shared]`、B=`[shared]`，fake manager 的 listing 遵守 after cursor，fake 授权 Port 仅对 A.shared 返回 job；15 次选择为 A.hidden、B.shared、A.shared、B.None、A.None、B.shared、A.hidden、B.None、A.shared、B.shared、A.None、B.None、A.hidden、B.shared、A.shared，job 出现在第 3/9/15 次，预算 8 下没有完整间隔等待，且每次请求的 state 都是新对象、请求结束后 ContextVar 已清空。
这些测试不启动数据库或模型，不证明生产授权链、SQL 行为或实际 pending 排空性能。

## 可选的更小设计

若未来允许改变 Host 的轮转策略，可以让 Host 在一次 factory 调用中只遍历真正有 pending 的库，或把库轮转与 worker 空探测合并；那会触及投影枚举/授权边界，超出本次最小修复范围，因此未实施。

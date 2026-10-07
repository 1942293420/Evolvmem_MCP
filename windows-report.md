# Windows Codex 后台上传 19 版本停滞 —— 只读排查报告

> 本报告为 2026-09-21 历史诊断，候选补丁已退役，现行协议见 `docs/windows-codex.md`；当前测试结果见下文“历史状态更新（2026-10-08）”。

- 任务目录：`/home/jiangli/hermes-memory-plugin`（诊断产物在 `windows/` 子目录）
- 执行时间：2026-09-20 16:38–16:50 UTC（本机 2026-09-21 00:38–00:50 CST）
- 授权范围：源码/队列元数据/回执只读；允许重放同一现有上传块 1 个版本、相同幂等参数
- 阶段状态：**只排查，未提交、未部署**；未修改线上程序/配置/服务/数据库/索引/凭据

## 0. 结论（先给结果）

**根因：服务端 `session_archive_upload` 在“最后一块”返回 `transcript_fork`，客户端把它吞掉，导致版本永久卡在最后一个 256 KiB 块。**

- 实测错误（单块重放，最小版本）：`{"error": "transcript_fork"}`，耗时 344 ms。
- 触发条件：客户端提交的内容与 **该 session 已归档的最大版本** 既非前缀关系、也非其扩展（rollout 被重写/重新序列化），`evolvmem/lan_capture.py` 的 fork 保护直接抛错。
- 为什么只有最后一块失败：fork 校验位于 `if complete:` 分支，只有补齐 `total_bytes` 的那一块才会走到。
- 为什么永久卡死：错误确定性复现，重放同一块永远同样失败；客户端 `Invoke-UploadQueue` 的 `catch { continue }` 只留下 `retry_pending`，不记录任何错误码。
- 客户端设计本身支持“重写后的 transcript 作为新的不可变版本”（`evolvmem-codex.ps1` 第 577–578 行注释），服务端没有对应的接受路径 —— 这是**客户端/服务端契约缺口**，不是网络故障。

## 1. 环境与可复现性事实

| 项 | 值 |
|---|---|
| SSH 目标 | `ASUS@192.168.1.112`（hostname `DESKTOP-OTTJ1G2`），PowerShell 5.1.26100.9444 |
| 客户端主目录 | `C:\Users\ASUS\AppData\Local\EvolvMem\Codex` |
| 安装的 `evolvmem-codex.ps1` | sha256 `2ff0d2d7149837f38ff2e0c054bc9358847d9dfd4c03ef44ecdc4e137dfaf116`，53262 B |
| 本仓库 `scripts/windows/evolvmem-codex.ps1` | sha256 `2ff0d2d7…f116`（**与安装副本逐字节一致**，源码结论可直接用于生产） |
| 计划任务 | `EvolvMem Codex Sync`，State=Ready，Enabled=True，每分钟一次，LastTaskResult=1 |
| 队列文件 | 38 个（19 × `.json` + 19 × `.bin`）；`archive-status` 回执 464 个；session 注册 84；session-cache 53 |
| 派发链路 | `evolvmem-sync.exe` → `powershell -NoProfile -ExecutionPolicy Bypass -File <client>\evolvmem-codex.ps1 -Action worker` → `Invoke-Worker`（进入上传前设置 `RpcTimeoutSeconds = 30`，所以**不是 3 秒超时问题**） |

> 说明：任务描述里的 `../queue-samples.jsonl` 在本机 `/home/jiangli/queue-samples.jsonl` 及邻近目录均不存在（已搜索 `/home/jiangli`（depth≤3）、`/tmp`、`~/.dsh`、`~/.hermes`）。本次报告使用我自己重新采样的等价数据（`windows/sample-t0..t3.json`），结论与任务描述一致。

## 2. 证据一：19 个版本在两个自然周期内零进展

用 `windows/probe-queue.ps1`（只读，仅输出 session_id / 大小 / 偏移 / 哈希 / worker 计数）采样 4 次，覆盖 16:40、16:41、16:46 多个自然周期：

| 采样 | UTC | 队列数 | 与 t0 的 offset+sha 完全一致 | acknowledged | pending | worker_status |
|---|---|---|---|---|---|---|
| t0 | 16:40:23 | 19 | — | 0 | 19 | retry_pending |
| t1 | 16:41:12 | 19 | 是 | 0 | 19 | retry_pending |
| t2 | 16:41:53 | 19 | 是 | 0 | 19 | retry_pending |
| t3（重放之后） | 16:46:53 | 19 | 是 | 0 | 19 | retry_pending |

worker 周期耗时：9.53 s / 9.33 s / 10.12 s / 13.91 s（与“最近完成周期 13 秒”一致）。
`capture_errors=0`、`discovery_errors=0`、`capture_failures=[]`（客户端从未记录上传阶段错误）。

19 个版本的完整清单（**每行“剩余”都 ≤ 262144，即只剩最后一块**；服务端 `received_bytes` 与 Windows `next_offset` 完全一致）：

| # | session_id | total_bytes | next_offset | 剩余 | 服务端 received_bytes | total − 最大已归档 | 服务端有该版本行 |
|---|---|---|---|---|---|---|---|
| 1 | 01a0bd06-0390-7363-b1c2-cf00d3130116 | 48673 | 0 | 48673 | 无行 | +74 | 否 |
| 2 | 01a0b225-388d-71a0-970a-572fa10cd337 | 55439 | 0 | 55439 | 无行 | +74 | 否 |
| 3 | 01a0b3c4-83ea-7e02-aa90-1727878ea646 | 59145 | 0 | 59145 | 无行 | +74 | 否 |
| 4 | 01a0bd06-560b-7cb3-9dc6-c74375afa053 | 59145 | 0 | 59145 | 无行 | +74 | 否 |
| 5 | 01a0b34c-4908-79f2-9beb-38639c214a46 | 59147 | 0 | 59147 | 无行 | +74 | 否 |
| 6 | 01a0b354-4257-7c31-8c9e-12093d8e42dc | 87422 | 0 | 87422 | 无行 | +1047 | 否 |
| 7 | 01a0adfc-de41-7c72-9a95-b8bb83742f68 | 103760 | 0 | 103760 | 无行 | +1047 | 否 |
| 8 | 01a0b246-3eec-7a01-9d8a-16f8f2d99d20 | 114312 | 0 | 114312 | 无行 | +1260 | 否 |
| 9 | 01a0b229-4487-7410-b1af-adb3e3e94f8d | 229676 | 0 | 229676 | 无行 | +1086 | 否 |
| 10 | 01a0b3dd-2b1f-70d3-af02-b76ccba38061 | 233791 | 0 | 233791 | 无行 | +3625 | 否 |
| 11 | 01a0b242-cc30-7dc1-a5fe-41a8b0332c03 | 374318 | 262144 | 112174 | 262144 | +2508 | 是 |
| 12 | 01a0b32a-717a-7071-aff3-6cc67c442fb4 | 446549 | 262144 | 184405 | 262144 | +4483 | 是 |
| 13 | 01a0aee4-e682-7683-bab7-f66c0845b398 | 513836 | 262144 | 251692 | 262144 | +6863 | 是 |
| 14 | 01a0b22c-8a8a-7973-8817-1ab3e07cd695 | 537461 | 524288 | 13173 | 524288 | +5784 | 是 |
| 15 | 01a0b267-a54d-7c41-8855-6cb0b1d28a5b | 864354 | 786432 | 77922 | 786432 | +13369 | 是 |
| 16 | 01a0aed0-3e27-79f0-8399-9e2a118e8018 | 1349461 | 1310720 | 38741 | 1310720 | +22691 | 是 |
| 17 | 01a0b22b-901c-7993-a948-9017676197d6 | 1613615 | 1572864 | 40751 | 1572864 | +18867 | 是 |
| 18 | 01a0af02-b585-7031-a6ef-349bc514cfd3 | 1717762 | 1572864 | 144898 | 1572864 | +27042 | 是 |
| 19 | 01a0aece-0205-7fd3-98d8-ca5e6cf3acbb | 3328751 | 3145728 | 183023 | 3145728 | −6002629 | 是 |

前 5 个 session 的“待传版本 − 最大已归档版本”**恰好都是 +74 字节**，说明这些 rollout 是被重新序列化（改写），而不是简单追加 —— 这正是 fork 保护判定“两者互不为前缀”的场景。

## 3. 证据二：实际 MCP 错误 = `transcript_fork`

方法（`windows/probe-upload.ps1`，**没有 dot-source 安装脚本、没有触发主逻辑**）：

1. 用 PowerShell AST（`[Parser]::ParseFile` + `FunctionDefinitionAst`）只提取安装脚本里的函数定义，放进临时模块（`New-Module`），再在模块作用域里补上脚本级变量（`ClientHome`/`ChunkBytes`/`RpcTimeoutSeconds=30` 等）；
2. 取**最小**待传版本（`total_bytes` 最小者）：session `01a0bd06-0390-7363-b1c2-cf00d3130116`，`total_bytes=48673`，`next_offset=0`，`sha256=7a26cc68da8273e61f0074ef736ea837ca838178fdf5865c5c0e97323ba86233`；
3. 在**原 Windows 进程内**用客户端自己的 `Unprotect-Bytes` 解开 DPAPI blob（正文不出进程、不落盘、不输出）；
4. 按客户端原样构造同一块、同一幂等参数（`request_id` 与客户端算法一致，同一 offset），只调用 1 次。

结果（`windows/replay-t0.json`）：

```
session_id   : 01a0bd06-0390-7363-b1c2-cf00d3130116
sha256       : 7a26cc68da8273e61f0074ef736ea837ca838178fdf5865c5c0e97323ba86233
total_bytes  : 48673   offset: 0   chunk_bytes: 48673
本地 blob 长度/哈希与 manifest 一致 : true / true
elapsed_ms   : 344
is_error     : true
error        : transcript_fork
```

同一进程内的两个只读 `session_archive_status` 探针（无重放）：

| session_id | status | next_offset / total | source_sha256 | extraction_status | archive_id | processing_error |
|---|---|---|---|---|---|---|
| 01a0bd06-0390-7363-b1c2-cf00d3130116 | archived | 48599 / 48599 | `8f4f707008f6…` | extracted | 483 | — |
| 01a0b354-4257-7c31-8c9e-12093d8e42dc | archived | 86375 / 86375 | `edc1c576efe2…` | **failed** | 466 | `extraction_failed` |

即：服务端对卡住的 session 只回报**旧的已归档版本**，卡住的版本连行都没有 —— 与任务描述的“部分小版本未建立行”一致。

重放安全性：该错误在 `if complete:` 分支内抛出，位于 `_write_stage` / `INSERT` / `archive_session` 之前，因此**没有写入任何 staging 文件、没有新增行、没有创建归档**。重放后 t3 采样确认 19 个 manifest 的 offset/sha 与 t0 完全一致。

## 4. 根因链路（源码定位）

`evolvmem/lan_capture.py::LanCapture.upload`（第 244–272 行）：

```python
if complete:                                   # 只有补齐 total_bytes 的最后一块才进入
    ...
    latest = self._latest(identity)            # 该 session 已归档的“最大 total_bytes”版本
    if latest is not None and latest['sha256'] != digest:
        previous = self.archiver.read_payload(latest['archive_id'])
        ...
        if prior is not None and prior.startswith(raw):
            return self._stale_receipt(...)    # 新版本是旧版本的前缀 → stale（客户端会接受并清队列）
        if prior is not None and not raw.startswith(prior):
            raise LanError('transcript_fork')  # ← 生产实际命中这里
```

客户端侧（`scripts/windows/evolvmem-codex.ps1`）：

- 第 576–581 行：`Test-FilePrefix` 失败即认为“transcript 被重写/分叉”，把 `sourceBytes` 归零，**故意生成一个全新的不可变版本**（注释原文：*A rewritten or divergent transcript begins a new immutable content version*）；
- 因此客户端会持续产生“与已归档最大版本互不为前缀”的新版本，而服务端只有 `append`（接受）和 `stale`（接受为陈旧）两条接受路径，**没有任何“新 lineage 根”的接受路径** → 该 session 被永久锁死；
- `_latest` 用 `ORDER BY total_bytes DESC` 取“最新”，把“最大”当成“最新”；一旦新 lineage 更小（第 19 例 3.3 MB vs 9.3 MB），即使接受了新根，后续上传仍会继续对着旧的大版本做 fork 判定；
- 第 700–703 行 `catch { continue }` 吞掉全部异常，`worker-status.json` 只剩 `retry_pending`；`Convert-McpToolResult`（第 164 行）本身还把远程错误码丢掉，只抛 `remote MCP tool failed` —— 这就是“看回执完全看不出原因”的原因。

**量化自洽**：实测单次失败 344 ms × 19 个版本 ≈ 6.5 s，加上扫描/捕获开销 ≈ 9–10 s，与实测周期 9.33–10.12 s 吻合；说明失败是服务端**快速返回错误**，不是超时、不是网络问题。

> **历史状态更新（2026-10-08）：第 5–6 节与第 9 节的候选方案已废弃，不作为现行协议依据。**
> 本报告记录的 `parent_sha256` “lineage root” 候选补丁从未被采用；现行上传协议是可选字段
> `source_order` / `current_sha256` 与 `lan_session_heads` 当前指针，`LanCapture._latest` 已不存在。
> 两处旧诊断测试已迁移到现行协议：`windows/test_transcript_fork_repro.py`（4 passed）与
> `windows/tests/test_lan_capture_lineage.py`（4 passed）；`windows/make_patches.py` 已删除；
> 两个补丁改名为 `patch-N-*.patch.txt` 并在文件头声明“请勿应用”（`git apply --check` 与
> GNU `patch` 均拒绝，已不是可应用的补丁）。以下原文保留为历史问题定位记录。

## 5. 合成数据复现（`windows/test_transcript_fork_repro.py`，4 passed）

用项目自带的隔离测试运行时（`tests/test_lan_sharing.py` 的 `lan` fixture，tmp_path + `LanRuntime`，**不触碰线上 runtime/DB/cache**）构造同形态数据：

| 用例 | 断言 | 结果 |
|---|---|---|
| `test_repro_rewritten_version_deadlocks_on_final_chunk` | 前面的块全部 `receiving`，**最后一块** `transcript_fork`；同参数重放仍 `transcript_fork`；status 只回报旧归档 | PASS |
| `test_largest_is_not_newest_so_a_smaller_new_lineage_can_never_win` | `_latest()` 返回最大而非最新版本；更小的重写版本永远 fork | PASS |
| `test_current_code_rejects_an_undeclared_extra_argument` | 现网 schema 会拒绝未声明的新参数（`invalid_arguments`） | PASS |
| `test_proposed_fix_accepts_declared_root_and_keeps_the_old_archive` | 声明后接受新根、旧归档保留、后续追加改用新根、未声明的 fork 仍被拒 | PASS |

## 6. 最小修复建议（补丁已生成，**未应用、未提交**）

### 6.1 服务端 `evolvmem/lan_capture.py` → `windows/patch-1-server-lineage-root.patch`

1. `session_archive_upload` 新增**可选**参数 `parent_sha256`（`required` 里排除它，**已部署的旧客户端不受影响**）；
2. `parent_sha256 == ''` 表示客户端明确声明“这是一个新的 lineage 根”：fork 校验不再抛错，改为接受并归档（旧归档保持不可变、不删除、不覆盖），回执加 `lineage: "new_root"` 便于审计；
3. `_latest()` 改为 `ORDER BY received_at DESC, rowid DESC`（“最新”按接收时间，而不是按体积）。

注意：第 3 点是**必需项**，不是可选项 —— 否则新 root 一旦比旧版本小，后续追加仍会拿旧的大版本做前缀判定，继续 fork。

### 6.2 客户端 `scripts/windows/evolvmem-codex.ps1` → `windows/patch-2-client-lineage-root.patch`

1. `Convert-McpToolResult` 抛出时带上远程错误码（`remote MCP tool failed: transcript_fork`）；
2. `Capture-Transcript` 在“检测到分叉/缓存不一致”时把 `lineage_break = true` 写进 manifest；
3. `Invoke-UploadQueue`：manifest 声明分叉时带 `parent_sha256=''`；若仍收到 `transcript_fork`，**只对同一块**用 `parent_sha256=''` 重试一次（可自愈已积压的 19 个版本，不需要删除或改动待传数据）；
4. 失败时把 `last_error` / `last_error_utc` 写回 manifest（只加字段，`next_offset` 不变），让“卡住的原因”可见而不是只有 `retry_pending`。

### 6.3 回归测试（建议落到 `tests/test_lan_capture_lineage.py`）

`windows/tests/test_lan_capture_lineage.py`（5 个用例：未声明仍拒绝 / 声明后归档且旧归档保留 / 追加改用新根 / 新参数可选 / 真追加不会被误标 new_root）。

在**补丁副本**上验证（复制到 `/tmp/evolvmem-patchtest`，`patch -p1` 后运行；线上源码树零改动）：

```
patched : tests/test_lan_capture_lineage.py + tests/test_lan_capture.py → 25 passed
base    : tests/test_lan_capture_lineage.py → 4 failed, 1 passed   （失败原因即 transcript_fork / invalid_arguments）
```

### 6.4 建议的配套改动（未包含在最小补丁内）

`LanCapture.claim_pending()` 仍假设“体积最大 = 最新”：

```sql
AND NOT EXISTS (SELECT 1 FROM lan_session_uploads b WHERE ... AND b.total_bytes > a.total_bytes)
```

对第 19 例（新根 3.3 MB < 旧版本 9.3 MB）会出现“归档成功但提取永不被领取”。建议同 6.1 第 3 点一起改成按 `rowid`（接收顺序）比较。本次最小补丁未改它，以免扩大行为面。

## 7. 任务 4：两项已归档提取失败的错误码

**统计口径已对上**：对 53 个 session 取“最新已归档版本”计数，得到 `extracted 33 / unassigned 18 / failed 2`，与协调端给出的 `33 提取成功 / 18 未分配 / 2 提取失败`（共 53）完全一致。

两项失败（`lan_session_uploads.error` = 客户端可见的 `processing_error`）：

| session_id | sha256(前12) | total_bytes | archive_id | error（稳定错误码） | extraction_result | received_at (CST) |
|---|---|---|---|---|---|---|
| 01a0b22b-901c-7993-a948-9017676197d6 | `094267e95ce5` | 1594748 | 403 | `extraction_failed` | `{}` | 2026-09-18 10:09:37 |
| 01a0b354-4257-7c31-8c9e-12093d8e42dc | `edc1c576efe2` | 86375 | 466 | `extraction_failed` | `{}` | 2026-09-18 15:04:31 |

- 两项的稳定错误码**相同且都是 `extraction_failed`**；它由 `evolvmem/lan_tools.py:169` 生成：
  `reason = str(exc) if isinstance(exc, LanError) else 'extraction_failed'`。
  也就是说，这两个失败的底层异常**不是 LanError**，异常类型/消息/栈被丢弃，`extraction_result` 一直是 `{}`，**从库和 MCP 回执无法再往下追**（预期中的可诊断码如 `extraction_summary_missing` / `extraction_summary_rejected` / `extraction_provider_unavailable` / `archive_payload_unavailable` 都不是）。
- 关联性判断：这 2 个 session 同时也在 19 个卡住版本之列（`01a0b22b` 待传 `503dcceb…`，`01a0b354` 待传 `642153fe…`），但**不能据此认定因果**：53 个 session 里 2 个失败全落在这 19 个中的概率约 12%（C(19,2)/C(53,2)），不显著；而且提取失败发生在 09-18，卡住版本是 09-20/21，两条链路（归档后提取 vs 最终块前缀校验）互不相关。
- **可以确认的关联只有“记录方式”这一层**：两种故障都被折叠成一个常量通用码并被客户端吞掉，因此现场都没有可诊断信息。建议的观测性改动见 6.2 第 1、4 点；提取侧建议在 `lan_tools.py:169` 同时保留 `type(exc).__name__`（脱敏、无内容）作为伴随码。

## 8. 未确认项 / 风险 / 最短下一步

1. **未确认**：这些 rollout 究竟由谁重写（Codex 客户端重写 vs 客户端捕获逻辑）无法从现有数据判定。判定成本最低的一步：在服务端 fork 分支里记录**无内容的**诊断三元组 —— `len(prior)`、`len(raw)`、**最长公共前缀字节数**（`os.path.commonprefix`/循环比较，只输出整数）。公共前缀接近 0 说明是头部重写，接近 `len(prior)` 说明是尾部差异。
2. **未做**：没有读取任何用户正文（DPAPI 正文只在原 Windows 进程内使用，未复制、未输出）；没有读 `llm_credentials.json`/token；没有跑任何会初始化/重建 cache 的线上 LanRuntime；本地 DB 仅以 `mode=ro` 查询 `lan_session_uploads` 的元数据列（未选 `extraction_result` 正文内容以外的任何 payload）。
3. **对线上无副作用**：仅重放 1 个已有块、参数与原客户端一致；重放路径在写库/写 staging 之前抛错；重放后采样（t3）确认队列零变化。
4. **修复验证的最短路径**：在测试副本上应用 `patch-1`+`patch-2` → 用一个真实卡住的 manifest 走一遍 `Invoke-UploadQueue`（1 个版本）→ 期望 `acknowledged_versions=1`、该 session 新增归档、旧的 archive 483/466 等仍在。当前阶段不动线上，等待上线协调。

## 9. 附件（`windows/` 目录，均无敏感信息）

| 文件 | 说明 |
|---|---|
| `sample-t0.json` … `sample-t3.json` | 4 次只读队列/worker 采样（t3 为重放后无副作用校验） |
| `replay-t0.json` | 单块重放结果（`transcript_fork`，344 ms）与两条只读 status 探针 |
| `hashes.json` | 安装副本哈希/大小/时间（与仓库源码逐字节一致） |
| `probe-queue.ps1` / `probe-upload.ps1` / `probe-hashes.ps1` / `winrun.sh` | 只读诊断脚本与 SSH+UTF16LE base64 传输封装 |
| `test_transcript_fork_repro.py` | 合成数据复现，已迁移到现行协议（未改源码、未猴补候选实现；4 passed） |
| `tests/test_lan_capture_lineage.py` | 重写版本回归，已迁移到现行协议（4 passed） |
| `patch-1-server-lineage-root.patch.txt` / `patch-2-client-lineage-root.patch.txt` | **已废弃**的候选补丁快照（不可应用；现行协议与禁止使用说明见文件头） |
| ~~`make_patches.py`~~ | 已删除；该脚本只能生成上述废弃补丁，Git 保留历史 |

> 源码树未被本任务改动：`evolvmem/lan_capture.py`、`evolvmem/lan_tools.py`、`scripts/windows/evolvmem-codex.ps1` 的 mtime 与内容保持原样（补丁在 `/tmp` 副本上验证）。仓库内其余 `M`（modified）状态是本任务开始前就存在的。

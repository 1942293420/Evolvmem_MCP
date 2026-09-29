# EvolvMem 服务端 degraded_legacy 根因排查报告

- 任务：定位常驻服务 `ready=false / reason_codes=[degraded_legacy]` 的根因，重点是
  “同一份数据，新连接评估正常、常驻进程却降级”的跨连接/线程健康门禁误判。
- 范围：只读源码与运行状态 + 合成库隔离实验。**未**改动线上代码/配置，**未**重启服务，
  **未**调用 `LanRuntime.initialize`，**未**读写用户正文/凭据；线上 `memory.db` 仅做
  只读的 schema/表名/计数/`quick_check` 查询。
- 产出脚本与原始日志：`server/repro/`（合成数据写在 `server/repro/work`、`scale`、
  `conc`、`threads`、`mixed`、`deterministic` 子目录，全部可删）。

---

## 0. 结论摘要

**已证实（合成库 + 真实源码类，可重复）**

1. `_quick_check_diagnostics` 依赖的 FTS5 校验存在**按连接缓存的结构快照**：某连接读过
   FTS5 表后，若另一连接先提交写入、本连接再在**新读快照**上跑 `PRAGMA quick_check`，会
   得到 `fts5: checksum mismatch for table "..."`，被映射成 `quick_check_failed`
   → `reason_codes=('degraded_legacy',)`。**磁盘数据本身没有损坏**（新连接同时刻
   `quick_check=ok`）。
2. 现网修复的 `SAVEPOINT → 刷新两张 context FTS → quick_check` 在**单线程调用**下是正确
   且对“外部进程写入”免疫的（N=2000/8000，插入/更新/删除/批量，0/80、0/250 失败）。
   它的正确性来自 SAVEPOINT 把“刷新读”和 `quick_check` 固定在同一个读事务快照里。
3. **一旦同一条连接上还有第二个线程做写事务**（`ContextStore.transaction()` 的
   `BEGIN IMMEDIATE`），两者会互踩：写事务抛
   `cannot start a transaction within a transaction`，其异常分支 `conn.rollback()`
   把健康检查的读事务提前结束；随后 `quick_check` 在新快照 + 旧 FTS 缓存下执行 →
   `quick_check_failed`。确定性 7 步重放稳定复现（见 §2.3），真实多线程交错下
   2/5 次实验出现误报（每次 250 次检查中 2 次），且**写请求失败每次必然出现**
   （25–53 次/轮）。
4. 因此“常驻进程降级 / 新连接正常”的差异可以用**连接本地的 FTS5 缓存 + 读事务被中断**
   完整解释；协调者用 `mode=ro` 新连接看到的 `quick_check=ok` **不能**否证该机制，也
   **不能**当作“FTS 旧快照已证实”。

**已排除（有实验反证）**

- `memories_fts` / `memories_fts_trigram`（外部内容 FTS5，`content=memories`）没被现网
  修复刷新**不是**本次误报来源：三种写（INSERT/UPDATE/DELETE）+ 检查连接事先读过该表，
  全部 `ok`（§3.1）。“补上 legacy FTS 两张表”没有证据支持。
- “`LIMIT 1` 刷新在 8336 行规模下太浅”：N=8000、更新最后一行/500 行/批量 100 插/100 删，
  `LIMIT 1` 刷新 + SAVEPOINT 全部 `ok`（§3.2）。
- “外部进程单独写入即可致降级”：独立连接写 250 次检查 0 失败（§3.3）。
- “线上数据真的坏了”：所有合成误报发生的同一时刻，新连接 `quick_check=ok`；协调者在
  线上新连接也得到 `ok`（§2.2/§2.3 step7）。

**仍未知（必须采到线上失败码才能定案）**

- 当前常驻进程 `diagnostics` 的确切字符串（`lan_tools._sanitize` 把 `diagnostics` 键整段
  丢掉，HTTP 侧看不到）。已复现的是 `quick_check_failed` 这一类；但
  `legacy_mapping_incomplete` / `layer_invariant_failed` / `context_vector_*` 同样是
  只存在于常驻进程状态的候选，**不能凭新连接评估结果推断**。
- 线上触发“同连接并发写”的具体线程：`LanTools.handle_request` 有
  `runtime._lan_dispatch_lock`（`evolvmem/lan_tools.py:97,445`），capture worker 的持久化
  也持锁（`lan_tools.py:126,144`），所以需要确认哪条路径在持锁之外写共享连接
  （候选：`mcp_server.py:1617` 的 init 线程、同进程内 hooks/dsh_bridge、或锁外的写路径）。

---

## 1. 代码事实（只读）

| 位置 | 事实 |
| --- | --- |
| `evolvmem/context_service.py:421-438` | `_quick_check_diagnostics()`：`SAVEPOINT evolvmem_health_snapshot` → 对 `context_layers_fts`、`context_layers_fts_trigram` 各做一次 `SELECT rowid ... LIMIT 1` → `PRAGMA quick_check` → `RELEASE`；任何异常都收敛为 `quick_check_failed`。 |
| `context_service.py:383-419` | `_primary_diagnostics()` 汇总 `validate_runtime` / `quick_check` / `count_by_status` / 逐条 `get_item` / projection lag / 向量类；非空即 `ready=False`、`reason_codes=('degraded_legacy',)`。 |
| `context_service.py:2292-2297` | PRIMARY 模式下 `_legacy_reader()` = `store.legacy_projection()`，即 **legacy `memories` 的 FTS 检索与健康检查共用同一条 ContextStore 连接**。 |
| `context_store.py:390-397,439-442` | 单条 `check_same_thread=False` 连接，`journal_mode=WAL`、`foreign_keys=ON`；`_connection()` 直接返回它（无锁）。 |
| `context_store.py:420-437` | `transaction()`：最外层 `BEGIN IMMEDIATE`，异常分支 `conn.rollback()`；`_transaction_depth` 是进程内共享计数器，不区分线程。 |
| `mcp_server.py:1452-1471` | `_live_status()`：PRIMARY 模式**每次工具调用**都调 `service._refresh_health()`。 |
| `lan_server.py:5,155` | `ThreadingHTTPServer`，每个请求一个线程。 |
| `lan_tools.py:97,445` | `self.lock = runtime._lan_dispatch_lock`，`handle_request` 全程持锁 → MCP 工具调用被串行化。 |
| `lan_runtime.py:159-175` | 另有 `evolvmem-session-extraction` 后台线程，每 5s 跑 `adapter.process_backfills()/process_pending()`；其写入点 `lan_tools.py:118-150` 也持同一把锁。 |
| `lan_tools.py:403-409` | `_sanitize` 递归丢弃键名 `diagnostics`、`embedding_diagnostics` 等 → HTTP 侧永远看不到细分原因。 |

线上（只读）事实：`memory.db` 里 **4 张 FTS5 虚拟表** —
`context_layers_fts(8336)`、`context_layers_fts_trigram(8336)`、
`memories_fts(3236)`、`memories_fts_trigram(3236)`；`memories=3236`；
`journal_mode=wal`；sqlite 3.53.1；单连接 `PRAGMA quick_check` = `ok`。

---

## 2. 复现实验（证据到命令与输出）

解释器：`/home/jiangli/hermes-memory-plugin/.venv/bin/python`（sqlite3 3.53.1）。
全部脚本使用合成库，导入生产源码类（`ContextStore`/`ContextService`/`MemoryStore`），
**不**调用 `LanRuntime.initialize`。

### 2.1 机制校准：FTS5 校验按连接缓存

```
$ .venv/bin/python server/repro/matrix_quick_check.py      # 原样摘录
FAIL | cal_ctx_fail   prior_read='SELECT rowid FROM context_layers_fts LIMIT 1' write="UPDATE context_layers SET content='x changed' WHERE id=1" refresh='' rows=['fts5: checksum mismatch for table "context_layers_fts"']
OK   | cal_ctx_ok     prior_read='SELECT rowid FROM context_layers_fts LIMIT 1' write=同一条 UPDATE refresh='SELECT rowid FROM context_layers_fts LIMIT 1;SELECT rowid FROM context_layers_fts_trigram LIMIT 1' rows=['ok']
OK   | cal_ctx_noread prior_read=None write=同一条 UPDATE refresh='' rows=['ok']
```

- 误报的必要条件：连接**曾经读过**该 FTS5 表 → 另一连接提交写入 → 本连接在**刷新之前**
  跑 `quick_check`。
- 刷新读一旦执行（且与 `quick_check` 同事务）即恢复正常 → 说明是连接本地缓存，不是磁盘损坏。

### 2.2 线上形状（单线程 + 外部写）不误报

```
$ .venv/bin/python server/repro/repro_fts_snapshot.py     # 真实 ContextService
S0 baseline = ()  S1 外部写 context_layers = ()  S2 legacy 检索后 = ()
S3 外部写 memories = ()   S3c raw rows = ['ok']   S6 新连接 = ()  S7b 无历史读 = ()
S9 status() = ready: False reasons: ('degraded_legacy',)   # 合成库缺投影/向量的旁证，与本议题无关
```

```
$ .venv/bin/python server/repro/scale_quick_check.py      # N=8000
OK | prior_update_last_lim1   更新最后一行 + LIMIT 1 刷新
OK | prior_update500_lim1     批量更新 500 行
OK | prior_insert100_lim1     批量插入 100 行（等价上传）
OK | prior_delete100_lim1     批量删除 100 行
FAIL| prior_update_last_nonrefresh   不刷新（对照，证明机制存在）
OK | prior_update_last_fullcount     用 count(*) 全扫刷新（不比 LIMIT 1 更强）
FAIL| refresh-then-concurrent-write  刷新后、quick_check 前另一连接提交 → 两张表都 mismatch
```

### 2.3 同一条连接上的并发写：确定性重放 + 真实多线程

确定性 7 步重放（无需线程，完全稳定）：

```
$ .venv/bin/python server/repro/deterministic_interleave.py
step0 quick_check = ['ok']
step3 concurrent BEGIN IMMEDIATE refused = cannot start a transaction within a transaction
step4 quick_check rows = ['fts5: checksum mismatch for table "context_layers_fts_trigram"',
                          'fts5: checksum mismatch for table "context_layers_fts"']
step5 RELEASE (finally 分支) = no such savepoint: evolvmem_health_snapshot
step6 == 线上 _quick_check_diagnostics 返回 quick_check_failed = True
step7 新连接（只读视角）quick_check = ['ok']
```

因果链：健康检查 `SAVEPOINT` 打开读事务并在其中刷新 FTS 缓存 → 同一连接上第二个线程的
`ContextStore.transaction()` 执行 `BEGIN IMMEDIATE` 失败 → 其 `except` 分支
`conn.rollback()`（`context_store.py:432-435`）结束健康检查的读事务 → `quick_check` 落到
**新快照**、而 FTS5 结构缓存仍是刷新时的版本 → checksum mismatch（且 `finally` 的
`RELEASE` 报 `no such savepoint`，异常路径本身也返回 `quick_check_failed`）。

真实多线程复跑（写线程与健康检查共享 `service.store` 的连接）：

```
$ for i in 1 2 3; do .venv/bin/python server/repro/mixed_write_health.py; done
same-conn writer      health_fails= 0/250 writes=346 write_errors= 46   (第一次)
same-conn writer      health_fails= 0/250 writes=348 write_errors= 53   (第二次)
same-conn writer      health_fails= 2/250 writes=346 write_errors= 25  first=('quick_check_failed',)  (第三次)
separate-conn writer  health_fails= 0/250 writes=577 write_errors=  0   (每轮对照)
```

读法：**写事务互踩是必然的**（25–53 次/轮 `cannot start a transaction within a transaction`），
健康检查误报是**时序相关**的（5 轮里 2 轮命中，约 0.2%–0.8%/次检查）。这正好解释
“启动时 ready=true、外部写入后降级”的现象：需要一次“写路径恰好和健康检查同连接重叠”。

补充对照（都被否证为修复方向）：

```
$ .venv/bin/python server/repro/concurrent_health.py   # 独立写线程 + 各检查策略
shipped   failures= 0/80      # SAVEPOINT + LIMIT 1 刷新（现网实现）
fullscan  failures= 0/80      # count(*) 全扫刷新
retry     failures= 0/80
fresh     failures= 6/80 first=('quick_check_failed',)   # 新连接但“刷新/quick_check 不同事务”
$ .venv/bin/python server/repro/concurrent_threads.py  # 两线程共享连接（只在健康检查之间交错）
A shared-conn no-lock = 0/300   B 加锁 = 0/300   C 自带短连接+单事务 = 0/300
```

结论：**“换新连接”本身不是修复**（`fresh` 6/80 反例）；有效形状是
“刷新 + `quick_check` 必须在同一个事务里”，要么保留 SAVEPOINT 并把连接访问串行化，
要么每次检查用独立连接但显式 `BEGIN … COMMIT` 包住两步。

---

## 3. 排除项明细

### 3.1 未刷新的 legacy FTS（外部内容 FTS5）——不成立

```
$ .venv/bin/python server/repro/matrix_quick_check.py   （memories 段全部 OK）
OK | prior_read='SELECT rowid FROM memories_fts LIMIT 1' write="UPDATE memories ..."  refresh=''
OK | prior_read='SELECT rowid FROM memories_fts LIMIT 1' write='DELETE FROM memories WHERE id=1' refresh=''
OK | prior_read='SELECT rowid FROM memories_fts LIMIT 1' write='INSERT INTO memories ...' refresh=''
OK | prior_read='SELECT rowid FROM memories_fts LIMIT 1' write=UPDATE refresh=仅补刷 context 两张表   # 等价现网代码
```

`content=memories` 的外部内容 FTS5 表在 sqlite 3.53.1 上**没有**出现 checksum mismatch，
即使检查连接事先读过该表、且写入由触发器走 `'delete' + insert` 路径。因此
“修复漏了 `memories_fts`/`memories_fts_trigram`”不能解释线上降级；把这两张表也加进刷新
列表属于无害但不解决问题的改动。

### 3.2 `LIMIT 1` 刷新深度——不成立

`scale_quick_check.py`：N=8000（接近线上 8336），更新最后一行 / 批量 500 更新 / 100 插入 /
100 删除后，`LIMIT 1` 刷新 + SAVEPOINT 均 `ok`；`count(*)` 全扫并不更强。唯一失败的是
**完全不刷新**或**刷新与检查跨事务**两种情形。

### 3.3 纯外部进程写入——不成立

`mixed_write_health.py` B 组：写线程用独立连接（`BEGIN IMMEDIATE` + commit，模拟另一进程/
另一连接的上传），健康检查 250 次 0 失败、写 0 错误。与协调者“新连接评估正常”的观察一致。

### 3.4 磁盘数据损坏——未被证据支持

所有合成误报的同一时刻，独立新连接 `quick_check=ok`；线上新连接同样 `ok`。当前现象更像
**连接本地视图不一致**，而不是数据库损坏。注意：这不能排除常驻进程另有其它诊断码
（见 §5 采集方法）。

---

## 4. 失败测试与建议回归用例（未落地，仅建议）

现有用例只覆盖单线程：

- `tests/test_context_service.py:266` `test_health_refreshes_fts_snapshot_after_another_client_write`
  （外部连接 UPDATE context_layers → 期望 `()`）——在本机实测通过；
- `tests/test_context_service.py:275` `test_health_snapshot_refresh_still_detects_corrupt_fts`
  （破坏 `context_layers_fts_content` → 期望 `('quick_check_failed',)`）——通过，
  说明“真损坏仍能被发现”，本次误报不是把真损坏也放过。

建议新增（脚本已是可运行雏形，可直接搬进 `tests/`）：

1. `test_health_check_conflict_with_same_connection_write`：同一 `ContextStore` 连接上，
   线程 A 循环 `_quick_check_diagnostics()`，线程 B 循环 `with store.transaction(): UPDATE context_layers …`；
   断言 (a) B 不出现 `cannot start a transaction within a transaction`，
   (b) A 不出现 `quick_check_failed`。（当前实现会失败：写错误 25–53 次/250 次写。）
2. `test_health_check_rejects_cross_transaction_refresh`：显式证明“刷新与 `quick_check`
   跨事务”会误报（`server/repro/concurrent_health.py` 的 `fresh` 分支形状），
   用于锁死修复方向。
3. 端到端：`_live_status()` 在并发写期间不应把 `ready` 翻成 `false`。

---

## 5. 线上失败码采集方法（最低干扰、无正文/无路径泄漏）

**为什么必须采**：`context_status` 的返回里其实带 `diagnostics`（`mcp_server.py:855`），
但 LAN 侧 `lan_tools._sanitize`（`lan_tools.py:408`）按**键名**丢弃
`diagnostics`/`embedding_diagnostics`，所以 HTTP 客户端只看到
`reason_codes=[degraded_legacy]`，无法区分是 `quick_check_failed`、
`legacy_mapping_incomplete`、`layer_invariant_failed` 还是 `context_vector_*`。
新连接复算只能覆盖“数据级”诊断，覆盖不到常驻连接的缓存状态与进程内向量状态。

按干扰从低到高：

1. **零接触（现在就能做，仅读、无写入、无正文）**——区分“连接本地误判”与“数据级失败”：
   - 对 LAN 依次调用只读的 `context_status`（owner 通道），记录 `ready/reason_codes` 与时间；
   - 同时刻在 DSH 侧用**只读**连接复算 `_quick_check_diagnostics()` 与
     `_projection_invariant_diagnostics()`（协调者已做过：均为 `()`）；
   - 观察 `ready` 是否在一次写操作（上传/抽取）后**自行回真**：若回真且无需人工干预，
     基本可判定为连接本地 FTS 缓存/事务交错类误报，而不是磁盘或投影数据损坏。
   - 全程不输出任何 memory/context 内容、不输出路径；只记录布尔与码。
2. **一次性、只读的在进程内探针（需要一次计划内发布，不重启业务数据面）**：
   在 `_context_status` 里把 `status.diagnostics` 同时以**不被 sanitize 的键名**返回
   （例如 `health_codes`，内容仍是无正文的短码列表）。这是把现有信息换个键名，
   不新增查询、不写库；之后只需调用一次 `context_status` 即可拿到确切失败码。
   若不便改工具契约，也可在状态转换时以 `logging` 记一行码（无正文、无路径）。
3. **不推荐**：gdb/py-spy 在生产进程里求值 Python 对象（可能死锁、且属于侵入式）；
   用 `LanRuntime.initialize` 或任何会重建缓存/向量的“诊断”脚本（会清掉现场）。

若上级只允许零接触，请接受结论停留在：**“已复现一类可造成 `degraded_legacy` 的同连接
交错误报机制，且它解释了‘新连接正常/常驻降级’的差异；但当前这一次降级的确切码未知”**。

---

## 6. 最小修复建议（按风险从低到高）

1. **让健康检查与写事务互不干扰（最小、直接针对已复现缺陷）**
   - 方案 A：`_quick_check_diagnostics` 每次用**独立短连接**，并显式
     `BEGIN` → 刷新 FTS → `PRAGMA quick_check` → `COMMIT`。证据：
     `concurrent_threads.py` C 组 0/300；反例 `concurrent_health.py` `fresh` 6/80
     证明“必须同一事务”，只换连接不够。
   - 方案 B：保留共享连接，但用一把连接级锁把“SAVEPOINT 段”与所有写事务串行化；
     同时把 `ContextStore.transaction()` 的最外层语句改为
     “`conn.in_transaction` 时用 `SAVEPOINT`，否则 `BEGIN IMMEDIATE`”，
     避免 `rollback()` 误杀别的线程的读事务（当前 25–53 次/轮写失败即由此而来）。
   - 任一方案都需要一次发布；发布前请先按 §5.1 留证，避免覆盖现场。
2. **失败码可见性**：`_context_status` 增加 sanitizer 安全的只读键（如 `health_codes`），
   或 `_sanitize` 对 `diagnostics` 做“只保留白名单短码”的放行。无正文、无路径泄漏。
3. **判定降级前的自证**：`quick_check` 返回非 `ok` 时（尤其 FTS5 checksum mismatch），
   在**同一事务**内再刷新一次并复检，仍失败才置 `degraded_legacy`；这可以消除偶发误报，
   又不会掩盖真损坏（现有 `test_health_snapshot_refresh_still_detects_corrupt_fts` 仍能通过）。
4. 不建议把 `memories_fts*` 加进刷新列表当作修复（§3.1 已否证），可作为一致性冗余，
   但不应作为本次降级的处置动作。

---

## 7. 未决问题与最短下一步

1. 【最短】按 §5.1 做一次零接触对照：连续调用 `context_status`，记录 `ready` 翻转与
   写操作时间的相关性；同时保留新连接只读复算结果。若 `ready` 自愈 → 按 §6.1 修。
2. 确认常驻进程内**哪条路径在 `_lan_dispatch_lock` 之外用共享连接写**（本次复现证明这
   是充分条件；`lan_tools` 的请求路径与 capture worker 持久化都持锁，需要实际栈/日志
   才能定位，可用 `health_codes` + 转换日志替代侵入式调试）。
3. 若 §5.2 拿到确切码不是 `quick_check_failed`，则本报告的机制不是本次根因，应按
   `legacy_mapping_incomplete` / `layer_invariant_failed` / `context_vector_*` 另立分支；
   **在此之前不得宣称根因已定**。

---

## 8. 合规与可清理项

- 未修改/未新增任何线上代码或配置；未重启、未触发 `LanRuntime.initialize`。
- 线上库查询仅限：`sqlite_master` 中 FTS 相关对象名与 DDL 前缀、
  4 张 FTS 表与 `memories` 的计数、`journal_mode`、`PRAGMA quick_check`；
  未读取任何正文、凭据或配置密钥。
- 本次新增文件（可整体删除，不影响仓库）：
  `server-report.md`、`server/repro/*.py`、`server/repro/*.log`、
  以及合成库目录 `server/repro/{work,scale,conc,threads,mixed,deterministic}/`。

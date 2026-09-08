# EvolvMem 中断任务恢复更新

用户已于 2026-09-09 批准下列方案，并指定 Kimi 实施。本文是本轮唯一设计与实施说明，不再增加 spec/plan/handoff 文档或重复索取设计确认。

## 目标与边界

- 业务目标：项目做到一半意外退出，重开后说“继续权限审批”等项目名，可以经 EvolvMem 找到最后已保存的进度与下一步；不误把其他项目或未验证的操作当成已完成。
- 可信内网、1～10 人低并发、单机使用；复用现有 Python、SQLite、项目注册、checkpoint、MCP 和会话来源解析。
- 不增加依赖、新服务、UI、登录、权限框架、多机/高并发机制，不重构无关模块。
- Kimi 仅处理本独立目录相关源码和虚构测试；不得读取其他项目、用户配置/凭据、真实记忆数据库或真实 Codex/Kimi 会话，不调用外部 MCP/业务 API。
- 真实库修复、历史补录、客户端规则接入和最终部署由主代理在本机完成。不得把真实记忆/业务内容发送给模型。
- 此工作区已合入主工作区现有未提交代码作为基线；只提交本轮修改，不回退基线或修改主工作区。
- 使用 `/home/jiangli/hermes-memory-plugin/.venv/bin/python -m pytest`，当前目录优先加载源码；不重复安装依赖。

## 已知事实

- 现有 `continuity_resume` 按项目及 workspace fingerprint 定位 focus；无绑定或无断点均可能返回 no_continuation。
- 项目登记、别名与 workspace 绑定已有 `project_cli` 和 `ProjectStore`；登记后创建断点仍依赖 agent 主动分步调用。
- 现有 `continuity_checkpoint` 能保存 objective、accepted_decisions、completed_steps、current_step、next_action，并有 revision CAS。
- 当前 `continuity_list` 只列已解析项目与工作目录的任务，从通用主目录难以发现其他项目。
- `scripts/extract_stale_sessions.py` 的离线补录仅扫描 Kimi wire；`experience_sources.py` 已能识别 Codex 来源和原生会话 ID，可复用。
- 原始会话摘要、经验案例和可续接任务是不同对象；产生摘要或经验不等于已创建任务断点。
- 主代理观察到真实 PRIMARY 的 `projection_lag_nonzero`；长连接还报告快照/向量差异。continuity 专用门禁应继续独立于经验检索的 serving gate。

## 实施内容与顺序

1. 项目首次使用与断点创建闭环
   - 给 agent 一个明确可调用的 MCP/CLI 入口，按调用方提供的实际项目目录、项目名和可选中文别名，幂等登记、绑定并创建/更新任务。
   - 明确的调用方项目声明允许自动登记；不能仅凭通用 home/cwd 或日志中的模糊词猜项目，也不能把所有 home 会话绑成一个默认项目。
   - 重复调用不创建重复项目、别名或任务；现有字段/状态/CAS 协议保持兼容。
   - 给出自动调用的简短 agent 规则：目标明确时 begin，关键结果/阻塞/结束时 checkpoint。优先更新现有 AGENTS.md，安装说明放在本文。

2. 分层查找与恢复
   - 保留精确 resume 语义，补一个在项目未解析或 focus 缺失/悬空时可调用的发现入口。
   - 支持项目名/中文别名/任务关键词查未完成任务；从通用目录也能找到候选。
   - 唯一有依据的候选提供可继续回读的 checkpoint；多个候选返回有限列表供选择，不擅自切换其他项目 focus。
   - 保留 workspace 核验/过期信息；可跨项目发现，不等于已经在正确代码目录执行。
   - 返回有用的失败分类，区分未登记、无任务和终态悬空指针；不要以降级为由隐藏 continuity 能力。

3. Codex 中断日志补录
   - 增加本地、无需外部模型的有界增量扫描/CLI，并复用已有来源解析；扫描范围和项目归属有明确入口参数或本地配置。
   - 支持持久 JSONL 的 session_meta、user 指令、工具调用与结果等实际结构；写虚构 fixtures 覆盖。
   - 已存在结构化 checkpoint 时优先复用；没有 checkpoint 时从可确定的目标和进度生成保守的未完成任务，并保留来源会话/行或事件 ID。
   - 助手说“完成”不是执行证据；没有结果的工具调用记为待核实，静默会话不等于任务完成。
   - 处理重复扫描、追加日志、尾部半行、超长消息、活跃会话、混合项目会话和含路径/凭据的内容；复用现有内容边界，无法可靠归属的条目作为候选而非猜写到项目。
   - 提供 dry-run 与有限 apply；逐来源版本幂等，不重复生成任务，不覆盖更新的人工 checkpoint、不复活已完成任务、不改变用户正在做的其他任务 focus。
   - 提供复用现有 cron 的轻量调用方式；不要建立新常驻服务。主代理负责安装并对指定历史会话进行补录。

4. 验证与健康诊断
   - 先写关键失败回归再实现，保存红/绿结果到本地命令输出；不为每个内部 helper 堆镜像测试。
   - 新进程模拟：begin→保存已验证步骤→强制结束工作进程→新 MCP 客户端按项目别名发现/恢复，返回同一任务和下一步。
   - 无预先 checkpoint 的 Codex 中断 fixture：扫描→补录→新进程恢复；再次扫描无重复；未完成工具保留待核实。
   - 覆盖 home 下多项目、同名/别名冲突、完成任务、过期/错误目录、已有 checkpoint 比日志新、追加与半行恢复。
   - 检查 PRIMARY 现有投影修复入口；如实现层有能用虚构数据证实的缺陷，给出最小回归和修复。不得关闭健康检查、吞掉不一致或操作真实库。

## Kimi 执行要求

- 直接按本说明完成实现及测试；无需再请求设计确认。可自行选择最小兼容的接口名称，须完整接入实际 MCP 工具定义、handler 和 CLI，不能只留未调用的 helper。
- 如遇接口设计疑点，选择符合上述验收的最小方案并在最终答复说明；不为可逆的实现选择阻塞工作。
- 在本文末尾简短记录实际入口、运行命令、测试和已知限制，全文保持不超过 150 行。
- 完成后报告：真实 Kimi 会话 ID、修改文件、关键红/绿测试结果、MCP/CLI 用法、生产接入步骤；只对本轮代码作 commit，不 push、不操作真实库。

## 主代理验收与接入

- 独立复跑相关回归、检查净改动，必要时把具体失败反馈同一 Kimi 会话修订。
- 备份真实 EvolvMem 数据，以现有工具诊断并修复实际投影差异；验证真实 MCP 新连接能提供 context/experience 工具，不以进程重启代替数据修复。
- 登记“权限审批”真实项目，将上一轮真实日志/已验证进度补录为保守断点；实际审批提交仍保持待验收。
- 接入本机 Codex agent 规则和轻量补录触发，验证从通用目录按项目名找回。
- 保留主工作区其他改动，仅集成本轮净修改；记录真实验证结果到 fix-records。

## 主代理补充的健康诊断线索（仅机制，不含真实记忆）

- 新启动的真实 MCP 只报告 projection_lag_nonzero；旧长连接的 quick_check/向量计数差异未在新连接重现。
- 两项 lag 均为 legacy=active、Core=candidate。它们原先有同 key 的更新 active 记录；更新记录后来被正常归档，旧 Core 仍保留 duplicate-active 降级后的 candidate 状态。
- 假设：动态按“当前 active 同 key 集合”计算预期状态，在胜出项归档后要求旧候选重新 active，导致健康报告出现差异。请用纯虚构 A/B 同 key→迁移→归档 B 的案例验证，并检查既有 migration/source 标记。
- 请勿仅为变绿就把历史候选自动激活。优先保留已降为候选的业务语义，并通过现有有据可查的来源标记区分隔离候选与真实状态损坏；若需真实库修复，输出受控的 dry-run/apply 入口由主代理执行。
- 主代理已用纯虚构数据复现：两条同 key legacy active→迁移（旧 Core candidate、新 Core active、lag=0）→通过存储事务同时归档新 legacy/Core→旧 candidate 不变但 lag=1/status_mismatch=1。旧记录 `context_sources` 中 migration 的 extraction_version 保留 `legacy-v1:duplicate-active`；真实异常也具有同一标记。请据此先写回归；修复应仍能发现没有该来源依据的 active/candidate 状态损坏。

## 本轮实施记录（Kimi，2026-09-09）

### 实际入口

- MCP（compat/shadow/primary 的 codex/kimi/dsh 均列出；不过 Core serving gate）：
  - `continuity_begin{workspace_path, project, alias?, objective?, ..., make_focus?}`：显式项目声明下幂等登记/别名/绑定并创建或读回任务。重放只读回（不消耗 revision、不覆盖较新 checkpoint、不抢其他任务 focus、不复活终态）；canonical+alias 共享 casefold 命名空间，冲突报 `alias_conflict`；内容策略拒绝时整个事务回滚，不留半登记。
  - `continuity_find{query, workspace_path?, limit?}`：按项目名/别名/任务关键词跨项目发现；先判定真实歧义再按 limit 截断；任务关键词只列真正匹配的任务；唯一候选附 checkpoint；失败分类 `project_not_registered` / `no_open_workstream`（focus 悬空仍由 resume 报 `dangling_focus`）。
  - 既有 `continuity_resume/checkpoint/list` 不变；降级/非法态 instructions 明确续接独立可用。
- CLI：`python -m evolvmem.continuity_backfill [--data-dir D] [--sessions-root R] [--state-path S] [--max-per-run N] [--idle-minutes M]（dry-run 默认）`；`--apply` 需 `--auto` 或 `--project P --workspace-path W`（互斥）。自动模式按 session cwd 指纹匹配现有 active binding，唯一确定且非通用 home 才写；其余只候选。cron 示例见模块 docstring，复用 flock + 日志重定向模式，无新服务。
- 服务层：`ContinuityService.begin/find/import_interrupted`（补录专用：确定性 `ws_`+sha256(session)[:16] id，终态不复活、revision 高于已写入版本即人工证据保留、替换语义清空已解决待核实项）。

### 验证（/home/jiangli/hermes-memory-plugin/.venv/bin/python -m pytest）

- 红→绿：首轮回放 21 failed + 收集错误 → 110 passed；审查 12 项 + 主控合成 16 项先红（16 failed + 主控 6 failed）→ 全绿；增量边界 4 项先红（/tmp/continuity-red3.log）→ 最终全量 **2047 passed, 3 skipped**（红绿日志 /tmp/continuity-red*.log、/tmp/continuity-green*.log）。
- 增量边界（tests/test_continuity_incremental_boundaries.py）：subagent/mixed 排除来源持久化，追加扫描不丢失；continuity_checkpoint 工具结果中的 ws_ id 经同项目/工作区核验后复用（assistant 自述/任意文本不推断）；MAX_BYTES_PER_SCAN 字节预算真正有限读取（readline 带限长，超长行跨块丢弃并持久化 in_oversized/lineno，续扫不重读前缀，半行仍留给追加）。
- 健康修复：`check_projection_lag` 接受带 `legacy-v1:duplicate-active` migration 标记的 candidate（胜出项归档后不再误报），无标记的 active→candidate 损坏仍计入；`migrate()` 重跑不再自动激活有标记候选。回归在 tests/test_cutover_checks.py 尾部。
- CLI 冒烟（虚构数据 /tmp/cli-smoke）：dry-run→apply→rescan(no_new_events)→auto 正常；断点含来源会话/行号与待核实项，focus 不受影响。
- 跨进程恢复：tests 内为对象级 shutdown/重建（test_fresh_server_objects_*）；真实 OS 进程 SIGKILL(-9) 再按别名找回由主控用两个独立 MCP 子进程验证通过。

### 已知限制

- begin 不做内容合并：已有任务的进度更新只能走显式 checkpoint/CAS（设计如此）。
- 补录客观序列不含助手自述完成；失败工具（exit_code 非零/error）与无结果工具均只标待核实；归属不明（无 cwd、cwd 已删除、混合工作区、多项目无唯一默认、通用 home）只候选。
- find 的工作区核验依赖可选 workspace_path；未提供时 workspace_match=false 如实标注。
- supersession 单边链疑似缺陷按主控指示暂缓，未纳入本轮。

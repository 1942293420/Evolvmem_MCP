/* EvolvMem Web Console v2 — Reactive State, 7-Tab Navigation & Scrubbed Stage 1-7 Data */
(function (root) {
  const E = (root.EvolvMemV2 = root.EvolvMemV2 || {});

  E.NAV_TABS = [
    { id: "overview", label: "态势总览", plan: false, icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="4" y="4" width="6" height="6" rx="1.2"/><rect x="14" y="4" width="6" height="6" rx="1.2"/><rect x="4" y="14" width="6" height="6" rx="1.2"/><rect x="14" y="14" width="6" height="6" rx="1.2"/></svg>' },
    { id: "review-inbox", label: "审阅箱", plan: false, icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>' },
    { id: "experiences", label: "经验库", plan: false, icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>' },
    { id: "history", label: "历史会话", plan: false, icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>' },
    { id: "evolution", label: "进化工坊", plan: true, icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12 3v4m0 10v4M5.6 5.6l2.8 2.8m7.2 7.2l2.8 2.8M3 12h4m10 0h4M5.6 18.4l2.8-2.8m7.2-7.2l2.8-2.8"/><circle cx="12" cy="12" r="3"/></svg>' },
    { id: "skills", label: "Skill 库", plan: true, icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12 2l8 4.5v9L12 20l-8-4.5v-9L12 2z"/><path d="M12 11l8-4.5M12 11v9M12 11L4 6.5"/></svg>' },
    { id: "control", label: "权限与底座", plan: false, icon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>' }
  ];

  E.MODULES = [
    { id: "overview", label: "M1 全景态势与拓扑", tab: "overview", stage: "Stage 1–7", status: "live", desc: "多智能体神经同步总线、系统总框架与实时动态" },
    { id: "review-inbox", label: "M2 L4 双轨审阅箱", tab: "review-inbox", stage: "Stage 2/3/BACK", status: "live", desc: "新会话与旧聊天独立队列、影子盲审与人工裁决" },
    { id: "experiences", label: "M3 个人经验库 v2", tab: "experiences", sub: "list", stage: "Stage 2/4", status: "live", desc: "三分类经验浏览、手工快速录入与按模型回滚" },
    { id: "pipelines", label: "M4 提取流水线与清洗", tab: "experiences", sub: "pipelines", stage: "Stage 3/4/BACK", status: "thin", desc: "双轨预算队列、五阶漏斗、LLM 计量账本与冷备档案" },
    { id: "history", label: "M5 PG 历史与捕获", tab: "history", stage: "Stage 5", status: "live", desc: "多端会话检索、原文对话阅读与跨端续接租约" },
    { id: "evolution", label: "M6 进化与按类晋升", tab: "evolution", sub: "metrics", stage: "Stage 6", status: "plan", desc: "双轨 5 指标对比、按类渐进自动化与休眠调优" },
    { id: "delivery", label: "M7 习惯·手册·避坑", tab: "evolution", sub: "handbook", stage: "Stage 6", status: "plan", desc: "个人习惯候选、<=1.5k tok 常驻手册发布与即时报错避坑" },
    { id: "skills", label: "M8 Skill 结晶宫格", tab: "skills", stage: "Stage 2/7", status: "plan", desc: "宫格总览、独立二级详情、合规分发与反事实评测" },
    { id: "control", label: "M9 权限·信誉·PG 底座", tab: "control", stage: "Stage 1/5", status: "thin", desc: "成员跨库授权、Agent 四级信誉与 PG 灾备底座" }
  ];

  E.REJECT_REASONS = [
    ["common_knowledge", "属于常识"],
    ["task_log", "过程任务日志"],
    ["one_off_bug", "一次性偶发 bug"],
    ["weak_evidence", "依据证据不足"],
    ["duplicate", "与现有经验重复"],
    ["wrong", "内容事实有误"],
    ["wrong_scope", "适用范围不准确"],
    ["other", "其他原因"]
  ];

  E.KIND_ZH = { pitfall: "避坑提示", procedure: "标准做法", decision: "架构决策", env_fact: "环境事实", preference: "工程偏好", habit: "工程习惯" };
  E.ORIGIN_ZH = { manual: "手工录入", legacy: "Stage 4 精华迁入", pipeline: "新会话提炼", backfill: "旧聊天回填", agent: "Agent 提议" };
  E.STATUS_ZH = { active: "生效中", probation: "试用观察", retired: "已退役", contested: "存在争议", challenger: "试用观察", dormant: "已退役", trash: "已撤销" };
  E.LAYER_META = [
    { key: "all", label: "全部拦截", desc: "全链路未入库或挂起记录" },
    { key: "Moments", label: "Moments 初筛", desc: "0-LLM 片段过滤" },
    { key: "Extract", label: "Extract 跳过", desc: "提炼器判定无泛化经验" },
    { key: "Quota", label: "Quota 信誉限流", desc: "模型会话配额或降级拦截" },
    { key: "L0", label: "L0 规则守卫", desc: "禁词、字数或证据指针拦截" },
    { key: "L1", label: "L1 语义去重", desc: "pgvector 相似 >=0.88 合并" },
    { key: "L2a", label: "L2a Flash 盲测", desc: "快模型未看经验即答对" },
    { key: "L2b", label: "L2b 强模复核", desc: "强模型 blind test 判定无效" },
    { key: "L3", label: "L3 调和试用", desc: "重复合并或 Challenger 试用" },
    { key: "L4", label: "L4 人工驳回", desc: "审阅箱人工拒绝可救回" },
    { key: "Stage4", label: "Stage4 清洗", desc: "旧库清洗淘汰与冷备归档" }
  ];
  E.HIST_DB_ZH = {
    evolvmem_dev_linux_task_ai_ece15068ea_6a0b050a: "Linux 主力研发设备库",
    evolvmem_dev_win_01c2ee8f_41d1_479c_b_8acef270: "Windows 桌面工作库",
    evolvmem_history_migration: "未归属历史迁移库"
  };

  function initialData() {
    return {
      route: "overview",
      theme: "light",
      libraryCtx: "owner_main",
      apiMode: "sandbox",
      csrfToken: "",
      ownerId: "owner_main",
      stageFilter: "all",
      sessionRemainingSec: 26840,
      simMode: "normal",
      selectedAgentName: "codex-linux-cli",
      selectedTopoId: "node-review",
      inboxTrack: "live",
      shadowMode: true,
      trackAgreement: {
        live: { reviewed_total: 16, evaluable_total: 16, agreed_total: 14, agreement_rate: 0.875 },
        backfill: { reviewed_total: 10, evaluable_total: 10, agreed_total: 8, agreement_rate: 0.8 }
      },
      selectedProposalUid: "prop-live-101",
      selectedProposalUids: [],
      editingUid: null,
      rejectBoxOpen: false,
      expSubtab: "list",
      expFilters: { category: "all", origin: "all", status: "active", q: "" },
      selectedExpId: "manual-7c12a809f1b2401190c2",
      intFilters: { layer: "all", track: "all", category: "all", q: "" },
      selectedIntId: "prop-int-l2b-01",
      interceptionFunnel: {
        layers: { Moments: 1, Extract: 1, Quota: 1, L0: 1, L1: 1, L2a: 1, L2b: 1, L3: 1, L4: 1, Stage4: 9134 },
        total_intercepted: 9,
        rescued_total: 2
      },
      interceptions: [
        {
          id: "prop-int-l2b-01", source_table: "experience_proposals", proposal_uid: "prop-int-l2b-01",
          layer: "L2b", sub_status: "gate_rejected", reason_code: "blind_solved_correctly",
          detail: "强模型在遮蔽候选经验后仍直接解出正确排查方案，判定通用模型已内化该常识，不额外占用经验注入预算。",
          title: "排查 Docker 容器 DNS 无法解析宿主机网关时需检查 daemon.json",
          category: "系统环境经验", kind: "pitfall", track: "live", session_id: "sess-live-408",
          agent_client: "codex", model: "deepseek-v4-flash",
          problem: "排查 Docker 容器内 DNS 解析失败时如何定位宿主机网关配置问题？",
          solution: "检查 /etc/docker/daemon.json 中 dns 与 bip 网段配置并重启 dockerd。",
          reusable_experience: "修改 Docker daemon.json 后需重载守护进程使容器内 resolv.conf 生效。",
          misbelief: "误以为重启单个容器即可刷新全局守护进程网桥 DNS。",
          scope: { os: "Linux", system: "Docker 网络" },
          evidence: [{ ref: "archive:23880", event: 31, snippet: "容器内部 ping 宿主机域名报 Temporary failure in name resolution -> 修改 daemon.json dns 后解决。" }],
          rescuable: true, rescued_note_id: null, tokens_used: 412, created_at: "今天 14:10",
          diagnostic: {
            review: null
          },
          gate_chain: [
            { decision_id: "dec-l0-01", layer: "L0", verdict: "pass", reason_code: "l0_passed", confidence: 1.0, judge_model: "deterministic", detail: "字段长度与证据校验通过", meta: { field_lengths: { problem: 34, solution: 42, reusable_experience: 44 }, limits: { problem: 120, solution: 240, reusable_experience: 120 }, require_evidence: true, evidence_count: 1 } },
            { decision_id: "dec-l1-01", layer: "L1", verdict: "pass", reason_code: "novel_candidate", confidence: 0.72, judge_model: "bge-base-zh-768d", detail: "与最近已有经验余弦相似度 0.61 < 0.88", meta: { cosine_similarity: 0.61, threshold: 0.88, nearest_note_id: "manual-7c12a809f1b2401190c2" } },
            { decision_id: "dec-l2a-01", layer: "L2a", verdict: "pass", reason_code: "escalate_uncertain", confidence: 0.54, judge_model: "deepseek-v4-flash", detail: "快模型盲测仅覆盖一半细节，升级 L2b 强模复核", meta: { blind_prompt: "某 Linux 容器内无法解析域名，已知需查网桥与守护进程，请给出最常见定位文件与恢复动作。", model_prediction: "检查 /etc/resolv.conf 与 docker network inspect。", flash_prediction: "检查 /etc/resolv.conf 与 docker network inspect。" } },
            { decision_id: "dec-l2b-01", layer: "L2b", verdict: "reject", reason_code: "blind_solved_correctly", confidence: 0.88, judge_model: "deepseek-chat", detail: "强模型未看经验即完整命中 daemon.json 与 dockerd 重载步骤", meta: { blind_prompt: "某 Linux 容器内无法解析域名，已知需查网桥与守护进程，请给出最常见定位文件与恢复动作。", flash_prediction: "检查 /etc/resolv.conf 与 docker network inspect。", model_prediction: "优先排查 /etc/docker/daemon.json 的 dns 字段与网桥掩码配置，修正后执行 systemctl reload/restart dockerd 使容器重新挂载 DNS。", tokens_used: 245 } }
          ],
          llm_calls: [{ id: 9501, track: "live", stage: "L2a_flash", model: "deepseek-v4-flash", input_tokens: 130, output_tokens: 37, cost_estimate: 0.00007, outcome: "escalate" }, { id: 9502, track: "live", stage: "L2b_strong", model: "deepseek-chat", input_tokens: 165, output_tokens: 80, cost_estimate: 0.00031, outcome: "reject" }]
        },
        {
          id: "prop-int-l2a-02", source_table: "experience_proposals", proposal_uid: "prop-int-l2a-02",
          layer: "L2a", sub_status: "gate_rejected", reason_code: "flash_trivial_pass",
          detail: "Flash 快模型盲测得分 >=0.85，说明该步骤属于开箱即用的基础 CLI 用法，L2a 快速挡回零强模开销。",
          title: "Git 查看某个分支最近 5 条单行提交记录的命令参数",
          category: "系统环境经验", kind: "procedure", track: "live", session_id: "sess-live-409",
          agent_client: "codex", model: "gpt-5.4",
          problem: "快速确认远端分支最新 5 条精简提交日志应使用哪个 git log 参数组合？",
          solution: "执行 git log -n 5 --oneline。",
          reusable_experience: "查看短提交摘要优先使用 --oneline 配合 -n 限定条数。",
          misbelief: "", scope: { tool: "git" },
          evidence: [{ ref: "archive:23884", event: 12, snippet: "用户要求确认最近 5 次提交哈希 -> git log -n 5 --oneline。" }],
          rescuable: true, rescued_note_id: null, tokens_used: 128, created_at: "今天 13:55",
          diagnostic: {}, gate_chain: [
            { decision_id: "dec-l0-02", layer: "L0", verdict: "pass", reason_code: "l0_passed", confidence: 1.0, judge_model: "deterministic", detail: "字段与证据完整", meta: { field_lengths: { problem: 37, solution: 23, reusable_experience: 28 }, limits: { problem: 120, solution: 240, reusable_experience: 120 }, require_evidence: true, evidence_count: 1 } },
            { decision_id: "dec-l1-02", layer: "L1", verdict: "pass", reason_code: "novel_candidate", confidence: 0.85, judge_model: "bge-base-zh-768d", detail: "库中无重复条目", meta: { cosine_similarity: 0.42, threshold: 0.88 } },
            { decision_id: "dec-l2a-02", layer: "L2a", verdict: "reject", reason_code: "flash_trivial_pass", confidence: 0.95, judge_model: "deepseek-v4-flash", detail: "快模型 1 轮盲猜即命中完全相同参数组合", meta: { blind_prompt: "如何精简列出分支最近 5 条 git 提交记录？", model_prediction: "使用 git log -n 5 --oneline 即可只输出短哈希与提交首行。", tokens_used: 128 } }
          ],
          llm_calls: [{ id: 9498, track: "live", stage: "L2a_flash", model: "deepseek-v4-flash", input_tokens: 92, output_tokens: 36, cost_estimate: 0.00005, outcome: "reject" }]
        },
        {
          id: "prop-int-l1-03", source_table: "experience_proposals", proposal_uid: "prop-int-l1-03",
          layer: "L1", sub_status: "merged", reason_code: "l1_semantic_duplicate",
          detail: "与已有生效经验 manual-7c12a809f1b2401190c2 向量余弦相似度达 0.924 (>= 0.88)，L1 自动合并证据次数不再重复新建卡片。",
          title: "PostgreSQL 主库连接超限时服务端不得自动退回本地旧 SQLite",
          category: "系统环境经验", kind: "decision", track: "live", session_id: "sess-live-406",
          agent_client: "claude", model: "claude-sonnet-4",
          problem: "主库连接池打满报 503 时是否应该暂时切回本地单机 sqlite 保底？",
          solution: "禁止静默切回本地旧文件，直接返回 503 快速失败防止多端双写分裂。",
          reusable_experience: "主库不可用时必须 Fail-Closed 报错，不得退化写入本地旧库。",
          misbelief: "觉得先写本地再同步可以避免接口报错。", scope: { os: "Linux", service: "主数据库服务" },
          evidence: [{ ref: "archive:23879", event: 44, snippet: "验收检查连接失败场景 -> 服务端严格返回 HTTP 503 pg_control_unavailable。" }],
          rescuable: false, rescued_note_id: null, tokens_used: 0, created_at: "今天 12:20",
          diagnostic: {
            target_note: { id: "manual-7c12a809f1b2401190c2", category: "系统环境经验", problem: "主数据库短暂不可用时，服务端为什么必须拒绝服务而不是自动回退本地旧文件？", solution: "服务端统一遵循快速失败阻断原则：主库连接耗尽直接返回 503，绝不静默切回已封存的本地旧库文件，从根本上杜绝多端双写脑裂。", occurrence_count: 5 }
          },
          gate_chain: [
            { decision_id: "dec-l0-03", layer: "L0", verdict: "pass", reason_code: "l0_passed", confidence: 1.0, judge_model: "deterministic", detail: "字段合规", meta: { field_lengths: { problem: 33, solution: 34, reusable_experience: 31 }, limits: { problem: 120, solution: 240, reusable_experience: 120 } } },
            { decision_id: "dec-l1-03", layer: "L1", verdict: "merge", reason_code: "l1_semantic_duplicate", confidence: 0.924, judge_model: "bge-base-zh-768d", target_note_id: "manual-7c12a809f1b2401190c2", detail: "cosine=0.924 >= 0.88，合并证据至既有经验", meta: { cosine_similarity: 0.924, threshold: 0.88, nearest_note_id: "manual-7c12a809f1b2401190c2" } }
          ],
          llm_calls: []
        },
        {
          id: "prop-int-l0-04", source_table: "experience_proposals", proposal_uid: "prop-int-l0-04",
          layer: "L0", sub_status: "gate_rejected", reason_code: "banned_task_log_phrase",
          detail: "命中 L0 流水账禁词且缺乏可回查原文证据指针，0-LLM 直接在门禁最前层挡掉，未进入任何模型评测。",
          title: "已完成前端弹窗样式调整并测试通过",
          category: "前端经验", kind: "pitfall", track: "live", session_id: "sess-live-403",
          agent_client: "kimi", model: "unknown-experimental",
          problem: "已完成前端弹窗样式调整并测试通过",
          solution: "按照要求更新 CSS 并刷新页面验证正常。",
          reusable_experience: "改动 CSS 后记得刷新页面检查效果。",
          misbelief: "", scope: { module: "CSS" }, evidence: [],
          rescuable: true, rescued_note_id: null, tokens_used: 0, created_at: "今天 11:40",
          diagnostic: {}, gate_chain: [
            { decision_id: "dec-l0-04", layer: "L0", verdict: "reject", reason_code: "banned_task_log_phrase", confidence: 1.0, judge_model: "deterministic", detail: "触发禁词「已完成」且 evidence 数组为空", meta: { field_lengths: { problem: 17, solution: 23, reusable_experience: 20 }, limits: { problem: 120, solution: 240, reusable_experience: 120 }, require_evidence: true, evidence_count: 0 } }
          ],
          llm_calls: []
        },
        {
          id: "int-ext-05", source_table: "pipeline_interceptions", interception_id: "int-ext-05",
          layer: "Extract", sub_status: "dropped", reason_code: "tool_fumble",
          detail: "自动提炼器判定该片段仅为本地误拼路径导致的短暂摸索试错（tool_fumble），未形成跨任务复用的方法论。",
          title: "Windows 测试脚本误用反斜杠路径引发 FileNotFoundError 后自行改回正斜杠",
          category: "系统环境经验", kind: "pitfall", track: "backfill", session_id: "sess-bf-1088",
          agent_client: "pipeline", model: "deepseek-v4-flash",
          problem: "Windows 下脚本传参未规范引号导致临时读盘报错后立刻手动修正",
          solution: "改为 pathlib.Path 传入正斜杠路径后通过。",
          reusable_experience: "跨平台 Python 脚本统一使用 pathlib 处理磁盘路径。",
          misbelief: "", scope: { os: "Windows" },
          evidence: [{ ref: "archive:12140", event: 8, snippet: "初次传参拼错文件名，第二次立刻改正路径执行通过。" }],
          rescuable: true, rescued_note_id: null, tokens_used: 320, created_at: "今天 04:15",
          diagnostic: { moment_id: "mom-1088-02", moment_kind: "retry", moment_summary: "连续 2 次执行文件读取命令因路径拼写修正后通过" },
          gate_chain: [{ decision_id: "int-ext-05", layer: "Extract", verdict: "reject", reason_code: "tool_fumble", judge_model: "deepseek-v4-flash", detail: "提炼器判定为一次性笔误重试 skip:tool_fumble", meta: { moment_id: "mom-1088-02", moment_kind: "retry" } }],
          llm_calls: []
        },
        {
          id: "int-quota-06", source_table: "pipeline_interceptions", interception_id: "int-quota-06",
          layer: "Quota", sub_status: "dropped", reason_code: "session_quota_exceeded",
          detail: "模型 unknown-experimental 信誉等级为 general，本会话提议上限为 2 条且已用尽，第 3 条候选被配额护栏拦截（人工可核验救回）。",
          title: "WSL2 与 Windows 宿主间转发 localhost 端口需监听 0.0.0.0",
          category: "系统环境经验", kind: "env_fact", track: "live", session_id: "sess-live-401",
          agent_client: "kimi", model: "unknown-experimental",
          problem: "Windows 浏览器无法访问 WSL2 内部仅绑定 127.0.0.1 的本地服务",
          solution: "开发环境临时联调需绑定目标网卡或通过局域网代理显式转发。",
          reusable_experience: "跨子系统联调时先核对 ss -tlnp 监听地址是否局限在环回接口。",
          misbelief: "以为 WSL2 内部服务默认和 Windows 共用 127.0.0.1 栈。", scope: { os: "Windows/WSL2" },
          evidence: [{ ref: "archive:23840", event: 55, snippet: "ss -tlnp 显示仅绑定 ::1:9388，补充 --bind 0.0.0.0 后连通。" }],
          rescuable: true, rescued_note_id: null, tokens_used: 0, created_at: "今天 09:30",
          diagnostic: { moment_id: "mom-401-03", moment_kind: "tool_error", moment_summary: "WSL2 端口监听排障片段命中第 3 次配额上限" },
          gate_chain: [{ decision_id: "int-quota-06", layer: "Quota", verdict: "reject", reason_code: "session_quota_exceeded", judge_model: "reputation_policy", detail: "session_quota_exceeded (limit=2 for general tier)", meta: { moment_id: "mom-401-03", effective_quota: 2 } }],
          llm_calls: []
        },
        {
          id: "trial-301", source_table: "challenger_trials", proposal_uid: "prop-chal-301",
          note_id: "exp-01j9probation09", layer: "L3", sub_status: "probation_hold", reason_code: "refinement",
          detail: "L3 判定新经验对既有方案构成细化补充（refinement），已写入 PG 但置于 probation 试用观察期（需连续命中成功 2 次才转正为 active）。",
          title: "静态 HTML 离线副本禁止 ES Module 导入并同步提供双兼容全局挂载",
          category: "前端经验", kind: "pitfall", track: "live", session_id: "sess-live-410",
          agent_client: "codex", model: "gpt-5.4",
          problem: "给用户交付可直接在本地双击打开的离线网页副本时，采用模块化脚本语法会被浏览器拦截",
          solution: "纯离线原型采用经典全局命名空间挂载并同步打包单文件内联 HTML 副本，兼容 file:// 与 HTTP 双模式。",
          reusable_experience: "需要兼顾离线双击预览的控制台副本禁止依赖跨文件 module 导入。",
          misbelief: "以为相对路径 ESM 在双击本地 HTML 时也可加载。", scope: { os: "跨平台浏览器", mode: "离线 + HTTP 双兼容" },
          evidence: [{ ref: "archive:23899", event: 24, snippet: "Chrome DevTools on file:// 报 CORS policy blocked。" }],
          rescuable: false, rescued_note_id: null, tokens_used: 290, created_at: "今天 14:15",
          diagnostic: {
            trial: { trial_id: 301, relation: "refinement", status: "trial", challenger_note_id: "exp-01j9probation09", incumbent_note_id: "exp-01j9rollbackdemo01", required_successes: 2, success_count: 1, failure_count: 0 },
            target_note: { id: "exp-01j9rollbackdemo01", category: "前端经验", problem: "信息过载会把系统诊断细节、原始 JSON 和内部编号铺满一级工作区，导致界面晦涩难看", solution: "遵从三级渐进披露原则保持冰川白昼阅读体验。", occurrence_count: 3 }
          },
          gate_chain: [
            { decision_id: "dec-l0-301", layer: "L0", verdict: "pass", reason_code: "l0_passed", confidence: 1.0, judge_model: "deterministic", detail: "规则校验通过", meta: {} },
            { decision_id: "dec-l1-301", layer: "L1", verdict: "pass", reason_code: "novel_candidate", confidence: 0.81, judge_model: "bge-base-zh-768d", detail: "cosine=0.71 < 0.88", meta: { cosine_similarity: 0.71, threshold: 0.88 } },
            { decision_id: "dec-l3-301", layer: "L3", verdict: "pass", reason_code: "refinement", confidence: 0.91, judge_model: "deepseek-chat", detail: "创建 Challenger Probation Trial #301 (1/2 成功)", meta: { relation: "refinement", trial_id: 301, needs_probation: true } }
          ],
          llm_calls: []
        },
        {
          id: "prop-int-l4-08", source_table: "experience_proposals", proposal_uid: "prop-int-l4-08",
          layer: "L4", sub_status: "human_rejected", reason_code: "wrong_scope",
          detail: "L4 审阅箱中被人工以「适用范围不准确 (wrong_scope)」驳回；若补充 OS/版本限定仍可在右下方「人工救回入库」。",
          title: "调整 vm.max_map_count 系统内核参数后立即使进程生效",
          category: "系统环境经验", kind: "procedure", track: "live", session_id: "sess-live-395",
          agent_client: "codex", model: "deepseek-chat",
          problem: "启动大内存向量索引进程报 max virtual memory areas vm.max_map_count 过低",
          solution: "执行 sysctl -w vm.max_map_count=262144 并写入 /etc/sysctl.d/99-evolvmem.conf。",
          reusable_experience: "配置 sysctl 内核限制时需同时执行写入文件防重启丢失。",
          misbelief: "误以为所有平台都需要 sysctl 调整该值（未注明仅限 Linux 宿主机）。",
          scope: { system: "向量索引" },
          evidence: [{ ref: "archive:23850", event: 18, snippet: "Linux 宿主机执行 sysctl --system 后向量进程正常拉起。" }],
          rescuable: true, rescued_note_id: null, tokens_used: 190, created_at: "昨天 21:05",
          diagnostic: { review: { user_verdict: "reject", user_reason: "wrong_scope", note: "缺少 os=Linux 适用条件限制，容易误推到 Windows 会话。" } },
          gate_chain: [
            { decision_id: "dec-l3-08", layer: "L3", verdict: "pass", reason_code: "new", confidence: 0.86, judge_model: "deepseek-chat", detail: "L3 建议新增", meta: {} },
            { decision_id: "dec-l4-08", layer: "L4", verdict: "reject", reason_code: "wrong_scope", confidence: 1.0, judge_model: "human_review", detail: "人工驳回：缺少 os=Linux 适用条件限制", meta: {} }
          ],
          llm_calls: []
        },
        {
          id: "seg-mom-09", source_table: "pipeline_segment_runs",
          layer: "Moments", sub_status: "skipped_zero_moments", reason_code: "skipped_zero_moments",
          detail: "会话分段零大模型前置初筛检测到 0 个报错/回滚/用户纠偏信号，直接零花费截断跳过。",
          title: "会话 sess-live-399 零 LLM 前置截断 (0 个关键时刻)",
          category: "系统环境经验", kind: "procedure", track: "live", session_id: "sess-live-399",
          agent_client: "pipeline", model: "zero_llm_moments",
          problem: "", solution: "", reusable_experience: "", misbelief: "",
          scope: { last_event_index: 42 }, evidence: [], rescuable: false, rescued_note_id: null, tokens_used: 0, created_at: "昨天 20:12",
          diagnostic: { segment_hash: "seg-8f12c9a1", status: "skipped_zero_moments", moments_count: 0 },
          gate_chain: [], llm_calls: []
        }
      ],
      rollbackState: { model: "deepseek-chat", previewed: false, affected: [], plan: null },
      backfillScopeMode: "month",
      backfillScopeVal: "2026-10",
      r9Tripped: false,
      histDb: "evolvmem_dev_linux_task_ai_ece15068ea_6a0b050a",
      histSubtab: "browse",
      histScope: "projects",
      histQuery: "",
      selectedBlockId: "blk-23863",
      evoSubtab: "metrics",
      dormantSlider: 0.45,
      handbookTarget: "AGENTS.md",
      handbookPublishedCount: 0,
      errorInput: "error: failed to run custom command: os error 206 filename or extension too long",
      skillFilter: "all",
      selectedSkillId: null,
      ctrlSubtab: "users",
      topoNodes: [
        { id: "node-clients", title: "多端接入与身份防冒充", stage: "Stage 1", status: "live", target: "control", sub: "reputation", summary: "Codex Linux/Win · Claude · Kimi · DSH 五端协同一体", detail: "5 端 Hook 传参识别模型身份，MCP 层递归剥离 _meta 提权字段。", metrics: "5 端在线 · DSH 74/74 · Codex 2/2" },
        { id: "node-control", title: "成员权限与模型信誉库", stage: "Stage 1 & 5", status: "thin", target: "control", sub: "users", summary: "飞书 8h 会话、定向跨库读授权与未知模型限流", detail: "evolvmem_control 共 31 张表并启用 FORCE RLS，停用账户同事务吊销全量会话。", metrics: "31 张表 · 强制 RLS · 未知模型限额 2/会话" },
        { id: "node-history", title: "PG 历史会话与多端捕获", stage: "Stage 5", status: "live", target: "history", sub: "browse", summary: "全文检索 + 语义混搜，原始对话只读零删改", detail: "7/7 原 SQLite 库 0400 只读退役，Web/MCP 由 PG17 承接 90 项目无损检索与附件回查。", metrics: "360/360 表零差异 · 0 SQLite 运行句柄" },
        { id: "node-pipeline", title: "双轨 0-token 提取与计量", stage: "Stage 3 / BACK", status: "thin", target: "experiences", sub: "pipelines", summary: "新会话与夜间旧聊天隔离调度，先筛关键时刻省成本", detail: "70%–80% 无关键时刻会话 0 LLM 跳过；metered_llm() 全仓强制入账并支持 R9 自动熔断。", metrics: "live 200万/日 · backfill 300万/晚 · 约 10k tok/条" },
        { id: "node-gate", title: "四层质量守卫 (L0–L3)", stage: "Stage 2 & 3", status: "thin", target: "review-inbox", summary: "硬规则过滤日志 -> 语义去重 -> 盲测 -> 关系对账", detail: "自动拦截任务日志禁词与无效证据引用，768 维余弦 >=0.88 自动合并相似卡片。", metrics: "40 条样本日志 100% 拦截 · v4 精准聚焦" },
        { id: "node-review", title: "L4 双轨人工审阅箱", stage: "Stage 2/3/BACK", status: "live", target: "review-inbox", summary: "新会话与旧聊天分栏排队，影子盲审校正一致率", detail: "裁决前隐藏系统闸门判断以验证真实人机一致率，通过后签发一次性 HMAC 入库票据。", metrics: "live 3 待审 / backfill 2 待审 · 一致率 87.5%" },
        { id: "node-exp", title: "个人经验库 v2 与回滚", stage: "Stage 2 & 4", status: "live", target: "experiences", sub: "list", summary: "三分类极简卡片、手工快速直通与按模型安全回滚", detail: "受单一原子写入口保护，容纳 39 条手工精品 + 707 条 Stage 4 迁入旧精华 + 增量经验。", metrics: "746 活跃基线 · 支持干跑 Dry-Run 两步回滚" },
        { id: "node-evo", title: "进化闭环与即时交付", stage: "Stage 6", status: "plan", target: "evolution", sub: "metrics", summary: "按类自动晋升、<=1.5k 常驻手册与报错即时避坑", detail: "0-token 关联测试结果演化 Beta 置信度，低效转 dormant 休眠不删，常驻手册超预算拦截。", metrics: "规划预置 · 推进至少 1 类达半自动" },
        { id: "node-skills", title: "Skill 自动结晶与评测", stage: "Stage 7", status: "plan", target: "skills", summary: "聚类经验+坑点生草稿，反事实历史回放评测上架", detail: "Linux 直写 ~/.claude/skills，Windows 坚持通过 MCP 按需拉取，绝不自动反向覆盖。", metrics: "规划预置 · 已正式发布 1 / 目标 3" }
      ],
      agents: [
        { agent: "codex-linux-cli", title: "Codex Linux 主力", os: "Linux 宿主", client: "codex", model: "gpt-5.4", tier: "primary", quota: 12, status: "online", activeProject: "evolvmem-pg-prod", lastArchiveId: 23863, lastSyncAt: "2 分钟前", syncNote: "本地核心工具全通，PG 主库直连健康" },
        { agent: "codex-win-01", title: "Codex Windows 终端", os: "Windows 11", client: "codex", model: "gpt-5.4", tier: "primary", quota: 12, status: "online", activeProject: "win-desktop-sync", lastArchiveId: 23891, lastSyncAt: "5 分钟前", syncNote: "双条归档队列零积压，本地 948 个 Skill 基线一致" },
        { agent: "dsh-win-01", title: "DSH 桌面伴侣", os: "Windows 11", client: "dsh", model: "deepseek-chat", tier: "general", quota: 4, status: "online", activeProject: "evolvmem-web-refactor", lastArchiveId: 23885, lastSyncAt: "9 分钟前", syncNote: "连续对话捕获确认 74/74 全绿" },
        { agent: "claude-linux-hook", title: "Claude Code Linux", os: "Linux 宿主", client: "claude", model: "claude-sonnet-4", tier: "primary", quota: 10, status: "online", activeProject: "evolvmem-pg-prod", lastArchiveId: 23879, lastSyncAt: "14 分钟前", syncNote: "会话开场记忆注入与 CAS 校验正常" },
        { agent: "kimi-cli-guest", title: "Kimi CLI (观察降级)", os: "Linux 宿主", client: "kimi", model: "unknown-experimental", tier: "general", quota: 2, status: "degraded", activeProject: "ui-prototype", lastArchiveId: 23840, lastSyncAt: "32 分钟前", syncNote: "未识别实验模型，自动限制单次提议配额为 2" }
      ],
      changesCursor: 148,
      changesStream: [
        { seq: 148, actor: "owner_main", model: "deepseek-chat", noteId: "exp-01j9stage4legacy707", action: "迁入 Stage 4 清洗保留精华经验", ts: "07:40" },
        { seq: 147, actor: "owner_main", model: "manual_web", noteId: "manual-a91c8e21b40f", action: "人工快速新增系统灾备经验", ts: "07:12" },
        { seq: 146, actor: "codex-win-01", model: "gpt-5.4", noteId: "exp-01j9pipeline001", action: "Windows 增量提取进入 L4 待审", ts: "06:48" }
      ],
      proposals: [
        {
          uid: "prop-live-101", track: "live", model: "deepseek-chat", agent: "codex-linux-cli", category: "系统环境经验", kind: "pitfall",
          problem: "开启强制行级权限隔离（FORCE RLS）的逻辑库恢复到新实例时，因缺少自定义角色报错",
          solution: "备份脚本先使用无密码角色导出参数生成角色脚本并剔除内置超级用户语句，恢复业务库前优先执行角色快照。",
          reusable: "凡是带有行级策略 TO 角色的 PostgreSQL 逻辑库做隔离恢复演练时，必须同步配套无密码集群角色快照。",
          misbelief: "误以为单独导出业务库转储文件并预建普通应用账号即可完整还原表策略。",
          scope: { os: "Linux", system: "PG 17.10 灾备" },
          evidence: { ref: "archive:23863", event: 412, snippet: "[隔离演练 :55435] 初始报错缺少 evolvmem_control_owner 角色 -> 补齐 --roles-only --no-role-passwords 生成 roles.sql -> 二次演练 31/31 表全绿通过。" },
          gateVerdict: "pass", gateLayer: "L3 (全新经验入库)", confidence: 0.94, l0Warnings: [], status: "pending"
        },
        {
          uid: "prop-live-102", track: "live", model: "deepseek-v4-flash", agent: "codex-win-01", category: "业务经验", kind: "procedure",
          problem: "局域网服务返回历史项目列表时存在双层嵌套字段，导致旧版客户端解析结果为空",
          solution: "在客户端解析响应时同时兼容读取外层解包后的项目数组与内层嵌套列表两种结构。",
          reusable: "消费跨端工具接口返回的列表数据时优先做一层安全解包，避免外层包装升级引起误判空列表。",
          misbelief: "以为所有列表工具返回的顶层字段必然直接就是数组。",
          scope: { os: "Windows", client: "局域网 MCP" },
          evidence: { ref: "archive:23891", event: 88, snippet: "校验客户端兼容 val.get('projects', {}).get('projects') -> 成功读取全部 90 个项目。" },
          gateVerdict: "pass", gateLayer: "L2b (强模型核验通过)", confidence: 0.89, l0Warnings: [], status: "pending"
        },
        {
          uid: "prop-live-103", track: "live", model: "unknown-experimental", agent: "kimi-cli-guest", category: "前端经验", kind: "pitfall",
          problem: "已完成用户需求并提交了网页构建脚本",
          solution: "查看日志发现任务已经做完，直接告诉用户成功即可。",
          reusable: "每次改完前端都要告诉用户已经做完。",
          misbelief: "",
          scope: { os: "浏览器前端", system: "页面交互" },
          evidence: { ref: "archive:99999-missing", event: 1, snippet: "在历史会话库中未检索到对应的原始对话片段。" },
          gateVerdict: "reject", gateLayer: "L0 (拦截任务流水账)", confidence: 0.98, l0Warnings: ["命中过程性流水账禁词「已完成」", "所引用的原文证据在历史库不可回查"], status: "pending"
        },
        {
          uid: "prop-backfill-201", track: "backfill", model: "deepseek-chat", agent: "backfill-worker", category: "系统环境经验", kind: "pitfall",
          problem: "旧聊天回填读取归档文件时若遇到半截坏行，会导致整月任务异常中断",
          solution: "逐行解析事件流时独立捕获单行格式异常并累计坏行计数后继续跳过，将错误严格隔离在单次会话边界内。",
          reusable: "离线批量扫盘处理历史对话归档时必须做单行级异常隔离，禁止单条坏数据拖垮整批游标。",
          misbelief: "以为历史年份自动落盘的导出文件每一行都是完整有效的。",
          scope: { month: "2026-10", source: "夜间回填" },
          evidence: { ref: "archive:12140", event: 19, snippet: "100 会话夜间试跑遇到单条截断行时记录 skipped_corrupt=1 后稳定跑完 100/100。" },
          gateVerdict: "pass", gateLayer: "L3 (补充背景细节)", confidence: 0.91, l0Warnings: [], status: "pending"
        },
        {
          uid: "prop-backfill-202", track: "backfill", model: "deepseek-chat", agent: "backfill-worker", category: "业务经验", kind: "decision",
          problem: "旧系统两张表存在同主键拆存记录，若直接 1:1 迁入会在新库生成重复经验卡片",
          solution: "迁移器先按共享主键把父子关联行归并成单个迁移单元并保留溯源清单，使 709 行合法旧条目合并精简为 707 条。",
          reusable: "多表关联旧数据归口迁入新结构前应先做语义主键合表，只把源主键记入溯源数组。",
          misbelief: "误认为分开存储在两张旧表中的记录各自代表两条独立的知识经验。",
          scope: { stage: "Stage 4", cleanup_id: "stage4-legacy-clean-20261114" },
          evidence: { ref: "archive:18402", event: 74, snippet: "归并 5938 与 5743 两组父子重叠条目，生成 707 条带溯源的唯一正式经验。" },
          gateVerdict: "pass", gateLayer: "L3 (全新经验入库)", confidence: 0.87, l0Warnings: [], status: "pending"
        }
      ],
      reviewStats: { agreed: 14, total: 16 },
      experiences: [
        { id: "manual-7c12a809f1b2401190c2", category: "系统环境经验", origin: "manual", model: "manual_web", status: "active", kind: "decision", revision: 3, problem: "主数据库短暂不可用时，服务端为什么必须拒绝服务而不是自动回退本地旧文件？", solution: "服务端统一遵循快速失败阻断原则：主库连接耗尽直接返回 503，绝不静默切回已封存的本地旧库文件，从根本上杜绝多端双写脑裂。", reusable: "多智能体共享的记忆状态以主库为唯一真实源，本地降级写入只会产生无法合并的脏分叉。", misbelief: "早期直觉希望断库时还能先写进本地兜底文件保活。", scope: { os: "Linux", service: "主数据库服务" }, evidenceCount: 5, betaA: 9, betaB: 1, stability: 0.94, updatedAt: "今天 07:12" },
        { id: "exp-01j9legacy3076keep", category: "业务经验", origin: "legacy", model: "clean_migrator", status: "active", kind: "env_fact", revision: 1, problem: "清理上万条历史旧经验时，如何避免批量淘汰把边界上有用的环境记录误删？", solution: "正式执行清理前抽取固定种子的 50 条拟删样本进行人工复核，将误删率死死卡在 10% 以下，对误判条目人工救回后直接转正迁入新库。", reusable: "大规模历史数据降噪清洗前，必须经过固定抽样盲检与低误删门槛核准，且删除前导压缩冷备保护 30 天。", misbelief: "认为自动化模型过滤后就能直接安全清空所有未入选条目。", scope: { origin: "历史精华清洗", sample: "50 条人工抽检" }, evidenceCount: 2, betaA: 6, betaB: 1, stability: 0.86, updatedAt: "昨天 22:10" },
        { id: "exp-01j9win206pitfall", category: "系统环境经验", origin: "pipeline", model: "deepseek-chat", status: "active", kind: "pitfall", revision: 2, problem: "Windows 命令行拼接过长脚本或复杂多层引号时，直接触发文件名或扩展名太长报错", solution: "当执行超过数千字符或包含复杂引号转义的脚本时，改为标准输入管道传入（如 Here-String 管道）或落盘临时脚本执行，避开进程命令行长度上限。", reusable: "Windows 自动化脚本一律优先使用标准输入流传输代码正文，不再往命令行参数里硬塞多行长文本。", misbelief: "以为在 Linux 终端能正常运行的长引号参数放到 Windows 下同样适用。", scope: { os: "Windows", shell: "PowerShell" }, evidenceCount: 4, betaA: 8, betaB: 1, stability: 0.91, updatedAt: "今天 02:35" },
        { id: "exp-01j9frontendvite09", category: "前端经验", origin: "agent", model: "gpt-5.4", status: "probation", kind: "pitfall", revision: 1, problem: "给用户交付可直接在本地双击打开的离线网页副本时，采用模块化脚本语法会被浏览器拦截", solution: "纯离线双开原型不引入外网构建框架与 ESM 跨域导入，采用经典全局命名空间挂载并同步打包单文件内联 HTML 副本，兼容本地文件与 HTTP 两种打开方式。", reusable: "需要兼顾离线双击预览的 Web 控制台副本禁止依赖外网 CDN 与跨文件 module 导入。", misbelief: "以为本地目录的相对路径导出在直接双击打开网页时也能像本地服务器一样加载。", scope: { os: "跨平台浏览器", mode: "离线 + HTTP 双兼容" }, evidenceCount: 3, betaA: 5, betaB: 1, stability: 0.81, updatedAt: "今天 14:15" },
        { id: "exp-01j9dormantold04", category: "业务经验", origin: "backfill", model: "deepseek-chat", status: "retired", kind: "procedure", revision: 2, problem: "旧版单机时代切换索引文件后手动触发全量向量索引重建的定时排查步骤", solution: "切换企业级主数据库并托管内置向量索引后，事务提交即保证索引同步一致，已不再需要人工运行旧版离散向量文件修复工具。", reusable: "底层存储换代后已经过时的运维经验直接退居已退役档案，停止在日常会话中推送，保留溯源可查。", misbelief: "升级后仍按老习惯定时跑单机向量索引扫描修复脚本。", scope: { stage: "旧架构归档" }, evidenceCount: 1, betaA: 2, betaB: 6, stability: 0.28, updatedAt: "5 天前" },
        { id: "exp-01j9rollbackdemo01", category: "前端经验", origin: "pipeline", model: "deepseek-chat", status: "active", kind: "preference", revision: 1, problem: "信息过载会把系统诊断细节、原始 JSON 和内部编号铺满一级工作区，导致界面晦涩难看", solution: "遵从三级渐进披露原则：一级页面仅保留清爽导航、大字标题与核心动作；二级面板呈现完整自然语言阅读；三级侧滑抽屉收纳所有底层指标与 JSON 溯源。", reusable: "面向人工决策的研发控制台应保持冰川白昼极简阅读体验，把排障运维细节统一收进可展开的三级抽屉。", misbelief: "总想把后台数据库里的每一个字段都在首页全部直接摊出来证明功能完善。", scope: { module: "控制台 UI 规范" }, evidenceCount: 3, betaA: 6, betaB: 0, stability: 0.89, updatedAt: "今天 14:50" },
        { id: "manual-deleted-9001", category: "业务经验", origin: "manual", model: "manual_web", status: "retired", kind: "decision", revision: 4, problem: "临时记录：历史迁移验证阶段临时容器检查备忘（已撤销退役）", solution: "临时演练容器已在全量切流完成后清理完毕，无需继续保留在日常推送列表中。", reusable: "阶段性验证用的临时操作备忘验收完即标为已退役保持经验库纯度。", scope: { stage: "临时演练" }, misbelief: "", evidenceCount: 1, betaA: 1, betaB: 2, stability: 0.35, updatedAt: "昨天 23:10" }
      ],
      llmCalls: [
        { id: 9412, track: "live", stage: "L2a_flash", model: "deepseek-v4-flash", inTok: 218, outTok: 76, cost: 0.00011, outcome: "escalate_to_strong", ref: "archive:23863" },
        { id: 9411, track: "live", stage: "L2b_strong", model: "deepseek-chat", inTok: 264, outTok: 142, cost: 0.00048, outcome: "pass_to_l3", ref: "archive:23863" },
        { id: 9410, track: "live", stage: "extract", model: "deepseek-v4-flash", inTok: 1840, outTok: 310, cost: 0.00072, outcome: "proposal_created", ref: "archive:23891" },
        { id: 9388, track: "backfill", stage: "extract", model: "deepseek-v4-flash", inTok: 2110, outTok: 185, cost: 0.00069, outcome: "skip:tool_fumble", ref: "archive:12140" },
        { id: 9387, track: "backfill", stage: "L3_reconcile", model: "deepseek-chat", inTok: 580, outTok: 194, cost: 0.00081, outcome: "enrich", ref: "archive:12140" }
      ],
      historyProjects: [
        { key: "evolvmem-pg-prod", title: "EvolvMem PG 切流与运维主工程", sessions: 28, blocks: 142, deleted: false, lastAt: "今天 08:02" },
        { key: "evolvmem-web-refactor", title: "Web Console v2 全模块重构", sessions: 9, blocks: 34, deleted: false, lastAt: "今天 14:22" },
        { key: "win-desktop-sync", title: "Windows Codex & DSH 双端捕获同步", sessions: 19, blocks: 74, deleted: false, lastAt: "今天 07:45" },
        { key: "legacy-scratch-temp", title: "旧版清理前验证草稿（已移至回收站）", sessions: 2, blocks: 6, deleted: true, lastAt: "昨天 14:30" }
      ],
      historyBlocks: [
        { id: "blk-23863", project: "evolvmem-pg-prod", title: "灾备隔离演练缺少自定义角色根因定位与全绿修复", sourceRef: "archive:23863", turns: 702, bm25: 0.91, trgm: 0.78, vector: 0.94, rrf: 0.93, hasAttachment: true, attachmentBytes: 122002, transcript: ["用户：隔离演练测试库恢复时在控制库报缺少角色错误是什么原因？", "助手：因为启用了行级权限隔离策略并指定了自定义所有者角色。我已补充无密码角色快照导出步骤并剔除冲突语句。", "工具校验：全量 9 个逻辑库隔离恢复与表行数比对完全一致！"] },
        { id: "blk-23891", project: "win-desktop-sync", title: "Windows 双端增量同步通道连通与 74 份对话全量确认", sourceRef: "archive:23891", turns: 164, bm25: 0.66, trgm: 0.58, vector: 0.88, rrf: 0.79, hasAttachment: false, attachmentBytes: 0, transcript: ["工具校验：跨端请求接口返回 23 项工具就绪，开场自动注入上下文 2,559 字符。", "同步伴侣：当前队列零待处理积压，已确认 74/74 份对话历史。"] },
        { id: "blk-unassigned-1", project: null, title: "未归属会话片段：Windows 本地 Skill 基线全量比对", sourceRef: "archive:23710", turns: 42, bm25: 0.44, trgm: 0.39, vector: 0.72, rrf: 0.56, hasAttachment: false, attachmentBytes: 0, transcript: ["巡检核对：948 个本地 Skill 扩展文件与迁移前基线保持逐字节完全一致。"] }
      ],
      categoriesEvo: [
        { category: "系统环境经验", mode: "partial", liveConcordance: 0.93, backfillConcordance: 0.89, samples: 42 },
        { category: "业务经验", mode: "review", liveConcordance: 0.88, backfillConcordance: 0.84, samples: 29 },
        { category: "前端经验", mode: "review", liveConcordance: 0.76, backfillConcordance: 0.71, samples: 11 }
      ],
      impressions: [
        { id: "imp-701", noteId: "exp-01j9win206pitfall", surface: "PostToolUse", outcome: "positive", signal: "改用标准输入管道执行后退出码 0 一次通过", deltaBeta: "+1 α", ts: "10 分钟前" },
        { id: "imp-700", noteId: "manual-7c12a809f1b2401190c2", surface: "SessionStart", outcome: "positive", signal: "未发生违规降级或同类人工纠正", deltaBeta: "+1 α", ts: "42 分钟前" },
        { id: "imp-699", noteId: "exp-01j9dormantold04", surface: "SearchRecall", outcome: "negative", signal: "人工指正 PG 时代不再手工修单机索引", deltaBeta: "+1 β -> 触发休眠", ts: "昨天 21:18" }
      ],
      habits: [
        { id: "hab-01", category: "系统环境经验", reasonCode: "适用范围偏差", occurrences: 4, text: "在 Windows 下执行超长或多引号脚本时，统一使用标准输入管道（python - / powershell -）替代命令行字符串参数", status: "candidate" },
        { id: "hab-02", category: "业务经验", reasonCode: "任务流水账", occurrences: 6, text: "避免把「已完成需求开发」当作经验记录，只沉淀可复用的问题根因与通用边界法则", status: "active" }
      ],
      handbookBlocks: [
        { id: "hb-1", title: "数据库单权威与故障快速阻断原则", tokens: 340, selected: true, body: "- 所有共享经验与历史以主数据库为唯一权威源，禁止私自写回退役单机库；测试禁止直接操作生产端口。" },
        { id: "hb-2", title: "Windows 脚本无引号管道传递规范", tokens: 290, selected: true, body: "- Windows 执行多行代码严禁在命令行拼接超过上限的转义串（防止 os error 206），一律改走 stdin 管道。" },
        { id: "hb-3", title: "隔离灾备恢复前同步无密码角色快照", tokens: 380, selected: true, body: "- 恢复启用了行级权限隔离策略的逻辑库前，必须先恢复 --roles-only --no-role-passwords 导出的角色脚本。" },
        { id: "hb-4", title: "静态离线交付禁止依赖外网框架与 ESM 导入", tokens: 260, selected: false, body: "- 需要同时支持浏览器双击预览的网页控制台禁用外网 CDN 与跨文件 module 导入。" },
        { id: "hb-5", title: "大规模历史清洗 50 条随机抽审与 30 天冷备期", tokens: 410, selected: false, body: "- 高淘汰率历史记录清除前必经固定种子盲检把控误删率（<=10%），并通过压缩冷备保留 30 天容灾退路。" }
      ],
      skills: [
        { id: "skl-pg-dr", name: "pg-rls-backup-restore-drill", title: "企业数据库权限隔离备份与灾备恢复演练", category: "系统环境经验", lifecycle: "active", tokenCost: 1120, summary: "针对包含行级访问控制策略的多库备份，自动先导出无密码集群角色快照并执行隔离环境恢复与全表对账。", attachedPitfalls: ["新实例若未预建策略指定的自定义角色会直接恢复失败", "导出集群角色脚本时必须排除内置 postgres 超级用户并禁止明文密码"], linuxSynced: true, winMode: "通过 LAN MCP 按需拉取或手动安装（不自动逆向覆写 Windows）", evalReport: { sessions: 12, errorDropPct: -78, tokenSavePct: 34 }, mdPreview: "# pg-rls-backup-restore-drill\n\n## Trigger\n备份或验证含 FORCE RLS 策略的多逻辑库实例。\n## Steps\n1. 先用 pg_dumpall --roles-only --no-role-passwords 生成 roles.sql 并过滤内置 postgres 行\n2. 分库导出校验 SHA256SUMS\n3. 先恢复角色脚本再恢复业务库\n## Pitfalls\n- 缺失自定义 role 时所有带 RLS 的表将拒绝建策略" },
        { id: "skl-win-pipe", name: "windows-powershell-stdin-runner", title: "Windows 安全无引号标准输入流执行规范", category: "系统环境经验", lifecycle: "trial", tokenCost: 840, summary: "解决 Windows 创建进程时的命令行长度溢出与多层嵌套引号转义断裂问题，保证跨端运维脚本稳定落盘与执行。", attachedPitfalls: ["命令行直接拼接长脚本易触达 os error 206 长度截断", "双引号跨 SSH 远端解析极易丢失转义层次"], linuxSynced: false, winMode: "通过 LAN MCP 按需拉取或手动安装（不自动逆向覆写 Windows）", evalReport: { sessions: 8, errorDropPct: -65, tokenSavePct: 28 }, mdPreview: "# windows-powershell-stdin-runner\n\n## Trigger\n在 Windows 宿主上执行多行诊断工具或跨端脚本。\n## Rules\n统一采用 Here-String 经由 stdin 管道传输代码，不在命令行参数位展开。" },
        { id: "skl-mcp-acl", name: "lan-mcp-directional-acl-check", title: "多租户跨库定向隔离与接口安全解包校验", category: "业务经验", lifecycle: "draft", tokenCost: 960, summary: "由 4 组接口验收成功经验聚类结晶，确保他人只读授权视角严格阻断所有越权写操作及防客户端提权伪造。", attachedPitfalls: ["忽略递归剥离请求体里的 _meta 客户端伪造信息可能造成身份越权", "未兼容接口外层包装字典会导致返回数组取空"], linuxSynced: false, winMode: "通过 LAN MCP 按需拉取或手动安装（不自动逆向覆写 Windows）", evalReport: null, mdPreview: "# lan-mcp-directional-acl-check\n\n## Candidate Sources\n聚类自 4 次跨租户验收记录与 2 条高频坑点。\n## Draft Checklist\n- 验证非本人库所有写/删/恢复端严格返回 403\n- 验证项目列表响应嵌套字典兼容解包" },
        { id: "skl-usearch-old", name: "legacy-sqlite-usearch-repair", title: "旧版单机分片向量索引文件手动重建流程", category: "系统环境经验", lifecycle: "deprecated", tokenCost: 620, summary: "主数据库全面接管持久化存储与内建 HNSW 向量索引后，该面向旧版离散文件索引的手动重建技能已正式废弃停用。", attachedPitfalls: ["所有退役单机数据库文件均保持只读 0400 归档状态，严禁再运行修改脚本"], linuxSynced: false, winMode: "已标记废弃 · 不再提供安装或注入分发", evalReport: { sessions: 4, errorDropPct: 0, tokenSavePct: 0 }, mdPreview: "# legacy-sqlite-usearch-repair [DEPRECATED]\n主数据库 + pgvector HNSW 全量上线后本技能正式退场，仅保留历史谱系。" }
      ],
      controlUsers: [
        { id: "owner_main", displayName: "主管理员", isAdmin: true, enabled: true, activeSessions: 2 },
        { id: "peer_dev", displayName: "研发协作者", isAdmin: false, enabled: true, activeSessions: 1 },
        { id: "audit_viewer", displayName: "系统观察审计员", isAdmin: false, enabled: true, activeSessions: 0 }
      ],
      grants: [
        { viewer: "peer_dev", owner: "owner_main", historyRead: true, experienceRead: true, revision: 2 },
        { viewer: "audit_viewer", owner: "owner_main", historyRead: false, experienceRead: true, revision: 1 }
      ],
      policyLocks: [
        { key: "ai_processing_enabled", val: false, locked: true, desc: "永久关闭无计量旧版自动全量提炼路径（成本红线锁定）" },
        { key: "experience_v2_enabled", val: true, locked: false, desc: "新版 PG 个人经验库读写开启" },
        { key: "experience_pipeline_enabled", val: true, locked: false, desc: "新会话 0-token 过滤与增量提炼流水线运行中" },
        { key: "experience_backfill_enabled", val: true, locked: false, desc: "旧聊天夜间独立回填窗（01:00–07:00 CST）已开启" },
        { key: "history_pg_enabled", val: true, locked: false, desc: "企业级 PG 历史会话服务切换生效中" },
        { key: "legacy_web_enabled", val: false, locked: false, desc: "旧自动总结与手动遗忘接口已关闭" },
        { key: "experience_gate_mode", val: "review", locked: false, desc: "当前全局保底全人工审阅模式，待阶段 6 按类渐进打开" }
      ],
      pgDatabases: [
        { name: "evolvmem_control", tables: 31, role: "用户会话、跨库授权与模型信誉控制" },
        { name: "evolvmem_experience", tables: 19, role: "个人经验库 v2 与变更增量流" },
        { name: "evolvmem_dev_linux_task_ai_ece15068ea_6a0b050a", tables: 44, role: "Linux 主力设备历史库" },
        { name: "evolvmem_dev_win_01c2ee8f_41d1_479c_b_8acef270", tables: 44, role: "Windows 设备历史库" },
        { name: "evolvmem_dev_win_acceptance_20261003_7b7dd738", tables: 44, role: "Windows 验收设备历史库" },
        { name: "evolvmem_history_peer", tables: 44, role: "协作者逻辑历史库" },
        { name: "evolvmem_history_migration", tables: 44, role: "未归属历史迁移库" },
        { name: "evolvmem_shared_public", tables: 44, role: "共享项目公共历史库" },
        { name: "postgres", tables: 2, role: "数据库维护元数据" }
      ],
      retiredSqlite: [
        "~/.claude/evolvmem/memory.db (0400 只读)",
        "~/.claude/evolvmem/project_board_sync.db (0400 只读)",
        "~/.config/evolvmem-console/users.db (0400 只读)",
        "~/.config/evolvmem-history/management.db (0400 只读)",
        "~/evolvmem-governance-backups/.../candidate.db (0400 只读)",
        "~/.local/share/evolvmem-lan/public/memory.db (0400 只读)",
        "~/.local/share/evolvmem-lan/users/<peer>/memory.db (0400 只读)"
      ]
    };
  }

  E.createInitialState = initialData;
  E.state = initialData();
  E.esc = function (s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };
  E.statusBadge = function (st) {
    if (st === "live") return '<span class="badge badge-ok">已上线</span>';
    if (st === "thin") return '<span class="badge badge-brand">引擎就绪</span>';
    return '<span class="badge badge-evolve">阶段 6–7 预置</span>';
  };
  E.scopeTags = function (scope) {
    if (!scope || typeof scope !== "object") return "";
    return Object.keys(scope).map(function (k) {
      return '<span class="badge">' + E.esc(scope[k]) + "</span>";
    }).join("");
  };
  E.canWrite = function () { return E.state.libraryCtx !== "peer_readonly"; };
  E.toast = function (msg, tone) {
    E.lastToast = { msg: msg, tone: tone || "info" };
    if (typeof root.document === "undefined") return;
    var box = root.document.getElementById("toast-box");
    if (!box) return;
    var el = root.document.createElement("div");
    el.className = "toast";
    if (tone === "warn") el.style.borderColor = "var(--warn)";
    if (tone === "danger") el.style.borderColor = "var(--danger)";
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 3400);
  };
})(typeof window !== "undefined" ? window : globalThis);

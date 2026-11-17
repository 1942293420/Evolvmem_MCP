/* EvolvMem Web Console v2 — Views: Extraction Pipelines & Stage 4 Clean (L2/L3), PG History Reader & Capture */
(function (root) {
  const E = (root.EvolvMemV2 = root.EvolvMemV2 || {});
  E.views = E.views || {};
  const esc = E.esc;

  function liveTable(rows, columns, empty) {
    if (!Array.isArray(rows)) return `<p class="muted">尚未接入或未核验。</p>`;
    if (!rows.length) return `<p class="muted">${esc(empty || "当前接口未返回记录。")}</p>`;
    return `<div class="table-wrap"><table><thead><tr>${columns.map(function (c) { return `<th>${esc(c[0])}</th>`; }).join("")}</tr></thead><tbody>${rows.map(function (row) {
      return `<tr>${columns.map(function (c) { return `<td>${esc(row[c[1]] == null ? "未核验" : row[c[1]])}</td>`; }).join("")}</tr>`;
    }).join("")}</tbody></table></div>`;
  }

  function renderLivePipeline() {
    const s = E.state;
    const p = s.livePipeline;
    if (!p) return E.renderExpSubtabs("pipelines") + E.unavailablePanel("提取流水线状态未核验", "尚未成功读取流水线接口，请重试。这里不展示演示预算或运行状态。");
    const circuits = Array.isArray(p.circuits) ? p.circuits : [];
    const tracks = ["live", "backfill"].map(function (track) {
      const c = circuits.find(function (row) { return row.track === track; });
      return `<section class="panel"><div class="panel-head"><h2>${track === "live" ? "新会话增量提炼" : "旧聊天夜间回填"}</h2><span class="badge">${c ? (c.paused ? "熔断暂停" : "熔断器未暂停") : "熔断状态未核验"}</span></div>
        <p>${c ? `原因：${esc(c.pause_reason || "接口未记录暂停原因")} · 更新时间：${esc(c.updated_at || "未返回")}` : "当前接口没有该轨道的熔断记录，不能据此认定调度运行正常。"}</p>
        ${track === "live" && c && c.paused ? `<button type="button" class="btn btn-ok btn-sm" data-act="toggle-r9" ${E.canWrite() ? "" : "disabled"}>人工复位实时轨道</button>` : ""}
        ${track === "backfill" ? E.plannedButton("调整回填范围与启动任务") : ""}
        <p class="muted">每日预算消耗、跳过率及完整运行健康尚未接入；下方仅展示接口实际返回的记录。</p></section>`;
    }).join("");
    return `${E.renderExpSubtabs("pipelines")}<div class="grid-2">${tracks}</div>
      <section class="panel"><h2>最近会话片段运行记录</h2><p class="muted">接口最多返回最近 60 条，不能作为全量处理覆盖率。</p>${liveTable(p.segments, [["轨道", "track"], ["会话", "session_id"], ["状态", "status"], ["关键时刻", "moments_count"], ["错误码", "error_code"], ["更新时间", "updated_at"]])}</section>
      <section class="panel"><h2>模型调用汇总</h2><p class="muted">按轨道、阶段和模型汇总；接口没有日期筛选，不能当作今日或单次调用账单。</p>${liveTable(p.llm_usage, [["轨道", "track"], ["阶段", "stage"], ["模型", "model"], ["调用数", "calls"], ["输入 Token", "input_tokens"], ["输出 Token", "output_tokens"], ["估算费用", "cost_estimate"]])}<button type="button" class="btn btn-sm" data-act="open-llm-ledger">查看调用汇总明细</button></section>
      <section class="panel"><h2>回填游标与任务状态</h2>${liveTable(p.backfill_cursors, [["范围类型", "scope_kind"], ["范围", "scope_value"], ["已处理", "processed_count"], ["已完成", "completed"], ["更新时间", "updated_at"]])}${liveTable(p.backfill_job_stats, [["任务状态", "status"], ["影子运行", "is_shadow"], ["数量", "count"]])}</section>
      <section class="panel"><h2>旧经验清洗登记</h2><p class="muted">下列是历史批次登记，保留数不等于当前生效数；误删率、冷备剩余天数及恢复能力尚未核验。</p>${liveTable(p.legacy_cleanups, [["批次", "cleanup_id"], ["登记删除数", "deleted_total"], ["登记保留数", "migrated_keep_total"], ["批准时间", "approved_at"]])}<button type="button" class="btn btn-sm" data-act="open-stage4-l3">查看清洗登记来源</button></section>`;
  }

  E.views.pipelines = function () {
    if (E.isProduction()) return renderLivePipeline();
    const s = E.state;
    const w = E.canWrite();
    const funnelSteps = [
      { label: "1. 扫描历史与新增原始会话", val: "100 会话", pct: 100, color: "var(--brand)" },
      { label: "2. 零消耗关键时刻初筛（直接跳过无价值对话）", val: "跳过 77 个 (0 tok)", pct: 77, color: "var(--ok)" },
      { label: "3. 进入提炼的关键时刻片段", val: "38 个关键段落", pct: 38, color: "var(--brand)" },
      { label: "4. L0–L3 自动质检拦截与语义去重合并", val: "拦截 32 · 合并 1", pct: 18, color: "var(--warn)" },
      { label: "5. 最终进入 L4 审阅箱的高纯度候选", val: "5 条新经验 + 1 合并", pct: 12, color: "var(--evolve)" }
    ];

    return `
      ${E.renderExpSubtabs("pipelines")}
      <div class="banner banner-warn">离线演示：以下预算、漏斗及清洗状态为示例，不代表现网数据。</div>

      ${s.r9Tripped ? `
        <div class="banner banner-danger">
          <span><b>R9 自动成本熔断已生效：</b>检测到单条成本过高或审阅通过率低于 30%，增量调度已安全暂停排队，原文会话未丢失。</span>
          <button type="button" class="btn btn-ok btn-sm" data-act="toggle-r9" ${w ? "" : "disabled"}>✓ 人工复位恢复调度</button>
        </div>` : ""}

      <div class="grid-2">
        <section class="panel">
          <div class="panel-head">
            <div>
              <h2>轨道 A · 新会话增量提炼</h2>
              <p>会话结束或静默 30 分钟后自动识别关键时刻</p>
            </div>
            <span class="badge ${s.r9Tripped ? "badge-danger" : "badge-ok"}">${s.r9Tripped ? "已熔断暂停" : "调度运行中"}</span>
          </div>
          <div>
            <div style="display:flex;justify-content:space-between;font-size:13.5px;margin-bottom:6px">
              <span>今日预算消耗（与旧聊天严格物理隔离）</span>
              <b>2.5% · 日上限 200 万 tok</b>
            </div>
            <div class="funnel-track"><div class="funnel-fill" style="width:14%;background:var(--brand)"></div></div>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;font-size:13.5px">
            <span class="muted">无时刻跳过率：<b style="color:var(--ok)">70%–80%（零花费）</b></span>
            ${s.r9Tripped || s.apiMode !== "live" ? `<button type="button" class="btn ${s.r9Tripped ? "btn-ok" : "btn-danger"} btn-sm" data-act="toggle-r9" ${w ? "" : "disabled"}>
              ${s.r9Tripped ? "恢复调度运行" : "⚠ 模拟触发 R9 熔断"}
            </button>` : `<span class="badge badge-ok">R9 成本守卫正常</span>`}
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div>
              <h2>轨道 B · 旧聊天夜间独立回填</h2>
              <p>仅限夜间窗口运行 · 按单项目或单月份互斥推进</p>
            </div>
            <span class="badge badge-evolve">01:00–07:00 CST</span>
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <select id="bf-scope-mode" style="flex:1" ${w ? "" : "disabled"}>
              <option value="month" ${s.backfillScopeMode === "month" ? "selected" : ""}>按月份推进（互斥不跨项目）</option>
              <option value="project" ${s.backfillScopeMode === "project" ? "selected" : ""}>按项目推进（互斥不跨月份）</option>
            </select>
            <select id="bf-scope-val" style="flex:1" ${w ? "" : "disabled"}>
              ${s.backfillScopeMode === "month"
                ? `<option value="2026-11" ${s.backfillScopeVal === "2026-11" ? "selected" : ""}>2026-11（等待夜间窗口）</option>
                   <option value="2026-10" ${s.backfillScopeVal === "2026-10" ? "selected" : ""}>2026-10（已完成 100 会话试跑）</option>
                   <option value="2026-09" ${s.backfillScopeVal === "2026-09" ? "selected" : ""}>2026-09（排队待回填）</option>`
                : `<option value="evolvmem-pg-prod" ${s.backfillScopeVal === "evolvmem-pg-prod" ? "selected" : ""}>EvolvMem PG 切流工程</option>
                   <option value="win-desktop-sync" ${s.backfillScopeVal === "win-desktop-sync" ? "selected" : ""}>Windows 双端同步</option>`}
            </select>
          </div>
          <div>
            <div style="display:flex;justify-content:space-between;font-size:13.5px;margin-bottom:6px">
              <span>夜间回填独立预算池</span>
              <b>2.6% · 夜上限 300 万 tok</b>
            </div>
            <div class="funnel-track"><div class="funnel-fill" style="width:15%;background:var(--evolve)"></div></div>
          </div>
        </section>
      </div>

      <div class="grid-2">
        <section class="panel">
          <div class="panel-head">
            <div><h2>五阶降噪过滤漏斗（实测 100 会话基线）</h2><p>在送交大模型提炼前先行 0-token 过滤绝大多数无价值对话</p></div>
            <span class="badge badge-ok">单条均耗 ~10k tok</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:12px">
            ${funnelSteps.map(function (st) {
              return `<div class="funnel-row">
                <div style="display:flex;justify-content:space-between;font-size:13.5px">
                  <span>${esc(st.label)}</span><b>${esc(st.val)}</b>
                </div>
                <div class="funnel-track"><div class="funnel-fill" style="width:${st.pct}%;background:${st.color}"></div></div>
              </div>`;
            }).join("")}
          </div>
          <div class="l3-bar">
            <span class="muted" style="font-size:13px">每次模型调用费用均经全仓统一计量</span>
            <button type="button" class="btn btn-sm" data-act="open-llm-ledger">📊 查看 LLM 计量调用账本 (L3)</button>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div><h2>旧版历史经验清洗与冷备成果</h2><p>原只读评估 9,843 条旧记录，人工抽检确认误删率可控后精华迁入新库</p></div>
            <span class="badge badge-ok">已安全归档</span>
          </div>
          <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px">
            <div class="kv-cell"><span>总评估旧记录</span><b style="font-size:20px">9,843</b></div>
            <div class="kv-cell"><span>精华迁入新库</span><b style="font-size:20px;color:var(--ok)">707 条</b></div>
            <div class="kv-cell"><span>误删抽样复核</span><b style="font-size:20px;color:var(--brand)">4.0%</b></div>
          </div>
          <div class="callout-box">
            其余 9,134 条低价值冗余已压缩冷备留存（剩余保护期 29 天），历史原对话一字未删；误判的 2 条边界记录已通过人工救回并迁入正式经验库。
          </div>
          <div class="l3-bar">
            <span class="muted" style="font-size:13px">含救回主键编号与冷备包校验信息</span>
            <button type="button" class="btn btn-sm" data-act="open-stage4-l3">🗄️ 查看抽样救回与冷备明细 (L3)</button>
          </div>
        </section>
      </div>`;
  };

  E.views.history = function () {
    const s = E.state;
    const w = E.canWrite();

    let header = `
      <div class="page-head">
        <div>
          <div class="eyebrow">HISTORY VAULT · 多端永久原文</div>
          <h1>PG 历史会话库与多端捕获</h1>
          <p>${E.isProduction() ? "读取已归档的原始对话，支持项目浏览、检索与原文阅读；完整覆盖需另行核验。" : "离线演示：以下会话、同步状态与附件为示例，不代表现网数据。"}</p>
        </div>
        <div class="topbar-actions">
          ${E.isProduction() ? '<div class="history-source-note"><span class="badge">当前读取所选用户历史库</span><span class="muted">设备库切换未接入</span></div>' : `
          <select id="hist-db-sel" aria-label="选择设备历史库">
            ${Object.keys(E.HIST_DB_ZH).map(function (k) {
              return `<option value="${esc(k)}" ${s.histDb === k ? "selected" : ""}>${esc(E.HIST_DB_ZH[k])}</option>`;
            }).join("")}
          </select>`}
        </div>
      </div>
      ${!w ? `<div class="banner banner-warn">当前处于他人授权只读视角：支持检索与阅读对话原文，移入回收站或恢复项目操作已禁用。</div>` : ""}
      <div class="subtabs" role="tablist">
        <button type="button" class="subtab ${s.histSubtab === "browse" ? "active" : ""}" data-act="hist-subtab" data-sub="browse">项目与会话检索</button>
        <button type="button" class="subtab ${s.histSubtab === "capture" ? "active" : ""}" data-act="hist-subtab" data-sub="capture">跨端续接与捕获状态</button>
      </div>`;

    if (s.histSubtab === "capture") {
      if (E.isProduction()) return header + E.unavailablePanel("跨端捕获状态尚未接入", "当前接口未提供客户端队列、上传回执、积压、连续性租约及解密耗时，不能判断同步健康。请通过真实客户端与服务端回执核验。") + `<section class="panel"><button type="button" class="btn btn-sm" data-act="open-capture-l3">查看捕获状态说明</button>${E.plannedButton("启动同步或回填")}</section>`;
      return `${header}
        <div class="grid-2">
          <section class="panel">
            <div class="panel-head"><div><h2>Windows 双端实时捕获通道</h2><p>本地桌面守护进程自动增量同步对话块至中心库</p></div><span class="badge badge-ok">零积压健康</span></div>
            <div class="callout-box"><b>Codex 桌面同步：</b>上次调度成功，最新 2 个增量片段已全部回执入库，待处理队列为空。</div>
            <div class="callout-box"><b>DSH 桌面伴侣捕获：</b>运行健康，全部 74 份跨设备会话历史确认完毕，异常计数为 0。</div>
          </section>
          <section class="panel">
            <div class="panel-head"><div><h2>跨端连续性交接与解密回查</h2><p>换台电脑继续工作时秒级接力最近活跃项目焦点</p></div><span class="badge badge-brand">零 LLM 消耗</span></div>
            <div class="callout-box"><b>加密归档按需直读：</b>调阅长篇历史会话无需大模型总结转写，4 毫秒内直接解密渲染完整对话轮次与截图附件。</div>
            <div class="callout-box"><b>分布式连续性回填锁：</b>严格单库并发锁调度，保护跨设备工作流焦点修订顺序不错乱。</div>
          </section>
        </div>
        <section class="panel" style="padding:14px 20px">
          <div class="l3-bar" style="border-top:0;padding-top:0">
            <span class="muted" style="font-size:13.5px">需要核查 cutover_write_journal 写入笔数与 SKIP LOCKED 锁参数？</span>
            <button type="button" class="btn btn-sm" data-act="open-capture-l3">📋 查看底层同步日志与租约明细 (L3)</button>
          </div>
        </section>`;
    }

    const q = s.histQuery.trim().toLowerCase();
    const visibleProjs = s.historyProjects.filter(function (p) { return s.histScope === "trash" ? p.deleted : !p.deleted; });
    const blocks = s.historyBlocks.filter(function (b) {
      if (s.histScope === "unassigned" && !q && b.project !== null) return false;
      if (s.histScope === "projects" && !q && b.project === null) return false;
      if (s.histScope === "trash") return false;
      if (!q || s.apiMode === "live") return true;
      return (b.title + b.transcript.join(" ")).toLowerCase().indexOf(q) >= 0;
    });
    const selBlk = blocks.find(function (b) { return b.id === s.selectedBlockId; }) || blocks[0] || null;
    if (selBlk) s.selectedBlockId = selBlk.id;
    const blkProj = selBlk && selBlk.project ? s.historyProjects.find(function (p) { return p.key === selBlk.project; }) : null;

    return `${header}
      <div class="panel" style="padding:14px 18px">
        <div class="filter-bar">
          ${s.histScope === "projects" && !q && s.historyProjects.some(function (p) { return !p.deleted; }) ? `
            ${E.renderHistoryProjectPicker()}` : ""}
          <input type="search" id="hist-q" value="${esc(s.histQuery)}" placeholder="三路混合搜索历史会话标题、命令或对话正文…" style="flex:1;min-width:220px" />
          <div class="cat-pills">
            <button type="button" class="cat-pill ${s.histScope === "projects" ? "active" : ""}" data-act="hist-scope" data-val="projects"><span>项目会话</span><strong>${s.historyProjects.filter(function (p) { return !p.deleted; }).length}</strong></button>
            <button type="button" class="cat-pill ${s.histScope === "unassigned" ? "active" : ""}" data-act="hist-scope" data-val="unassigned"><span>未归属片段</span></button>
            ${w ? `<button type="button" class="cat-pill ${s.histScope === "trash" ? "active" : ""}" data-act="hist-scope" data-val="trash"><span>回收站</span><strong>${s.historyProjects.filter(function (p) { return p.deleted; }).length}</strong></button>` : ""}
          </div>
        </div>
      </div>

      <p class="muted history-project-help">项目来自已归档会话的项目归属；括号数字为可见历史片段数。点击项目选择框可输入名称查找。</p>

      ${s.histScope === "trash" ? `
        <section class="panel">
          <div class="panel-head"><div><h2>已移至回收站的项目 (${visibleProjs.length})</h2><p>回收站仅保留目录标记，随时可一键恢复原有会话关联</p></div></div>
          ${visibleProjs.length === 0 ? `<p class="muted">回收站为空。</p>` : visibleProjs.map(function (p) {
            return `<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 14px;border-radius:10px;background:var(--bg-subtle);border:1px solid var(--line)">
              <div><b>${esc(p.title)}</b><div class="muted" style="font-size:13px">${p.sessions} 个会话 · ${p.blocks} 片段 · ${esc(p.lastAt)}</div></div>
              <button type="button" class="btn btn-ok btn-sm" data-act="toggle-proj-del" data-key="${esc(p.key)}" ${w ? "" : "disabled"}>恢复项目</button>
            </div>`;
          }).join("")}
        </section>
      ` : `
        <div class="split-workspace">
          <div class="list-panel">
            <div class="list-head"><strong>匹配会话记录 (${blocks.length})</strong><span class="muted" style="font-size:12.5px">三路混合相关性排序</span></div>
            <div class="list-scroll">
              ${blocks.length === 0 ? `<div style="padding:28px" class="muted">没有找到匹配的历史会话。</div>` : blocks.map(function (b) {
                const projObj = b.project ? s.historyProjects.find(function (p) { return p.key === b.project; }) : null;
                return `<button type="button" class="list-entry ${selBlk && selBlk.id === b.id ? "active" : ""}" data-act="select-block" data-id="${esc(b.id)}">
                  <div class="list-entry-meta">
                    <span class="badge badge-brand">${projObj ? esc(projObj.title) : "未归属会话"}</span>
                    <span>${b.turns} 轮对话</span>
                  </div>
                  <div class="list-entry-title">${esc(b.title)}</div>
                </button>`;
              }).join("")}
            </div>
          </div>

          <section class="detail-panel" aria-label="历史会话原文阅读器">
            ${!selBlk ? `<p class="muted">请从左侧列表选择一段历史会话阅读完整上下文。</p>` : `
              <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px">
                <div>
                  <div class="tag-row" style="margin-bottom:6px">
                    ${E.isProduction() ? `<span class="badge">检索评分明细尚未核验</span>` : `<span class="badge badge-ok">混搜综合分 ${(selBlk.rrf * 100).toFixed(0)}</span>`}
                    ${blkProj ? `<span class="badge">${esc(blkProj.title)}</span>` : `<span class="badge">未归属片段</span>`}
                    ${selBlk.hasAttachment ? `<span class="badge badge-brand">📎 含过程截图</span>` : ""}
                  </div>
                  <h2 class="prose-lead">${esc(selBlk.title)}</h2>
                </div>
                ${blkProj ? `
                  <button type="button" class="btn ${blkProj.deleted ? "btn-ok" : "btn-danger"} btn-sm" data-act="toggle-proj-del" data-key="${esc(blkProj.key)}" ${w ? "" : "disabled"}>
                    ${blkProj.deleted ? "恢复所属项目" : "软删所属项目"}
                  </button>` : ""}
              </div>

              <div class="chat-feed">
                ${selBlk.transcript.map(function (line, idx) {
                  const isAssist = idx % 2 === 1;
                  const splitPos = line.indexOf("：");
                  const role = splitPos > 0 ? line.slice(0, splitPos) : (isAssist ? "助手" : "用户");
                  const msg = splitPos > 0 ? line.slice(splitPos + 1) : line;
                  return `<div class="chat-bubble ${isAssist ? "assistant" : ""}">
                    <div class="chat-role">${esc(role)}</div>
                    <div>${esc(msg)}</div>
                  </div>`;
                }).join("")}
              </div>

              ${selBlk.hasAttachment && E.isProduction() ? E.unavailablePanel("附件预览尚未接入", "此页面尚未读取真实附件内容，不展示示例截图。") : selBlk.hasAttachment ? `
                <div style="padding:14px;border-radius:10px;background:var(--bg-subtle);border:1px solid var(--line)">
                  <div style="font-size:13px;font-weight:700;margin-bottom:8px">已解密内嵌附件截图（按需直读）</div>
                  <svg width="100%" height="88" viewBox="0 0 420 88"><rect width="420" height="88" rx="9" fill="#0f172a" stroke="#38bdf8"/><text x="20" y="38" fill="#34d399" font-family="monospace" font-size="14">DR ISOLATION DRILL · PASS</text><text x="20" y="64" fill="#cbd5e1" font-family="monospace" font-size="12">roles_restored=true · 9/9 DBs exact match</text></svg>
                </div>` : ""}

              <div class="l3-bar">
                <span class="muted" style="font-size:13px">BM25 / 三元组 / 向量分数拆解及物理库标识已放入三级抽屉</span>
                <button type="button" class="btn btn-sm" data-act="open-hist-meta" data-id="${esc(selBlk.id)}">🔬 查看混搜评分拆解与物理来源 (L3)</button>
              </div>
            `}
          </section>
        </div>
      `}`;
  };
})(typeof window !== "undefined" ? window : globalThis);

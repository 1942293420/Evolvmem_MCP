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
    const s=E.state, w=E.canWrite(), prod=E.isProduction(), q=s.histQuery.trim();
    const entity=prod?'文件夹':'项目', projects=s.historyProjects || [];
    const selected=projects.find(p=>p.key===s.selectedProjectKey && !p.deleted);
    const blocks=(s.historyBlocks || []).filter(b=>s.histScope!=='trash' && (q || (s.histScope==='unassigned'?b.project===null:b.project!==null)));
    const block=blocks.find(b=>b.id===s.selectedBlockId) || blocks[0];
    if(block)s.selectedBlockId=block.id;
    const group=block && projects.find(p=>p.key===block.project);
    const folderPath=block && (block.workspacePath || group?.workspacePath) || '';
    const title=value=>esc(E.historyDisplayTitle(value));
    const unassigned=Number.isInteger(s.liveHistory?.unassigned_count)?s.liveHistory.unassigned_count:null;
    const help=`<details class="history-help"><summary>阅读说明</summary><div class="history-help-body"><p>文件夹按 Codex 实际工作文件夹和其他客户端已核实目录分组；无可靠工作文件夹的记录进入未归属；括号为会话片段数。搜索可选当前文件夹或所有文件夹。</p><p>有明确时区的时间统一显示北京时间（UTC+8）；来源时区未注明时保留原值。入库时间不代表原对话发生时间。</p>${prod?'<p>当前读取所选用户历史库；设备库切换未接入。完整归档覆盖和检索分项评分需另行核验。</p>':''}</div></details>`;
    const header=`<div class="history-heading"><h1>历史会话${prod?'':' · 离线演示'}</h1><div class="subtabs" role="tablist"><button type="button" class="subtab ${s.histSubtab==='browse'?'active':''}" data-act="hist-subtab" data-sub="browse">文件夹与会话检索</button><button type="button" class="subtab ${s.histSubtab==='capture'?'active':''}" data-act="hist-subtab" data-sub="capture">跨端续接与捕获状态</button></div>${help}</div>${!w?'<div class="history-readonly">当前为授权只读视角，可检索与阅读原文。</div>':''}`;
    if(s.histSubtab==='capture')return header+E.unavailablePanel('跨端捕获状态尚未接入','此页尚未读取客户端队列、上传回执、积压与连续性租约，不能据此判断同步健康。')+`<section class="panel"><div class="history-actions"><button type="button" class="btn btn-sm" data-act="open-capture-l3">查看捕获状态说明</button>${E.plannedButton('启动同步或回填')}</div></section>`;
    const filters=`<div class="history-filters"><div class="filter-bar">
      ${s.histScope==='projects'?E.renderHistoryProjectPicker():`<span class="history-scope-label">${s.histScope==='trash'?'回收站':'未归属片段'}</span>`}
      <input type="search" id="hist-q" value="${esc(s.histQuery)}" aria-label="搜索历史会话" placeholder="搜索标题或对话正文…" ${s.histScope==='trash'?'disabled':''}/>
      ${s.histScope==='projects'?`<select id="hist-search-range" aria-label="搜索范围"><option value="folder" ${s.histSearchRange!=='all'?'selected':''}>当前文件夹</option><option value="all" ${s.histSearchRange==='all'?'selected':''}>所有文件夹</option></select>`:''}
      <div class="cat-pills"><button type="button" class="cat-pill ${s.histScope==='projects'?'active':''}" data-act="hist-scope" data-val="projects"><span>文件夹</span><strong>${projects.filter(p=>!p.deleted).length}</strong></button><button type="button" class="cat-pill ${s.histScope==='unassigned'?'active':''}" data-act="hist-scope" data-val="unassigned"><span>未归属片段</span><strong>${unassigned===null?'—':unassigned}</strong></button>${w?`<button type="button" class="cat-pill ${s.histScope==='trash'?'active':''}" data-act="hist-scope" data-val="trash"><span>回收站</span><strong>${projects.filter(p=>p.deleted).length}</strong></button>`:''}</div>
    </div></div>`;
    if(s.histScope==='trash')return header+filters+`<section class="panel history-trash"><h2>回收站中的文件夹与项目</h2><p class="muted">恢复后重新显示分组内的会话；此操作不删除本地文件夹或会话原文。</p>${projects.filter(p=>p.deleted).map(p=>`<div class="history-trash-row"><div><b>${esc(p.title)}</b><p class="muted">${esc(p.workspacePath || '')} · ${p.blocks} 个会话片段</p></div><button type="button" class="btn btn-ok btn-sm" data-act="toggle-proj-del" data-key="${esc(p.key)}">恢复${p.kind==='workspace'?'文件夹':'项目'}</button></div>`).join('') || '<p>回收站为空。</p>'}</section>`;
    const list=`<div class="list-panel"><div class="list-head"><strong>会话 (${blocks.length})</strong>${selected && w && s.histScope==='projects' && (!q || s.histSearchRange!=='all')?`<button type="button" class="btn btn-ghost btn-sm" data-act="toggle-proj-del" data-key="${esc(selected.key)}" aria-label="管理文件夹 ${esc(selected.title)}">管理文件夹</button>`:''}</div><div id="history-list-scroll" class="list-scroll">
      ${blocks.map(b=>`<button type="button" class="list-entry ${block?.id===b.id?'active':''}" data-act="select-block" data-id="${esc(b.id)}"><div class="list-entry-title">${title(b.title)}</div><div class="list-entry-meta"><span>${b.turns==null?'轮次未记录':esc(b.turns)+' 轮对话'}</span><span>${b.timeBasis==='archive_fallback'?'入库：':''}${esc(E.formatHistoryTime(b.lastAt))}</span></div>${q && s.histSearchRange==='all'?`<span class="muted">${esc(projects.find(p=>p.key===b.project)?.title || '未归属')}</span>`:''}</button>`).join('') || '<p class="history-empty">没有找到匹配的会话。</p>'}
    </div></div>`;
    let reader='<p class="history-empty">从左侧选择会话，阅读原文。</p>';
    if(block) {
      const messages=E.historyMessages(block), count=messages.length;
      reader=`<div class="history-reader-head"><h2 class="prose-lead" title="${title(block.title)}">${title(block.title)}</h2><div class="history-reader-meta"><span>${block.messageCount==null?'消息数未记录':esc(block.messageCount)+' 条消息'}</span><span>${block.timeBasis==='archive_fallback'?'入库时间：':'最近记录：'}${esc(E.formatHistoryTime(block.lastAt))}</span><button type="button" class="btn btn-ghost btn-sm" data-act="history-source-info" data-id="${esc(block.id)}">来源与记录信息</button></div></div>
      <div id="history-reader-scroll" class="history-reader-scroll" data-block-id="${esc(block.id)}"><div class="chat-feed">
      ${messages.map((m,i)=>{const role=m.role==='assistant'?'助手':m.role==='user'?'用户':m.role || '消息',basis=m.time_basis || block.timeBasis;return `<div class="chat-bubble ${m.role==='assistant'?'assistant':''}" data-message-index="${i}"><div class="chat-meta"><div class="chat-role">${esc(role)}</div><span class="chat-time">${basis==='archive_fallback'?'入库时间：':'记录时间：'}${esc(E.formatHistoryTime(m.recorded_at))}</span></div><div class="history-message">${E.renderHistoryContent(m.content)}</div></div>`;}).join('')}
      </div><div class="history-pagination" role="status"><span>已显示 ${count}${block.messageCount==null?'':' / '+esc(block.messageCount)} 条消息${block._hasMore===false?' · 已到末尾':''}</span>${block._turnsError?`<span class="history-load-error">${esc(block._turnsError)}</span>`:''}${block._hasMore || !block._turnsLoaded?`<button type="button" class="btn btn-sm" data-act="history-load-more" data-id="${esc(block.id)}" ${block._turnsLoading?'disabled':''}>${block._turnsLoading?'正在加载…':block._turnsError?'重试加载':count?'加载更多消息':'加载消息'}</button>`:''}</div></div>`;
    }
    return header+filters+`<div class="split-workspace history-split">${list}<section class="detail-panel history-reader" aria-label="历史会话原文阅读器">${reader}</section></div>`;
  };
})(typeof window !== "undefined" ? window : globalThis);

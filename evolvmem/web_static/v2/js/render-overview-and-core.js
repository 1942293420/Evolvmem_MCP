/* EvolvMem Web Console v2 — Views: Overview (Topology & Stage Flow), Review Inbox (L1/L2), Experiences & Rollback */
(function (root) {
  const E = (root.EvolvMemV2 = root.EvolvMemV2 || {});
  E.views = E.views || {};
  const esc = E.esc;

  E.renderExpSubtabs = function (activeSub) {
    const w = E.canWrite();
    const fTotal = (E.state.interceptionFunnel && E.state.interceptionFunnel.total_intercepted) || E.state.interceptions.length;
    return `
      <div class="page-head">
        <div>
          <div class="eyebrow">EXPERIENCE VAULT &amp; PIPELINE</div>
          <h1>个人经验库与提炼中心</h1>
          <p>浏览经过核验的可复用经验、追溯各层未写入拦截账本、跟踪双轨 0-token 漏斗或按模型回滚。</p>
        </div>
        <div class="topbar-actions">
          <button type="button" class="btn btn-primary" data-act="open-new-exp" ${w ? "" : "disabled"}>+ 手工新增经验</button>
        </div>
      </div>
      ${!w ? `<div class="banner banner-warn">当前正在查看他人授权库（只读视角），仅展示对方生效中（Active）经验；拦截明细、退役/试用记录与写回滚操作均已安全屏蔽。</div>` : ""}
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
        <div class="subtabs" role="tablist">
          <button type="button" class="subtab ${activeSub === "list" ? "active" : ""}" data-act="exp-subtab" data-sub="list">正式经验浏览</button>
          ${w ? `<button type="button" class="subtab ${activeSub === "interceptions" ? "active" : ""}" data-act="exp-subtab" data-sub="interceptions">未写入 / 门控拦截 (${fTotal})</button>` : ""}
          <button type="button" class="subtab ${activeSub === "pipelines" ? "active" : ""}" data-act="exp-subtab" data-sub="pipelines">提取流水线与清洗档案</button>
          <button type="button" class="subtab ${activeSub === "rollback" ? "active" : ""}" data-act="exp-subtab" data-sub="rollback">按模型安全回滚</button>
        </div>
      </div>`;
  };

  E.views.overview = function () {
    const s = E.state;
    const w = E.canWrite();
    const selAgent = s.agents.find(function (a) { return a.agent === s.selectedAgentName; }) || s.agents[0];
    const pendingLive = s.proposals.filter(function (p) { return p.status === "pending" && p.track === "live"; }).length;
    const pendingBack = s.proposals.filter(function (p) { return p.status === "pending" && p.track === "backfill"; }).length;
    const activeExp = s.experiences.filter(function (x) { return x.status === "active"; }).length;
    const activeSkills = s.skills.filter(function (k) { return k.lifecycle === "active"; }).length;
    const leftAgents = s.agents.slice(0, 2);
    const rightAgents = s.agents.slice(2, 4);

    function agentNode(a) {
      const selCls = a.agent === selAgent.agent ? "selected" : "";
      return `<button type="button" class="agent-node ${selCls}" data-act="select-agent" data-name="${esc(a.agent)}">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;margin-bottom:4px">
          <strong style="font-size:13.5px;display:flex;align-items:center;gap:6px">
            <span class="dot ${a.status === "online" ? "dot-ok" : "dot-warn"}"></span>${esc(a.title)}
          </strong>
          <span class="badge ${a.status === "online" ? "badge-ok" : "badge-warn"}">${a.status === "online" ? "在线" : "限额中"}</span>
        </div>
        <div class="muted" style="font-size:12px;display:flex;justify-content:space-between">
          <span>${esc(a.os)}</span><span>最近更新 · ${esc(a.lastSyncAt)}</span>
        </div>
      </button>`;
    }

    return `
      <div class="page-head">
        <div>
          <div class="eyebrow">EVOLVMEM NEURAL MATRIX · 态势总览</div>
          <h1>多智能体记忆同步与进化中枢</h1>
          <p>一屏掌握跨端智能体实时归档状态、双轨审阅待办、有效经验存量与系统七阶进化全景。</p>
        </div>
        <div class="topbar-actions">
          <button type="button" class="btn btn-evolve" data-act="goto-tab" data-route="review-inbox">处理 ${pendingLive + pendingBack} 条待审经验 →</button>
          <button type="button" class="btn btn-primary" data-act="simulate-sync" ${w ? "" : "disabled"}>⚡ 模拟多端增量同步</button>
        </div>
      </div>

      <div class="kpi-grid">
        <div class="kpi-card">
          <div class="kpi-label"><span>协同接入智能体</span><span class="badge badge-ok">在线健康</span></div>
          <div class="kpi-value">${s.agents.filter(function (a) { return a.status === "online"; }).length} / ${s.agents.length}</div>
          <div class="kpi-bar"><i style="width:88%"></i></div>
          <div class="kpi-sub">跨端增量块与连续对话全量回执</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label"><span>双轨人工审阅待办</span><span class="badge badge-warn">需把关</span></div>
          <div class="kpi-value">${pendingLive + pendingBack} 条</div>
          <div class="kpi-bar"><i style="width:45%"></i></div>
          <div class="kpi-sub">新会话 ${pendingLive} 条 · 夜间回填 ${pendingBack} 条</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label"><span>已沉淀活跃经验</span><span class="badge badge-ok">权威主库</span></div>
          <div class="kpi-value">${739 + activeExp} 条</div>
          <div class="kpi-bar"><i style="width:92%"></i></div>
          <div class="kpi-sub">含手工精品与 Stage 4 迁入精华</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label"><span>进化闭环达标类别</span><span class="badge badge-evolve">阶段 6</span></div>
          <div class="kpi-value">1 / 3</div>
          <div class="kpi-bar"><i style="width:65%"></i></div>
          <div class="kpi-sub">系统环境经验已进入半自动晋升</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label"><span>正式发布共享 Skill</span><span class="badge badge-brand">阶段 7</span></div>
          <div class="kpi-value">${activeSkills} / 3 个</div>
          <div class="kpi-bar"><i style="width:${Math.min(100, Math.round((activeSkills / 3) * 100))}%"></i></div>
          <div class="kpi-sub">目标完成至少 3 个评测上架 Skill</div>
        </div>
      </div>

      <div class="grid-asymmetric">
        <section class="panel">
          <div class="panel-head">
            <div>
              <h2>多智能体实时神经同步拓扑</h2>
              <p>点击任意客户端节点查看工作状态摘要，或点击下方按钮打开三级技术溯源抽屉</p>
            </div>
            <span class="badge badge-ok">PG 权威中心在线</span>
          </div>

          <div class="topology-wrap">
            <svg class="neural-wires" viewBox="0 0 800 220" preserveAspectRatio="none" aria-hidden="true">
              <path class="wire-base" d="M 220 58 C 310 58, 330 110, 400 110" /><path class="wire-flow" d="M 220 58 C 310 58, 330 110, 400 110" />
              <path class="wire-base" d="M 220 162 C 310 162, 330 110, 400 110" /><path class="wire-flow" d="M 220 162 C 310 162, 330 110, 400 110" />
              <path class="wire-base" d="M 580 58 C 490 58, 470 110, 400 110" /><path class="wire-flow" d="M 580 58 C 490 58, 470 110, 400 110" />
              <path class="wire-base" d="M 580 162 C 490 162, 470 110, 400 110" /><path class="wire-flow" d="M 580 162 C 490 162, 470 110, 400 110" />
            </svg>
            <div class="agent-col">${leftAgents.map(agentNode).join("")}</div>
            <div class="hub-core">
              <strong style="color:var(--brand);font-size:14px">MEMORY HUB</strong>
              <span class="muted" style="font-size:12px;margin-top:3px">PostgreSQL 17<br/>HNSW 向量底座</span>
              <span class="badge badge-ok" style="margin-top:6px">全量在线</span>
            </div>
            <div class="agent-col">${rightAgents.map(agentNode).join("")}</div>
          </div>

          <div class="callout-box" style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
            <div>
              <div style="font-weight:700;margin-bottom:2px">${esc(selAgent.title)} · 当前活跃项目：${esc(selAgent.activeProject)}</div>
              <div class="secondary" style="font-size:13.5px">${esc(selAgent.syncNote)}</div>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap">
              <button type="button" class="btn btn-sm" data-act="open-agent-l3" data-name="${esc(selAgent.agent)}">查看节点技术明细 (L3)</button>
              <button type="button" class="btn btn-primary btn-sm" data-act="goto-tab" data-route="history">前往历史会话</button>
            </div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head">
            <div>
              <h2>系统设计总框架七阶全景图</h2>
              <p>点击任意架构链路环节，侧滑查看底层库表与直接跳转入口</p>
            </div>
            <span class="badge">Stage 1–7</span>
          </div>
          <div style="display:flex;flex-direction:column;gap:8px;max-height:420px;overflow-y:auto">
            ${s.topoNodes.map(function (n) {
              return `<button type="button" class="stage-flow-row" data-act="open-topo-l3" data-id="${esc(n.id)}">
                <div style="min-width:0">
                  <div style="font-weight:650;font-size:14.5px;color:var(--ink)">${esc(n.title)}</div>
                  <div class="muted truncate" style="font-size:13px">${esc(n.summary)}</div>
                </div>
                <div style="display:flex;align-items:center;gap:8px;flex-shrink:0">
                  ${E.statusBadge(n.status)}
                  <span class="muted">›</span>
                </div>
              </button>`;
            }).join("")}
          </div>
        </section>
      </div>

      <section class="panel">
        <div class="panel-head">
          <div>
            <h2>近期同步与经验变动动态</h2>
            <p>反映跨端新提交入库或经验状态变更（底层事件游标明细已折叠至三级抽屉）</p>
          </div>
          <button type="button" class="btn btn-sm" data-act="open-changes-l3">查看增量游标流水 (L3)</button>
        </div>
        <div style="display:grid;gap:8px">
          ${s.changesStream.slice(0, 3).map(function (c) {
            return `<div style="display:flex;justify-content:space-between;align-items:center;gap:12px;padding:10px 14px;border-radius:10px;background:var(--bg-subtle);border:1px solid var(--line)">
              <span><b>${esc(c.action)}</b> <span class="muted" style="font-size:13px">· 来源模型：${esc(c.model)}</span></span>
              <span class="muted" style="font-size:13px">${esc(c.ts)}</span>
            </div>`;
          }).join("")}
        </div>
      </section>`;
  };

  E.views["review-inbox"] = function () {
    const s = E.state;
    const w = E.canWrite();
    const list = s.proposals.filter(function (p) { return p.track === s.inboxTrack; });
    const sel = list.find(function (p) { return p.uid === s.selectedProposalUid; }) || list[0] || null;
    if (sel) s.selectedProposalUid = sel.uid;

    const livePending = s.proposals.filter(function (p) { return p.track === "live" && p.status === "pending"; }).length;
    const backPending = s.proposals.filter(function (p) { return p.track === "backfill" && p.status === "pending"; }).length;
    const ta = s.trackAgreement || {};
    const curStats = ta[s.inboxTrack] || s.reviewStats;
    const agreedNum = curStats.agreed_total != null ? curStats.agreed_total : curStats.agreed;
    const evalNum = curStats.evaluable_total != null ? curStats.evaluable_total : curStats.total;
    const rate = evalNum ? ((agreedNum / evalNum) * 100).toFixed(1) : "0.0";
    const hideGate = sel && s.shadowMode && sel.status === "pending";
    const isEdit = sel && s.editingUid === sel.uid;

    return `
      <div class="page-head">
        <div>
          <div class="eyebrow">L4 REVIEW INBOX · 人工把关</div>
          <h1>双轨经验审阅箱</h1>
          <p>新会话增量提议与夜间旧聊天回填独立排队审阅，确认价值后正式写入个人经验库。</p>
        </div>
        <div class="topbar-actions">
          <label class="badge" style="cursor:pointer;padding:6px 12px;font-size:13px">
            <input type="checkbox" id="chk-shadow-mode" ${s.shadowMode ? "checked" : ""} />
            &nbsp;影子盲审模式（提交后揭晓系统建议）
          </label>
          <span class="badge badge-brand">${s.inboxTrack === "live" ? "新会话" : "旧聊天"}一致率 ${rate}%</span>
        </div>
      </div>

      ${!w ? `<div class="banner banner-warn">当前正在查看他人授权库（只读视角）：可以阅读队列内容与原文证据，不能执行通过、修改或驳回操作。</div>` : ""}

      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
        <div class="subtabs" role="tablist">
          <button type="button" class="subtab ${s.inboxTrack === "live" ? "active" : ""}" data-act="switch-track" data-track="live">新会话队列 (${livePending} 待审)</button>
          <button type="button" class="subtab ${s.inboxTrack === "backfill" ? "active" : ""}" data-act="switch-track" data-track="backfill">旧聊天回填队列 (${backPending} 待审)</button>
        </div>
        ${s.selectedProposalUids.length ? `
          <div style="display:flex;gap:8px;align-items:center">
            <button type="button" class="btn btn-ok btn-sm" data-act="batch-approve" ${w ? "" : "disabled"}>批量通过已选 (${s.selectedProposalUids.length})</button>
            <button type="button" class="btn btn-danger btn-sm" data-act="batch-reject-open" ${w ? "" : "disabled"}>批量驳回 (${s.selectedProposalUids.length})</button>
          </div>
        ` : ""}
      </div>

      <div class="split-workspace">
        <div class="list-panel">
          <div class="list-head">
            <strong>待审与近期处理清单 (${list.length})</strong>
            <span class="muted" style="font-size:12.5px">点击条目查看详情</span>
          </div>
          <div class="list-scroll">
            ${list.length === 0 ? `<div style="padding:28px" class="muted">当前队列暂无条目。</div>` : list.map(function (p) {
              const checked = s.selectedProposalUids.indexOf(p.uid) >= 0;
              const isAct = sel && p.uid === sel.uid;
              const stTag = p.status === "approved" ? '<span class="badge badge-ok">已入库</span>'
                : p.status === "rejected" ? '<span class="badge badge-danger">已驳回</span>'
                : '<span class="badge badge-warn">待审阅</span>';
              return `<div class="list-entry ${isAct ? "active" : ""}" data-act="select-proposal" data-uid="${esc(p.uid)}">
                <div class="list-entry-meta">
                  <span style="display:flex;align-items:center;gap:6px">
                    ${p.status === "pending" ? `<input type="checkbox" class="prop-check" data-uid="${esc(p.uid)}" ${checked ? "checked" : ""} ${w ? "" : "disabled"} />` : ""}
                    <span class="badge">${esc(p.category)}</span>
                    ${stTag}
                  </span>
                  <span>${esc(E.KIND_ZH[p.kind] || p.kind)}</span>
                </div>
                <div class="list-entry-title">${esc(p.problem)}</div>
              </div>`;
            }).join("")}
          </div>
        </div>

        <section class="detail-panel" aria-label="审阅详情阅读区">
          ${!sel ? `<p class="muted">请从左侧队列选择一条待审阅提议。</p>` : `
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
              <div class="tag-row">
                <span class="badge badge-brand">${esc(sel.category)}</span>
                <span class="badge">${esc(E.KIND_ZH[sel.kind] || sel.kind)}</span>
                ${sel.status === "approved" ? `<span class="badge badge-ok">已签发入库</span>`
                  : sel.status === "rejected" ? `<span class="badge badge-danger">已驳回 · ${esc(sel.rejectReason)}</span>`
                  : `<span class="badge badge-warn">等待人工裁决</span>`}
              </div>
              <span class="muted" style="font-size:13px">${sel.track === "live" ? "新会话增量提炼" : "旧聊天夜间回填"}</span>
            </div>

            ${sel.l0Warnings.length ? `<div class="banner banner-danger">质量守卫告警：该条目存在过程性流水账风险或原文证据不可回查，建议仔细核实或驳回。</div>` : ""}

            ${hideGate
              ? `<div class="banner banner-info"><span>🔒 <b>盲审保护已开启：</b>为防止先入为主，系统自动评估结论将在您点击「通过」或「驳回」后揭晓。</span></div>`
              : `<div class="banner ${sel.gateVerdict === "pass" ? "badge-ok" : "banner-danger"}" style="border-width:1px">
                  <span>系统质量闸门评估：<b>${sel.gateVerdict === "pass" ? "建议保留入库" : "建议拦截拒绝"}</b>（置信度 ${(sel.confidence * 100).toFixed(0)}%）</span>
                </div>`}

            ${isEdit ? `
              <div style="display:flex;flex-direction:column;gap:12px">
                <label><div><strong>核心问题描述</strong></div><input type="text" id="edit-prob-${esc(sel.uid)}" maxlength="120" style="width:100%" value="${esc(sel.problem)}" /></label>
                <label><div><strong>验证解决办法</strong></div><textarea id="edit-sol-${esc(sel.uid)}" rows="3" maxlength="240" style="width:100%">${esc(sel.solution)}</textarea></label>
                <label><div><strong>可复用经验准则</strong></div><input type="text" id="edit-reus-${esc(sel.uid)}" maxlength="120" style="width:100%" value="${esc(sel.reusable)}" /></label>
                <div style="display:flex;gap:10px">
                  <button type="button" class="btn btn-primary" data-act="save-edit" data-uid="${esc(sel.uid)}">保存修订</button>
                  <button type="button" class="btn" data-act="cancel-edit">取消</button>
                </div>
              </div>
            ` : `
              <div class="prose-section">
                <h3>问题与触发场景</h3>
                <h2 class="prose-lead">${esc(sel.problem)}</h2>
              </div>
              <div class="prose-section">
                <h3>已验证解决办法</h3>
                <p class="prose-body">${esc(sel.solution)}</p>
              </div>
              <div class="callout-box">
                <div style="font-size:12.5px;font-weight:700;color:var(--brand);margin-bottom:4px">可复用经验准则（将在相似场景主动提示）</div>
                <div>${esc(sel.reusable)}</div>
              </div>
              ${sel.misbelief ? `<div class="prose-section"><h3>常见认知误区</h3><p class="prose-body secondary">${esc(sel.misbelief)}</p></div>` : ""}
              <div class="tag-row"><span class="muted" style="font-size:13px">适用范围：</span>${E.scopeTags(sel.scope)}</div>
            `}

            ${sel.status === "pending" && !isEdit ? `
              <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
                <button type="button" class="btn btn-ok" data-act="approve-prop" data-uid="${esc(sel.uid)}" ${w ? "" : "disabled"}>✓ 通过并正式入库</button>
                <button type="button" class="btn" data-act="start-edit" data-uid="${esc(sel.uid)}" ${w ? "" : "disabled"}>✎ 编辑措辞</button>
                <button type="button" class="btn btn-danger" data-act="toggle-reject-box" ${w ? "" : "disabled"}>✕ 驳回提议 ▾</button>
              </div>
              ${s.rejectBoxOpen && w ? `
                <div style="padding:14px;border-radius:11px;background:var(--bg-subtle);border:1px solid var(--danger-border);display:flex;flex-direction:column;gap:8px">
                  <div style="font-size:13px;font-weight:700;color:var(--danger)">请选择驳回原因（计入系统自动校准统计）：</div>
                  <div class="tag-row">
                    ${E.REJECT_REASONS.map(function (r) {
                      return `<button type="button" class="btn btn-sm" data-act="reject-prop" data-uid="${esc(sel.uid)}" data-reason="${esc(r[0])}">${esc(r[1])}</button>`;
                    }).join("")}
                  </div>
                </div>
              ` : ""}
            ` : ""}

            <div class="l3-bar">
              <span class="muted" style="font-size:13px">需要核对原始会话或闸门底层日志？</span>
              <div style="display:flex;gap:8px;flex-wrap:wrap">
                <button type="button" class="btn btn-sm" data-act="open-evidence" data-uid="${esc(sel.uid)}">📄 查看原文对话证据 (L3)</button>
                <button type="button" class="btn btn-sm" data-act="open-prop-meta" data-uid="${esc(sel.uid)}">🔬 查看闸门与技术明细 (L3)</button>
              </div>
            </div>
          `}
        </section>
      </div>`;
  };

  E.views.experiences = function () {
    const s = E.state;
    if (s.expSubtab === "pipelines") return E.views.pipelines();
    if (s.expSubtab === "interceptions" && E.renderInterceptionsView) return E.renderInterceptionsView();
    const w = E.canWrite();

    if (s.expSubtab === "rollback") {
      const models = ["deepseek-chat", "deepseek-v4-flash", "gpt-5.4", "unknown-experimental", "manual_web"];
      const rb = s.rollbackState;
      return `
        ${E.renderExpSubtabs("rollback")}
        <section class="panel" style="max-width:860px">
          <div class="panel-head">
            <div>
              <h2>按模型两步安全回滚</h2>
              <p>若某个推理模型产生批量低质写入，可先通过干跑预览精确确认受影响范围，再一键撤销恢复原状。</p>
            </div>
            <span class="badge badge-warn">两步安全锁</span>
          </div>
          <div class="filter-bar">
            <label style="flex:1;min-width:240px">
              <span class="muted" style="font-size:13px;display:block;margin-bottom:4px">选择需要核查撤销的来源模型</span>
              <select id="rb-model-sel" style="width:100%" ${w ? "" : "disabled"}>
                ${models.map(function (m) { return `<option value="${esc(m)}" ${rb.model === m ? "selected" : ""}>${esc(m)}</option>`; }).join("")}
              </select>
            </label>
            <div style="display:flex;gap:10px;align-self:flex-end">
              <button type="button" class="btn btn-primary" data-act="rb-dry-run" ${w ? "" : "disabled"}>1. 演练预览影响范围</button>
              <button type="button" class="btn btn-danger" data-act="rb-apply" ${w && rb.previewed && rb.affected.length ? "" : "disabled"}>2. 确认执行回滚 (${rb.affected.length} 条)</button>
            </div>
          </div>
          ${rb.previewed ? `
            <div style="padding:16px;border-radius:11px;background:var(--bg-subtle);border:1px solid var(--line);display:flex;flex-direction:column;gap:8px">
              <strong>演练结果：共涉及 ${rb.affected.length} 条活跃经验</strong>
              ${rb.affected.length === 0 ? `<div class="muted">模型 ${esc(rb.model)} 当前没有活跃生效的可回滚经验。</div>`
                : rb.affected.map(function (a) {
                  return `<div style="padding:8px 12px;border-radius:8px;background:var(--surface-solid);border:1px solid var(--line)">
                    <span class="badge">${esc(a.category)}</span> &nbsp;<b>${esc(a.problem)}</b>
                  </div>`;
                }).join("")}
            </div>
          ` : `<div class="muted" style="font-size:13.5px">请先选择来源模型并点击「1. 演练预览影响范围」，解锁确认回滚按钮。</div>`}
        </section>`;
    }

    const f = s.expFilters;
    const basePool = s.experiences.filter(function (x) { return w || x.status === "active"; });
    const catCounts = {
      all: basePool.length,
      前端经验: basePool.filter(function (x) { return x.category === "前端经验"; }).length,
      业务经验: basePool.filter(function (x) { return x.category === "业务经验"; }).length,
      系统环境经验: basePool.filter(function (x) { return x.category === "系统环境经验"; }).length
    };
    const effectiveStatus = w ? f.status : "active";
    const visible = basePool.filter(function (x) {
      if (f.category !== "all" && x.category !== f.category) return false;
      if (f.origin !== "all" && x.origin !== f.origin) return false;
      if (effectiveStatus !== "all" && x.status !== effectiveStatus) return false;
      if (f.q) {
        const q = f.q.toLowerCase();
        if ((x.problem + x.solution + x.reusable).toLowerCase().indexOf(q) < 0) return false;
      }
      return true;
    });
    const sel = visible.find(function (x) { return x.id === s.selectedExpId; }) || visible[0] || null;
    if (sel) s.selectedExpId = sel.id;

    return `
      ${E.renderExpSubtabs("list")}
      <div class="cat-pills">
        ${[["all", "全部分类"], ["前端经验", "前端经验"], ["业务经验", "业务经验"], ["系统环境经验", "系统环境经验"]].map(function (c) {
          return `<button type="button" class="cat-pill ${f.category === c[0] ? "active" : ""}" data-act="exp-cat" data-val="${esc(c[0])}">
            <span>${esc(c[1])}</span><strong>${catCounts[c[0]]}</strong>
          </button>`;
        }).join("")}
      </div>

      <div class="panel" style="padding:14px 18px">
        <div class="filter-bar">
          <select id="exp-f-origin">
            <option value="all" ${f.origin === "all" ? "selected" : ""}>全部来源</option>
            <option value="manual" ${f.origin === "manual" ? "selected" : ""}>手工录入</option>
            <option value="legacy" ${f.origin === "legacy" ? "selected" : ""}>Stage 4 精华迁入</option>
            <option value="pipeline" ${f.origin === "pipeline" ? "selected" : ""}>新会话提炼</option>
            <option value="backfill" ${f.origin === "backfill" ? "selected" : ""}>旧聊天回填</option>
            <option value="agent" ${f.origin === "agent" ? "selected" : ""}>Agent 提议</option>
          </select>
          <select id="exp-f-status" ${w ? "" : "disabled"}>
            <option value="active" ${effectiveStatus === "active" ? "selected" : ""}>生效中 (Active)</option>
            ${w ? `<option value="probation" ${effectiveStatus === "probation" ? "selected" : ""}>试用观察 (Probation)</option>
            <option value="retired" ${effectiveStatus === "retired" ? "selected" : ""}>已退役 (Retired)</option>
            <option value="all" ${effectiveStatus === "all" ? "selected" : ""}>全部状态</option>` : ""}
          </select>
          <input type="search" id="exp-f-q" placeholder="搜索经验问题、方案或准则…" value="${esc(f.q)}" style="flex:1;min-width:220px" />
          <span class="badge">当前命中 ${visible.length} 条</span>
        </div>
      </div>

      <div class="split-workspace">
        <div class="list-panel">
          <div class="list-head"><strong>经验列表 (${visible.length})</strong><span class="muted" style="font-size:12.5px">三分类清爽视图</span></div>
          <div class="list-scroll">
            ${visible.length === 0 ? `<div style="padding:28px" class="muted">没有符合当前筛选条件的经验。</div>` : visible.map(function (x) {
              const stCls = x.status === "active" ? "badge-ok" : x.status === "probation" ? "badge-warn" : "badge-danger";
              return `<button type="button" class="list-entry ${sel && sel.id === x.id ? "active" : ""}" data-act="select-exp" data-id="${esc(x.id)}">
                <div class="list-entry-meta">
                  <span style="display:flex;gap:6px"><span class="badge">${esc(x.category)}</span><span class="badge ${stCls}">${esc(E.STATUS_ZH[x.status] || x.status)}</span></span>
                  <span>${esc(x.updatedAt)}</span>
                </div>
                <div class="list-entry-title">${esc(x.problem)}</div>
              </button>`;
            }).join("")}
          </div>
        </div>

        <section class="detail-panel" aria-label="经验详情阅读区">
          ${!sel ? `<p class="muted">请从左侧列表选择一条经验进行阅读。</p>` : `
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
              <div class="tag-row">
                <span class="badge badge-brand">${esc(sel.category)}</span>
                <span class="badge">${esc(E.ORIGIN_ZH[sel.origin] || sel.origin)}</span>
                <span class="badge ${sel.status === "active" ? "badge-ok" : sel.status === "probation" ? "badge-warn" : ""}">${esc(E.STATUS_ZH[sel.status] || sel.status)}</span>
              </div>
              ${sel.status !== "retired"
                ? `<button type="button" class="btn btn-danger btn-sm" data-act="trash-exp" data-id="${esc(sel.id)}" ${w ? "" : "disabled"}>退役 / 撤销经验</button>`
                : `<button type="button" class="btn btn-ok btn-sm" data-act="restore-exp" data-id="${esc(sel.id)}" ${w ? "" : "disabled"}>恢复为生效状态</button>`}
            </div>
            <div class="prose-section">
              <h3>遇到的核心问题</h3>
              <h2 class="prose-lead">${esc(sel.problem)}</h2>
            </div>
            <div class="prose-section">
              <h3>已验证解决办法</h3>
              <p class="prose-body">${esc(sel.solution)}</p>
            </div>
            <div class="callout-box">
              <div style="font-size:12.5px;font-weight:700;color:var(--brand);margin-bottom:4px">可复用原则</div>
              <div>${esc(sel.reusable)}</div>
            </div>
            ${sel.misbelief ? `<div class="prose-section"><h3>常见误区对照</h3><p class="prose-body secondary">${esc(sel.misbelief)}</p></div>` : ""}
            <div class="tag-row"><span class="muted" style="font-size:13px">适用场景：</span>${E.scopeTags(sel.scope)}</div>
            <div class="l3-bar">
              <span class="muted" style="font-size:13px">稳定度、Beta 置信度与数据库 ID 已收纳入三级溯源面板</span>
              <button type="button" class="btn btn-sm" data-act="open-exp-meta" data-id="${esc(sel.id)}">🔬 查看进化参数与溯源元数据 (L3)</button>
            </div>
          `}
        </section>
      </div>`;
  };
})(typeof window !== "undefined" ? window : globalThis);

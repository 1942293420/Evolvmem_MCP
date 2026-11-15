/* EvolvMem Web Console v2 — L3 Deep Inspection Drawers & Owner Rescue Modal */
(function (root) {
  const E = (root.EvolvMemV2 = root.EvolvMemV2 || {});
  const esc = E.esc || function (v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };

  const GATE_STAGES = ["Moments", "Extract", "Quota", "L0", "L1", "L2a", "L2b", "L3", "L4", "Writer"];

  function byId(id) {
    return typeof root.document !== "undefined" ? root.document.getElementById(id) : null;
  }

  function kvCell(k, v, mono) {
    return `<div class="kv-cell"><div class="kv-k">${k}</div><div class="kv-v ${mono ? "mono" : ""}">${v}</div></div>`;
  }

  function openD(title, bodyHtml, sub) {
    if (E.openDrawer) return E.openDrawer(title, bodyHtml, sub);
  }

  function formatRemain(sec) {
    const n = Math.max(0, Number(sec) || 0);
    const h = Math.floor(n / 3600);
    const m = Math.floor((n % 3600) / 60);
    const s = n % 60;
    return h + "h " + m + "m " + s + "s";
  }

  function renderGateStepper(chain, finalLayer, isRescued) {
    const byLayer = {};
    (chain || []).forEach(function (step) {
      if (step && step.layer) byLayer[step.layer] = step;
    });
    const hitIdx = GATE_STAGES.indexOf(finalLayer);
    return `
      <div class="prose-section"><h3>全链路门控步进链 (Gate Stepper)</h3></div>
      <div class="gate-stepper">
        ${GATE_STAGES.map(function (st, idx) {
          const step = byLayer[st];
          let cls = "skip";
          let label = "未触达";
          let detail = "";
          if (step) {
            const v = String(step.verdict || "").toLowerCase();
            if (v === "pass" || v === "accept" || v === "write" || v === "rescued") {
              cls = "pass";
              label = "通过";
            } else if (v === "merge" || v === "downgrade" || v === "trial" || v === "shadow" || v === "warn") {
              cls = "warn";
              label = step.verdict;
            } else {
              cls = "fail";
              label = step.verdict || "拦截";
            }
            detail = [step.reason_code, step.judge_model, step.latency_ms != null ? step.latency_ms + "ms" : ""]
              .filter(Boolean).join(" · ");
          } else if (finalLayer === st) {
            cls = (st === "L1" || st === "L3") ? "warn" : "fail";
            label = "在此拦截";
          } else if (isRescued && st === "Writer") {
            cls = "pass";
            label = "救回入库";
          } else if (hitIdx > idx) {
            cls = "pass";
            label = "已通过";
          }
          return `<div class="gate-step ${cls}">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
              <strong>${esc(st)}</strong>
              <span class="badge ${cls === "pass" ? "badge-ok" : cls === "fail" ? "badge-danger" : cls === "warn" ? "badge-warn" : ""}">${esc(label)}</span>
            </div>
            ${detail ? `<div class="muted mono" style="font-size:11.5px;margin-top:4px">${esc(detail)}</div>` : ""}
          </div>`;
        }).join("")}
      </div>`;
  }

  function renderLlmCallsTable(calls) {
    if (!calls || !calls.length) {
      return `<div class="muted" style="font-size:13px">该记录未触发 LLM 调用或为 0-LLM 规则拦截（0 Token）。</div>`;
    }
    return `<div class="table-wrap"><table>
      <thead><tr><th>阶段</th><th>模型</th><th>In/Out Tok</th><th>成本</th><th>结果</th></tr></thead>
      <tbody>${calls.map(function (c) {
        const cost = c.cost_estimate != null ? "$" + Number(c.cost_estimate).toFixed(5) : "-";
        const toks = (c.input_tokens || 0) + "/" + (c.output_tokens || 0);
        return `<tr><td>${esc(c.stage || "-")} (${esc(c.track || "live")})</td><td class="mono">${esc(c.model || "-")}</td><td class="mono">${esc(toks)}</td><td class="mono">${esc(cost)}</td><td>${esc(c.outcome || "-")}${c.is_shadow ? " [Shadow]" : ""}</td></tr>`;
      }).join("")}</tbody></table></div>`;
  }

  E.openDiagDrawer = function () {
    const s = E.state;
    openD("系统诊断、底层遥测与场景演练", `
      <div class="kv-grid">
        ${kvCell("API 运行模式", s.apiMode === "live" ? "Live HTTP (/api/console-v2/*)" : "离线可交互预览沙盒", true)}
        ${kvCell("当前主库视角", esc(s.libraryCtx) + (E.canWrite() ? " (本人可读写)" : " (他人只读)"), true)}
        ${kvCell("LAN MCP 服务", "&lt;lan-mcp-host&gt;:9378 [ONLINE]", true)}
        ${kvCell("向量与主库引擎", "PG 17.10 + pgvector 0.8.2 (HNSW 768d)", true)}
        ${kvCell("旧版提炼策略锁", "ai_processing_enabled=false [LOCKED]", true)}
        ${kvCell("SQLite 运行句柄", "0 (7/7 @ 0400 RETIRED)", true)}
        ${kvCell("集成代码版本", "evolution/integration @ d5a77b5", true)}
        ${kvCell("8h 会话有效期", `<span id="diag-sess-remain">${formatRemain(s.sessionRemainingSec)}</span> (非滑动)`, true)}
      </div>
      <div class="callout-box">
        <div style="font-weight:700;margin-bottom:6px">能力实现成熟度与阶段筛选</div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px;font-size:13px">
          <span><span class="dot dot-ok"></span> 已上线 HTTP API</span>
          <span><span class="dot" style="background:var(--brand)"></span> Stage 1–5 引擎就绪</span>
          <span><span class="dot dot-plan"></span> Stage 6–7 规划预置契约</span>
        </div>
        <select id="stage-filter-sel" style="width:100%">
          <option value="all" ${s.stageFilter === "all" ? "selected" : ""}>显示全部阶段模块 (Stage 1–7 + BACK)</option>
          <option value="1-5" ${s.stageFilter === "1-5" ? "selected" : ""}>高亮已实现与引擎就绪 (Stage 1–5)</option>
          <option value="6-7" ${s.stageFilter === "6-7" ? "selected" : ""}>高亮规划预置进化交付 (Stage 6–7)</option>
        </select>
      </div>
      <div>
        <div style="font-weight:700;font-size:13.5px;margin-bottom:8px">异常态与鉴权场景演练</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button type="button" class="btn btn-sm ${s.simMode === "normal" ? "btn-primary" : ""}" data-act="set-sim" data-mode="normal">正常运行 (PG 健康)</button>
          <button type="button" class="btn btn-sm ${s.simMode === "sso_login" ? "btn-primary" : ""}" data-act="set-sim" data-mode="sso_login">演练 飞书 SSO 登录页</button>
          <button type="button" class="btn btn-sm ${s.simMode === "pg_503" ? "btn-danger" : ""}" data-act="set-sim" data-mode="pg_503">演练 PG 503 Fail-Closed</button>
          <button type="button" class="btn btn-sm" data-act="reset-demo">↺ 重置演示数据</button>
        </div>
      </div>`, "三级诊断 · 全局运维与安全边界");
  };

  function renderExpTraceHtml(x, tr) {
    const t = tr || {};
    const evs = t.events || x.events || [];
    const rels = t.relations || x.relations || [];
    const imps = t.impressions || [];
    const trials = t.trials || [];
    const chainObj = t.gate_chain || x.gate_chain;
    const chain = Array.isArray(chainObj) ? chainObj : ((chainObj && chainObj.decisions) || []);
    const calls = t.llm_calls || (chainObj && chainObj.llm_calls) || [];
    const rescued = x.origin === "manual_rescue" || evs.some(function (e) {
      return e.event_type === "manual_rescue" || e.action === "manual_rescue" || String(e.proposal_uid || "").indexOf("rescue") >= 0;
    });
    return `
      <div class="kv-grid">
        ${kvCell("经验 ID", esc(x.id), true)}${kvCell("版本号 / 状态", "rev." + x.revision + " · " + esc(x.status), true)}
        ${kvCell("经验分类/类型", esc(x.category + " / " + x.kind), false)}${kvCell("来源渠道 / 模型", esc(x.origin + " · " + x.model), true)}
        ${kvCell("证据支撑次数", x.evidenceCount + " 次", true)}${kvCell("Beta(α, β) 分布", "(" + x.betaA + ", " + x.betaB + ")", true)}
      </div>
      ${chain.length ? renderGateStepper(chain, "Writer", rescued) : `<div class="muted" style="font-size:13px">${t._loading ? "正在读取 PG 完整 L0–L4 门控链与历史事件…" : "手动写入或他人只读视角不展示私有提议门控细节。"}</div>`}
      <div class="prose-section" style="margin-top:10px"><h3>生命周期事件轨迹 (${evs.length})</h3></div>
      ${evs.length ? evs.map(function (ev) {
        const actLabel = ev.action || ev.event_type || "-";
        const actorLabel = ev.actor_id || ev.actor || "";
        const detLabel = ev.detail || [ev.agent_client, ev.model, ev.proposal_uid].filter(Boolean).join(" · ");
        return `<div class="callout-box" style="margin-bottom:6px"><b>${esc(actLabel)}</b> <span class="muted mono">${esc((ev.created_at || "").slice(0, 19))} · ${esc(actorLabel)}</span>${detLabel ? `<div class="secondary">${esc(detLabel)}</div>` : ""}</div>`;
      }).join("") : `<div class="muted" style="font-size:13px">暂无公开事件记录（检索次数：${imps.length}）。</div>`}
      ${trials.length ? `<div class="prose-section"><h3>Challenger 试用观察 (${trials.length})</h3>${trials.map(function (trItem) {
        return `<div class="kv-row"><span>${esc(trItem.relation)} (${esc(trItem.status)})</span><span class="mono">${trItem.success_count || 0}/${trItem.required_successes || 2}</span></div>`;
      }).join("")}</div>` : ""}
      ${calls.length ? `<div class="prose-section"><h3>关联 LLM 消耗 (${calls.length})</h3>${renderLlmCallsTable(calls)}</div>` : ""}
      ${rels.length ? `<div class="prose-section"><h3>图谱关系 (${rels.length})</h3>${rels.map(function (r) {
        return `<div class="kv-row"><span>${esc(r.relation)} (${esc(r.from_uid)} → ${esc(r.to_uid)})</span><span class="muted">${esc(r.reason || "")}</span></div>`;
      }).join("")}</div>` : ""}
      <div><div class="muted" style="font-size:12.5px;margin-bottom:4px">适用范围 scope JSON：</div><div class="codebox">${esc(JSON.stringify(x.scope || {}, null, 2))}</div></div>`;
  }

  function renderIntTraceHtml(it, td) {
    const d = td || {};
    const chain = d.gate_chain || it.gate_chain || [];
    const calls = d.llm_calls || it.llm_calls || [];
    const meta = d.meta_json || it.meta_json || it.diagnostic || {};
    return `
      <div class="tag-row">
        <span class="badge ${it.rescued_note_id ? "badge-ok" : "badge-danger"}">拦截层：${esc(it.layer)}</span>
        <span class="badge">${esc(it.reason_code)}</span>
        <span class="badge">${esc(it.track || "live")}</span>
      </div>
      ${renderGateStepper(chain, it.layer, !!it.rescued_note_id)}
      <div class="prose-section" style="margin-top:10px"><h3>LLM Token 与调用明细 (${calls.length})</h3>${renderLlmCallsTable(calls)}</div>
      <div class="prose-section" style="margin-top:10px"><h3>完整诊断元数据 (meta_json)</h3><div class="codebox">${esc(JSON.stringify(meta, null, 2))}</div></div>`;
  }

  function openEvidenceBox(ref, quote) {
    const renderBody = function (msgs, loading) {
      return `
        <div class="kv-grid">${kvCell("归档引用指针", esc(ref || "archive"), true)}${kvCell("LLM 调用", "0 次（直读 PG）", false)}</div>
        ${quote ? `<div class="callout-box"><b>锚点摘录：</b>${esc(quote)}</div>` : ""}
        ${msgs && msgs.length ? msgs.map(function (m) {
          const idx = m.turn_index != null ? m.turn_index : m.event_index;
          const role = m.role || m.speaker || "turn";
          const txt = m.content != null ? m.content : (m.text || "");
          return `<div class="callout-box" style="margin-bottom:6px;${m.is_anchor ? "border-left-color:var(--brand)" : ""}">
            <div class="muted mono" style="font-size:11.5px">#${esc(idx)} · ${esc(role)} ${m.is_anchor ? "★ 锚点" : ""}</div>
            <div style="white-space:pre-wrap">${esc(txt)}</div>
          </div>`;
        }).join("") : `<div class="muted" style="font-size:13px">${loading ? "正在从 PG em_history 读取会话窗口…" : (quote ? "演示快照已展示锚点原文。" : "暂无更多上下文消息。")}</div>`}`;
    };
    openD("历史会话原文证据 · " + (ref || "archive"), renderBody([], E.state.apiMode === "live"), "三级溯源 · 0-LLM 原文检索");
    if (E.state.apiMode === "live" && ref && E.api && E.api.fetchEvidenceContext) {
      E.api.fetchEvidenceContext(ref).then(function (data) {
        const b = byId("drawer-body");
        if (b && data) b.innerHTML = renderBody(data.turns || data.messages || [], false);
      });
    }
  }

  E.openRescueModal = function (it) {
    if (!it || !E.openModal) return;
    const cat = it.category || "系统环境经验";
    const body = `
      <div class="banner" style="margin-bottom:10px">被拦截层级：<b>${esc(it.layer)}</b>（${esc(it.reason_code)}）。修订后将签发一次性 AdmissionTicket 经 ExperienceWriter 入库。</div>
      <div style="display:flex;flex-direction:column;gap:8px">
        <label><div class="muted" style="font-size:12.5px">经验分类</div>
          <select id="rescue-cat" style="width:100%">
            <option value="系统环境经验" ${cat === "系统环境经验" ? "selected" : ""}>系统环境经验</option>
            <option value="业务经验" ${cat === "业务经验" ? "selected" : ""}>业务经验</option>
            <option value="前端经验" ${cat === "前端经验" ? "selected" : ""}>前端经验</option>
          </select>
        </label>
        <label><div class="muted" style="font-size:12.5px">Problem · 核心问题（≤120 字）</div><textarea id="rescue-prob" rows="2" style="width:100%">${esc(it.problem || it.title)}</textarea></label>
        <label><div class="muted" style="font-size:12.5px">Solution · 确定解法（≤240 字）</div><textarea id="rescue-sol" rows="3" style="width:100%">${esc(it.solution || "")}</textarea></label>
        <label><div class="muted" style="font-size:12.5px">Reusable Experience · 可复用原则（≤120 字）</div><textarea id="rescue-reus" rows="2" style="width:100%">${esc(it.reusable_experience || "")}</textarea></label>
        <label><div class="muted" style="font-size:12.5px">救回审计理由</div><input id="rescue-note" type="text" style="width:100%" value="${esc("Owner manual rescue from " + it.layer + " (" + it.reason_code + ")")}" /></label>
      </div>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:12px">
        <button type="button" class="btn" data-act="close-modal">取消</button>
        <button type="button" class="btn btn-primary" data-act="rescue-submit" data-id="${esc(it.id)}">确认救回并签发 Ticket 入库</button>
      </div>`;
    E.openModal("人工救回未写入经验 (" + it.id + ")", body);
  };

  E.openL3Drawer = function (act, ds) {
    const s = E.state;
    const d = ds || {};
    if (act === "open-diag") return E.openDiagDrawer();
    if (act === "open-int-l3" || act === "open-int-trace") {
      const it = (s.interceptions || []).find(function (x) { return x.id === (d.id || s.selectedIntId); });
      if (!it) return;
      openD("拦截门控步进链与元数据 · " + it.id, renderIntTraceHtml(it, null), it.layer + " · " + it.reason_code);
      if (s.apiMode === "live" && typeof root.fetch === "function") {
        root.fetch("/api/console-v2/interceptions/" + encodeURIComponent(it.id) + "/trace", { credentials: "same-origin" })
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (td) { const b = byId("drawer-body"); if (b && td && td.ok) b.innerHTML = renderIntTraceHtml(it, td.item || td); })
          .catch(function () {});
      }
      return;
    }
    if (act === "open-int-evidence") {
      const it = (s.interceptions || []).find(function (x) { return x.id === d.id; });
      const ev = it && it.evidence && it.evidence[0];
      return openEvidenceBox(ev ? ev.ref : (d.ref || ""), ev ? ev.quote : "");
    }
    if (act === "open-exp-meta" || act === "open-exp-l3" || act === "open-exp-trace" || act === "open-exp-history" || act === "open-exp-relations") {
      const x = s.experiences.find(function (i) { return i.id === (d.id || s.selectedExpId); });
      if (!x) return;
      openD("经验全链路追溯与关联图谱 · " + x.id, renderExpTraceHtml(x, { _loading: s.apiMode === "live" }), "三级溯源 · 正式库条目");
      if (s.apiMode === "live" && E.api && E.api.fetchExpTrace) {
        E.api.fetchExpTrace(x.id).then(function (tr) { const b = byId("drawer-body"); if (b && tr) b.innerHTML = renderExpTraceHtml(x, tr); });
      }
      return;
    }
    if (act === "open-evidence" || act === "open-prop-evidence" || act === "open-exp-evidence") {
      const p = d.uid ? s.proposals.find(function (x) { return x.uid === d.uid; }) : null;
      return openEvidenceBox(d.ref || (p && p.evidence && p.evidence.ref) || "", d.quote || (p && p.evidence && p.evidence.snippet) || "");
    }
    if (act === "open-prop-meta" || act === "open-prop-l3" || act === "open-prop-gate") {
      const p = s.proposals.find(function (x) { return x.uid === (d.uid || d.id); });
      if (!p) return;
      const blind = s.shadowMode && p.status === "pending";
      return openD("提议底层元数据与 L0–L4 闸门检验", `
        <div class="kv-grid">
          ${kvCell("Proposal UID", esc(p.uid), true)}${kvCell("所属轨道", esc(p.track), true)}
          ${kvCell("来源 Agent / 模型", esc(p.agent + " · " + p.model), true)}
          ${kvCell("系统闸门判断", blind ? "🔒 盲审保护中（提交裁决后揭晓）" : esc(p.gateLayer + " · " + (p.confidence * 100).toFixed(0) + "%"), false)}
        </div>
        ${!blind && p.gateChain ? renderGateStepper(p.gateChain, p.gateVerdict === "pass" ? "L4" : "L2b", false) : ""}
        ${p.l0Warnings && p.l0Warnings.length ? `<div class="banner banner-danger">L0 硬规则：${esc(p.l0Warnings.join("；"))}</div>` : ""}
        <div><div class="muted" style="font-size:12.5px;margin-bottom:4px">scope 约束 JSON：</div><div class="codebox">${esc(JSON.stringify(p.scope || {}, null, 2))}</div></div>`);
    }
    if (act === "open-agent-l3") {
      const a = s.agents.find(function (x) { return x.agent === d.name; }) || s.agents[0];
      return openD("客户端节点溯源 · " + a.title, `<div class="kv-grid">${kvCell("Agent ID", esc(a.agent), true)}${kvCell("模型", esc(a.model), true)}${kvCell("信誉/配额", esc(a.tier) + " / " + a.quota, true)}${kvCell("项目", esc(a.activeProject), true)}</div><div class="callout-box">网关启用 _meta 伪造剥离；低通过率模型自动降级限额。</div>`);
    }
    if (act === "open-topo-l3") {
      const n = s.topoNodes.find(function (x) { return x.id === d.id; }) || s.topoNodes[0];
      return openD("系统架构节点 · " + n.title, `<div class="tag-row"><span class="badge">${esc(n.stage)}</span>${E.statusBadge(n.status)}</div><p class="prose-body">${esc(n.detail)}</p><div class="kv-grid">${kvCell("实测遥测", esc(n.metrics), true)}${kvCell("目标视图", "#" + esc(n.target), true)}</div><div><button type="button" class="btn btn-primary" data-act="goto-module" data-route="${esc(n.target)}" data-sub="${esc(n.sub || "")}">前往管理模块 →</button></div>`, "三级溯源 · 系统总设计框架");
    }
    if (act === "open-changes-l3") {
      return openD("experience_changes_since 增量游标流", `<div class="kv-grid">${kvCell("同步游标", "cursor #" + s.changesCursor, true)}${kvCell("机制", "PG LISTEN / NOTIFY", true)}</div><div class="table-wrap"><table><thead><tr><th>Seq</th><th>Actor</th><th>Model</th><th>Note ID</th><th>Action</th></tr></thead><tbody>${s.changesStream.map(function (c) { return `<tr><td class="mono">#${c.seq}</td><td class="mono">${esc(c.actor)}</td><td class="mono">${esc(c.model)}</td><td class="mono">${esc(c.noteId)}</td><td>${esc(c.action)}</td></tr>`; }).join("")}</tbody></table></div>`);
    }
    if (act === "open-llm-ledger") {
      return openD("metered_llm() 全仓强制计量账本", `<div class="table-wrap"><table><thead><tr><th>ID</th><th>轨道</th><th>阶段</th><th>模型</th><th>In/Out</th><th>费用</th><th>结论</th></tr></thead><tbody>${s.llmCalls.map(function (c) { return `<tr><td class="mono">#${c.id}</td><td class="mono">${esc(c.track)}</td><td class="mono">${esc(c.stage)}</td><td class="mono">${esc(c.model)}</td><td class="mono">${c.inTok}/${c.outTok}</td><td class="mono">$${c.cost.toFixed(5)}</td><td>${esc(c.outcome)}</td></tr>`; }).join("")}</tbody></table></div>`);
    }
    if (act === "open-stage4-l3") {
      return openD("Stage 4 只读清洗与冷备档案", `<div class="kv-grid">${kvCell("批次", "stage4-legacy-clean-20261114", true)}${kvCell("保留与误删率", "9,843 评估 -> 707 合并 · 误删率 4.0%", true)}${kvCell("冷备包", "9,134 条 .jsonl.gz (Linux 0600)", true)}</div>`);
    }
    if (act === "open-hist-meta") {
      const b = s.historyBlocks.find(function (i) { return i.id === d.id; });
      if (b) return openD("历史会话检索评分详情", `<div class="kv-grid">${kvCell("逻辑库", esc(s.histDb), true)}${kvCell("归档指针", esc(b.sourceRef), true)}${kvCell("RRF 总分", String(b.rrf), true)}${kvCell("BM25/trgm/HNSW", b.bm25 + "/" + b.trgm + "/" + b.vector, true)}</div>`);
    }
    if (act === "open-capture-l3") {
      return openD("跨端捕获与 sync_cursors 日志", `<div class="kv-grid">${kvCell("Codex / DSH", "LastTaskResult=0 · 2/2 ack & 74/74 confirmed", true)}${kvCell("回填锁", "FOR UPDATE OF u SKIP LOCKED · llm_calls=0", true)}</div>`);
    }
    if (act === "open-impressions-l3") {
      return openD("retrieval_impressions 曝光归因明细", `<div class="table-wrap"><table><thead><tr><th>ID</th><th>入口</th><th>绑定经验</th><th>0-token 信号</th><th>Beta 变化</th></tr></thead><tbody>${s.impressions.map(function (i) { return `<tr><td class="mono">${esc(i.id)}</td><td>${esc(i.surface)}</td><td class="mono">${esc(i.noteId)}</td><td>${esc(i.signal)}</td><td class="mono">${esc(i.deltaBeta)}</td></tr>`; }).join("")}</tbody></table></div>`);
    }
    if (act === "open-handbook-l3") {
      const sel = s.handbookBlocks.filter(function (b) { return b.selected; });
      return openD("常驻手册编译源码 · " + s.handbookTarget, `<div class="codebox">${esc("<!-- EVOLVMEM:MANAGED:START -->\n" + sel.map(function (b) { return b.body; }).join("\n") + "\n<!-- EVOLVMEM:MANAGED:END -->")}</div>`);
    }
    if (act === "open-errsig-l3") {
      const m = E.matchPostToolUse(s.errorInput);
      return openD("PostToolUse 报错签名详情", `<div class="kv-grid">${kvCell("归一化 Key", esc(m.sig), true)}${kvCell("匹配 ID", esc(m.note ? m.note.id : "无"), true)}</div><div class="codebox">${esc(s.errorInput)}</div>`);
    }
    if (act === "open-skill") {
      const k = s.skills.find(function (i) { return i.id === d.id; });
      if (k) return openD("SKILL.md 原文 · " + k.name, `<div class="codebox">${esc(k.mdPreview)}</div>`);
    }
    if (act === "open-rep-l3") {
      return openD("四级模型信誉与防冒充规则", `<div class="callout-box">_meta 剥离 · user &gt; primary (10-12) &gt; general (4) &gt; judge · 通过率 &lt;30% 触发降级。</div>`);
    }
    if (act === "open-dr-l3") {
      return openD("PG 9 逻辑库与 :55435 灾备回执", `<div class="table-wrap"><table><thead><tr><th>物理库名</th><th>表数</th><th>职责</th></tr></thead><tbody>${s.pgDatabases.map(function (x) { return `<tr><td class="mono">${esc(x.name)}</td><td class="mono">${x.tables}</td><td>${esc(x.role)}</td></tr>`; }).join("")}</tbody></table></div>`);
    }
    if (act === "open-policy-l3") {
      return openD("退役 SQLite 路径与 Policy 键值", `<div class="codebox">${esc((s.retiredSqlite || []).join("\n"))}</div><div class="table-wrap"><table><thead><tr><th>键</th><th>值</th><th>说明</th></tr></thead><tbody>${s.policyLocks.map(function (p) { return `<tr><td class="mono">${esc(p.key)}</td><td class="mono">${String(p.val)}</td><td>${esc(p.desc)}</td></tr>`; }).join("")}</tbody></table></div>`);
    }
  };
})(typeof window !== "undefined" ? window : globalThis);

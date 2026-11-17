/* EvolvMem Web Console v2 — L3 Deep Inspection Drawers & Owner Rescue Modal */
(function (root) {
  const E = (root.EvolvMemV2 = root.EvolvMemV2 || {});
  const esc = E.esc || function (v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  };

  const GATE_STAGES = ["Moments", "Extract", "Quota", "L0", "L1", "L2a", "L2b", "L3", "L4", "Writer"];
  let requestEpoch = 0;

  function byId(id) {
    return typeof root.document !== "undefined" ? root.document.getElementById(id) : null;
  }
  function currentResponse(){
    const ticket=E.session?E.session.capture():null;
    return ()=>!ticket || (E.session && E.session.isCurrent(ticket));
  }

  function kvCell(k, v, mono) {
    return `<div class="kv-cell"><div class="kv-k">${k}</div><div class="kv-v ${mono ? "mono" : ""}">${v}</div></div>`;
  }

  function openD(title, bodyHtml, sub) {
    requestEpoch += 1;
    if (!E.isProduction()) bodyHtml = '<div class="banner banner-warn">离线演示：内容为示例，不代表现网数据。</div>' + bodyHtml;
    if (E.openDrawer) return E.openDrawer(title, bodyHtml, sub);
  }

  function drawerToken() {
    return {request: requestEpoch, drawer: E.drawerEpoch || 0, state: E.state,
      library: E.state.libraryCtx, libraryEpoch: E.state.libraryEpoch || 0,
      sessionCurrent: currentResponse()};
  }

  function writeDrawer(token, html) {
    if (!token.sessionCurrent() || token.request !== requestEpoch || token.drawer !== (E.drawerEpoch || 0) ||
        token.state !== E.state || token.library !== E.state.libraryCtx ||
        token.libraryEpoch !== (E.state.libraryEpoch || 0)) return;
    const body = byId("drawer-body");
    if (body) body.innerHTML = html;
  }

  function unavailable(title, detail) {
    return openD(title, E.unavailablePanel("尚未接入 / 未核验", detail), "三级诊断 · 实际数据边界");
  }

  function actualJson(value, missing) {
    return value == null ? `<p class="muted">${esc(missing || "尚未成功读取接口，未核验。")}</p>` : `<div class="codebox">${esc(JSON.stringify(value, null, 2))}</div>`;
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
          let label = E.isProduction() ? "未记录" : "未触达";
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
            const stepMeta = (step.meta && typeof step.meta === "object") ? step.meta : {};
            const predPreview = (st === "L2a" || st === "L2b") && stepMeta.model_prediction
              ? String(stepMeta.model_prediction) : "";
            return `<div class="gate-step ${cls}">
              <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
                <strong>${esc(st)}</strong>
                <span class="badge ${cls === "pass" ? "badge-ok" : cls === "fail" ? "badge-danger" : cls === "warn" ? "badge-warn" : ""}">${esc(label)}</span>
              </div>
              ${detail ? `<div class="muted mono" style="font-size:11.5px;margin-top:4px">${esc(detail)}</div>` : ""}
              ${step.detail ? `<div class="secondary" style="font-size:12.5px;margin-top:4px">${esc(step.detail)}</div>` : ""}
              ${predPreview ? `<div class="muted" style="font-size:12px;margin-top:4px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">模型盲猜：${esc(predPreview)}</div>` : ""}
            </div>`;
          } else if (finalLayer === st) {
            cls = (st === "L1" || st === "L3") ? "warn" : "fail";
            label = "在此拦截";
          } else if (isRescued && st === "Writer") {
            cls = "pass";
            label = "救回入库";
          } else if (hitIdx > idx && !E.isProduction()) {
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
      if (E.isProduction()) return '<div class="muted">接口未返回关联调用明细；不能据此认定没有调用或消耗为零。</div>';
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
    if (E.isProduction()) {
      const policy = (s.liveControl && s.liveControl.policy) || (s.livePipeline && s.livePipeline.policy);
      return openD("系统诊断与数据来源", `<div class="kv-grid">${kvCell("API 运行模式", esc(s.apiMode || "尚未连接"), true)}${kvCell("当前库视角", esc(s.libraryCtx || "尚未核验"), true)}</div>
        ${E.unavailablePanel("运行遥测未核验", "客户端未接入 LAN MCP 健康、SQLite 句柄、进程版本、数据库版本和会话到期时间的实时核验；这里不显示固定健康结论。")}
        <h3>已读取的策略配置</h3>${actualJson(policy)}
        ${E.plannedButton("生产场景演练与重置")}`, "三级诊断 · 不以演示替代运行状态");
    }
    openD("系统诊断、底层遥测与场景演练", `
      <div class="kv-grid">
        ${kvCell("API 运行模式", s.apiMode === "live" ? "Live HTTP (/api/console-v2/*)" : "离线可交互预览沙盒", true)}
        ${kvCell("当前主库视角", esc(s.libraryCtx) + (E.canWrite() ? " (本人可读写)" : " (他人只读)"), true)}
        ${kvCell("LAN MCP 服务", "&lt;lan-mcp-host&gt;:9378 [ONLINE]", true)}
        ${kvCell("向量与主库引擎", "PG 17.10 + pgvector 0.8.2 (HNSW 768d)", true)}
        ${kvCell("旧版提炼策略锁", "ai_processing_enabled=false [LOCKED]", true)}
        ${kvCell("SQLite 运行句柄", "0 (7/7 @ 0400 RETIRED)", true)}
        ${kvCell("集成代码版本", "live-v2 (PG 17.10)", true)}
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
        ${kvCell("证据支撑次数", x.evidenceCount == null ? "未返回" : x.evidenceCount + " 次", true)}${kvCell("Beta(α, β) 分布", E.isProduction() ? "效果归因尚未接入" : "(" + x.betaA + ", " + x.betaB + ")", true)}
      </div>
      ${chain.length ? renderGateStepper(chain, "Writer", rescued) : `<div class="muted" style="font-size:13px">${t._loading ? "正在读取 PG 完整 L0–L4 门控链与历史事件…" : "手动写入或他人只读视角不展示私有提议门控细节。"}</div>`}
      <div class="prose-section" style="margin-top:10px"><h3>生命周期事件轨迹 (${evs.length})</h3></div>
      ${evs.length ? evs.map(function (ev) {
        const actLabel = ev.action || ev.event_type || "-";
        const actorLabel = ev.actor_id || ev.actor || "";
        const detLabel = ev.detail || [ev.agent_client, ev.model, ev.proposal_uid].filter(Boolean).join(" · ");
        return `<div class="callout-box" style="margin-bottom:6px"><b>${esc(actLabel)}</b> <span class="muted mono">${esc((ev.created_at || "").slice(0, 19))} · ${esc(actorLabel)}</span>${detLabel ? `<div class="secondary">${esc(detLabel)}</div>` : ""}</div>`;
      }).join("") : `<div class="muted" style="font-size:13px">${E.isProduction() ? "接口未返回公开事件；检索总次数尚未核验。" : "暂无公开事件记录（检索次数：" + imps.length + "）。"}</div>`}
      ${trials.length ? `<div class="prose-section"><h3>Challenger 试用观察 (${trials.length})</h3>${trials.map(function (trItem) {
        return `<div class="kv-row"><span>${esc(trItem.relation)} (${esc(trItem.status)})</span><span class="mono">${esc(trItem.success_count == null ? "未返回" : trItem.success_count)}/${esc(trItem.required_successes == null ? "未返回" : trItem.required_successes)}</span></div>`;
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
        <div class="kv-grid">${kvCell("归档引用指针", esc(ref || "未提供"), true)}${kvCell("读取方式", E.isProduction() ? "历史原文接口；调用消耗未单独核验" : "0 次（直读 PG）", false)}</div>
        ${quote ? `<div class="callout-box"><b>锚点摘录：</b>${esc(quote)}</div>` : ""}
        ${msgs && msgs.length ? msgs.map(function (m) {
          const idx = m.turn_index != null ? m.turn_index : m.event_index;
          const role = m.role || m.speaker || "turn";
          const txt = m.content != null ? m.content : (m.text || "");
          return `<div class="callout-box" style="margin-bottom:6px;${m.is_anchor ? "border-left-color:var(--brand)" : ""}">
            <div class="muted mono" style="font-size:11.5px">#${esc(idx)} · ${esc(role)} ${m.is_anchor ? "★ 锚点" : ""}</div>
            <div style="white-space:pre-wrap">${esc(txt)}</div>
          </div>`;
        }).join("") : `<div class="muted" style="font-size:13px">${loading ? "正在读取历史会话窗口…" : (E.isProduction() ? "接口未返回更多上下文；摘录不能代替完整来源核验。" : (quote ? "演示快照已展示锚点原文。" : "暂无更多上下文消息。"))}</div>`}`;
    };
    openD("历史会话原文证据 · " + (ref || "archive"), renderBody([], E.state.apiMode === "live"), "三级溯源 · 0-LLM 原文检索");
    const token = drawerToken();
    if (E.state.apiMode === "live" && ref && E.api && E.api.fetchEvidenceContext) {
      E.api.fetchEvidenceContext(ref).then(function (data) {
        writeDrawer(token, data ? renderBody(data.turns || data.messages || [], false) : E.unavailablePanel("原文读取失败", "来源尚未核验，请重试。"));
      }).catch(function () { writeDrawer(token, E.unavailablePanel("原文读取失败", "来源尚未核验，请重试。")); });
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
    if (E.isProduction()) {
      if (["open-capture-l3", "open-dr-l3", "open-handbook-l3", "open-errsig-l3"].includes(act)) {
        return unavailable("功能来源说明", "捕获健康、灾备恢复、手册编译和报错触发尚未接入对应核验接口；没有实际执行或发布。");
      }
      if (act === "open-topo-l3") {
        const n = (s.topoNodes || []).find(function (x) { return x.id === d.id; });
        if (!n) return unavailable("架构节点", "没有找到当前节点。");
        return openD("架构节点 · " + n.title, `<p>${esc(n.stage)} · 模块导航，运行状态尚未核验。</p><button type="button" class="btn btn-primary" data-act="goto-module" data-route="${esc(n.target)}" data-sub="${esc(n.sub || "")}">前往管理模块</button>`);
      }
      if (act === "open-stage4-l3") return openD("旧经验清洗登记来源", '<p>历史批次登记不等于当前生效数，冷备可恢复性及误删率未核验。</p>' + actualJson(s.livePipeline && s.livePipeline.legacy_cleanups));
      if (act === "open-policy-l3") return openD("已读取的策略配置", actualJson((s.liveControl && s.liveControl.policy) || (s.livePipeline && s.livePipeline.policy)) + '<p>退役文件权限与路径尚未接入核验。</p>');
      if (act === "open-impressions-l3") {
        const rows = s.liveEvolution && s.liveEvolution.impressions;
        return openD("实际检索曝光记录", '<p>下列为接口返回的检索命中汇总，不代表实际使用、成功或效果归因；Beta变化尚未接入。</p>' + (Array.isArray(rows) ? `<div class="table-wrap"><table><thead><tr><th>经验</th><th>入口</th><th>客户端</th><th>模型</th><th>次数</th></tr></thead><tbody>${rows.map(function (r) { return `<tr><td>${esc(r.item_uid)}</td><td>${esc(r.channel)}</td><td>${esc(r.agent_client)}</td><td>${esc(r.model)}</td><td>${esc(r.hits)}</td></tr>`; }).join("")}</tbody></table></div>${rows.length ? "" : '<p>当前接口没有返回曝光记录。</p>'}` : actualJson(null)));
      }
      if (act === "open-skill") {
        const k = (s.skills || []).find(function (x) { return x.id === d.id; });
        return k ? openD("Skill 目录信息 · " + k.name, `<p>${esc(k.summary || "目录未提供描述")}</p><p>SKILL.md 原文件尚未接入读取；目录摘要不作为源码。</p>`) : unavailable("Skill 目录信息", "没有找到当前目录项。");
      }
      if (act === "open-hist-meta") {
        const b = (s.historyBlocks || []).find(function (x) { return x.id === d.id; });
        return b ? openD("历史会话来源", `<div class="kv-grid">${kvCell("归档指针", esc(b.sourceRef || b.id), true)}</div><p>BM25、trgm、HNSW及归一化评分未由当前接口分别提供，尚未核验。</p>`) : unavailable("历史会话来源", "未找到当前会话。");
      }
      if (act === "open-llm-ledger") {
        const rows = s.livePipeline && s.livePipeline.llm_usage;
        return openD("模型调用汇总", '<p>按轨道、阶段、模型汇总；不代表逐笔明细或今日费用。</p>' + (Array.isArray(rows) ? renderLlmCallsTable(rows) : actualJson(null)));
      }
    }
    if (act === "open-int-l3" || act === "open-int-trace") {
      const it = (s.interceptions || []).find(function (x) { return x.id === (d.id || s.selectedIntId); });
      if (!it) return;
      openD("拦截门控步进链与元数据 · " + it.id, renderIntTraceHtml(it, null), it.layer + " · " + it.reason_code);
      const token = drawerToken();
      if (s.apiMode === "live" && (E.session || typeof root.fetch === "function")) {
        (E.session ? E.session.request('GET',"/api/console-v2/interceptions/" + encodeURIComponent(it.id) + "/trace").then(function(r){return {ok:r.status===200,json:function(){return Promise.resolve(r.data);}};}) : root.fetch("/api/console-v2/interceptions/" + encodeURIComponent(it.id) + "/trace", { credentials: "same-origin" }))
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (td) { writeDrawer(token, td && td.ok ? renderIntTraceHtml(it, td.item || td) : E.unavailablePanel("拦截溯源读取失败", "完整链路尚未核验，请重试。")); })
          .catch(function () { writeDrawer(token, E.unavailablePanel("拦截溯源读取失败", "完整链路尚未核验，请重试。")); });
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
      const token = drawerToken();
      if (s.apiMode === "live" && E.api && E.api.fetchExpTrace) {
        E.api.fetchExpTrace(x.id).then(function (tr) { writeDrawer(token, tr ? renderExpTraceHtml(x, tr) : E.unavailablePanel("经验溯源读取失败", "完整链路尚未核验，请重试。")); }).catch(function () { writeDrawer(token, E.unavailablePanel("经验溯源读取失败", "完整链路尚未核验，请重试。")); });
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
      if (!a) return unavailable("客户端来源", "尚未读取真实客户端归属或信誉记录。");
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

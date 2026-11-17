/* EvolvMem Web Console v2 — M3 Non-Write Interception Ledger (8-Layer Funnel + L1/L2/L3 + Rescue) */
(function (root) {
  const E = (root.EvolvMemV2 = root.EvolvMemV2 || {});
  const esc = E.esc;

  E.renderL2BlindComparison = function (opts) {
    const o = opts || {};
    const chain = o.chain || [];
    const l2aStep = chain.find(function (d) { return d && d.layer === "L2a"; }) || null;
    const l2bStep = chain.find(function (d) { return d && d.layer === "L2b"; }) || null;
    const hitStep = (o.finalLayer === "L2a" ? l2aStep : (l2bStep || l2aStep)) || chain[chain.length - 1] || {};
    const mHit = (hitStep && hitStep.meta) || {};
    const mA = (l2aStep && l2aStep.meta) || {};
    const mB = (l2bStep && l2bStep.meta) || {};

    const blindPrompt = mHit.blind_prompt || mA.blind_prompt || mB.blind_prompt ||
      ("触发范围: " + JSON.stringify(o.scope || {}) + "\n问题现象: " + (o.problem || ""));
    const l2aPred = String(mA.model_prediction || mB.flash_prediction || "").trim();
    const l2bPred = String(mB.model_prediction || "").trim();
    const l2aModel = (l2aStep && l2aStep.judge_model) || "deepseek-v4-flash";
    const l2bModel = (l2bStep && l2bStep.judge_model) || "deepseek-chat";
    const activeModel = (hitStep && hitStep.judge_model) || o.proposerModel || "-";
    const confPct = hitStep && hitStep.confidence != null ? Math.round(Number(hitStep.confidence) * 100) + "%" : "-";
    const rationale = (hitStep && hitStep.detail) || o.fallbackDetail || "";

    let leftBody = "";
    if (l2aPred && l2bPred) {
      const aTag = l2aStep && l2aStep.verdict === "reject" ? "直接命中" : "未完全命中 -> 升级复核";
      const bTag = l2bStep && l2bStep.verdict === "reject" ? "命中核心机制" : "仍未命中私有事实";
      leftBody = `
        <div class="prose-section">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:4px">
            <span class="badge">${esc("L2a 快模初筛 (" + l2aModel + ")")}</span>
            <span class="muted" style="font-size:12px">${esc(aTag)}</span>
          </div>
          <div style="white-space:pre-wrap;color:var(--ink)">${esc(l2aPred)}</div>
        </div>
        <div style="border-top:1px dashed var(--line);padding-top:8px;margin-top:4px">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:4px">
            <span class="badge badge-warn">${esc("L2b 强模复核 (" + l2bModel + ")")}</span>
            <span class="muted" style="font-size:12px">${esc(bTag)}</span>
          </div>
          <div style="white-space:pre-wrap;color:var(--ink)">${esc(l2bPred)}</div>
        </div>`;
    } else if (l2aPred || l2bPred) {
      const onlyPred = l2bPred || l2aPred;
      const onlyModel = l2bPred ? l2bModel : (l2aPred ? l2aModel : activeModel);
      leftBody = `
        <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;margin-bottom:4px">
          <span class="badge badge-warn">${esc(onlyModel)}</span>
          <span class="muted" style="font-size:12px">${esc(o.finalLayer || "L2")}</span>
        </div>
        <div style="white-space:pre-wrap;color:var(--ink)">${esc(onlyPred)}</div>`;
    } else {
      leftBody = `<div class="muted" style="font-size:13px">该历史条目未记录模型原始盲猜文本（仅保留下方评审结论）</div>`;
    }

    const layerTag = o.finalLayer || (l2bStep ? "L2b" : "L2a");
    const verdictStrip = o.isRejected
      ? `<div class="callout-box" style="border-left-color:var(--danger);background:var(--danger-soft)">
          <div style="font-weight:700;color:var(--danger);margin-bottom:4px">④ 模型盲猜 vs 候选经验 · 判定无增量差异（拦截于 ${esc(layerTag)} · ${esc(o.reasonCode || "-")} · 置信度 ${esc(confPct)}）</div>
          <div>${esc(rationale || "模型在不看该经验的情况下已推导出相同核心解法，判定为通用常识不入库；若含关键私有口径可点击右上角「人工救回入库」。")}</div>
        </div>`
      : `<div class="callout-box" style="border-left-color:var(--ok);background:var(--ok-soft)">
          <div style="font-weight:700;color:var(--ok);margin-bottom:4px">④ 模型盲猜 vs 候选经验 · 判定具备增量记忆价值（通过 L2 盲测 · 置信度 ${esc(confPct)}）</div>
          <div>${esc(rationale || "模型裸测未能答出上述候选经验中的关键事实或私有边界，建议审阅入库。")}</div>
        </div>`;

    return `
      <div class="prose-section"><h3>L2 盲测对照诊断（${esc(layerTag)} · ${esc(o.reasonCode || "-")}）</h3></div>
      <div class="compare-card" style="background:var(--surface-solid)">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
          <h4>① 盲测输入题面</h4>
          <span class="muted" style="font-size:12px">已遮蔽候选解法与误区，仅向模型提供范围与问题</span>
        </div>
        <div style="white-space:pre-wrap;color:var(--ink-secondary)">${esc(blindPrompt)}</div>
      </div>
      <div class="grid-2">
        <div class="compare-card" style="border-color:var(--warn-border)">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap">
            <h4>② 模型无经验盲猜输出 · 裸测基线</h4>
            <span class="badge badge-warn">无记忆裸测</span>
          </div>
          ${leftBody}
        </div>
        <div class="compare-card" style="border-color:var(--brand-border)">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;flex-wrap:wrap">
            <h4>③ 会话提炼候选经验 · 本次提议</h4>
            <span class="badge badge-brand">实战提炼候选 · ${esc(o.proposerModel || "-")}</span>
          </div>
          <div><strong>候选解法：</strong><div style="white-space:pre-wrap;color:var(--ink);margin-top:2px">${esc(o.solution || "-")}</div></div>
          ${o.misbelief ? `<div style="padding-top:6px;border-top:1px dashed var(--line)"><strong style="color:var(--warn)">纠正误区：</strong><span class="secondary">${esc(o.misbelief)}</span></div>` : ""}
        </div>
      </div>
      ${verdictStrip}`;
  };

  function renderL2LayerDiagnostic(it) {
    const chain = it.gate_chain || [];
    const diag = it.diagnostic || {};
    const hitStep = chain.find(function (d) { return d.layer === it.layer && (d.verdict === "reject" || d.verdict === "merge"); }) || chain[chain.length - 1] || {};
    const meta = hitStep.meta || {};

    if (it.layer === "L2a" || it.layer === "L2b") {
      return E.renderL2BlindComparison({
        chain: chain,
        finalLayer: it.layer,
        isRejected: true,
        reasonCode: it.reason_code,
        problem: it.problem || it.title,
        solution: it.solution,
        reusable: it.reusable_experience,
        misbelief: it.misbelief,
        scope: it.scope,
        proposerModel: it.model,
        fallbackDetail: it.detail
      });
    }

    if (it.layer === "L1") {
      const cos = typeof meta.cosine_similarity === "number" ? meta.cosine_similarity : 0.90;
      const pct = Math.min(100, Math.max(0, Math.round(cos * 100)));
      const tgt = diag.target_note || {};
      return `
        <div class="prose-section">
          <h3>L1 语义向量去重对比（余弦相似度 ${cos.toFixed(3)} / 阈值 ${meta.threshold || 0.88}）</h3>
          <div class="sim-meter"><i style="width:${pct}%"></i></div>
        </div>
        <div class="grid-2">
          <div class="compare-card">
            <h4>新候选经验问题与解法</h4>
            <b>${esc(it.problem)}</b>
            <div class="secondary">${esc(it.solution)}</div>
          </div>
          <div class="compare-card" style="border-color:var(--ok-border)">
            <h4>命中既有生效经验 (${esc(meta.nearest_note_id || tgt.id || "-")})</h4>
            <b>${esc(tgt.problem || "主库相似经验")}</b>
            <div class="secondary">${esc(tgt.solution || "证据次数已自动累加，无需重复建卡。")}</div>
          </div>
        </div>`;
    }

    if (it.layer === "L0") {
      const fl = meta.field_lengths || { problem: it.problem.length, solution: it.solution.length, reusable_experience: (it.reusable_experience || "").length };
      const lim = meta.limits || { problem: 120, solution: 240, reusable_experience: 120 };
      const evCnt = meta.evidence_count != null ? meta.evidence_count : (it.evidence ? it.evidence.length : 0);
      return `
        <div class="prose-section"><h3>L0 硬规则与字段边界检查 (${esc(it.reason_code)})</h3></div>
        <div class="check-grid">
          <div class="check-item"><span>problem 字数</span><b>${fl.problem} / ${lim.problem} 字</b></div>
          <div class="check-item"><span>solution 字数</span><b>${fl.solution} / ${lim.solution} 字</b></div>
          <div class="check-item"><span>reusable 字数</span><b>${fl.reusable_experience} / ${lim.reusable_experience} 字</b></div>
          <div class="check-item"><span>原始证据指针</span><b style="color:${evCnt ? "var(--ok)" : "var(--danger)"}">${evCnt} 条 (${evCnt ? "有效" : "缺失"})</b></div>
        </div>`;
    }

    if (it.layer === "L3") {
      const tr = diag.trial || {};
      const tgt = diag.target_note || {};
      const req = tr.required_successes || 2;
      const cur = tr.success_count || 0;
      const pct = Math.min(100, Math.round((cur / Math.max(1, req)) * 100));
      return `
        <div class="prose-section">
          <h3>L3 关系调和与 Challenger 试用观察进度（关系：${esc(tr.relation || it.reason_code)}）</h3>
          <div class="sim-meter"><i style="width:${pct}%;background:var(--brand)"></i></div>
          <div class="muted" style="font-size:13px">试用成功 ${cur} / ${req} 次 · 失败 ${tr.failure_count || 0} 次</div>
        </div>
        ${tgt.problem ? `<div class="callout-box"><b>被对照旧经验 (${esc(tgt.id)})：</b>${esc(tgt.problem)}</div>` : ""}`;
    }

    if (it.layer === "Extract" || it.layer === "Quota" || it.layer === "Moments") {
      return `
        <div class="callout-box" style="border-left-color:var(--warn)">
          <div style="font-weight:700;margin-bottom:4px">${esc(it.layer)} 前置拦截明细 · ${esc(it.reason_code)}</div>
          <div class="secondary">${esc(diag.moment_summary || it.detail)}</div>
          ${it.session_id ? `<div class="muted mono" style="font-size:12px;margin-top:4px">session=${esc(it.session_id)} · model=${esc(it.model)}</div>` : ""}
        </div>`;
    }

    const rev = diag.review || {};
    return `
      <div class="callout-box" style="border-left-color:var(--danger)">
        <div style="font-weight:700;margin-bottom:4px">${esc(it.layer)} 处理结论 · ${esc(it.reason_code)}</div>
        <div>${esc(rev.note || it.detail)}</div>
      </div>`;
  }

  E.renderInterceptionsView = function () {
    const s = E.state;
    const w = E.canWrite();
    if (!w) {
      return `
        ${E.renderExpSubtabs("interceptions")}
        <div class="banner banner-danger">🔒 为保护私有提炼过程、被拒草稿与模型调试日志，仅知识库本人可查看「未写入 / 门控拦截追踪」明细；他人只读视角仅限浏览已公开发布且处于 Active 状态的正式经验。</div>`;
    }

    const f = s.intFilters;
    const funnel = s.interceptionFunnel || { layers: {}, total_intercepted: 0, rescued_total: 0 };
    const layerCounts = funnel.layers || {};
    const allItems = s.interceptions || [];

    const visible = allItems.filter(function (it) {
      if (f.layer !== "all" && it.layer !== f.layer) return false;
      if (f.track !== "all" && it.track !== f.track) return false;
      if (f.category !== "all" && it.category !== f.category) return false;
      if (f.q) {
        const blob = (it.title + it.reason_code + it.detail + it.problem + it.solution).toLowerCase();
        if (blob.indexOf(f.q.toLowerCase()) < 0) return false;
      }
      return true;
    });

    const sel = visible.find(function (x) { return x.id === s.selectedIntId; }) || visible[0] || null;
    if (sel) s.selectedIntId = sel.id;

    const totalNonStage4 = Object.keys(layerCounts).filter(function (k) { return k !== "Stage4"; }).reduce(function (a, k) { return a + (layerCounts[k] || 0); }, 0);

    return `
      ${E.renderExpSubtabs("interceptions")}

      <section class="panel" style="padding:16px 18px;gap:12px">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
          <div>
            <strong>全链路未写入拦截漏斗（点击任意层级即刻定位是被哪一层拦下）</strong>
            <div class="muted" style="font-size:13px">覆盖 Moments 0-LLM 初筛 → Extract → Quota → L0 → L1 → L2a → L2b → L3 → L4 → Stage 4</div>
          </div>
          <span class="badge badge-ok">累计已人工救回 ${funnel.rescued_total || 0} 条</span>
        </div>
        <div class="funnel-pills">
          ${E.LAYER_META.map(function (lm) {
            const cnt = lm.key === "all" ? (totalNonStage4 || allItems.length) : (layerCounts[lm.key] != null ? layerCounts[lm.key] : 0);
            return `<button type="button" class="funnel-pill ${f.layer === lm.key ? "active" : ""}" data-act="int-layer" data-val="${esc(lm.key)}" title="${esc(lm.desc)}">
              <span>${esc(lm.label)}</span><strong>${cnt}</strong>
            </button>`;
          }).join("")}
        </div>
        <div class="filter-bar">
          <select id="int-f-track">
            <option value="all" ${f.track === "all" ? "selected" : ""}>全部轨道 (live + backfill)</option>
            <option value="live" ${f.track === "live" ? "selected" : ""}>仅新会话实时 (live)</option>
            <option value="backfill" ${f.track === "backfill" ? "selected" : ""}>仅旧聊天回填 (backfill)</option>
          </select>
          <select id="int-f-cat">
            <option value="all" ${f.category === "all" ? "selected" : ""}>全部分类</option>
            <option value="系统环境经验" ${f.category === "系统环境经验" ? "selected" : ""}>系统环境经验</option>
            <option value="业务经验" ${f.category === "业务经验" ? "selected" : ""}>业务经验</option>
            <option value="前端经验" ${f.category === "前端经验" ? "selected" : ""}>前端经验</option>
          </select>
          <input type="search" id="int-f-q" value="${esc(f.q)}" placeholder="搜索未入库候选问题、拦截原因或会话 ID…" style="flex:1;min-width:220px" />
          <span class="badge">当前命中 ${visible.length} 条</span>
        </div>
      </section>

      <div class="split-workspace">
        <div class="list-panel">
          <div class="list-head"><strong>被拦截 / 未写入清单 (${visible.length})</strong><span class="muted" style="font-size:12.5px">极简 L1 摘要队列</span></div>
          <div class="list-scroll">
            ${visible.length === 0 ? `<div style="padding:28px" class="muted">当前过滤层级下暂无拦截记录。</div>` : visible.map(function (it) {
              const cls = it.rescued_note_id ? "badge-ok" : (it.layer === "L1" || it.layer === "L3" ? "badge-brand" : "badge-danger");
              return `<button type="button" class="list-entry ${sel && sel.id === it.id ? "active" : ""}" data-act="select-int" data-id="${esc(it.id)}">
                <div class="list-entry-meta">
                  <span style="display:flex;gap:6px;align-items:center">
                    <span class="badge ${cls}">${esc(it.layer)} 拦截</span>
                    <span class="badge">${esc(it.category)}</span>
                  </span>
                  <span>${esc(it.track === "backfill" ? "回填" : "实时")} · ${esc(it.created_at || "")}</span>
                </div>
                <div class="list-entry-title">${esc(it.title)}</div>
              </button>`;
            }).join("")}
          </div>
        </div>

        <section class="detail-panel" aria-label="L2 拦截原因与救回工作台">
          ${!sel ? `<p class="muted">请从左侧队列选择一条未写入记录查看具体拦截层级与原因。</p>` : `
            <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
              <div class="tag-row">
                <span class="badge ${sel.rescued_note_id ? "badge-ok" : "badge-danger"}">拦截层：${esc(sel.layer)}</span>
                <span class="badge">${esc(sel.reason_code)}</span>
                <span class="badge">${esc(sel.category)}</span>
                ${sel.rescued_note_id ? `<span class="badge badge-ok">✅ 已人工救回入库 (${esc(sel.rescued_note_id)})</span>` : ""}
              </div>
              ${sel.rescuable && !sel.rescued_note_id ? `<button type="button" class="btn btn-primary btn-sm" data-act="rescue-open" data-id="${esc(sel.id)}">🛠 人工救回入库 (签发 Ticket)</button>` : ""}
            </div>

            <div class="prose-section">
              <h3>核心问题与拦截原因说明</h3>
              <h2 class="prose-lead">${esc(sel.problem || sel.title)}</h2>
              ${sel.layer === "L2a" || sel.layer === "L2b" ? "" : `<p class="secondary" style="margin:8px 0 0">${esc(sel.detail)}</p>`}
            </div>

            ${renderL2LayerDiagnostic(sel)}

            ${sel.reusable_experience ? `
              <div class="callout-box">
                <div style="font-size:12.5px;font-weight:700;color:var(--brand);margin-bottom:4px">候选提炼可复用原则</div>
                <div>${esc(sel.reusable_experience)}</div>
              </div>` : ""}

            <div class="l3-bar">
              <span class="muted" style="font-size:13px">完整 L0–L4 闸门步骤条、LLM Token 成本与原文证据在三级抽屉查看</span>
              <div style="display:flex;gap:8px;flex-wrap:wrap">
                ${sel.evidence && sel.evidence.length ? `<button type="button" class="btn btn-sm" data-act="open-int-evidence" data-id="${esc(sel.id)}">📄 原文会话证据 (L3)</button>` : ""}
                <button type="button" class="btn btn-sm" data-act="open-int-l3" data-id="${esc(sel.id)}">🔬 完整门禁步进链与元数据 (L3)</button>
              </div>
            </div>
          `}
        </section>
      </div>`;
  };
})(typeof window !== "undefined" ? window : globalThis);

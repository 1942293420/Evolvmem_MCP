/* EvolvMem Web Console v2 — Top Navigation Shell, Event Dispatcher & L3 Traceability Drawers */
(function (root) {
  const E = (root.EvolvMemV2 = root.EvolvMemV2 || {});
  const esc = E.esc;
  function currentResponse(){const ticket=E.session?E.session.capture():null;return ()=>!ticket || (E.session && E.session.isCurrent(ticket));}

  function formatRemain(sec) {
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return h + "h " + (m < 10 ? "0" : "") + m + "m";
  }

  function kvCell(label, val, isMono) {
    return `<div class="kv-cell"><span>${esc(label)}</span><b class="${isMono ? "mono" : ""}">${val}</b></div>`;
  }

  E.openDrawer = function (title, html, subLabel) {
    E.drawerEpoch = (E.drawerEpoch || 0) + 1;
    if (typeof root.document === "undefined") return;
    const subEl = root.document.getElementById("drawer-sub");
    if (subEl) subEl.textContent = subLabel || "三级溯源 · 技术明细";
    root.document.getElementById("drawer-title").textContent = title;
    root.document.getElementById("drawer-body").innerHTML = html;
    root.document.getElementById("drawer-backdrop").classList.add("open");
  };

  E.closeDrawer = function () {
    E.drawerEpoch = (E.drawerEpoch || 0) + 1;
    if (typeof root.document === "undefined") return;
    root.document.getElementById("drawer-backdrop").classList.remove("open");
  };

  E.openModal = function (title, html) {
    if (typeof root.document === "undefined") return;
    root.document.getElementById("modal-title").textContent = title;
    root.document.getElementById("modal-body").innerHTML = html;
    root.document.getElementById("modal-backdrop").classList.add("open");
  };

  E.closeModal = function () {
    if (typeof root.document === "undefined") return;
    root.document.getElementById("modal-backdrop").classList.remove("open");
  };

  E.renderTopNav = function () {
    const s = E.state;
    const activeTab = s.route === "pipelines" ? "experiences" : s.route === "delivery" ? "evolution" : s.route;
    const pendingTotal = s.proposals.filter(function (p) { return p.status === "pending"; }).length;
    const activeExps = s.experiences.filter(function (x) { return x.status === "active"; }).length;
    const draftSkills = s.skills.filter(function (k) { return k.lifecycle === "draft"; }).length;

    return E.NAV_TABS.map(function (t) {
      const dimmed = (s.stageFilter === "1-5" && t.plan) || (s.stageFilter === "6-7" && !t.plan);
      let badge = "";
      if (t.id === "review-inbox") badge = `<span class="nav-count ${pendingTotal ? "warn" : ""}">${pendingTotal}</span>`;
      else if (t.id === "experiences") badge = `<span class="nav-count">${activeExps}</span>`;
      else if (t.id === "skills") badge = `<span class="nav-count">${draftSkills} 草</span>`;
      if (E.isProduction()) {
        const m = s.liveMetrics;
        if (t.id === "review-inbox") badge = '<span class="nav-count">' + (m && m.pending_live != null && m.pending_backfill != null ? m.pending_live + m.pending_backfill : "—") + '</span>';
        else if (t.id === "experiences") badge = '<span class="nav-count">' + (m && m.active_notes != null ? m.active_notes : "—") + '</span>';
        else if (t.id === "skills") badge = '<span class="nav-count">' + (s.liveEvolution ? s.skills.length : "—") + '</span>';
      }
      return `<button type="button" class="nav-tab ${activeTab === t.id ? "active" : ""} ${dimmed ? "dimmed" : ""}" data-act="goto-tab" data-route="${t.id}">
        ${t.icon}<span>${esc(t.label)}</span>${badge}
      </button>`;
    }).join("");
  };

  E.renderMain = function () {
    const s = E.state;
    if(E.session && (!E.session.isReady() || !E.session.dataReady))return E.session.panel();
    if (E.isProduction() && s.simMode !== "sso_login") {
      if (s.apiMode === "loading") return '<section class="panel" role="status"><h1>正在连接 EvolvMem</h1><p>正在读取当前账号和页面数据。</p></section>';
      if (s.apiError || s.apiMode === "error") return '<section class="panel" role="alert"><h1>页面数据加载失败</h1><p class="secondary">' + esc(s.apiError || "暂时无法连接服务，请重试。") + '</p><button type="button" class="btn btn-primary" data-act="retry-api">重新加载</button></section>';
      if (s.pageLoading) return '<section class="panel" role="status"><h1>正在读取当前库</h1><p>读取完成后显示所选库的数据。</p></section>';
    }
    if (s.simMode === "pg_503") {
      return `<div class="panel" style="max-width:680px;margin:48px auto;border-color:var(--danger)">
        <span class="badge badge-danger mono" style="align-self:flex-start">HTTP 503 · pg_control_unavailable</span>
        <h1 style="margin:0;font-size:22px">PostgreSQL 权威库暂不可用（Fail-Closed 安全阻断）</h1>
        <p class="secondary">严格遵守主库单权威红线：当 PG 主集群不可达且重试耗尽时，服务端明确阻断并返回 503，<b>绝不静默降级或写入已封存的 0400 旧 SQLite</b>。</p>
        <div><button type="button" class="btn btn-primary" data-act="set-sim" data-mode="normal">恢复正常连通状态</button></div>
      </div>`;
    }
    if (s.simMode === "sso_login") {
      return `<div class="panel" style="max-width:460px;margin:48px auto">
        <span class="badge badge-ok mono" style="align-self:flex-start">FEISHU OAUTH // 8h NON-SLIDING</span>
        <h1 style="margin:0;font-size:22px">登录 EvolvMem Neural Matrix</h1>
        <p class="secondary">仅允许控制库已登记的启用账户通过飞书身份鉴权；新建网页会话固定有效期 8 小时，停用账户同事务吊销全量凭证。</p>
        <div style="display:flex;gap:10px;flex-wrap:wrap">
          ${s.apiMode === "live"
            ? `<a class="btn btn-primary" href="/auth/login">飞书登录</a>`
            : `<button type="button" class="btn btn-primary" data-act="set-sim" data-mode="normal">授权登录 (个人主库管理员)</button>`}
          ${E.isProduction() ? "" : '<button type="button" class="btn" data-act="set-sim" data-mode="normal">返回控制台</button>'}
        </div>
      </div>`;
    }
    const viewKey = s.route === "pipelines" ? "pipelines" : s.route === "delivery" ? "evolution" : s.route;
    if (s.route === "delivery" && s.evoSubtab !== "handbook") s.evoSubtab = "handbook";
    if (s.route === "pipelines") s.expSubtab = "pipelines";
    const fn = E.views[viewKey] || E.views.overview;
    return (E.isProduction() ? "" : '<div class="banner banner-warn" role="status">离线演示：以下为示例数据，操作不会写入正式系统。</div>') + fn();
  };

  function openDiagDrawer() { if (E.openDiagDrawer) E.openDiagDrawer(); }
  function openL3Drawer(act, ds) { if (E.openL3Drawer) E.openL3Drawer(act, ds); }

  E.openCmdPalette = function (presetQ) {
    const q = String(presetQ || "").toLowerCase();
    const hits = E.MODULES.filter(function (m) { return !q || (m.label + m.desc + m.stage).toLowerCase().indexOf(q) >= 0; });
    const exps = E.state.experiences.filter(function (x) { return x.status !== "retired" && (!q || (x.problem + x.solution + x.id).toLowerCase().indexOf(q) >= 0); }).slice(0, 4);
    E.openModal("全局搜索与直达跳转 (Ctrl+K)", `
      <input type="search" id="cmd-search-input" placeholder="输入模块名称、阶段号或经验正文关键词…" value="${esc(presetQ || "")}" style="width:100%" />
      <div class="muted" style="font-size:12.5px;font-weight:700">功能模块 (${hits.length})</div>
      <div style="display:flex;flex-direction:column;gap:6px">
        ${hits.map(function (m) {
          return `<button type="button" class="stage-flow-row" data-act="cmd-jump" data-route="${m.id}">
            <span><b>${esc(m.label)}</b> <span class="muted" style="font-size:13px">· ${esc(m.desc)}</span></span>${E.statusBadge(m.status)}
          </button>`;
        }).join("")}
      </div>
      ${exps.length ? `<div class="muted" style="font-size:12.5px;font-weight:700">命中经验 (${exps.length})</div>` + exps.map(function (x) {
        return `<button type="button" class="stage-flow-row" data-act="cmd-jump" data-route="experiences" data-expid="${esc(x.id)}">
          <span><span class="badge">${esc(x.category)}</span> &nbsp;${esc(x.problem)}</span>
        </button>`;
      }).join("") : ""}
    `);
    const inp = root.document && root.document.getElementById("cmd-search-input");
    if (inp) { inp.focus(); if (inp.setSelectionRange) inp.setSelectionRange(inp.value.length, inp.value.length); }
  };

  E.openNewExpModal = function () {
    const reqId = "req-" + Date.now().toString(36);
    E.openModal("新增结构化经验（快速直通入库）", `
      <div class="banner banner-info">受主库唯一写入口与 request_id 幂等保护，确保同内容重放不会重复写库。</div>
      <div class="grid-2">
        <label><div><strong>经验分类</strong></div><select id="new-exp-cat" style="width:100%"><option>系统环境经验</option><option>业务经验</option><option>前端经验</option></select></label>
        <label><div><strong>幂等键 (request_id)</strong></div><input type="text" id="new-exp-reqid" value="${esc(reqId)}" style="width:100%" /></label>
      </div>
      <label><div><strong>核心问题描述（<=120 字）</strong></div><input type="text" id="new-exp-prob" maxlength="120" style="width:100%" placeholder="在什么场景或条件下遇到了什么问题？" /></label>
      <label><div><strong>已验证解决办法（<=240 字）</strong></div><textarea id="new-exp-sol" rows="3" maxlength="240" style="width:100%" placeholder="经过实测验证通过的标准解法…"></textarea></label>
      <label><div><strong>可复用经验准则（<=120 字）</strong></div><input type="text" id="new-exp-reus" maxlength="120" style="width:100%" placeholder="日后遇到类似问题应遵循的原则…" /></label>
      <div style="display:flex;gap:8px;justify-content:flex-end"><button type="button" class="btn btn-primary" data-act="submit-new-exp">提交并写入正式库</button></div>
    `);
  };

  E.renderAll = function () {
    if (typeof root.document === "undefined") return;
    const doc = root.document;
    const s = E.state;
    const actEl = doc.activeElement;
    const focusInfo = actEl && actEl.id ? { id: actEl.id, start: actEl.selectionStart, end: actEl.selectionEnd } : null;
    doc.documentElement.setAttribute("data-theme", s.theme);
    const thSel = doc.getElementById("theme-preset");
    if (thSel && thSel.value !== s.theme) thSel.value = s.theme;
    const ctxSel = doc.getElementById("ctx-lib-sel");
    if (ctxSel) ctxSel.disabled = E.isProduction() && s.apiMode !== "live";
    if (ctxSel && ctxSel.value !== s.libraryCtx) ctxSel.value = s.libraryCtx;
    const navEl = doc.getElementById("top-nav");
    const mainEl = doc.getElementById("workspace-main");
    if (navEl) navEl.innerHTML = E.renderTopNav();
    if (mainEl) mainEl.innerHTML = E.renderMain();
    if (E.isProduction() && mainEl) mainEl.querySelectorAll("[data-act]").forEach(function (el) {
      if (E.isPlannedAction(el.getAttribute("data-act"))) {
        el.disabled = true; el.title = "待开发，尚未接通后台";
        el.textContent = el.textContent.replace(/ · 待开发$/, "") + " · 待开发";
      }
    });
    if(E.session)E.session.syncHeader();
    if (focusInfo) {
      const next = doc.getElementById(focusInfo.id);
      if (next && mainEl && mainEl.contains(next)) {
        next.focus({preventScroll:true});
        if (typeof focusInfo.start === "number" && next.setSelectionRange) next.setSelectionRange(focusInfo.start, focusInfo.end);
      }
    }
  };

  E.isPlannedAction = function (act) {
    return /^(promote-cat|demote-cat|toggle-dormant|publish-handbook|habit-confirm|habit-dismiss|eval-skill|skill-stage|simulate-sync|reset-demo|set-sim|restore-exp)$/.test(act);
  };
  E.applyAction = function (act, ds) {
    const s = E.state;
    const current=currentResponse();
    if(act==='auth-logout'){if(E.session)E.session.logout();return false;}
    if(act==='auth-retry'){if(E.session)E.session.check();return false;}
    if (act === "retry-api") { if (E.api) E.api.bootstrap(); return false; }
    if (E.isProduction() && E.isPlannedAction(act)) { E.toast("该功能待开发，尚未接通后台。", "warn"); return false; }
    if(E.session && (!E.session.isReady() || !E.session.dataReady))return false;
    if (!E.canWrite() && /^(approve-prop|reject-prop|batch-approve|batch-reject-apply|trash-exp|restore-exp|submit-new-exp|rb-dry-run|rb-apply|toggle-user|toggle-grant|toggle-r9|toggle-proj-del)$/.test(act)) {
      E.toast("只读权限不可执行写操作", "danger");
      return false;
    }
    if (act === "goto-tab" || act === "goto-module") {
      const r = ds.route || "overview";
      if (r === "pipelines") { s.route = "experiences"; s.expSubtab = "pipelines"; }
      else if (r === "delivery") { s.route = "evolution"; s.evoSubtab = "handbook"; }
      else {
        s.route = r;
        if (ds.sub && r === "experiences") s.expSubtab = ds.sub;
        if (ds.sub && r === "evolution") s.evoSubtab = ds.sub;
        if (ds.sub && r === "control") s.ctrlSubtab = ds.sub;
        if (ds.sub && r === "history") s.histSubtab = ds.sub;
      }
      if (ds.expid) { s.expSubtab = "list"; s.selectedExpId = ds.expid; }
      s.simMode = "normal";
      E.closeDrawer();
      if (typeof root.location !== "undefined") root.location.hash = "#" + s.route;
      if (s.apiMode === "live" && E.api) E.api.loadTab(s.route);
      return true;
    }
    if (act === "set-sim") { if(E.session)return false;s.simMode = ds.mode || "normal"; E.closeDrawer(); return true; }
    if (act === "select-agent") { s.selectedAgentName = ds.name; return true; }
    if (act === "select-proposal") { s.selectedProposalUid = ds.uid; s.editingUid = null; s.rejectBoxOpen = false; return true; }
    if (act === "toggle-reject-box") { s.rejectBoxOpen = !s.rejectBoxOpen; return true; }
    if (act === "exp-subtab") {
      s.expSubtab = ds.sub;
      if (s.apiMode === "live" && E.api) E.api.loadTab(s.route);
      return true;
    }
    if (act === "exp-cat") {
      s.expFilters.category = ds.val;
      if (s.apiMode === "live" && E.api) E.api.loadExperiences().then(function () { E.renderAll(); });
      return true;
    }
    if (act === "select-exp") { s.selectedExpId = ds.id; return true; }
    if (act === "int-layer") {
      s.intFilters.layer = ds.val || "all";
      if (s.apiMode === "live" && E.api) E.api.loadInterceptions().then(function () { E.renderAll(); });
      return true;
    }
    if (act === "select-int") { s.selectedIntId = ds.id; return true; }
    if (act === "rescue-open") {
      if (!E.canWrite()) { E.toast("只读权限不可救回", "danger"); return false; }
      const it = (s.interceptions || []).find(function (x) { return x.id === ds.id; });
      if (it && E.openRescueModal) E.openRescueModal(it);
      return false;
    }
    if (act === "rescue-submit") {
      if (!E.canWrite()) { E.toast("只读权限不可操作", "danger"); return false; }
      const doc = root.document;
      const catEl = doc && doc.getElementById("rescue-cat");
      const probEl = doc && doc.getElementById("rescue-prob");
      const solEl = doc && doc.getElementById("rescue-sol");
      const reusEl = doc && doc.getElementById("rescue-reus");
      const noteEl = doc && doc.getElementById("rescue-note");
      const payload = {
        category: catEl ? catEl.value : "系统环境经验",
        problem: probEl ? probEl.value.trim() : "",
        solution: solEl ? solEl.value.trim() : "",
        reusable_experience: reusEl ? reusEl.value.trim() : ""
      };
      const note = noteEl ? noteEl.value.trim() : "";
      if (!payload.problem || !payload.solution) { E.toast("Problem 与 Solution 不能为空", "warn"); return false; }
      if (s.apiMode === "live" && E.api) {
        E.api.rescueInterception(ds.id, payload, note).then(function (ok) { if (current() && ok && E.closeModal) E.closeModal(); });
        return false;
      }
      const it = (s.interceptions || []).find(function (x) { return x.id === ds.id; });
      const newId = "exp-rescue-" + String(Date.now()).slice(-4);
      if (it) {
        it.rescued_note_id = newId;
        it.rescuable = false;
      }
      if (s.interceptionFunnel) s.interceptionFunnel.rescued_total = (s.interceptionFunnel.rescued_total || 0) + 1;
      s.experiences.unshift({
        id: newId, category: payload.category, kind: (it && it.kind) || "pitfall",
        origin: "manual_rescue", model: (it && it.model) || "human-rescue",
        status: "active", revision: 1, problem: payload.problem, solution: payload.solution,
        reusable: payload.reusable_experience, misbelief: (it && it.misbelief) || "",
        scope: { rescued_from: (it && it.layer) || "gate" },
        evidence: (it && it.evidence) || [], evidenceCount: (it && it.evidence && it.evidence.length) || 0,
        occurrenceCount: 1, importance: 7, pinned: false, betaA: 1, betaB: 0, stability: 0.92,
        updatedAt: "2026-11-15 17:00"
      });
      if (E.closeModal) E.closeModal();
      E.toast("已签发 Ticket 人工救回并写入经验库：" + newId);
      return true;
    }
    if (act === "hist-subtab") { s.histSubtab = ds.sub; return true; }
    if (act === "hist-scope") {
      s.histScope = ds.val;
      if (s.apiMode === "live" && E.api) { E.api.loadHistory().then(function () { E.renderAll(); }); return true; }
      return true;
    }
    if (act === "select-block") {
      s.selectedBlockId = ds.id;
      if (s.apiMode === "live" && E.api) E.api.loadBlockTurns(ds.id).then(function () { E.renderAll(); });
      return true;
    }
    if (act === "evo-subtab") { s.evoSubtab = ds.sub; return true; }
    if (act === "ctrl-subtab") { s.ctrlSubtab = ds.sub; return true; }
    if (act === "open-skill-detail") { s.selectedSkillId = ds.id; return true; }
    if (act === "close-skill-detail") { s.selectedSkillId = null; return true; }
    if (act === "simulate-sync") {
      s.changesCursor += 1;
      s.changesStream.unshift({ seq: s.changesCursor, actor: "codex-win-01", model: "gpt-5.4", noteId: "exp-sync-" + s.changesCursor, action: "跨端实时写入推送已完成确认", ts: "刚刚" });
      E.toast("已触发多端增量同步推送 · cursor=#" + s.changesCursor);
      return true;
    }
    if (act === "switch-track") {
      s.inboxTrack = ds.track; s.selectedProposalUids = []; s.rejectBoxOpen = false;
      if (s.apiMode === "live" && E.api) E.api.loadInbox().then(function () { E.renderAll(); });
      return true;
    }
    if (act === "start-edit") { s.editingUid = ds.uid; s.rejectBoxOpen = false; return true; }
    if (act === "cancel-edit") { s.editingUid = null; return true; }
    if (act === "save-edit") {
      const p = s.proposals.find(function (x) { return x.uid === ds.uid; });
      if (p && typeof root.document !== "undefined") {
        p.problem = root.document.getElementById("edit-prob-" + ds.uid).value.trim();
        p.solution = root.document.getElementById("edit-sol-" + ds.uid).value.trim();
        p.reusable = root.document.getElementById("edit-reus-" + ds.uid).value.trim();
        p._edited = true;
        p.l0Warnings = []; s.editingUid = null; E.toast("已保存修订内容");
      }
      return true;
    }
    if (act === "approve-prop") {
      const p = s.proposals.find(function (x) { return x.uid === ds.uid; });
      if (p && p.status === "pending") {
        if (s.apiMode === "live" && E.api) {
          E.api.decideProposal(p.uid, p._edited ? "edit_accept" : "accept", null, p._edited ? { problem: p.problem, solution: p.solution, reusable_experience: p.reusable } : null);
          return false;
        }
        p.status = "approved";
        if (p.gateVerdict === "pass" || p.gateVerdict === "accept") {
          s.reviewStats.agreed += 1;
          if (s.trackAgreement && s.trackAgreement[p.track]) s.trackAgreement[p.track].agreed_total += 1;
        }
        s.reviewStats.total += 1;
        if (s.trackAgreement && s.trackAgreement[p.track]) {
          s.trackAgreement[p.track].evaluable_total += 1;
          s.trackAgreement[p.track].reviewed_total += 1;
        }
        s.rejectBoxOpen = false;
        s.experiences.unshift({
          id: "exp-" + p.uid, category: p.category, origin: p.track, model: p.model, status: "active", kind: p.kind, revision: 1,
          problem: p.problem, solution: p.solution, reusable: p.reusable, misbelief: p.misbelief, scope: p.scope,
          evidenceCount: 1, betaA: 2, betaB: 0, stability: 0.72, updatedAt: "刚刚"
        });
        E.toast("已通过并正式写入个人经验库");
      }
      return true;
    }
    if (act === "reject-prop") {
      const p = s.proposals.find(function (x) { return x.uid === ds.uid; });
      if (p && p.status === "pending") {
        if (s.apiMode === "live" && E.api) {
          E.api.decideProposal(p.uid, "reject", ds.reason, null);
          return false;
        }
        p.status = "rejected"; p.rejectReason = ds.reason; s.rejectBoxOpen = false;
        if (p.gateVerdict === "reject") {
          s.reviewStats.agreed += 1;
          if (s.trackAgreement && s.trackAgreement[p.track]) s.trackAgreement[p.track].agreed_total += 1;
        }
        s.reviewStats.total += 1;
        if (s.trackAgreement && s.trackAgreement[p.track]) {
          s.trackAgreement[p.track].evaluable_total += 1;
          s.trackAgreement[p.track].reviewed_total += 1;
        }
        E.toast("已驳回提议 · 原因：" + ds.reason, "warn");
      }
      return true;
    }
    if (act === "batch-approve") { s.selectedProposalUids.slice().forEach(function (uid) { E.applyAction("approve-prop", { uid: uid }); }); s.selectedProposalUids = []; return true; }
    if (act === "batch-reject-open") {
      E.openModal("批量选择驳回原因", `<div class="tag-row">${E.REJECT_REASONS.map(function (r) {
        return `<button type="button" class="btn btn-danger btn-sm" data-act="batch-reject-apply" data-reason="${r[0]}">${esc(r[1])}</button>`;
      }).join("")}</div>`);
      return false;
    }
    if (act === "batch-reject-apply") { s.selectedProposalUids.slice().forEach(function (uid) { E.applyAction("reject-prop", { uid: uid, reason: ds.reason }); }); s.selectedProposalUids = []; E.closeModal(); return true; }
    if (act === "trash-exp") {
      if (s.apiMode === "live" && E.api) {
        E.api.writeExperience({ action: "retire", note_id: ds.id }).then(function (ok) { if (current() && ok) E.toast("已退居 retired 归档态", "warn"); });
        return false;
      }
      const x = s.experiences.find(function (i) { return i.id === ds.id; });
      if (x) x.status = "retired";
      E.toast("已退居 retired 归档态", "warn");
      return true;
    }
    if (act === "restore-exp") { const x = s.experiences.find(function (i) { return i.id === ds.id; }); if (x) x.status = "active"; E.toast("已恢复为 active 生效态"); return true; }
    if (act === "open-new-exp") { E.openNewExpModal(); return false; }
    if (act === "submit-new-exp") {
      const doc = root.document;
      const reqId = doc.getElementById("new-exp-reqid").value.trim();
      const cat = doc.getElementById("new-exp-cat").value;
      const prob = doc.getElementById("new-exp-prob").value.trim() || "新建手工结构化经验";
      const sol = doc.getElementById("new-exp-sol").value.trim() || "通过快速通道原子写入个人经验库。";
      const reus = doc.getElementById("new-exp-reus").value.trim() || "保持规范积累与定期复用。";
      if (s.apiMode === "live" && E.api) {
        E.api.writeExperience({ action: "create", request_id: reqId, category: cat, kind: "procedure", problem: prob, solution: sol, reusable_experience: reus })
          .then(function (ok) { if (current() && ok) { E.closeModal(); E.toast("已签发 Ticket 写入正式经验库"); } });
        return false;
      }
      if (s.experiences.some(function (x) { return x.reqId === reqId; })) { E.toast("命中幂等键去重：仅写入一次"); E.closeModal(); return false; }
      const nid = "manual-" + Date.now().toString(16);
      s.experiences.unshift({
        id: nid, reqId: reqId, category: cat, origin: "manual", model: "manual_web", status: "active", kind: "procedure", revision: 1,
        problem: prob, solution: sol, reusable: reus,
        misbelief: "", scope: { mode: "手工快速录入" }, evidenceCount: 1, betaA: 3, betaB: 0, stability: 0.85, updatedAt: "刚刚"
      });
      s.expSubtab = "list"; s.selectedExpId = nid; E.closeModal(); E.toast("经验已保存并写入经验库"); return true;
    }
    if (act === "rb-dry-run") {
      const sel = root.document && root.document.getElementById("rb-model-sel");
      if (sel) s.rollbackState.model = sel.value;
      if (s.apiMode === "live" && E.api) {
        E.api.rollbackDryRun(s.rollbackState.model).then(function (res) {
          if(!current())return;
          if (res.status === 200 && res.data.ok) {
            s.rollbackState.plan = res.data.plan;
            const ops = (res.data.plan && res.data.plan.operations) || [];
            s.rollbackState.affected = ops.map(function (op) {
              const hit = s.experiences.find(function (e) { return e.id === op.note_id; });
              return hit || { id: op.note_id, problem: (op.action || "rollback") + " · " + op.note_id, model: s.rollbackState.model };
            });
            s.rollbackState.previewed = true;
            E.renderAll();
          } else E.toast("预演失败", "danger");
        });
        return false;
      }
      s.rollbackState.affected = s.experiences.filter(function (x) { return x.model === s.rollbackState.model && x.status === "active"; });
      s.rollbackState.previewed = true; return true;
    }
    if (act === "rb-apply") {
      if (E.isProduction() && !s.rollbackState.plan) { E.toast("请先完成回滚预演，当前未执行回滚。", "warn"); return false; }
      if (s.apiMode === "live" && E.api && s.rollbackState.plan) {
        E.api.rollbackExecute(s.rollbackState.plan).then(function (res) {
          if(!current())return;
          if (res.status === 200 && res.data.ok) {
            s.rollbackState.previewed = false; s.rollbackState.affected = []; s.rollbackState.plan = null;
            E.api.loadExperiences().then(function () { E.renderAll(); });
            E.toast("回滚执行成功", "warn");
          } else E.toast("回滚失败", "danger");
        });
        return false;
      }
      const n = s.rollbackState.affected.length;
      s.rollbackState.affected.forEach(function (a) { a.status = "retired"; });
      s.rollbackState.previewed = false; s.rollbackState.affected = [];
      E.toast("回滚执行成功：已退役 " + n + " 条受影响经验", "warn"); return true;
    }
    if (act === "toggle-r9") {
      if (s.apiMode === "live" && E.api) {
        if (s.r9Tripped) { E.api.resetCircuit("live"); }
        else { E.toast("当前实时提炼调度运行正常", "info"); }
        return false;
      }
      s.r9Tripped = !s.r9Tripped;
      E.toast(s.r9Tripped ? "已模拟触发 R9 自动成本熔断" : "已恢复增量提取调度", s.r9Tripped ? "danger" : "info");
      return true;
    }
    if (act === "toggle-proj-del") {
      const p = s.historyProjects.find(function (i) { return i.key === ds.key; });
      if (s.apiMode === "live" && E.api && p) { E.api.manageHistoryProject(p.deleted ? "restore" : "delete", p.key, p.title); return false; }
      if (p) { p.deleted = !p.deleted; E.toast(p.deleted ? "项目已移入回收站" : "项目已恢复正常展示"); }
      return true;
    }
    if (act === "promote-cat") { const c = s.categoriesEvo.find(function (i) { return i.category === ds.cat; }); if (c) { c.mode = c.mode === "review" ? "partial" : "auto"; E.toast(c.category + " 已推进至 " + c.mode); } return true; }
    if (act === "demote-cat") { const c = s.categoriesEvo.find(function (i) { return i.category === ds.cat; }); if (c) { c.mode = "review"; c.liveConcordance = 0.72; E.toast(c.category + " 一致率回落，已退回人工审阅", "danger"); } return true; }
    if (act === "toggle-dormant") { const x = s.experiences.find(function (i) { return i.id === ds.id; }); if (x) { x.status = x.status === "retired" ? "active" : "retired"; E.toast("已切换状态为 " + x.status); } return true; }
    if (act === "publish-handbook") { s.handbookPublishedCount += 1; E.toast("已发布更新至 " + s.handbookTarget + " (#" + s.handbookPublishedCount + ")"); return true; }
    if (act === "habit-confirm") { const h = s.habits.find(function (i) { return i.id === ds.id; }); if (h) h.status = "active"; E.toast("已采纳为个人工程习惯"); return true; }
    if (act === "habit-dismiss") { const h = s.habits.find(function (i) { return i.id === ds.id; }); if (h) h.status = "dismissed"; return true; }
    if (act === "err-preset") { s.errorInput = ds.text; return true; }
    if (act === "skill-filter") { s.skillFilter = ds.val; return true; }
    if (act === "eval-skill") { const k = s.skills.find(function (i) { return i.id === ds.id; }); if (k) { k.evalReport = { sessions: 10, errorDropPct: -72, tokenSavePct: 31 }; E.toast("反事实回放评测完成：报错率 -72%，Token 节省 +31%"); } return true; }
    if (act === "skill-stage") { const k = s.skills.find(function (i) { return i.id === ds.id; }); if (k) { k.lifecycle = ds.next; E.toast("Skill 状态已更新为 " + ds.next); } return true; }
    if (act === "toggle-user") {
      const u = s.controlUsers.find(function (i) { return i.id === ds.id; });
      if (s.apiMode === "live" && E.api && u) { E.api.postControlAction({ action: "update_user", id: u.id, enabled: !u.enabled, revision: u.revision }); return false; }
      if (u) { u.enabled = !u.enabled; if (!u.enabled) u.activeSessions = 0; E.toast(u.enabled ? "成员已启用" : "成员已停用并吊销会话", "warn"); }
      return true;
    }
    if (act === "toggle-grant") {
      const g = s.grants[Number(ds.idx)];
      if (s.apiMode === "live" && E.api && g) {
        const nextHist = ds.field === "historyRead" ? !g.historyRead : g.historyRead;
        const nextExp = ds.field === "experienceRead" ? !g.experienceRead : g.experienceRead;
        E.api.postControlAction({ action: "set_grant", viewer_id: g.viewer, owner_id: g.owner, history_read: nextHist, experience_read: nextExp, revision: g.revision });
        return false;
      }
      if (g) { g[ds.field] = !g[ds.field]; g.revision += 1; }
      return true;
    }
    if (act === "cmd-jump") { E.applyAction("goto-tab", ds); E.closeModal(); return true; }
    if (act === "open-cmd") { E.openCmdPalette(""); return false; }
    if (act === "close-drawer") { E.closeDrawer(); return false; }
    if (act === "close-modal") { E.closeModal(); return false; }
    if (act === "reset-demo") { E.state = E.createInitialState(); E.closeDrawer(); E.toast("演示状态已恢复默认"); return true; }
    if (act.indexOf("open-") === 0) { if (E.openL3Drawer) E.openL3Drawer(act, ds); return false; }
    return false;
  };

  E.bindEvents = function () {
    const doc = root.document;
    if (!doc) return;
    doc.addEventListener("click", function (ev) {
      if (ev.target && ev.target.id === "drawer-backdrop") { E.closeDrawer(); return; }
      if (ev.target && ev.target.id === "modal-backdrop") { E.closeModal(); return; }
      if (ev.target && ev.target.classList && ev.target.classList.contains("prop-check")) return;
      const el = ev.target.closest("[data-act]");
      if (!el) return;
      if (el.tagName === "A") ev.preventDefault();
      if (E.applyAction(el.getAttribute("data-act"), el.dataset || {})) E.renderAll();
    });
    doc.addEventListener("change", function (ev) {
      const s = E.state;
      const t = ev.target;
      if (t.id === "ctx-lib-sel") {
        if (E.session && !E.session.isReady()) return;
        if (s.apiMode === "live" && E.api) { E.api.switchLibrary(t.value); return; }
        s.libraryCtx = t.value;
        E.toast("切换视角：" + t.options[t.selectedIndex].text);
        E.renderAll();
      }
      else if (t.id === "theme-preset") { s.theme = t.value; E.renderAll(); }
      else if (t.id === "stage-filter-sel") { s.stageFilter = t.value; E.renderAll(); }
      else if (t.id === "chk-shadow-mode") { s.shadowMode = t.checked; E.renderAll(); }
      else if (t.classList.contains("prop-check")) {
        const uid = t.getAttribute("data-uid");
        if (t.checked && s.selectedProposalUids.indexOf(uid) < 0) s.selectedProposalUids.push(uid);
        else s.selectedProposalUids = s.selectedProposalUids.filter(function (i) { return i !== uid; });
        E.renderAll();
      }
      else if (t.id === "exp-f-origin") { s.expFilters.origin = t.value; if (s.apiMode === "live" && E.api) E.api.loadExperiences().then(function () { E.renderAll(); }); else E.renderAll(); }
      else if (t.id === "exp-f-status") { s.expFilters.status = t.value; if (s.apiMode === "live" && E.api) E.api.loadExperiences().then(function () { E.renderAll(); }); else E.renderAll(); }
      else if (t.id === "int-f-track") { s.intFilters.track = t.value; if (s.apiMode === "live" && E.api) E.api.loadInterceptions().then(function () { E.renderAll(); }); else E.renderAll(); }
      else if (t.id === "int-f-cat") { s.intFilters.category = t.value; if (s.apiMode === "live" && E.api) E.api.loadInterceptions().then(function () { E.renderAll(); }); else E.renderAll(); }
      else if (t.id === "int-f-q") { s.intFilters.q = t.value; if (s.apiMode === "live" && E.api) E.api.loadInterceptions().then(function () { E.renderAll(); }); else E.renderAll(); }
      else if (t.id === "bf-scope-mode") { s.backfillScopeMode = t.value; s.backfillScopeVal = t.value === "month" ? "2026-10" : "evolvmem-pg-prod"; E.renderAll(); }
      else if (t.id === "bf-scope-val") { s.backfillScopeVal = t.value; E.renderAll(); }
      else if (t.id === "hist-proj-sel") { s.selectedProjectKey = t.value; if (s.apiMode === "live" && E.api) E.api.loadHistory().then(function () { E.renderAll(); }); else E.renderAll(); }
      else if (t.id === "hist-db-sel") { s.histDb = t.value; E.renderAll(); }
      else if (t.id === "hb-target") { s.handbookTarget = t.value; E.renderAll(); }
      else if (t.classList.contains("hb-chk")) {
        const b = s.handbookBlocks.find(function (i) { return i.id === t.getAttribute("data-id"); });
        if (b) { b.selected = t.checked; E.renderAll(); }
      }
    });
    var _histTimer = null;
    doc.addEventListener("input", function (ev) {
      const s = E.state;
      const t = ev.target;
      if (t.id === "exp-f-q") { s.expFilters.q = t.value; E.renderAll(); }
      else if (t.id === "int-f-q") { s.intFilters.q = t.value; E.renderAll(); }
      else if (t.id === "hist-q") {
        s.histQuery = t.value;
        E.renderAll();
        if (s.apiMode === "live" && E.api) {
          clearTimeout(_histTimer);
          _histTimer = setTimeout(function () { E.api.loadHistory().then(function () { E.renderAll(); }); }, 280);
        }
      }
      else if (t.id === "dormant-slider") { s.dormantSlider = Number(t.value); E.renderAll(); }
      else if (t.id === "err-tester-input") { s.errorInput = t.value; E.renderAll(); }
      else if (t.id === "cmd-search-input") { E.openCmdPalette(t.value); }
    });
    doc.addEventListener("keydown", function (ev) {
      if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "k") { ev.preventDefault(); E.openCmdPalette(""); }
      if (ev.key === "Escape") { E.closeDrawer(); E.closeModal(); }
    });
  };

  if (typeof root.document !== "undefined") {
    root.document.addEventListener("DOMContentLoaded", function () {
      const h = (root.location && root.location.hash || "").replace("#", "");
      if (h === "principles") E.applyAction("goto-tab", { route: "overview" });
      else if (h && E.MODULES.some(function (m) { return m.id === h; })) E.applyAction("goto-tab", { route: h });
      E.bindEvents();
      E.renderAll();
      if (E.api && E.api.bootstrap) E.api.bootstrap();
      root.setInterval(function () {
        if (E.state.sessionRemainingSec > 0) E.state.sessionRemainingSec -= 1;
        const el = root.document.getElementById("diag-sess-remain");
        if (el) el.textContent = formatRemain(E.state.sessionRemainingSec);
      }, 1000);
    });
    root.addEventListener('hashchange',function(){
      if(!E.session || !E.session.isReady() || !E.session.dataReady)return;
      const h=root.location.hash.replace(/^#/, '');
      if(h && h!==E.state.route && E.MODULES.some(function(m){return m.id===h;}))E.applyAction('goto-tab',{route:h});
      E.session.save();
    });
  }
})(typeof window !== "undefined" ? window : globalThis);

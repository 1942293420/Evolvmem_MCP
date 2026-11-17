/* EvolvMem Web Console v2 — Dual-Mode Live HTTP (/api/console-v2/*) & Offline Sandbox Adapter */
(function (root) {
  const E = (root.EvolvMemV2 = root.EvolvMemV2 || {});

  function canProbeServer() {
    if (typeof root.fetch !== "function" || !root.location) return false;
    return root.location.protocol === "http:" || root.location.protocol === "https:";
  }

  function qs(obj) {
    const pairs = [];
    Object.keys(obj || {}).forEach(function (k) {
      const v = obj[k];
      if (v !== undefined && v !== null && v !== "") {
        pairs.push(encodeURIComponent(k) + "=" + encodeURIComponent(String(v)));
      }
    });
    return pairs.length ? "?" + pairs.join("&") : "";
  }

  function viewKey() { return E.state.libraryCtx + ":" + (E.state.libraryEpoch || 0); }
  let experienceEpoch = 0, experienceTimer = null, historyEpoch = 0;
  function historySignature(){const s=E.state;return JSON.stringify([viewKey(),s.histScope,s.histQuery,s.selectedProjectKey,s.histSearchRange]);}
  function experienceSignature() { return JSON.stringify([viewKey(), E.state.expFilters, E.state.expOffset || 0]); }
  function clearLiveData() {
    E.state.expTotal = null; E.state.expCategoryCounts = null; E.state.expDiagnostics = null; E.state.expLoading = false; E.state.expOffset = 0;
    if (typeof E.clearLiveData === "function") { E.clearLiveData(); return; }
    Object.keys(E.state).forEach(function (key) { if (Array.isArray(E.state[key]) && key !== "availableLibraries") E.state[key] = []; });
    ["liveMetrics", "liveEvolution", "livePipeline", "liveHistory", "liveControl"].forEach(function (key) { E.state[key] = null; });
  }
  async function req(method, path, body, valid, options) {
    const scope = requestContext();
    const opts = { method: method, credentials: "same-origin", headers: {} };
    if (method === "POST") {
      opts.headers["Content-Type"] = "application/json";
      if (E.state.csrfToken) opts.headers["X-CSRF-Token"] = E.state.csrfToken;
      opts.body = JSON.stringify(body || {});
    }
    let result;
    try {
      if (E.session) result = await E.session.request(method, "/api/console-v2" + path, body, valid, options);
      else {
        const resp = await root.fetch("/api/console-v2" + path, opts);
        let data;
        try { data = await resp.json(); } catch (_e) { data = { ok: false, error: "invalid_json" }; }
        result = { status: resp.status, data: data };
      }
    } catch (_err) { result = { status: 0, data: { ok: false, error: "network_unavailable" } }; }
    if (!currentRequest(scope) || (valid && !valid())) return { status: 409, data: { ok: false, error: "stale_view" } };
    if (!result || !result.data || typeof result.data !== "object") result = { status: result ? result.status : 0, data: { ok: false, error: "invalid_json" } };
    if (result.status === 409 && ["stale_response", "stale_view", "session_not_ready"].includes(result.data.error)) return result;
    if (method === "GET" && (result.status !== 200 || !result.data.ok) &&
        (!(options && options.preserveOnError) || [401,403].includes(result.status))) {
      clearLiveData();
      if (result.status === 401) { E.state.simMode = "sso_login"; E.state.csrfToken = ""; }
      else E.state.apiError = "读取 " + path.split("?")[0] + " 失败（" + (result.data.error || result.status) + "）。请重新加载。";
      E.renderAll();
    }
    return result;
  }
  function requestContext(){return {state:E.state,view:viewKey(),session:E.session || null,ticket:E.session?E.session.capture():null};}
  function currentRequest(scope){return !!scope && scope.state===E.state && scope.view===viewKey() && (!scope.ticket || (scope.session===E.session && E.session.isCurrent(scope.ticket)));}

  function historyMetadata(block) {
    const count = value => Number.isInteger(value) && value >= 0 ? value : null;
    return { firstAt: block.observed_first_at ?? null, lastAt: block.observed_last_at ?? null,
      turns: count(block.turn_count), messageCount: count(block.message_count), timeBasis: block.time_basis || null };
  }

  function normalizeExp(n) {
    return {
      id: n.id, category: n.category, kind: n.kind || "pitfall",
      origin: n.origin || "manual", model: n.source_model || n.model || "unknown",
      status: n.status || "active", revision: n.revision || 1,
      problem: n.problem, solution: n.solution,
      reusable: n.reusable_experience || n.reusable || "",
      misbelief: n.misbelief || "", scope: n.scope || {},
      evidence: n.evidence || [], evidenceCount: n.evidence_count || (n.evidence ? n.evidence.length : 0),
      occurrenceCount: n.occurrence_count || 1, importance: n.importance || 5,
      pinned: !!n.pinned, betaA: n.confidence_a == null ? null : n.confidence_a, betaB: n.confidence_b == null ? null : n.confidence_b,
      stability: n.stability == null ? null : n.stability, updatedAt: (n.updated_at || "").slice(0, 16)
    };
  }

  function normalizeProp(p) {
    const ev = (p.evidence_preview && p.evidence_preview[0]) || {};
    return {
      uid: p.uid, track: p.source_track || "live", model: p.model || "unknown",
      agent: p.agent_client || "pipeline", category: p.category, kind: p.kind || "pitfall",
      problem: p.problem, solution: p.solution, reusable: p.reusable_experience || "",
      misbelief: p.misbelief || "", scope: p.scope || {},
      evidence: { ref: ev.ref || "", event: 0, snippet: ev.quote || "" },
      gateVerdict: p.gate_verdict, gateLayer: p.gate_reason || "",
      confidence: p.gate_confidence != null ? p.gate_confidence : 0,
      gateChain: p.gate_chain || null, review: p.review || null,
      l0Warnings: [], status: p.review ? (p.review.user_verdict === "reject" ? "rejected" : "approved") : "pending",
      rejectReason: p.review ? p.review.user_reason : null
    };
  }

  E.api = {
    async bootstrap(options) {
      if (!canProbeServer()) return false;
      let current = () => true;
      try {
        if (E.session && !(options && options.statusKnown) && !await E.session.check({resume:false})) return true;
        const session = E.session, startup=session?session.capture(false):null;
        const s = E.state, wantedLibrary = s.libraryCtx;
        s.libraryEpoch = (s.libraryEpoch || 0) + 1;
        const epoch = s.libraryEpoch;
        current = () => s===E.state && s.libraryEpoch===epoch && (!startup || (session===E.session && session.isCurrent(startup)));
        clearLiveData(); s.apiError = null; s.apiMode = "loading"; s.simMode = "normal";
        if (session) { session.dataReady=false; session.syncHeader(); }
        E.renderAll();
        const res = await req("GET", "/me");
        if(!current())return true;
        if (res.status === 401) {
          E.state.apiMode = "live";
          E.state.simMode = "sso_login";
          E.renderAll();
          return true;
        }
        if (res.status !== 200 || !res.data.ok) {
          if(res.status===409 && ["stale_response", "stale_view"].includes(res.data.error))return true;
          if(E.session && E.session.isReady())E.session.block('unavailable');
          else { s.apiMode="error"; E.renderAll(); }
          return !!E.session;
        }
        s.apiMode = "live";
        s.csrfToken = res.data.csrf_token || "";
        s.currentUser = res.data.current_user || null;
        s.ownerId = (res.data.current_user && res.data.current_user.id) || "";
        s.livePolicy = res.data.policy || {};
        s.policyLocks = Object.keys(s.livePolicy).map(function (key) { return { key: key, val: s.livePolicy[key], desc: key, locked: false }; });
        s.availableLibraries = res.data.libraries || [];
        if (!s.availableLibraries.some(function(lib){return lib.id===wantedLibrary;})) {
          s.libraryCtx = s.ownerId;
          if(E.session)E.session.discardDraft();
        } else s.libraryCtx = wantedLibrary;
        this.syncLibrarySelect();
        await this.loadOverview();
        if(!current())return true;
        if (!s.apiError && s.simMode !== "sso_login") await this.loadTab(s.route);
        if(!current())return true;
        if (s.apiError || s.simMode === "sso_login") {
          if (E.session && E.session.isReady()) E.session.block('unavailable');
          return !!E.session;
        }
        if(E.session && !E.session.isReady())return true;
        if(E.session){E.session.dataReady=true;E.session.syncHeader();}
        E.renderAll();
        if(E.session)E.session.restoreDraft();
        return true;
      } catch (_err) {
        if (!current()) return true;
        if(E.session){E.session.block('unavailable');return true;}
        E.state.apiMode = "error";
        E.state.apiError = "页面数据加载失败，请重新加载。";
        E.renderAll();
        return false;
      }
    },

    async switchLibrary(library) {
      if (E.session && !E.session.switchLibrary(library)) return false;
      const s = E.state;
      s.libraryCtx = library;
      s.libraryEpoch = (s.libraryEpoch || 0) + 1;
      const scope = requestContext();
      clearLiveData(); s.apiError = null; s.pageLoading = true; E.renderAll();
      E.closeDrawer();
      await this.loadOverview();
      if (!currentRequest(scope)) return false;
      if (!s.apiError && s.simMode !== "sso_login") await this.loadTab(s.route);
      if (!currentRequest(scope)) return false;
      s.pageLoading = false;
      if (s.apiError || s.simMode === "sso_login") {
        if (E.session && E.session.isReady()) E.session.block('unavailable');
        else E.renderAll();
        return false;
      }
      this.syncLibrarySelect();
      if (E.session) { E.session.dataReady=true; E.session.syncHeader(); }
      E.renderAll();
      if (E.session) E.session.restoreDraft();
      return true;
    },

    syncLibrarySelect() {
      if (typeof root.document === "undefined" || !E.state.availableLibraries) return;
      const sel = root.document.getElementById("ctx-lib-sel");
      if (!sel) return;
      sel.innerHTML = E.state.availableLibraries.map(function (lib) {
        const isSelf = lib.id === E.state.ownerId;
        const label = (lib.display_name || lib.id) + (isSelf ? " (个人主库 · 可读写)" : " (他人授权 · 只读)");
        return '<option value="' + E.esc(lib.id) + '"' + (E.state.libraryCtx === lib.id ? " selected" : "") + ">" + E.esc(label) + "</option>";
      }).join("");
    },

    async loadOverview(options) {
      const requestScope=requestContext();
      if (E.state.apiMode !== "live") return;
      const s = E.state;
      const oid = s.libraryCtx === s.ownerId ? "" : s.libraryCtx;
      const res = await req("GET", "/overview" + qs({ owner_id: oid }), undefined, undefined, options);
      if(!currentRequest(requestScope))return null;
      if (res.status === 200 && res.data.ok) {
        s.liveMetrics = res.data.metrics;
        s.changesStream = [];
        if (res.data.interception_funnel) s.interceptionFunnel = res.data.interception_funnel;
        if (res.data.policy && typeof res.data.policy.shadow_mode === "boolean") s.shadowMode = res.data.policy.shadow_mode;
        if (Array.isArray(res.data.circuits)) {
          s.r9Tripped = res.data.circuits.some(function (c) { return !!c.paused; });
        }
        if (Array.isArray(res.data.recent_events)) {
          s.changesStream = res.data.recent_events.map(function (e) {
            return {
              seq: e.seq, actor: e.agent_client || s.ownerId, model: e.model || "unknown",
              noteId: e.note_id, action: e.action, ts: (e.created_at || "").slice(0, 16)
            };
          });
          s.changesCursor = s.changesStream.length ? s.changesStream[0].seq : 0;
        }
        return true;
      }
      return false;
    },

    scheduleExperienceSearch() {
      ++experienceEpoch;
      clearTimeout(experienceTimer);
      const scope = requestContext();
      E.state.expOffset = 0; E.state.expLoading = true; E.renderAll();
      experienceTimer = setTimeout(() => {
        if (currentRequest(scope) && E.state.apiMode === "live") this.loadExperiences().then(() => E.renderAll());
      }, 280);
    },

    async loadExperiences(options) {
      const requestScope=requestContext();
      if (E.state.apiMode !== "live") return;
      clearTimeout(experienceTimer);
      const epoch=++experienceEpoch, signature=experienceSignature();
      const valid=()=>currentRequest(requestScope) && epoch===experienceEpoch && signature===experienceSignature();
      const s=E.state, f=s.expFilters;
      s.expLoading=true;
      const oid=s.libraryCtx===s.ownerId ? "" : s.libraryCtx;
      const st=E.canWrite() ? f.status : "active";
      const res=await req("GET", "/experiences"+qs({owner_id:oid,category:f.category,
        origin:f.origin,status:st,q:f.q,limit:60,offset:s.expOffset || 0}), undefined, valid, options);
      if (!valid()) return null;
      s.expLoading=false;
      if (res.status===200 && res.data.ok) {
        s.experiences=(res.data.items || []).map(normalizeExp);
        s.expTotal=res.data.total; s.expCategoryCounts=res.data.category_counts;
        s.expDiagnostics=res.data.diagnostics || null; s.apiError=null;
        if (res.data.interception_funnel) s.interceptionFunnel=res.data.interception_funnel;
        return true;
      }
      return false;
    },

    async loadInterceptions() {
      const requestScope=requestContext();
      if (E.state.apiMode !== "live" || !E.canWrite()) return;
      const s=E.state, f = s.intFilters;
      const res = await req("GET", "/interceptions" + qs({ layer: f.layer, track: f.track, category: f.category, q: f.q }));
      if(!currentRequest(requestScope))return null;
      if (res.status === 200 && res.data.ok) {
        s.interceptions = res.data.items || [];
        if (res.data.funnel) s.interceptionFunnel = res.data.funnel;
      }
    },

    async loadInbox() {
      const requestScope=requestContext();
      if (E.state.apiMode !== "live" || !E.canWrite()) return;
      const s=E.state;
      const res = await req("GET", "/review-inbox" + qs({ source_track: s.inboxTrack, include_decided: "1" }));
      if(!currentRequest(requestScope))return null;
      if (res.status === 200 && res.data.ok) {
        const others = s.proposals.filter(function (p) { return p.track !== s.inboxTrack; });
        s.proposals = (res.data.items || []).map(normalizeProp).concat(others);
        if (res.data.track_agreement) s.trackAgreement = res.data.track_agreement;
        s.shadowMode = !!res.data.shadow_mode;
      }
    },

    async loadTab(route) {
      const requestScope=requestContext();
      if (E.state.apiMode !== "live") return;
      E.state.apiError = null;
      if (route === "overview") await this.loadOverview();
      else if (route === "review-inbox") await this.loadInbox();
      else if (route === "pipelines" || (route === "experiences" && E.state.expSubtab === "pipelines")) {
        await this.loadPipeline();
        if(!currentRequest(requestScope))return null;
      } else if (route === "experiences") {
        if (E.state.expSubtab === "interceptions") await this.loadInterceptions();
        else await this.loadExperiences();
      } else if (route === "history") await this.loadHistory();
      else if (route === "evolution" || route === "skills" || route === "delivery") await this.loadEvolutionAndSkills();
      else if (route === "control") await this.loadControl();
      if(!currentRequest(requestScope))return null;
      E.renderAll();
    },

    async loadPipeline() {
      const requestScope=requestContext();
      if (E.state.apiMode !== "live" || !E.canWrite()) return;
      const s = E.state;
      const res = await req("GET", "/pipeline");
      if(!currentRequest(requestScope))return null;
      if (res.status === 200 && res.data.ok) {
        s.livePipeline = res.data;
        s.llmCalls = [];
        if (Array.isArray(res.data.circuits)) {
          s.r9Tripped = res.data.circuits.some(function (c) { return !!c.paused; });
        }
        if (Array.isArray(res.data.llm_usage)) {
          s.llmCalls = res.data.llm_usage.map(function (c, i) {
            return {
              id: i + 1, track: c.track, stage: c.stage, model: c.model,
              inTok: c.input_tokens || 0, outTok: c.output_tokens || 0,
              cost: Number(c.cost_estimate || 0), outcome: (c.calls || 0) + " calls", ref: "-"
            };
          });
        }
      }
    },

    async loadBlockTurns(blockId, more) {
      const requestScope=requestContext(), s=E.state;
      if(s.apiMode!=="live" || !blockId)return;
      const blk=s.historyBlocks.find(b=>b.id===blockId);
      if(!blk || blk._turnsLoading || (more && blk._hasMore===false))return;
      if(!more && blk._turnsLoaded)return;
      const offset=more && blk._turnsLoaded?(blk._nextOffset || 0):0, limit=40;
      const valid=()=>currentRequest(requestScope) && s.historyBlocks.includes(blk);
      blk._turnsLoading=true;blk._turnsError=null;
      try {
        const res=await req("GET", "/history"+qs({view:"turns",block_id:blockId,owner_id:s.libraryCtx===s.ownerId?"":s.libraryCtx,limit,offset}),undefined,valid,{preserveOnError:true});
        if(!valid())return;
        if(res.status!==200 || !res.data.ok || res.data.pg_connected===false || !Array.isArray(res.data.messages)) {
          blk._turnsError="消息读取失败，已显示内容保留，请重试。";return;
        }
        const page=res.data.messages;
        blk.messages=offset?(blk.messages || []).concat(page):page;
        blk.timeBasis=res.data.time_basis || blk.timeBasis || null;
        blk.transcript=blk.messages.map(m=>(m.role==="assistant"?"助手":m.role==="user"?"用户":m.role || "消息")+"："+String(m.content || ""));
        blk.transcriptTimes=blk.messages.map(m=>m.recorded_at ?? null);
        blk.transcriptTimeBases=blk.messages.map(m=>m.time_basis || blk.timeBasis);
        blk._nextOffset=Number.isInteger(res.data.next_offset)?res.data.next_offset:offset+page.length;
        blk._hasMore=typeof res.data.has_more==="boolean"?res.data.has_more:page.length===limit;
        blk._turnsLoaded=true;
      } finally {blk._turnsLoading=false;}
    },

    async loadHistory() {
      const requestScope=requestContext();
      if (E.state.apiMode !== "live") return;
      const s = E.state;
      const epoch=++historyEpoch;let signature=historySignature();
      const valid=()=>currentRequest(requestScope) && epoch===historyEpoch && signature===historySignature();
      const view = viewKey();
      const oid = s.libraryCtx === s.ownerId ? "" : s.libraryCtx;
      const res = await req("GET", "/history" + qs({ view: "projects", grouping: "workspace", owner_id: oid }), undefined, valid);
      if (!valid() || view !== viewKey() || res.status !== 200 || !res.data.ok) return;
      s.liveHistory = res.data;
      const seen = {};
      const projs = (res.data.projects || []).map(function (p) {
        const key = p.project;
        seen[key] = true;
        return {
          key: key, title: p.display_name || key, sessions: p.block_count || 0,
          blocks: p.block_count || 0, deleted: p.status === "deleted", kind: "workspace", workspacePath: p.workspace_path || "",
          lastAt: (p.last_at || "").slice(0, 16)
        };
      });
      if (E.canWrite()) {
        const tRes = await req("GET", "/history" + qs({ view: "trash", owner_id: oid }), undefined, valid);
        if (!valid() || view !== viewKey() || tRes.status !== 200 || !tRes.data.ok) return;
        if (tRes.status === 200 && tRes.data.ok) {
          (tRes.data.items || []).forEach(function (it) {
            if (it.kind === "project" || it.kind === "workspace") {
              const k = it.project || it.key;
              const hit = projs.find(function (p) { return p.key === k && p.kind === it.kind; });
              if (hit) hit.deleted = true;
              else if (k && !seen[k]) {
                seen[k] = true;
                projs.push({ key: k, title: it.title || k, sessions: it.block_count || 0, blocks: it.block_count || 0, deleted: true, kind: it.kind, workspacePath: it.workspace_path || "", lastAt: (it.deleted_at || "").slice(0, 16) });
              }
            }
          });
        }
      }
      s.historyProjects = projs;
      const active=projs.filter(p=>!p.deleted);
      if(!active.some(p=>p.key===s.selectedProjectKey))s.selectedProjectKey=active[0]?.key || null;
      signature=historySignature();
      const q = (s.histQuery || "").trim();
      if (s.histScope === "unassigned" && !q) {
        const uRes = await req("GET", "/history" + qs({ view: "unassigned", grouping: "workspace", owner_id: oid }), undefined, valid);
        if (!valid() || view !== viewKey() || uRes.status !== 200 || !uRes.data.ok) return;
        if (uRes.status === 200 && uRes.data.ok) {
          s.historyBlocks = (uRes.data.blocks || []).map(function (b) {
            return {
              id: b.block_id, project: null, workspacePath: "", workspaceReason: b.reason || "", title: b.title || b.block_id,
              sourceRef: "pg:" + b.block_id, ...historyMetadata(b),
              bm25: 0, trgm: 0, vector: 0, rrf: 1, hasAttachment: false, attachmentBytes: 0, transcript: []
            };
          });
        }
      } else if (q && s.histScope !== "trash") {
        if(s.histScope === "projects" && s.histSearchRange !== "all" && !s.selectedProjectKey){s.historyBlocks=[];s.selectedBlockId=null;return;}
        const sRes = await req("GET", "/history" + qs({ view: "search", grouping: "workspace", scope: s.histScope === "unassigned" ? "unassigned" : "", project: s.histScope === "projects" && s.histSearchRange !== "all" ? s.selectedProjectKey : "", q: q, owner_id: oid }), undefined, valid);
        if (!valid() || view !== viewKey() || sRes.status !== 200 || !sRes.data.ok) return;
        if (sRes.status === 200 && sRes.data.ok) {
          s.historyBlocks = (sRes.data.items || []).map(function (it) {
            return {
              id: it.block_id, project: (it.projects && it.projects[0]) || null, workspacePath: it.workspace_path || "", workspaceReason: it.reason || "",
              title: it.title || it.block_id, sourceRef: "pg:" + it.block_id, ...historyMetadata(it),
              bm25: 0, trgm: 0, vector: 0, rrf: Math.min(1, Number(it.score || 0.5)),
              hasAttachment: false, attachmentBytes: 0, transcript: it.snippet ? ["摘要：" + it.snippet] : []
            };
          });
        }
      } else if (s.histScope !== "trash") {
        const activeProjs = projs.filter(function (p) { return !p.deleted; });
        const curKey = activeProjs.some(function (p) { return p.key === s.selectedProjectKey; })
          ? s.selectedProjectKey : (activeProjs[0] && activeProjs[0].key);
        s.selectedProjectKey = curKey || null;
        if (curKey) {
          const pRes = await req("GET", "/history" + qs({ view: "project", grouping: "workspace", project_id: curKey, owner_id: oid }), undefined, valid);
          if (!valid() || view !== viewKey() || pRes.status !== 200 || !pRes.data.ok) return;
          if (pRes.status === 200 && pRes.data.ok) {
            s.historyBlocks = (pRes.data.blocks || []).map(function (b) {
              return {
                id: b.block_id, project: curKey, workspacePath: b.workspace_path || (activeProjs.find(p => p.key === curKey) || {}).workspacePath || "", title: b.title || b.block_id,
                sourceRef: "pg:" + b.block_id, ...historyMetadata(b),
                bm25: 0, trgm: 0, vector: 0, rrf: 1, hasAttachment: false, attachmentBytes: 0, transcript: []
              };
            });
          }
        } else s.historyBlocks = [];
      }
      if (s.historyBlocks.length && (!s.selectedBlockId || !s.historyBlocks.some(function (b) { return b.id === s.selectedBlockId; }))) {
        s.selectedBlockId = s.historyBlocks[0].id;
      }
      if (s.selectedBlockId && s.histScope !== "trash") {
        await this.loadBlockTurns(s.selectedBlockId);
        if(!valid())return null;
      }
    },

    async manageHistoryProject(action, key, title) {
      const requestScope=requestContext();
      const project = E.state.historyProjects.find(function (p) { return p.key === key; });
      const kind = project && project.kind === "workspace" ? "workspace" : "project";
      const res = await req("POST", "/history/manage", { action: action, kind: kind, project: key, key: key, title: title });
      if(!currentRequest(requestScope))return null;
      if (res.status !== 200 || !res.data.ok) {
        E.toast("历史" + (kind === "workspace" ? "文件夹" : "项目") + "操作失败：" + (res.data.error || res.status), "danger");
        return false;
      }
      await this.loadHistory();
      if(!currentRequest(requestScope))return null;
      E.toast((kind === "workspace" ? "文件夹" : "项目") + (action === "delete" ? "已移入回收站" : "已恢复正常展示"));
      E.renderAll();
      return true;
    },

    async loadEvolutionAndSkills() {
      const requestScope=requestContext();
      if (E.state.apiMode !== "live") return;
      const s = E.state;
      const oid = s.libraryCtx === s.ownerId ? "" : s.libraryCtx;
      const res = await req("GET", "/evolution-and-skills" + qs({ owner_id: oid }));
      if(!currentRequest(requestScope))return null;
      if (res.status === 200 && res.data.ok) {
        s.liveEvolution = res.data;
        const cat = res.data.skills_catalog || {};
        s.skills = [];
        if (Array.isArray(cat.rows)) {
          s.skills = cat.rows.map(function (r) {
            const pTitle = (r.presentation && r.presentation.title) || r.name;
            const cName = (r.categories && r.categories[0]) || r.origin || "官方技能";
            return {
              id: r.id || r.name, name: r.name, title: pTitle, category: cName,
              lifecycle: "catalog", tokenCost: null,
              summary: (r.presentation && r.presentation.description) || r.description || "", attachedPitfalls: [], linuxSynced: null,
              winMode: "安装与分发状态尚未核验", catalogSource: r.source || r.origin || "", fileCount: r.file_count,
              evalReport: null,
              mdPreview: null
            };
          });
        }
      }
    },

    async loadControl() {
      const requestScope=requestContext();
      if (E.state.apiMode !== "live") return;
      const s = E.state;
      const oid = s.libraryCtx === s.ownerId ? "" : s.libraryCtx;
      const res = await req("GET", "/control" + qs({ owner_id: oid }));
      if(!currentRequest(requestScope))return null;
      if (res.status === 200 && res.data.ok) {
        s.liveControl = res.data;
        s.controlUsers = []; s.grants = []; s.agents = [];
        if (Array.isArray(res.data.users)) {
          s.controlUsers = res.data.users.map(function (u) {
            return {
              id: u.id, displayName: u.display_name || u.id, isAdmin: !!u.is_admin,
              enabled: !!u.enabled, revision: u.revision || 0, activeSessions: null
            };
          });
        }
        if (Array.isArray(res.data.grants)) {
          s.grants = res.data.grants.map(function (g) {
            return {
              viewer: g.viewer_id, owner: g.owner_id, historyRead: !!g.history_read,
              experienceRead: !!g.experience_read, revision: g.revision || 0
            };
          });
        }
        if (Array.isArray(res.data.model_reputation)) {
          s.agents = res.data.model_reputation.map(function (r) {
            return {
              agent: r.agent_client + ":" + r.model, title: r.agent_client + " (" + r.model + ")",
              os: "PG Verified", client: r.agent_client, model: r.model, tier: r.tier,
              quota: r.effective_quota, status: r.effective_quota > 0 ? "online" : "limited",
              activeProject: "PG 主库", lastSyncAt: "实时",
              syncNote: "提议 " + r.proposed + " · 采纳 " + r.accepted + (r.alert ? " [" + r.alert + "]" : "")
            };
          });
        }
      }
    },

    async postControlAction(body) {
      const requestScope=requestContext();
      const res = await req("POST", "/control/action", body);
      if(!currentRequest(requestScope))return null;
      if (res.status !== 200 || !res.data.ok) {
        E.toast("权限变更失败：" + (res.data.error || res.status), "danger");
        return false;
      }
      await this.loadControl();
      if(!currentRequest(requestScope))return null;
      E.toast("控制面权限已保存至 PG");
      E.renderAll();
      return true;
    },

    async refreshExperienceViews() {
      const scope=requestContext();
      const list=await this.loadExperiences({preserveOnError:true});
      if (!currentRequest(scope)) return null;
      const overview=await this.loadOverview({preserveOnError:true});
      if (!currentRequest(scope)) return null;
      return list===true && overview===true;
    },

    async writeExperience(body, options) {
      const requestScope=requestContext();
      const res = await req("POST", "/experiences/write", body);
      if(!currentRequest(requestScope))return null;
      if ((res.status !== 200 && res.status !== 201) || !res.data.ok) {
        if (options && options.returnError) return {ok:false,status:res.status,error:res.data.error || String(res.status)};
        E.toast("经验写入失败：" + (res.data.error || res.status), "danger");
        return null;
      }
      if (options && options.deferRefresh) return res.data;
      await this.loadExperiences();
      if(!currentRequest(requestScope))return null;
      await this.loadOverview();
      if(!currentRequest(requestScope))return null;
      E.renderAll();
      return res.data;
    },

    async rescueInterception(itemId, editedPayload, note) {
      const requestScope=requestContext();
      const res = await req("POST", "/interceptions/rescue", { item_id: itemId, edited_payload: editedPayload, note: note });
      if(!currentRequest(requestScope))return null;
      if (res.status !== 200 || !res.data.ok) {
        E.toast("救回失败：" + (res.data.error || res.status), "danger");
        return false;
      }
      await this.loadInterceptions();
      if(!currentRequest(requestScope))return null;
      await this.loadExperiences();
      if(!currentRequest(requestScope))return null;
      E.toast("已签发 Ticket 人工救回并写入经验库！");
      E.renderAll();
      return true;
    },

    async fetchExpTrace(noteId) {
      const requestScope=requestContext();
      if (E.state.apiMode !== "live") return null;
      const oid = E.state.libraryCtx === E.state.ownerId ? "" : E.state.libraryCtx;
      const res = await req("GET", "/experiences/" + encodeURIComponent(noteId) + "/trace" + qs({ owner_id: oid }));
      if(!currentRequest(requestScope))return null;
      return res.status === 200 && res.data.ok ? res.data : null;
    },

    async fetchEvidenceContext(ref) {
      const requestScope=requestContext();
      if (E.state.apiMode !== "live") return null;
      const oid = E.state.libraryCtx === E.state.ownerId ? "" : E.state.libraryCtx;
      const res = await req("GET", "/evidence-context" + qs({ ref: ref, owner_id: oid }));
      if(!currentRequest(requestScope))return null;
      return res.status === 200 && res.data.ok ? res.data : null;
    },

    async decideProposal(uid, verdict, reason, edits) {
      const requestScope=requestContext();
      const res = await req("POST", "/review-inbox/decide", { proposal_uid: uid, user_verdict: verdict, user_reason: reason, edited_payload: edits });
      if(!currentRequest(requestScope))return null;
      if (res.status !== 200 || !res.data.ok) {
        E.toast("提交失败：" + (res.data.error || res.status), "danger");
        return false;
      }
      await this.loadInbox();
      if(!currentRequest(requestScope))return null;
      E.renderAll();
      return true;
    },

    async rollbackDryRun(model) {
      const requestScope=requestContext();
      return await req("POST", "/experiences/rollback", { mode: "dry_run", agent_client: "codex", model: model });
    },

    async rollbackExecute(plan) {
      const requestScope=requestContext();
      return await req("POST", "/experiences/rollback", { mode: "execute", plan: plan });
    },

    async resetCircuit(track) {
      const requestScope=requestContext();
      const res = await req("POST", "/control/action", { action: "reset_circuit", track: track || "live" });
      if(!currentRequest(requestScope))return null;
      if (res.status !== 200 || !res.data.ok) {
        E.toast("解除熔断失败：" + ((res.data && res.data.error) || res.status), "danger");
        return false;
      }
      await this.loadOverview();
      if(!currentRequest(requestScope))return null;
      await this.loadPipeline();
      if(!currentRequest(requestScope))return null;
      E.toast("已解除熔断并恢复实时提炼调度", "info");
      E.renderAll();
      return true;
    }
  };
})(typeof window !== "undefined" ? window : globalThis);

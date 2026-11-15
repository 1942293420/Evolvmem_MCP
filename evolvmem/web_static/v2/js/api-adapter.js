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

  async function req(method, path, body) {
    const opts = { method: method, credentials: "same-origin", headers: {} };
    if (method === "POST") {
      opts.headers["Content-Type"] = "application/json";
      if (E.state.csrfToken) opts.headers["X-CSRF-Token"] = E.state.csrfToken;
      opts.body = JSON.stringify(body || {});
    }
    const resp = await root.fetch("/api/console-v2" + path, opts);
    let data = null;
    try { data = await resp.json(); } catch (_e) { data = { ok: false, error: "invalid_json" }; }
    return { status: resp.status, data: data || {} };
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
      pinned: !!n.pinned, betaA: Math.max(1, n.occurrence_count || 1), betaB: n.status === "retired" ? 3 : 0,
      stability: n.status === "retired" ? 0.35 : 0.90, updatedAt: (n.updated_at || "").slice(0, 16)
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
    async bootstrap() {
      if (!canProbeServer()) return false;
      try {
        const res = await req("GET", "/me");
        if (res.status === 401) {
          E.state.apiMode = "live";
          E.state.simMode = "sso_login";
          E.renderAll();
          return true;
        }
        if (res.status !== 200 || !res.data.ok) return false;
        const s = E.state;
        s.apiMode = "live";
        s.csrfToken = res.data.csrf_token || "";
        s.ownerId = (res.data.current_user && res.data.current_user.id) || "owner_main";
        s.availableLibraries = res.data.libraries || [];
        s.libraryCtx = s.ownerId;
        this.syncLibrarySelect();
        await this.loadOverview();
        await this.loadTab(s.route);
        E.renderAll();
        return true;
      } catch (_err) {
        return false;
      }
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

    async loadOverview() {
      if (E.state.apiMode !== "live") return;
      const oid = E.state.libraryCtx === E.state.ownerId ? "" : E.state.libraryCtx;
      const res = await req("GET", "/overview" + qs({ owner_id: oid }));
      if (res.status === 200 && res.data.ok) {
        E.state.liveMetrics = res.data.metrics;
        if (res.data.interception_funnel) E.state.interceptionFunnel = res.data.interception_funnel;
        if (res.data.policy && typeof res.data.policy.shadow_mode === "boolean") E.state.shadowMode = res.data.policy.shadow_mode;
      }
    },

    async loadExperiences() {
      if (E.state.apiMode !== "live") return;
      const f = E.state.expFilters;
      const oid = E.state.libraryCtx === E.state.ownerId ? "" : E.state.libraryCtx;
      const st = E.canWrite() ? f.status : "active";
      const res = await req("GET", "/experiences" + qs({ owner_id: oid, category: f.category, origin: f.origin, status: st, q: f.q }));
      if (res.status === 200 && res.data.ok) {
        E.state.experiences = (res.data.items || []).map(normalizeExp);
        if (res.data.interception_funnel) E.state.interceptionFunnel = res.data.interception_funnel;
      }
    },

    async loadInterceptions() {
      if (E.state.apiMode !== "live" || !E.canWrite()) return;
      const f = E.state.intFilters;
      const res = await req("GET", "/interceptions" + qs({ layer: f.layer, track: f.track, category: f.category, q: f.q }));
      if (res.status === 200 && res.data.ok) {
        E.state.interceptions = res.data.items || [];
        if (res.data.funnel) E.state.interceptionFunnel = res.data.funnel;
      }
    },

    async loadInbox() {
      if (E.state.apiMode !== "live" || !E.canWrite()) return;
      const res = await req("GET", "/review-inbox" + qs({ source_track: E.state.inboxTrack, include_decided: "1" }));
      if (res.status === 200 && res.data.ok) {
        const others = E.state.proposals.filter(function (p) { return p.track !== E.state.inboxTrack; });
        E.state.proposals = (res.data.items || []).map(normalizeProp).concat(others);
        if (res.data.track_agreement) E.state.trackAgreement = res.data.track_agreement;
        E.state.shadowMode = !!res.data.shadow_mode;
      }
    },

    async loadTab(route) {
      if (E.state.apiMode !== "live") return;
      if (route === "overview") await this.loadOverview();
      else if (route === "review-inbox") await this.loadInbox();
      else if (route === "pipelines" || (route === "experiences" && E.state.expSubtab === "pipelines")) {
        await this.loadPipeline();
      } else if (route === "experiences") {
        if (E.state.expSubtab === "interceptions") await this.loadInterceptions();
        else await this.loadExperiences();
      } else if (route === "history") await this.loadHistory();
      else if (route === "evolution" || route === "skills" || route === "delivery") await this.loadEvolutionAndSkills();
      else if (route === "control") await this.loadControl();
      E.renderAll();
    },

    async loadPipeline() {
      if (E.state.apiMode !== "live" || !E.canWrite()) return;
      const res = await req("GET", "/pipeline");
      if (res.status === 200 && res.data.ok) E.state.livePipeline = res.data;
    },

    async loadHistory() {
      if (E.state.apiMode !== "live") return;
      const oid = E.state.libraryCtx === E.state.ownerId ? "" : E.state.libraryCtx;
      const res = await req("GET", "/history" + qs({ view: "projects", owner_id: oid }));
      if (res.status === 200 && res.data.ok) E.state.liveHistory = res.data;
    },

    async loadEvolutionAndSkills() {
      if (E.state.apiMode !== "live") return;
      const oid = E.state.libraryCtx === E.state.ownerId ? "" : E.state.libraryCtx;
      const res = await req("GET", "/evolution-and-skills" + qs({ owner_id: oid }));
      if (res.status === 200 && res.data.ok) E.state.liveEvolution = res.data;
    },

    async loadControl() {
      if (E.state.apiMode !== "live") return;
      const oid = E.state.libraryCtx === E.state.ownerId ? "" : E.state.libraryCtx;
      const res = await req("GET", "/control" + qs({ owner_id: oid }));
      if (res.status === 200 && res.data.ok) E.state.liveControl = res.data;
    },

    async writeExperience(body) {
      const res = await req("POST", "/experiences/write", body);
      if ((res.status !== 200 && res.status !== 201) || !res.data.ok) {
        E.toast("经验写入失败：" + (res.data.error || res.status), "danger");
        return null;
      }
      await this.loadExperiences();
      await this.loadOverview();
      E.renderAll();
      return res.data;
    },

    async rescueInterception(itemId, editedPayload, note) {
      const res = await req("POST", "/interceptions/rescue", { item_id: itemId, edited_payload: editedPayload, note: note });
      if (res.status !== 200 || !res.data.ok) {
        E.toast("救回失败：" + (res.data.error || res.status), "danger");
        return false;
      }
      await this.loadInterceptions();
      await this.loadExperiences();
      E.toast("已签发 Ticket 人工救回并写入经验库！");
      E.renderAll();
      return true;
    },

    async fetchExpTrace(noteId) {
      if (E.state.apiMode !== "live") return null;
      const oid = E.state.libraryCtx === E.state.ownerId ? "" : E.state.libraryCtx;
      const res = await req("GET", "/experiences/" + encodeURIComponent(noteId) + "/trace" + qs({ owner_id: oid }));
      return res.status === 200 && res.data.ok ? res.data : null;
    },

    async fetchEvidenceContext(ref) {
      if (E.state.apiMode !== "live") return null;
      const oid = E.state.libraryCtx === E.state.ownerId ? "" : E.state.libraryCtx;
      const res = await req("GET", "/evidence-context" + qs({ ref: ref, owner_id: oid }));
      return res.status === 200 && res.data.ok ? res.data : null;
    },

    async decideProposal(uid, verdict, reason, edits) {
      const res = await req("POST", "/review-inbox/decide", { proposal_uid: uid, user_verdict: verdict, user_reason: reason, edited_payload: edits });
      if (res.status !== 200 || !res.data.ok) {
        E.toast("提交失败：" + (res.data.error || res.status), "danger");
        return false;
      }
      await this.loadInbox();
      E.renderAll();
      return true;
    },

    async rollbackDryRun(model) {
      return await req("POST", "/experiences/rollback", { mode: "dry_run", agent_client: "codex", model: model });
    },

    async rollbackExecute(plan) {
      return await req("POST", "/experiences/rollback", { mode: "execute", plan: plan });
    }
  };
})(typeof window !== "undefined" ? window : globalThis);

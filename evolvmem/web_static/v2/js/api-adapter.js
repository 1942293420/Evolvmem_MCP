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
      const s = E.state;
      const oid = s.libraryCtx === s.ownerId ? "" : s.libraryCtx;
      const res = await req("GET", "/overview" + qs({ owner_id: oid }));
      if (res.status === 200 && res.data.ok) {
        s.liveMetrics = res.data.metrics;
        if (res.data.interception_funnel) s.interceptionFunnel = res.data.interception_funnel;
        if (res.data.policy && typeof res.data.policy.shadow_mode === "boolean") s.shadowMode = res.data.policy.shadow_mode;
        if (Array.isArray(res.data.circuits)) {
          s.r9Tripped = res.data.circuits.some(function (c) { return !!c.paused; });
        }
        if (Array.isArray(res.data.recent_events) && res.data.recent_events.length) {
          s.changesStream = res.data.recent_events.map(function (e) {
            return {
              seq: e.seq, actor: e.agent_client || s.ownerId, model: e.model || "unknown",
              noteId: e.note_id, action: e.action, ts: (e.created_at || "").slice(0, 16)
            };
          });
          s.changesCursor = s.changesStream[0].seq || s.changesCursor;
        }
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
      const s = E.state;
      const res = await req("GET", "/pipeline");
      if (res.status === 200 && res.data.ok) {
        s.livePipeline = res.data;
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

    async loadBlockTurns(blockId) {
      if (E.state.apiMode !== "live" || !blockId) return;
      const s = E.state;
      const oid = s.libraryCtx === s.ownerId ? "" : s.libraryCtx;
      const res = await req("GET", "/history" + qs({ view: "turns", block_id: blockId, owner_id: oid, limit: 40 }));
      if (res.status === 200 && res.data.ok) {
        const blk = s.historyBlocks.find(function (b) { return b.id === blockId; });
        if (blk) {
          blk.transcript = (res.data.messages || []).map(function (m) {
            const role = m.role === "assistant" ? "助手" : m.role === "user" ? "用户" : (m.role || "消息");
            return role + "：" + String(m.content || "");
          });
          blk._turnsLoaded = true;
        }
      }
    },

    async loadHistory() {
      if (E.state.apiMode !== "live") return;
      const s = E.state;
      const oid = s.libraryCtx === s.ownerId ? "" : s.libraryCtx;
      const res = await req("GET", "/history" + qs({ view: "projects", owner_id: oid }));
      if (res.status !== 200 || !res.data.ok) return;
      s.liveHistory = res.data;
      const seen = {};
      const projs = (res.data.projects || []).map(function (p) {
        const key = p.project;
        seen[key] = true;
        return {
          key: key, title: p.display_name || key, sessions: p.block_count || 0,
          blocks: p.block_count || 0, deleted: p.status === "deleted",
          lastAt: (p.last_at || "").slice(0, 16)
        };
      });
      if (E.canWrite()) {
        const tRes = await req("GET", "/history" + qs({ view: "trash", owner_id: oid }));
        if (tRes.status === 200 && tRes.data.ok) {
          (tRes.data.items || []).forEach(function (it) {
            if (it.kind === "project") {
              const k = it.project || it.key;
              const hit = projs.find(function (p) { return p.key === k; });
              if (hit) hit.deleted = true;
              else if (k && !seen[k]) {
                seen[k] = true;
                projs.push({ key: k, title: it.title || k, sessions: 0, blocks: 0, deleted: true, lastAt: (it.deleted_at || "").slice(0, 16) });
              }
            }
          });
        }
      }
      s.historyProjects = projs;
      const q = (s.histQuery || "").trim();
      if (s.histScope === "unassigned") {
        const uRes = await req("GET", "/history" + qs({ view: "unassigned", owner_id: oid }));
        if (uRes.status === 200 && uRes.data.ok) {
          s.historyBlocks = (uRes.data.blocks || []).map(function (b) {
            return {
              id: b.block_id, project: null, title: b.title || b.block_id,
              sourceRef: "pg:" + b.block_id, turns: b.turn_count || 0,
              bm25: 0, trgm: 0, vector: 0, rrf: 1, hasAttachment: false, attachmentBytes: 0, transcript: []
            };
          });
        }
      } else if (q) {
        const sRes = await req("GET", "/history" + qs({ view: "search", q: q, owner_id: oid }));
        if (sRes.status === 200 && sRes.data.ok) {
          s.historyBlocks = (sRes.data.items || []).map(function (it) {
            return {
              id: it.block_id, project: (it.projects && it.projects[0]) || null,
              title: it.title || it.block_id, sourceRef: "pg:" + it.block_id, turns: 0,
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
          const pRes = await req("GET", "/history" + qs({ view: "project", project_id: curKey, owner_id: oid }));
          if (pRes.status === 200 && pRes.data.ok) {
            s.historyBlocks = (pRes.data.blocks || []).map(function (b) {
              return {
                id: b.block_id, project: curKey, title: b.title || b.block_id,
                sourceRef: "pg:" + b.block_id, turns: b.turn_count || 0,
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
      }
    },

    async manageHistoryProject(action, key, title) {
      const res = await req("POST", "/history/manage", { action: action, kind: "project", project: key, key: key, title: title });
      if (res.status !== 200 || !res.data.ok) {
        E.toast("历史项目操作失败：" + (res.data.error || res.status), "danger");
        return false;
      }
      await this.loadHistory();
      E.toast(action === "delete" ? "项目已移入回收站" : "项目已恢复正常展示");
      E.renderAll();
      return true;
    },

    async loadEvolutionAndSkills() {
      if (E.state.apiMode !== "live") return;
      const s = E.state;
      const oid = s.libraryCtx === s.ownerId ? "" : s.libraryCtx;
      const res = await req("GET", "/evolution-and-skills" + qs({ owner_id: oid }));
      if (res.status === 200 && res.data.ok) {
        s.liveEvolution = res.data;
        const cat = res.data.skills_catalog || {};
        if (Array.isArray(cat.rows) && cat.rows.length) {
          s.skills = cat.rows.slice(0, 36).map(function (r) {
            const pTitle = (r.presentation && r.presentation.title) || r.name;
            const cName = (r.categories && r.categories[0]) || r.origin || "官方技能";
            return {
              id: r.id || r.name, name: r.name, title: pTitle, category: cName,
              lifecycle: "active", tokenCost: Math.max(120, Number(r.file_count || 1) * 160),
              summary: r.description || "", attachedPitfalls: [], linuxSynced: true,
              winMode: "通过 LAN MCP 按需拉取或手动安装（不自动覆写 Windows）",
              evalReport: null,
              mdPreview: "# " + r.name + "\n\n" + (r.description || "") + "\n\nSource: " + (r.source || r.origin || "")
            };
          });
        }
      }
    },

    async loadControl() {
      if (E.state.apiMode !== "live") return;
      const s = E.state;
      const oid = s.libraryCtx === s.ownerId ? "" : s.libraryCtx;
      const res = await req("GET", "/control" + qs({ owner_id: oid }));
      if (res.status === 200 && res.data.ok) {
        s.liveControl = res.data;
        if (Array.isArray(res.data.users) && res.data.users.length) {
          s.controlUsers = res.data.users.map(function (u) {
            return {
              id: u.id, displayName: u.display_name || u.id, isAdmin: !!u.is_admin,
              enabled: !!u.enabled, revision: u.revision || 0, activeSessions: u.login_bound ? 1 : 0
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
        if (Array.isArray(res.data.model_reputation) && res.data.model_reputation.length) {
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
      const res = await req("POST", "/control/action", body);
      if (res.status !== 200 || !res.data.ok) {
        E.toast("权限变更失败：" + (res.data.error || res.status), "danger");
        return false;
      }
      await this.loadControl();
      E.toast("控制面权限已保存至 PG");
      E.renderAll();
      return true;
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

/* EvolvMem Web Console v2 — Views: Evolution Workshop (L2 Subtabs), Skill Grid -> Detail Page, Control & DR */
(function (root) {
  const E = (root.EvolvMemV2 = root.EvolvMemV2 || {});
  E.views = E.views || {};
  const esc = E.esc;

  E.matchPostToolUse = function (rawText) {
    const t = String(rawText || "").toLowerCase();
    if (t.indexOf("os error 206") >= 0 || t.indexOf("too long") >= 0) {
      return { sig: "win_cmd_len_overflow_206", note: E.state.experiences.find(function (x) { return x.id === "exp-01j9win206pitfall"; }) };
    }
    if (t.indexOf("role") >= 0 && (t.indexOf("does not exist") >= 0 || t.indexOf("rls") >= 0 || t.indexOf("55435") >= 0)) {
      return { sig: "pg_restore_missing_rls_role", note: E.state.experiences[0] };
    }
    if (t.indexOf("cors") >= 0 || t.indexOf("type=module") >= 0) {
      return { sig: "file_proto_esm_cors", note: E.state.experiences.find(function (x) { return x.id === "exp-01j9frontendvite09"; }) };
    }
    return { sig: t.trim() ? "unmatched_generic_error" : "empty_input", note: null };
  };

  E.views.evolution = function () {
    const s = E.state;
    const w = E.canWrite();
    const head = `
      <div class="page-head">
        <div>
          <div class="eyebrow">STAGE 6 EVOLUTION &amp; DELIVERY · 规划预置交互契约</div>
          <h1>记忆进化工坊与主动交付</h1>
          <p>按经验类别渐进自动化晋升、控制 1,500 token 预算发布常驻手册，并对工具报错精确推送单条避坑提醒。</p>
        </div>
        <span class="badge badge-evolve">阶段 6 交互契约先行</span>
      </div>
      ${!w ? `<div class="banner banner-warn">当前为他人授权只读视角：允许模拟浏览，已禁用类别晋升、休眠切换与手册发布操作。</div>` : ""}
      <div class="subtabs" role="tablist">
        <button type="button" class="subtab ${s.evoSubtab === "metrics" ? "active" : ""}" data-act="evo-subtab" data-sub="metrics">进化效果与自动晋升</button>
        <button type="button" class="subtab ${s.evoSubtab === "handbook" ? "active" : ""}" data-act="evo-subtab" data-sub="handbook">常驻手册与个人习惯</button>
        <button type="button" class="subtab ${s.evoSubtab === "errors" ? "active" : ""}" data-act="evo-subtab" data-sub="errors">即时报错避坑测试</button>
      </div>`;

    if (s.evoSubtab === "handbook") {
      const selBlocks = s.handbookBlocks.filter(function (b) { return b.selected; });
      const totalTokens = selBlocks.reduce(function (acc, b) { return acc + b.tokens; }, 0);
      const overLimit = totalTokens > 1500;
      const pct = Math.min(100, Math.round((totalTokens / 1500) * 100));
      return `${head}
        <div class="grid-asymmetric">
          <section class="panel">
            <div class="panel-head">
              <div><h2>常驻手册受控发布器</h2><p>严格限制总预算不超过 1,500 tokens，人工核准后写入项目说明文件</p></div>
              <select id="hb-target" ${w ? "" : "disabled"}>
                <option value="AGENTS.md" ${s.handbookTarget === "AGENTS.md" ? "selected" : ""}>AGENTS.md 受控区段</option>
                <option value="CLAUDE.md" ${s.handbookTarget === "CLAUDE.md" ? "selected" : ""}>CLAUDE.md 受控区段</option>
              </select>
            </div>
            <div>
              <div style="display:flex;justify-content:space-between;font-size:13.5px;margin-bottom:6px">
                <span>预算容量硬门禁（上限 1,500 tokens）</span>
                <b style="color:${overLimit ? "var(--danger)" : "var(--ok)"}">${totalTokens} / 1,500 tokens ${overLimit ? "（超出上限禁止发布）" : "（合规）"}</b>
              </div>
              <div class="funnel-track"><div class="funnel-fill" style="width:${pct}%;background:${overLimit ? "var(--danger)" : "var(--ok)"}"></div></div>
            </div>
            <div style="display:flex;flex-direction:column;gap:8px">
              ${s.handbookBlocks.map(function (b) {
                return `<label style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:11px 14px;border-radius:10px;background:var(--bg-subtle);border:1px solid var(--line);cursor:pointer">
                  <span style="display:flex;align-items:center;gap:10px"><input type="checkbox" class="hb-chk" data-id="${esc(b.id)}" ${b.selected ? "checked" : ""} ${w ? "" : "disabled"} /> <b>${esc(b.title)}</b></span>
                  <span class="badge">${b.tokens} tok</span>
                </label>`;
              }).join("")}
            </div>
            <div class="l3-bar">
              <span class="muted" style="font-size:13px">已发布次数：${s.handbookPublishedCount}</span>
              <div style="display:flex;gap:8px;flex-wrap:wrap">
                <button type="button" class="btn btn-sm" data-act="open-handbook-l3">📄 预览生成源码 (L3)</button>
                <button type="button" class="btn btn-primary" data-act="publish-handbook" ${!w || overLimit || !selBlocks.length ? "disabled" : ""}>确认发布至 ${esc(s.handbookTarget)}</button>
              </div>
            </div>
          </section>

          <section class="panel">
            <div class="panel-head"><div><h2>个人工程习惯候选池</h2><p>当同因人工纠偏出现 2 次及以上时，自动汇总为候选长期习惯</p></div></div>
            ${s.habits.map(function (h) {
              const stCls = h.status === "active" ? "badge-ok" : h.status === "dismissed" ? "badge-danger" : "badge-warn";
              const stTxt = h.status === "active" ? "已生效" : h.status === "dismissed" ? "已忽略" : "待确认";
              return `<div class="callout-box" style="display:flex;flex-direction:column;gap:8px">
                <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
                  <span class="badge">${esc(h.category)} · 重复 ${h.occurrences} 次</span>
                  <span class="badge ${stCls}">${stTxt}</span>
                </div>
                <div style="font-size:14.5px">${esc(h.text)}</div>
                ${h.status === "candidate" ? `<div style="display:flex;gap:8px">
                  <button type="button" class="btn btn-ok btn-sm" data-act="habit-confirm" data-id="${esc(h.id)}" ${w ? "" : "disabled"}>采纳为习惯</button>
                  <button type="button" class="btn btn-sm" data-act="habit-dismiss" data-id="${esc(h.id)}" ${w ? "" : "disabled"}>忽略</button>
                </div>` : ""}
              </div>`;
            }).join("")}
          </section>
        </div>`;
    }

    if (s.evoSubtab === "errors") {
      const matchRes = E.matchPostToolUse(s.errorInput);
      return `${head}
        <section class="panel" style="max-width:920px">
          <div class="panel-head">
            <div><h2>工具报错即时避坑演练台</h2><p>模拟工具执行失败后的瞬时报错签名归一化，单次严格仅推送最相关的 1 条避坑建议避免刷屏</p></div>
            <span class="badge badge-evolve">单次限推 1 条</span>
          </div>
          <div class="tag-row">
            <button type="button" class="btn btn-sm" data-act="err-preset" data-text="error: failed to run custom command: os error 206 filename or extension too long">示例：Windows 超长命令 206</button>
            <button type="button" class="btn btn-sm" data-act="err-preset" data-text='ERROR: role "evolvmem_control_owner" does not exist in RLS policy on 55435'>示例：灾备恢复缺失角色</button>
            <button type="button" class="btn btn-sm" data-act="err-preset" data-text="Blocked by CORS policy: script type=module on file://">示例：离线脚本 CORS 阻断</button>
          </div>
          <textarea id="err-tester-input" rows="3" style="width:100%" placeholder="粘贴任意终端或工具报错信息…">${esc(s.errorInput)}</textarea>
          ${matchRes.note ? `
            <div class="callout-box" style="border-left-color:var(--ok)">
              <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
                <span class="badge badge-ok">精确命中 1 条专属避坑经验</span>
                <b>${esc(matchRes.note.category)}</b>
              </div>
              <div style="font-size:15.5px">${esc(matchRes.note.solution)}</div>
            </div>` : `<div class="banner banner-info">未匹配到高置信专属避坑经验，系统自动静默跳过（零噪声打扰）。</div>`}
          <div class="l3-bar">
            <span class="muted" style="font-size:13px">底层报错归一化指纹可于三级面板核查</span>
            <button type="button" class="btn btn-sm" data-act="open-errsig-l3">🔬 查看报错签名详情 (L3)</button>
          </div>
        </section>`;
    }

    const belowThresh = s.experiences.filter(function (x) { return x.status !== "retired" && x.stability < s.dormantSlider; });
    const modeLabel = { review: "人工审阅", partial: "半自动部分放行", auto: "全自动免审" };
    return `${head}
      <section class="panel">
        <div class="panel-head">
          <div><h2>双轨进化核心效果对照（新会话 vs 旧聊天）</h2><p>两套轨道独立计算重复纠正率、单位入库成本与人工一致率，绝不混用指标</p></div>
          <button type="button" class="btn btn-sm" data-act="open-impressions-l3">🔬 查看曝光归因流水 (L3)</button>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>核心效果指标</th><th>新会话实时轨道</th><th>旧聊天回填轨道</th><th>健康达标线</th></tr></thead>
            <tbody>
              <tr><td><b>重复纠正率</b></td><td>6.2% <span class="badge badge-ok">持续下降</span></td><td>9.4%</td><td>&lt; 12%</td></tr>
              <tr><td><b>重复踩坑率</b></td><td>4.8%</td><td>7.9%</td><td>&lt; 10%</td></tr>
              <tr><td><b>注入有效使用率</b></td><td><b>68.5%</b></td><td>54.2%</td><td>&ge; 50%</td></tr>
              <tr><td><b>单条入库平均开销</b></td><td>10,059 tokens</td><td>12,958 tokens</td><td>&le; 30,000 tokens</td></tr>
              <tr><td><b>人类审阅一致率</b></td><td><b>87.5%</b></td><td>81.0%</td><td>&ge; 30%（跌破即停用保护）</td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <div class="grid-2">
        <section class="panel">
          <div class="panel-head"><div><h2>按经验分类渐进自动化开关</h2><p>一致率达标可升级半自动，若一致率下滑系统立即回退全人工把关</p></div></div>
          ${s.categoriesEvo.map(function (c) {
            const mCls = c.mode === "auto" ? "badge-ok" : c.mode === "partial" ? "badge-brand" : "badge-warn";
            return `<div style="padding:14px;border-radius:11px;background:var(--bg-subtle);border:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">
              <div>
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
                  <strong style="font-size:15.5px">${esc(c.category)}</strong>
                  <span class="badge ${mCls}">${esc(modeLabel[c.mode] || c.mode)}</span>
                </div>
                <div class="muted" style="font-size:13px">新会话一致率 ${(c.liveConcordance * 100).toFixed(0)}% · 旧聊天一致率 ${(c.backfillConcordance * 100).toFixed(0)}% · 已累计 ${c.samples} 次评判</div>
              </div>
              <div style="display:flex;gap:8px">
                <button type="button" class="btn btn-primary btn-sm" data-act="promote-cat" data-cat="${esc(c.category)}" ${w ? "" : "disabled"}>晋升 ↑</button>
                <button type="button" class="btn btn-danger btn-sm" data-act="demote-cat" data-cat="${esc(c.category)}" ${w ? "" : "disabled"}>退回人工 ↓</button>
              </div>
            </div>`;
          }).join("")}
        </section>

        <section class="panel">
          <div class="panel-head">
            <div><h2>低效经验休眠保护</h2><p>稳定性跌破阈值的经验仅停止日常主动注入，绝不从历史记录删除</p></div>
            <span class="badge">阈值 ${s.dormantSlider.toFixed(2)}</span>
          </div>
          <div style="display:flex;align-items:center;gap:12px">
            <span class="muted" style="font-size:13px">稳定性门槛：</span>
            <input type="range" id="dormant-slider" min="0.20" max="0.90" step="0.05" value="${s.dormantSlider}" style="flex:1" />
            <span class="badge badge-warn">${belowThresh.length} 条建议休眠</span>
          </div>
          ${s.experiences.filter(function (x) { return w || x.status === "active"; }).slice(0, 4).map(function (x) {
            const sleepSug = x.stability < s.dormantSlider;
            return `<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;padding:10px 12px;border-radius:10px;background:var(--bg-subtle);border:1px solid var(--line)">
              <div style="min-width:0;flex:1">
                <div class="truncate" style="font-weight:600;font-size:14px">${esc(x.problem)}</div>
                <div class="muted" style="font-size:12.5px">稳定度 ${x.stability} · ${sleepSug ? "低于当前门槛，建议休眠" : "表现稳定"}</div>
              </div>
              <button type="button" class="btn btn-sm" data-act="toggle-dormant" data-id="${esc(x.id)}" ${w ? "" : "disabled"}>${x.status === "retired" ? "重新唤醒" : "设为休眠"}</button>
            </div>`;
          }).join("")}
        </section>
      </div>`;
  };

  E.views.skills = function () {
    const s = E.state;
    const w = E.canWrite();
    const lcLabels = { all: "全部", draft: "草稿", trial: "试用中", active: "正式生效", deprecated: "已废弃" };
    const lcBadges = { draft: "badge-evolve", trial: "badge-brand", active: "badge-ok", deprecated: "badge-danger" };
    const activeCnt = s.skills.filter(function (k) { return k.lifecycle === "active"; }).length;

    if (s.selectedSkillId) {
      const k = s.skills.find(function (item) { return item.id === s.selectedSkillId; }) || s.skills[0];
      return `
        <div><button type="button" class="btn" data-act="close-skill-detail">← 返回 Skill 宫格</button></div>
        <section class="panel">
          <div class="panel-head">
            <div>
              <div class="tag-row" style="margin-bottom:8px">
                <span class="badge">${esc(k.category)}</span>
                <span class="badge ${lcBadges[k.lifecycle] || ""}">${esc(lcLabels[k.lifecycle] || k.lifecycle)}</span>
              </div>
              <h1 style="margin:0;font-size:23px">${esc(k.title)}</h1>
            </div>
            <div style="display:flex;gap:8px;flex-wrap:wrap">
              <button type="button" class="btn btn-evolve" data-act="eval-skill" data-id="${esc(k.id)}">运行反事实历史评测</button>
              ${k.lifecycle === "draft" ? `<button type="button" class="btn btn-primary" data-act="skill-stage" data-id="${esc(k.id)}" data-next="trial" ${w ? "" : "disabled"}>推进至试用期</button>` : ""}
              ${k.lifecycle === "trial" ? `<button type="button" class="btn btn-ok" data-act="skill-stage" data-id="${esc(k.id)}" data-next="active" ${w ? "" : "disabled"}>正式发布生效</button>` : ""}
              ${k.lifecycle === "active" ? `<button type="button" class="btn btn-danger" data-act="skill-stage" data-id="${esc(k.id)}" data-next="deprecated" ${w ? "" : "disabled"}>标记废弃</button>` : ""}
            </div>
          </div>
          <p class="prose-body">${esc(k.summary)}</p>
        </section>

        <div class="grid-2">
          <section class="panel">
            <div class="panel-head"><div><h2>挂载避坑清单与多端合规分发</h2><p>在复用执行方案的同时自动附上关联失败教训</p></div></div>
            <div style="display:flex;flex-direction:column;gap:8px">
              ${k.attachedPitfalls.map(function (pt) {
                return `<div class="callout-box" style="border-left-color:var(--warn)">⚠ ${esc(pt)}</div>`;
              }).join("")}
            </div>
            <div class="kv-grid">
              <div class="kv-cell"><span>Linux 服务端同步</span><b>${k.linuxSynced ? "已写入本地 Skills 目录" : "未写入（草稿/待发布）"}</b></div>
              <div class="kv-cell"><span>Windows 端交付方式</span><b>${esc(k.winMode)}</b></div>
            </div>
          </section>

          <section class="panel">
            <div class="panel-head"><div><h2>反事实历史会话回放评测</h2><p>在真实历史会话中模拟开启与关闭该 Skill 的效果对比</p></div></div>
            ${k.evalReport ? `
              <div class="kv-grid">
                <div class="kv-cell"><span>回放样本数</span><b>${k.evalReport.sessions} 场历史对话</b></div>
                <div class="kv-cell"><span>同类报错变化</span><b style="color:var(--ok)">${k.evalReport.errorDropPct}%</b></div>
                <div class="kv-cell"><span>排障 Token 节省</span><b style="color:var(--brand)">+${k.evalReport.tokenSavePct}%</b></div>
              </div>` : `<div class="banner banner-info">尚未运行历史回放对比评测，请点击右上角按钮执行实测。</div>`}
            <div class="l3-bar">
              <span class="muted" style="font-size:13px">原始 Markdown 定义与指令标识符在三级面板查看</span>
              <button type="button" class="btn btn-sm" data-act="open-skill" data-id="${esc(k.id)}">📄 查看 SKILL.md 源码 (L3)</button>
            </div>
          </section>
        </div>`;
    }

    const list = s.skills.filter(function (k) { return s.skillFilter === "all" || k.lifecycle === s.skillFilter; });
    return `
      <div class="page-head">
        <div>
          <div class="eyebrow">SKILL CATALOG &amp; CRYSTALLIZATION · 阶段 2 / 7</div>
          <h1>共享 Skill 库与自动结晶宫格</h1>
          <p>将高频同类的成功实践与避坑教训自动聚类为标准化 Skill，点击任意卡片进入二级详情页。</p>
        </div>
        <span class="badge ${activeCnt >= 3 ? "badge-ok" : "badge-evolve"}">正式生效 ${activeCnt} / 3 个达标</span>
      </div>
      <div class="banner banner-info">分发规范：Linux 客户端可直接落地；Windows 客户端严格采用按需拉取或手动安装，平台绝不主动反向覆盖 Windows 本地环境。</div>
      <div class="cat-pills">
        ${["all", "draft", "trial", "active", "deprecated"].map(function (st) {
          return `<button type="button" class="cat-pill ${s.skillFilter === st ? "active" : ""}" data-act="skill-filter" data-val="${st}"><span>${esc(lcLabels[st])}</span></button>`;
        }).join("")}
      </div>
      <div class="skill-grid">
        ${list.map(function (k) {
          return `<button type="button" class="skill-card" data-act="open-skill-detail" data-id="${esc(k.id)}">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;width:100%">
              <span class="badge">${esc(k.category)}</span>
              <span class="badge ${lcBadges[k.lifecycle] || ""}">${esc(lcLabels[k.lifecycle] || k.lifecycle)}</span>
            </div>
            <h3>${esc(k.title)}</h3>
            <p>${esc(k.summary)}</p>
            <div class="muted" style="font-size:12.5px;margin-top:auto;padding-top:8px;border-top:1px solid var(--line);width:100%;display:flex;justify-content:space-between">
              <span>挂载 ${k.attachedPitfalls.length} 条避坑</span>
              <span>${k.evalReport ? `报错 ${k.evalReport.errorDropPct}%` : "待评测"} →</span>
            </div>
          </button>`;
        }).join("")}
      </div>`;
  };

  E.views.control = function () {
    const s = E.state;
    const w = E.canWrite();
    const head = `
      <div class="page-head">
        <div>
          <div class="eyebrow">CONTROL PLANE &amp; INFRASTRUCTURE · 阶段 1 &amp; 5</div>
          <h1>成员权限、Agent 信誉与数据底座</h1>
          <p>管理多成员跨库单向只读授权、各端大模型提议配额分级，以及主数据库与退役文件的全生命周期保障。</p>
        </div>
      </div>
      ${!w ? `<div class="banner banner-warn">当前为他人授权只读视角，禁用成员启停与跨库授权变更。</div>` : ""}
      <div class="subtabs" role="tablist">
        <button type="button" class="subtab ${s.ctrlSubtab === "users" ? "active" : ""}" data-act="ctrl-subtab" data-sub="users">成员与跨库授权</button>
        <button type="button" class="subtab ${s.ctrlSubtab === "reputation" ? "active" : ""}" data-act="ctrl-subtab" data-sub="reputation">Agent 信誉与配额</button>
        <button type="button" class="subtab ${s.ctrlSubtab === "infra" ? "active" : ""}" data-act="ctrl-subtab" data-sub="infra">数据底座与安全策略</button>
      </div>`;

    if (s.ctrlSubtab === "reputation") {
      return `${head}
        <section class="panel">
          <div class="panel-head"><div><h2>多端 Agent 与四级模型信誉表</h2><p>根据历史通过率自动授予每次会话的经验提议上限，防止低质未知模型刷屏</p></div></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>协同客户端</th><th>运行环境</th><th>绑定模型</th><th>信誉分级</th><th>单次提议上限</th><th>状态说明</th></tr></thead>
              <tbody>
                ${s.agents.map(function (a) {
                  return `<tr><td><b>${esc(a.title)}</b></td><td>${esc(a.os)}</td><td>${esc(a.model)}</td><td><span class="badge">${esc(a.tier)}</span></td><td>${a.quota} 条/会话</td><td>${esc(a.syncNote)}</td></tr>`;
                }).join("")}
              </tbody>
            </table>
          </div>
          <div class="l3-bar">
            <span class="muted" style="font-size:13px">包含防客户端伪造提权拦截细节</span>
            <button type="button" class="btn btn-sm" data-act="open-rep-l3">🔬 查看防冒充剥离与降级规则 (L3)</button>
          </div>
        </section>`;
    }

    if (s.ctrlSubtab === "infra") {
      return `${head}
        <div class="grid-2">
          <section class="panel">
            <div class="panel-head"><div><h2>PostgreSQL 17 主数据集群</h2><span class="badge badge-ok">全量在线</span></div></div>
            <p class="secondary" style="margin:0">全部 9 个逻辑库统一托管于主数据库集群，强约束行级隔离（FORCE RLS）并开启页面校验和，运行时旧 SQLite 句柄数归零。</p>
          </section>
          <section class="panel">
            <div class="panel-head"><div><h2>每日自动化全备与隔离恢复演练</h2><span class="badge badge-ok">演练全绿</span></div></div>
            <p class="secondary" style="margin:0">每日打包导出 9 库二进制备份、无密码角色脚本与校验摘要，已于隔离演练端口验证整库与表行数零差异恢复。</p>
          </section>
          <section class="panel">
            <div class="panel-head"><div><h2>旧版 7 个 SQLite 只读封存保护</h2><span class="badge badge-ok">旧数据零删除</span></div></div>
            <p class="secondary" style="margin:0">迁移期间的所有旧历史与旧记忆文件全部设置只读权限完整留存于原宿主机，作为永久可审计底层对照。</p>
          </section>
          <section class="panel">
            <div class="panel-head"><div><h2>生产级策略防误触红线锁</h2><span class="badge badge-danger">已锁定保护</span></div></div>
            <p class="secondary" style="margin:0">永久锁死无计量旧版全量总结开关，强制全部增量与回填路径受双轨预算上限及 R9 熔断器保护。</p>
          </section>
        </div>
        <section class="panel" style="padding:14px 20px">
          <div class="l3-bar" style="border-top:0;padding-top:0">
            <span class="muted" style="font-size:13.5px">运维巡检或审计时，可在下方一键展开详细库表、物理路径与演练回执</span>
            <div style="display:flex;gap:8px;flex-wrap:wrap">
              <button type="button" class="btn btn-sm" data-act="open-dr-l3">🗄️ 查看 9 个物理库与灾备回执 (L3)</button>
              <button type="button" class="btn btn-sm" data-act="open-policy-l3">🔒 查看退役文件路径与策略开关键值 (L3)</button>
            </div>
          </div>
        </section>`;
    }

    return `${head}
      <div class="grid-2">
        <section class="panel">
          <div class="panel-head"><div><h2>成员账号与活动会话管理</h2><p>停用协作者账号将同事务立即吊销其全部网页登录凭证</p></div></div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>成员</th><th>角色</th><th>活跃会话</th><th>账号控制</th></tr></thead>
              <tbody>
                ${s.controlUsers.map(function (u) {
                  return `<tr><td><b>${esc(u.displayName)}</b></td><td>${u.isAdmin ? "系统管理员" : "普通成员"}</td><td>${u.activeSessions == null ? "尚未接入" : u.activeSessions + " 个"}</td>
                    <td><button type="button" class="btn btn-sm ${u.enabled ? "btn-ok" : "btn-danger"}" data-act="toggle-user" data-id="${esc(u.id)}" ${w && !u.isAdmin ? "" : "disabled"}>${u.enabled ? "启用中（点击停用）" : "已停用（点击恢复）"}</button></td></tr>`;
                }).join("")}
              </tbody>
            </table>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head"><div><h2>定向跨库只读授权矩阵</h2><p>被授权方只能以只读方式检索或学习他人库内容，不可编辑或删除</p></div></div>
          ${s.grants.map(function (g, idx) {
            return `<div style="padding:14px;border-radius:11px;background:var(--bg-subtle);border:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
              <div><b>${esc(g.viewer)}</b> → ${esc(g.owner)}</div>
              <div style="display:flex;gap:8px;flex-wrap:wrap">
                <button type="button" class="btn btn-sm ${g.historyRead ? "btn-primary" : ""}" data-act="toggle-grant" data-idx="${idx}" data-field="historyRead" ${w ? "" : "disabled"}>读取历史：${g.historyRead ? "允许" : "禁止"}</button>
                <button type="button" class="btn btn-sm ${g.experienceRead ? "btn-primary" : ""}" data-act="toggle-grant" data-idx="${idx}" data-field="experienceRead" ${w ? "" : "disabled"}>读取经验：${g.experienceRead ? "允许" : "禁止"}</button>
              </div>
            </div>`;
          }).join("")}
        </section>
      </div>`;
  };
})(typeof window !== "undefined" ? window : globalThis);

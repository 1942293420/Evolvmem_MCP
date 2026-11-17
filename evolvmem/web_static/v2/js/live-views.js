/* Production views use API facts; file previews retain explicitly labelled demo views. */
(function (root) {
  const E = root.EvolvMemV2, esc = E.esc;
  const preview = { overview: E.views.overview, evolution: E.views.evolution, skills: E.views.skills, control: E.views.control };
  function head(title, detail) { return '<div class="page-head"><div><h1>' + esc(title) + '</h1><p>' + esc(detail) + '</p></div></div>'; }
  function value(v, unit) { return v == null ? '待加载' : esc(v) + (unit || ''); }
  function kpi(title, v, detail) { return '<div class="kpi-card"><div class="kpi-label">' + esc(title) + '</div><div class="kpi-value">' + v + '</div><div class="kpi-sub">' + esc(detail) + '</div></div>'; }

  E.views.overview = function () {
    if (!E.isProduction()) return preview.overview();
    const s = E.state, m = s.liveMetrics || {};
    const total = m.pending_live == null || m.pending_backfill == null ? null : m.pending_live + m.pending_backfill;
    const flow = [
      ['原文采集与上传', '客户端上传会话原文', 'history'], ['原文归档', '按实际工作文件夹浏览原文并核对记录时间；无可靠目录进入未归属', 'history'],
      ['候选提炼与门禁', '实时与历史回填分轨处理', 'pipelines'], ['人工审阅', '核对内容与原始证据', 'review-inbox'],
      ['正式经验', '保留有效经验及来源', 'experiences'], ['按需检索', '供后续任务查询与使用', 'experiences']
    ];
    return head('记忆与经验总览', '数量来自当前所选库的接口结果；客户端健康、灾备及效果评测尚未接入实时监测。') +
      '<div class="kpi-grid">' + kpi('有效经验', value(m.active_notes, ' 条'), '当前有效经验总量') +
      kpi('待审阅候选', value(total, ' 条'), '新会话 ' + value(m.pending_live, ' 条') + ' · 历史回填 ' + value(m.pending_backfill, ' 条')) +
      kpi('自动进化与 Skill 评测', '待开发', '不以预置数值表示已经达标') + '</div>' +
      (E.session ? '<section class="panel" aria-labelledby="sso-principles-title"><h2 id="sso-principles-title">登录与知识库访问</h2><ol class="sso-flow"><li><strong>1. 飞书登录总控台</strong>总控台确认当前用户是否获准进入 EvolvMem。</li><li><strong>2. 核对内部权限</strong>沿用内部账号绑定、启停状态和角色权限。</li><li><strong>3. 打开已授权知识库</strong>继续按原归属和跨库授权查看资料。</li></ol><p>退出、账号变更或权限失效后隐藏资料；同一用户重新登录后回到当前页面。后台核对身份未发生变化时，保留正文和阅读位置；恢复输入焦点不自动滚动页面。</p></section>' : '') +
      '<section class="panel"><div class="panel-head"><div><h2>系统工作流程</h2><p>功能关系说明，非实时健康监测。点击环节查看对应页面。</p></div></div>' +
      '<div style="display:flex;gap:10px;flex-wrap:wrap" aria-label="原文到经验的工作流程">' + flow.map(function (step, i) {
        return '<button type="button" class="stage-flow-row" style="flex:1 1 180px" data-act="goto-tab" data-route="' + step[2] + '"><span><b>' + (i + 1) + '. ' + step[0] + '</b><br><span class="muted">' + step[1] + '</span></span></button>';
      }).join('') + '</div><p class="secondary">后续规划：经验交付记录 → 使用结果归因 → 可逆休眠与渐进自动化 → 习惯、手册和 Skill；这些能力尚未形成完整闭环。</p></section>' +
      '<section class="panel"><div class="panel-head"><h2>最近经验变更</h2><button type="button" class="btn btn-sm" data-act="goto-tab" data-route="review-inbox">前往审阅箱</button></div>' +
      (s.changesStream.length ? '<div class="table-wrap"><table><thead><tr><th>时间</th><th>操作</th><th>来源</th></tr></thead><tbody>' + s.changesStream.map(function (x) {
        return '<tr><td>' + esc(x.ts) + '</td><td>' + esc(x.action) + '</td><td>' + esc(x.actor) + '</td></tr>';
      }).join('') + '</tbody></table></div>' : '<p class="muted">当前暂无变更记录。</p>') + '</section>';
  };

  E.views.evolution = function () {
    if (!E.isProduction()) return preview.evolution();
    const s = E.state, tabs = [['metrics', '进化指标'], ['habits', '个人习惯'], ['handbook', '常驻手册'], ['errsig', '报错避坑']];
    let html = head('进化工坊', '以下能力待开发。页面不模拟执行，也不展示未经测量的效果。') +
      '<div class="subtabs">' + tabs.map(function (t) { return '<button type="button" class="subtab ' + (s.evoSubtab === t[0] ? 'active' : '') + '" data-act="evo-subtab" data-sub="' + t[0] + '">' + t[1] + '</button>'; }).join('') + '</div>';
    if (s.evoSubtab === 'habits') return html + E.unavailablePanel('个人习惯候选', '重复纠偏识别及候选生成尚未接入，没有已确认的习惯可以在此发布。') + E.plannedButton('确认习惯');
    if (s.evoSubtab === 'handbook') return html + E.unavailablePanel('常驻手册', '尚未生成手册草稿或发布差异。确认发布能力接通前，不会改写 AGENTS.md 或 CLAUDE.md。') + E.plannedButton('生成与发布手册');
    if (s.evoSubtab === 'errsig') return html + E.unavailablePanel('工具报错即时避坑', '工具事件监听、报错签名匹配及实际推送尚未接入。') + E.plannedButton('运行报错匹配');
    return html + '<section class="panel"><div class="panel-head"><h2>双轨效果指标</h2><span class="badge">待开发</span></div>' +
      '<div class="table-wrap"><table><thead><tr><th>指标</th><th>新会话</th><th>历史回填</th></tr></thead><tbody>' +
      ['重复纠正率', '重复踩坑率', '注入有效使用率', '可归因的单条入库成本', '自动化校准一致率'].map(function (label) {
        return '<tr><td>' + label + '</td><td>尚未接入</td><td>尚未接入</td></tr>';
      }).join('') + '</tbody></table></div><p class="secondary">实际人工审阅统计请在审阅箱查看；部分曝光记录不代表已经完成使用结果归因。</p>' +
      '<button type="button" class="btn btn-sm" data-act="open-impressions-l3">查看已有曝光记录</button></section>' +
      E.unavailablePanel('自动晋升与休眠', '按类别自动切换、反馈置信度与可逆休眠尚未接入实际执行。') + E.plannedButton('调整自动化策略');
  };

  E.views.skills = function () {
    if (!E.isProduction()) return preview.skills();
    const s = E.state, h = head('Skill 目录', '展示后端同步的真实目录。自动结晶、试用发布和反事实评测待开发。');
    if (s.selectedSkillId) {
      const k = s.skills.find(function (item) { return item.id === s.selectedSkillId; });
      const back = '<button type="button" class="btn" data-act="close-skill-detail">返回 Skill 目录</button>';
      if (!k) return h + back + E.unavailablePanel('未找到该 Skill', '目录已更新，请返回重新选择。');
      return h + back + '<section class="panel"><div class="panel-head"><h2>' + esc(k.title) + '</h2><span class="badge">已同步目录</span></div><p class="prose-body">' + esc(k.summary) + '</p>' +
        '<div class="kv-grid"><div class="kv-cell"><span>来源</span><b>' + esc(k.catalogSource || '未提供') + '</b></div><div class="kv-cell"><span>文件数量</span><b>' + value(k.fileCount) + '</b></div></div>' +
        '<p class="secondary">这里只展示目录说明，未加载 SKILL.md 原文；安装、分发和运行效果未核验。</p></section>' +
        E.unavailablePanel('自动 Skill 生命周期与评测', '当前目录条目不等于自动生成并评测通过的 Skill。没有真实评测报告时不显示节省比例。') +
        E.plannedButton('反事实评测') + ' ' + E.plannedButton('试用与正式发布');
    }
    return h + '<div class="banner banner-info">已同步目录 ' + s.skills.length + ' 条。目录存在不代表已经安装或通过效果评测。</div>' +
      (s.skills.length ? '<div class="skill-grid">' + s.skills.map(function (k) {
        return '<button type="button" class="skill-card" data-act="open-skill-detail" data-id="' + esc(k.id) + '"><span class="badge">' + esc(k.category) + '</span><h3>' + esc(k.title) + '</h3><p>' + esc(k.summary) + '</p><span class="muted">查看目录说明 →</span></button>';
      }).join('') + '</div>' : '<section class="panel"><p class="muted">当前暂无 Skill 目录记录。</p></section>');
  };

  E.views.control = function () {
    if (!E.isProduction() || E.state.ctrlSubtab !== 'infra') return preview.control();
    return head('数据底座与生效配置', '下方展示接口返回的配置；配置开启不等于后台任务已成功执行。') +
      '<button type="button" class="btn" data-act="ctrl-subtab" data-sub="users">返回成员与授权</button>' +
      '<section class="panel"><h2>当前配置</h2><div class="table-wrap"><table><thead><tr><th>配置项</th><th>值</th></tr></thead><tbody>' +
      E.state.policyLocks.map(function (x) { return '<tr><td>' + esc(x.key) + '</td><td>' + esc(typeof x.val === 'object' ? JSON.stringify(x.val) : x.val) + '</td></tr>'; }).join('') +
      '</tbody></table></div></section>' + E.unavailablePanel('运行健康与灾备监测', '数据库句柄、备份完成时间及恢复演练最新结果尚未接入本页实时核验，不以固定数字表示健康。');
  };
})(typeof window !== 'undefined' ? window : globalThis);

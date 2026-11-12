/* Present real queue state without turning system failures into human approval. */
(() => {
  window.EvolvWorkflowOverview={html({metrics:m,queue:q,settings:s,overview,compact=false,loading={},errors={}},esc){
    const c=q?.counts, unknown='—', link=tab=>`#knowledge/organization?tab=${tab}`;
    const waiting=c?(c.pending||0)+(c.running||0):null;
    const mode=s?(s.auto_new?'新资料自动处理已开启':'新资料自动处理已暂停'):'自动处理状态无法读取';
    const sync=!m?'同步状态未知':m.index.pending?'新变动正在同步':m.index.exists?'检索索引已同步':'检索索引尚未建立';
    const step=(n,title)=>`<li><span>${n}</span><b>${title}</b></li>`;
    const o=overview;
    if(compact){
      const status=s?(s.auto_new?'新资料自动处理已开启':'新资料自动处理已暂停'):loading.settings?'正在读取自动处理状态…':'自动处理状态暂不可用';
      const failed=Object.keys(errors).length>0;
      return `<section class="refresh-overview-grid" data-dual-overview>${[['capabilities','经验与能力','capabilities'],['archives','项目摘要','project_archives'],['intake','待分类','intake']].map(([key,title,field])=>`<a href="#knowledge/${key==='archives'?'archives':key==='intake'?'capabilities?tab=intake':'capabilities'}" data-overview-${key}><span>${title}</span><strong>${o?.[field]??unknown}</strong><em>${key==='archives'?'浏览项目库与历史':key==='intake'?'确认归类或新增分类':'按能力检索历史经验'} →</em></a>`).join('')}</section><section class="flow-health" data-flow-health><div><span class="health-dot ${s?.auto_new?'on':''}"></span><strong>${status}</strong></div><a class="ui-button" href="${link('all')}">查看运行记录 →</a></section>${failed?`<p class="flow-unavailable" role="status">${o||s?'部分状态更新失败，保留上次读取的结果。':'部分状态暂不可用。'} <button class="ui-button" data-refresh>重新读取</button></p>`:''}`;
    }
    return `<section class="refresh-overview-grid" data-dual-overview><a href="#knowledge/capabilities" data-overview-capabilities><span>经验与能力</span><strong>${o?.capabilities??unknown}</strong><em>按能力检索历史经验 →</em></a><a href="#knowledge/archives" data-overview-archives><span>项目摘要</span><strong>${o?.project_archives??unknown}</strong><em>浏览项目库与历史 →</em></a><a href="#knowledge/capabilities?tab=intake" data-overview-intake><span>待分类</span><strong>${o?.intake??unknown}</strong><em>确认归类或新增分类 →</em></a></section>
      <section class="flow-health" data-flow-health><div><span class="health-dot ${s?.auto_new?'on':''}"></span><strong>${mode}</strong></div><a class="ui-button" href="${link('all')}">查看运行记录 →</a></section>
      ${!m?'<p class="flow-unavailable" role="status">统计暂不可用，请刷新重试。</p>':''}
      ${!q?'<p class="flow-unavailable" role="status">任务状态暂不可用，请刷新重试。</p>':''}
      <div class="flow-summary">
        <a href="${link('all')}" class="flow-stat"><span>系统正在处理</span><strong>${waiting??unknown}<span class="flow-unit">个任务</span></strong><p>${c?`排队 ${c.pending||0} · 运行 ${c.running||0}`:'状态未知'}</p><em>无需逐条审核</em></a>
        <a href="${link('review')}" class="flow-stat needs-decision" data-flow-link="review"><span>待你判断</span><strong>${m?.review_groups??unknown}<span class="flow-unit">组 / ${m?.review_units??unknown} 条</span></strong><em>处理例外 →</em></a>
        <a href="${link('failed')}" class="flow-stat ${c?.failed?'needs-repair':''}" data-flow-link="failed"><span>系统问题</span><strong>${c?.failed??unknown}<span class="flow-unit">个任务</span></strong><em>查看原因与重试 →</em></a>
        <a href="${link('completed')}" class="flow-stat"><span>自动完成</span><strong>${m?.automatic_completed??unknown}<span class="flow-unit">个任务</span></strong><em>查看结果 →</em></a>
      </div>
      <section class="flow-pipeline"><div class="section-head"><h2>自动处理流程</h2><span class="sync-state ${m?.index.pending?'pending':''}">${sync}</span></div><ol>${step('01','采集与清洗')}${step('02','项目归类')}${step('03','提炼与入库')}${step('04','同步与召回')}</ol></section>
      <div class="flow-boundaries"><section><h3>自动处理</h3><div class="flow-tags"><span>归属明确</span><span>原话有依据</span><span>普通进度只留历史</span></div></section><section><h3>需要你判断</h3><div class="flow-tags"><a href="${link('review')}">归属不明或冲突</a><a href="#knowledge/qa">问答内容存疑</a><a href="#experiences">方法缺少验证</a></div></section></div>`;
  }};
})();

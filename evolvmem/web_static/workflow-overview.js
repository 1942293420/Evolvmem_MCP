/* Present real queue state without turning system failures into human approval. */
(() => {
  window.EvolvWorkflowOverview={html({metrics:m,queue:q,settings:s},esc){
    const c=q?.counts, unknown='—', link=tab=>`#knowledge/organization?tab=${tab}`;
    const waiting=c?(c.pending||0)+(c.running||0):null;
    const mode=s?(s.auto_new?'新资料自动处理已开启':'新资料自动处理已暂停'):'自动处理状态无法读取';
    const sync=!m?'同步状态未知':m.index.pending?'新变动正在同步':m.index.exists?'检索索引已同步':'检索索引尚未建立';
    const step=(n,title)=>`<li><span>${n}</span><b>${title}</b></li>`;
    return `<section class="flow-health" data-flow-health><div><span class="health-dot ${s?.auto_new?'on':''}"></span><strong>${mode}</strong></div><a class="ui-button" href="${link('all')}">查看运行记录 →</a></section>
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

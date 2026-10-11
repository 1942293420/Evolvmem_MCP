(() => {
  window.EvolvOrganizationMetrics={html(m,esc){
    const percent=n=>n==null?'未评估':(100*n).toFixed(1)+'%';
    const recovery=m.index.last_recovery;
    const coverage=m.index.coverage||{};
    const pending=coverage.pending_count;
    const sync=coverage.state==='unavailable'?'语义服务暂不可用':m.index.pending?(pending==null?'待检查':pending?`更新中 · 待补 ${pending} 条`:'更新中 · 核对已变更资料'):m.index.exists?'索引已同步':'索引尚未建立';
    const coverageText=coverage.document_count==null?'尚无覆盖数量':`已覆盖 ${coverage.indexed_count} / ${coverage.document_count} 条`;
    const elapsed=recovery?`${(recovery.duration_ms/1000).toFixed(1)} 秒`:'暂无测量';
    const sample=m.feedback.total?`抽检 ${m.feedback.total} 条，标记有误 ${m.feedback.incorrect} 条`:'未评估：尚无当前条目的抽检反馈';
    return `<div class="section-head"><h2>整理效果</h2><a href="#knowledge/organization">前往自动整理 →</a></div><div class="organization-metrics-grid">${[
      ['自动完成',percent(m.automatic_rate),`${m.automatic_completed} / ${m.settled_tasks} 个已结束任务；人工完成 ${m.manual_completed} 个`],
      ['待核对',`${m.review_groups} 组 / ${m.review_units} 条`,''],
      ['仅留历史',`${m.history_only_units} 条`,''],
      ['指导已复用',`${m.guidance_reused_units} 条`,''],
      ['索引同步',sync,`${coverageText}${recovery?`；上次同步用时 ${elapsed}`:''}`],
      ['抽检反馈',percent(m.feedback.error_rate),sample]
    ].map(([title,value,note])=>`<div class="organization-metric"><span>${esc(title)}</span><strong>${esc(value)}</strong>${note?`<p>${esc(note)}</p>`:''}</div>`).join('')}</div><details><summary>查看统计口径</summary><p>${esc(m.scope)}。</p><p>抽检只统计你对当前版本的明确反馈，不能代表全库误判率。新增资料先支持关键词查找，后台通常每分钟检查并补齐语义索引。</p></details>`;
  }};
})();

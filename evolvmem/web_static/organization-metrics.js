(() => {
  window.EvolvOrganizationMetrics={html(m,esc){
    const percent=n=>n==null?'未评估':(100*n).toFixed(1)+'%';
    const recovery=m.index.last_recovery;
    const sync=m.index.pending?'仍有变动待同步':m.index.exists?'索引已同步':'索引尚未建立';
    const elapsed=recovery?`${(recovery.duration_ms/1000).toFixed(1)} 秒`:'暂无测量';
    const sample=m.feedback.total?`抽检 ${m.feedback.total} 条，标记有误 ${m.feedback.incorrect} 条`:'未评估：尚无当前条目的抽检反馈';
    return `<div class="section-head"><h2>整理效果</h2><a href="#knowledge/organization">前往自动整理 →</a></div><div class="organization-metrics-grid">${[
      ['自动完成',percent(m.automatic_rate),`${m.automatic_completed} / ${m.settled_tasks} 个已结束任务；人工完成 ${m.manual_completed} 个`],
      ['待核对',`${m.review_groups} 组 / ${m.review_units} 条`,'集中预览条件后批量处理'],
      ['仅留历史',`${m.history_only_units} 条`,'纯助手进度可查找、可恢复处理'],
      ['指导已复用',`${m.guidance_reused_units} 条`,'当前资料实际沿用指导的数量'],
      ['索引同步',sync,recovery?`最近一轮 ${elapsed}，编码 ${recovery.encoded_operations} 次；${recovery.status==='staged'?'已追平':'未完成，等待重试'}`:elapsed],
      ['抽检反馈',percent(m.feedback.error_rate),sample]
    ].map(([title,value,note])=>`<div class="organization-metric"><span>${esc(title)}</span><strong>${esc(value)}</strong><small>${esc(note)}</small></div>`).join('')}</div><details><summary>统计口径</summary><p class="hint">${esc(m.scope)}。仅留历史也属于处理完成；排队和运行中的任务不计分母。指导按当前实际应用条目计数，历史未记录的应用不倒推。抽检只统计你对当前版本的明确反馈，样本由你选择，不能代表全库误判率。索引耗时是最近一次恢复的执行时间；${recovery?`从待同步到该轮结束约 ${(recovery.pending_ms/1000).toFixed(1)} 秒。`:'尚未记录恢复测量。'}全文检索可先于向量同步可用。</p></details>`;
  }};
})();

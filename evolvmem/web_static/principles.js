/* The diagrams mirror shipped automatic processing and explicit exception decisions. */
window.EvolvPrinciples={render(mount){
 if(mount.dataset.ready)return;mount.dataset.ready='true';
 const modules=[['总览','自动进度 · 待判断 · 系统问题','#home'],['自动整理','集中判断 · 一次指导 · 后续复用','#knowledge/organization'],['项目历史','会话原文 · 项目摘要','#knowledge/projects'],['经验知识','可用问答 · 验证依据','#knowledge/qa'],['任务断点','当前进度 · 下一步','#progress'],['数据清洗','补跑资料 · 修改与删除','#knowledge/cleaning'],['存量资料','旧资料归类 · 补充与纠正','#knowledge/intake'],['整理规则','修改处理规则 · 样例预览','#knowledge/skill']];
 const text=(x,y,lines,cls='')=>`<text x="${x}" y="${y}" text-anchor="middle" class="${cls}">${lines.map((s,i)=>`<tspan x="${x}" dy="${i?26:0}">${s}</tspan>`).join('')}</text>`;
 const box=(x,y,w,h,lines,cls='')=>`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="12" class="node ${cls}"/>${text(x+w/2,y+h/2-(lines.length-1)*13+6,lines)}`;
 const arrow=(d,label,x,y)=>`<path d="${d}" class="edge" marker-end="url(#arrow)"/>${label?text(x,y,[label],'edge-label'):''}`;
 const diamond=(x,y,label)=>`<path d="M${x} ${y-38} L${x+130} ${y} L${x} ${y+38} L${x-130} ${y}Z" class="decision"/>${text(x,y+6,[label])}`;
 const svg=(title,h,body)=>`<svg viewBox="0 0 980 ${h}" role="img" aria-label="${title}" xmlns="http://www.w3.org/2000/svg"><title>${title}</title><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="#657987"/></marker></defs>${body}</svg>`;
 const moduleMap=svg('工作台、整理成果和管理入口',530,
  text(180,34,['工作台'])+text(490,34,['整理成果'])+text(800,34,['管理与调整'])+
  [[modules[0],40,70],[modules[1],40,205],[modules[2],350,70],[modules[3],350,205],[modules[4],350,340],[modules[5],660,70],[modules[6],660,205],[modules[7],660,340]].map(([m,x,y])=>box(x,y,280,100,[m[0],m[1]])).join(''));
 const flow=svg('从新会话到历史与可用知识',880,
  box(325,20,330,70,['新会话自动进入处理队列'],'accent')+arrow('M490 90V125')+
  box(325,125,330,80,['自动清洗、分段、判断项目','使用已保存规则和适用指导'])+arrow('M490 205V250')+diamond(490,288,'归属与内容明确？')+
  arrow('M620 288H800V360','需你判断',783,270)+box(675,360,270,85,['只核对有疑问的资料','查看原因、原话和当前判断'],'warm')+
  arrow('M800 445V482H490','保存决定',794,474)+arrow('M490 326V520','明确',530,420)+
  box(330,520,320,65,['保存项目历史与摘要'])+arrow('M490 585V615H210V650')+arrow('M490 615H755V650')+
  box(50,650,320,80,['历史可查','当时讨论、要求和决定了什么'])+
  box(595,650,320,80,['提炼可用知识','明确无冲突自动保存，歧义待确认'])+
  arrow('M210 730V785H490V805')+arrow('M755 730V785H490')+box(305,805,370,60,['按问题召回历史、知识或两者'],'accent'));
 const activity=svg('系统自动处理与用户例外判断',960,
  '<rect x="24" y="18" width="932" height="920" rx="16" class="swimlane"/><path d="M600 18V938 M24 75H956" class="lane-line"/>'+text(305,53,['系统自动处理'])+text(775,53,['只在必要时由你决定'])+
  box(140,110,330,80,['清洗与项目分类','原始对话保留供核对'])+arrow('M305 190V220')+diamond(305,258,'规则与依据明确？')+
  arrow('M435 258H765V315','有歧义',739,243)+box(650,315,270,90,['为什么需要你 · 关键原话','选择项目或保留处理方式'],'warm')+
  arrow('M765 405V450H305','保存决定',760,440)+arrow('M305 296V490','明确',348,385)+
  box(140,490,330,75,['自动保存历史并提炼知识'])+arrow('M305 565V600')+diamond(305,638,'答案与原话一致？')+
  arrow('M435 638H650','需核对',537,618)+box(650,595,270,90,['只核对冲突与缺失条件','未采纳的 AI 建议只留历史'],'warm')+
  arrow('M765 685V745H305','确认或修正',772,738)+arrow('M305 676V780','通过',346,735)+
  box(140,780,330,90,['保存并更新检索','可用知识不需要重复审核'])+
  box(650,780,270,90,['方法经验须有真实验证依据','未验证的方法保留候选'],'warm'));
 const organization=svg('持续整理与指导复用',900,
  text(280,42,['系统自动处理'])+text(770,42,['用户处理例外'])+
  box(105,80,350,90,['自动接收新会话','同会话续写可参考前文已确认项目'])+arrow('M280 170V205')+
  box(105,205,350,80,['自动清洗、归类和提炼','核对原话与内容范围'])+arrow('M455 245H630','存在疑问',542,223)+
  box(630,205,290,100,['同类资料集中判断','原因、关键原话、前文项目依据'],'warm')+arrow('M775 305V350')+
  box(630,350,290,90,['填写条件与例外','预览本次将处理哪些资料'],'warm')+arrow('M775 440V490')+diamond(775,528,'以后同类适用？')+
  arrow('M645 528H475V680','仅本次',542,510)+box(315,680,320,80,['只修正本次符合条件的资料'])+
  arrow('M775 566V610')+box(650,610,275,80,['保存指导 · 后续符合时复用','不符合或有冲突仍待判断'],'warm')+
  arrow('M925 655H950V180H280V205')+
  arrow('M280 285V355','完成',321,329)+box(105,355,350,80,['历史与知识自动保存','自动结果可抽检，无需全部复核'])+
  arrow('M280 435V485')+box(105,485,350,80,['按资料用途同步更新检索','检索模型变化后重新建立索引'])+
  box(105,795,820,70,['临时故障自动重试；仍未完成的在「系统问题」处理'],'accent'));
 const diagrams={modules:moduleMap,flow,activity,organization};
 const titles={modules:'各页面负责什么',flow:'新资料自动成为历史与知识',activity:'什么时候需要你参与',organization:'整篇自动整理，指导一次后续复用'};
 mount.innerHTML=`<header class="data-page-header"><h1>工作原理</h1></header><nav class="principles-tabs" aria-label="工作原理图类型"><button data-principle="modules" aria-pressed="true">页面职责</button><button data-principle="flow" aria-pressed="false">完整流程</button><button data-principle="activity" aria-pressed="false">系统与人工</button><button data-principle="organization" aria-pressed="false">指导复用</button></nav><section class="principle-board"><div class="section-head"><h2 id="principle-title">${titles.modules}</h2><button class="ui-button" data-diagram-size>放大查看</button></div><div class="product-diagram" tabindex="0" aria-label="流程图，可横向滚动">${moduleMap}</div></section><div class="principle-links">${modules.map(m=>`<a href="${m[2]}"><strong>${m[0]} →</strong></a>`).join('')}</div>`;
 mount.onclick=e=>{const b=e.target.closest('[data-principle]');if(b){mount.querySelectorAll('[data-principle]').forEach(t=>t.setAttribute('aria-pressed',String(t===b)));const board=mount.querySelector('.product-diagram');board.innerHTML=diagrams[b.dataset.principle];board.scrollLeft=0;mount.querySelector('#principle-title').textContent=titles[b.dataset.principle];}const zoom=e.target.closest('[data-diagram-size]');if(zoom){const board=mount.querySelector('.product-diagram');board.classList.toggle('enlarged');zoom.textContent=board.classList.contains('enlarged')?'恢复大小':'放大查看';}};
}};

/* Product diagrams use explicit SVG activity notation, within the main shell. */
window.EvolvPrinciples={render(mount){
 if(mount.dataset.ready)return;mount.dataset.ready='true';
 const modules=[['数据清洗','清洗 Skill · 原文与 AI 预览','修改清洗稿 · 分类 · 永久删除','#knowledge/cleaning'],['项目历史','已整理 · 清洗后待入库项目','归属 Skill · 预览与批量保存','#knowledge/projects'],['经验知识','分类问答 · 适用条件','经验案例 · 来源与验证','#knowledge/qa'],['任务断点','任务目标 · 当前进度','下一步 · 阻塞 · 续接','#progress'],['资料整理','入库复核 · 核对内容','确认入库 · 纠正与归档','#knowledge/intake'],['Skill 规则','提炼 · 入库 · 协作','独立文件导出 · 待确认验证','#knowledge/skill']];
 const text=(x,y,lines,cls='')=>`<text x="${x}" y="${y}" text-anchor="middle" class="${cls}">${lines.map((s,i)=>`<tspan x="${x}" dy="${i?24:0}">${s}</tspan>`).join('')}</text>`;
 const box=(x,y,w,h,lines,cls='')=>`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="12" class="node ${cls}"/>${text(x+w/2,y+h/2-(lines.length-1)*12+6,lines)}`;
 const arrow=(d,label,x,y)=>`<path d="${d}" class="edge" marker-end="url(#arrow)"/>${label?text(x,y,[label],'edge-label'):''}`;
 const diamond=(x,y,label)=>`<path d="M${x} ${y-38} L${x+100} ${y} L${x} ${y+38} L${x-100} ${y}Z" class="decision"/>${text(x,y+6,[label])}`;
 const svg=(title,h,body)=>`<svg viewBox="0 0 980 ${h}" role="img" aria-label="${title}" xmlns="http://www.w3.org/2000/svg"><title>${title}</title><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="#657987"/></marker></defs>${body}</svg>`;
 const moduleMap=svg('六个业务模块及各自职责',610,
   box(330,20,320,65,['EvolvMem · 个人知识库'],'accent')+
   arrow('M490 85V115H166V145')+arrow('M490 115V145')+arrow('M490 115H814V145')+
   modules.slice(0,3).map((m,i)=>box(26+i*324,145,280,132,[m[0],m[1],m[2]])).join('')+
   text(490,330,['待处理资料先确认清洗，再到项目历史预览归属；原文保留供核对。'],'caption')+
   modules.slice(3).map((m,i)=>box(26+i*324,385,280,132,[m[0],m[1],m[2]],'warm')).join('')+
   text(490,566,['清洗与归属规则放在各自操作页面；提炼、入库与协作规则单独维护。'],'caption'));
 const flow=svg('待处理资料先清洗再归类的业务流程',930,
   box(305,25,370,70,['① 接收待处理资料','保留原文作为核对依据'])+arrow('M490 95V140')+
   box(305,140,370,85,['② 数据清洗','编辑并保存 Skill → 勾选 → AI 预览'])+
   arrow('M675 182H735','不需要保留',787,129)+box(735,147,220,80,['确认永久删除','有引用时先处理引用'],'warm')+
   arrow('M490 225V275')+box(305,275,370,85,['③ 核对或修改清洗稿','确认资料类别，批量保存'])+
   arrow('M490 360V410')+box(305,410,370,75,['④ 项目历史 · 待入库项目','仅接收已经确认的清洗稿'],'warm')+
   arrow('M490 485V535')+box(305,535,370,85,['⑤ 预览项目归属','按归属 Skill 判断，你可手动改选'])+
   arrow('M490 620V670')+box(305,670,370,80,['⑥ 单条或批量保存分类','失败项保留草稿，变化的来源重新核对'])+
   arrow('M490 750V800')+box(305,800,370,80,['⑦ 历史查阅与后续提炼','清洗稿、原话依据与经验分别保留'],'accent'));
 const activity=svg('资料入库活动图：系统与用户如何协作',1190,
   '<rect x="24" y="18" width="932" height="1144" rx="16" class="swimlane"/><path d="M650 18V1162 M24 82H956" class="lane-line"/>'+
   text(337,57,['系统处理'])+text(802,57,['用户确认'])+
   '<circle cx="337" cy="112" r="10" class="terminal"/>'+
   arrow('M337 122V150')+box(207,150,260,64,['接收待处理资料，保留原文'])+
   arrow('M337 214V246')+diamond(337,284,'清洗已确认？')+
   arrow('M437 284H688','[否]',563,269)+box(688,249,230,70,['清洗 Skill → AI 预览','修改清洗稿与类别 → 保存'],'warm')+
   arrow('M803 319V366H345')+arrow('M337 322V358','[是]',375,345)+
   '<path d="M337 358L345 366L337 374L329 366Z" class="decision"/>'+
   arrow('M337 374V404')+box(207,404,260,76,['清洗后待入库项目','按归属 Skill 生成项目建议'])+
   arrow('M467 442H688')+box(688,408,230,70,['核对或修改项目归属','批量保存分类'],'warm')+
   arrow('M803 478V494H337V514')+box(207,514,260,82,['读取已确认清洗稿','提炼摘要与分类问答'])+
   arrow('M337 596V628')+diamond(337,666,'内容有疑问？')+
   arrow('M437 666H688','[是]',554,650)+box(688,628,230,76,['核对原话与整理结果','修正理解，决定是否入库'],'warm')+
   arrow('M803 704V744')+diamond(803,782,'确认保留？')+
   arrow('M703 782H345','[是]',564,766)+arrow('M337 704V774','[否]',375,744)+
   '<path d="M337 774L345 782L337 790L329 782Z" class="decision"/>'+
   arrow('M903 782H938V1088H918','[否]',927,895)+box(698,1050,220,76,['不入库 / 暂存待确认'],'warm')+arrow('M808 1126V1130')+'<circle cx="808" cy="1144" r="14" fill="none" stroke="#344b5b" stroke-width="2"/><circle cx="808" cy="1144" r="9" class="terminal"/>'+
   arrow('M337 790V830')+'<rect x="137" y="830" width="400" height="7" class="terminal"/>'+
   arrow('M179 837V875')+arrow('M497 837V875')+
   box(45,875,268,82,['历史：保存清洗正文','项目关联会话摘要'])+box(363,875,268,82,['知识：保存分类问答','需求与经验保留各自用途'])+
   arrow('M179 957V993')+arrow('M497 957V993')+'<rect x="137" y="993" width="400" height="7" class="terminal"/>'+
   arrow('M337 1000V1032')+box(207,1032,260,68,['按需检索与协作学习'])+
   arrow('M337 1100V1130')+'<circle cx="337" cy="1144" r="14" fill="none" stroke="#344b5b" stroke-width="2"/><circle cx="337" cy="1144" r="9" class="terminal"/>');
 const diagrams={modules:moduleMap,flow,activity};
 mount.innerHTML=`<header class="data-page-header"><span class="knowledge-eyebrow">产品说明</span><h1>工作原理</h1><p>先看六个模块各管什么，再看一段对话怎样成为知识，以及你什么时候需要参与。</p></header><nav class="principles-tabs" aria-label="工作原理图类型"><button data-principle="modules" aria-pressed="true">模块职责图</button><button data-principle="flow" aria-pressed="false">业务流程图</button><button data-principle="activity" aria-pressed="false">入库活动图</button></nav><section class="principle-board"><div class="section-head"><div><h2 id="principle-title">六个业务模块，分别负责什么</h2><p id="principle-description" class="hint">项目摘要属于项目历史；任务断点只回答如何继续任务。</p></div><button class="ui-button" data-diagram-size>放大图中文字</button></div><p class="diagram-scroll-hint">小屏可横向滚动看清文字。</p><div class="product-diagram" tabindex="0" aria-label="图示，可横向滚动">${moduleMap}</div><p class="diagram-legend" hidden>活动图符号：● 开始 · ◎ 结束 · 圆角框为动作 · 菱形为判断 · 黑条为分支汇合。</p></section><div class="principle-links">${modules.map(m=>`<a href="${m[3]}"><strong>${m[0]} →</strong><span>${m[1]}</span></a>`).join('')}</div>`;
 mount.onclick=e=>{const b=e.target.closest('[data-principle]');if(b){mount.querySelectorAll('[data-principle]').forEach(t=>t.setAttribute('aria-pressed',String(t===b)));const kind=b.dataset.principle;const board=mount.querySelector('.product-diagram');board.innerHTML=diagrams[kind];board.scrollLeft=0;mount.querySelector('.diagram-legend').hidden=kind!=='activity';mount.querySelector('#principle-title').textContent=({modules:'六个业务模块，分别负责什么',flow:'一段对话，怎样成为可用知识',activity:'系统自动处理；有疑问时由你决定'})[kind];mount.querySelector('#principle-description').textContent=({modules:'项目摘要属于项目历史；任务断点只回答如何继续任务。',flow:'待处理资料先确认清洗稿，再归类到项目；预览不保存，确认后才进入下一步。',activity:'左右泳道区分系统和用户；箭头上的条件说明为什么进入待确认。'})[kind];}if(e.target.closest('[data-diagram-size]')){const board=mount.querySelector('.product-diagram');board.classList.toggle('enlarged');e.target.textContent=board.classList.contains('enlarged')?'恢复图大小':'放大图中文字';}};
}};

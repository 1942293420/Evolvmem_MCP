/* Product diagrams use explicit SVG activity notation, within the main shell. */
window.EvolvPrinciples={render(mount){
 if(mount.dataset.ready)return;mount.dataset.ready='true';
 const modules=[['项目历史','项目登记 · 名称与别名','项目摘要 · 历次清洗对话','#knowledge/projects'],['经验知识','分类问答 · 适用条件','经验案例 · 来源与验证','#knowledge/qa'],['任务断点','任务目标 · 当前进度','下一步 · 阻塞 · 续接','#progress'],['资料整理','核对归属 · 检查正文','确认入库 · 纠正与归档','#knowledge/intake'],['Skill 规则','归属 · 清洗 · 提炼','入库 · 协作学习','#knowledge/skill']];
 const text=(x,y,lines,cls='')=>`<text x="${x}" y="${y}" text-anchor="middle" class="${cls}">${lines.map((s,i)=>`<tspan x="${x}" dy="${i?24:0}">${s}</tspan>`).join('')}</text>`;
 const box=(x,y,w,h,lines,cls='')=>`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="12" class="node ${cls}"/>${text(x+w/2,y+h/2-(lines.length-1)*12+6,lines)}`;
 const arrow=(d,label,x,y)=>`<path d="${d}" class="edge" marker-end="url(#arrow)"/>${label?text(x,y,[label],'edge-label'):''}`;
 const diamond=(x,y,label)=>`<path d="M${x} ${y-38} L${x+100} ${y} L${x} ${y+38} L${x-100} ${y}Z" class="decision"/>${text(x,y+6,[label])}`;
 const svg=(title,h,body)=>`<svg viewBox="0 0 980 ${h}" role="img" aria-label="${title}" xmlns="http://www.w3.org/2000/svg"><title>${title}</title><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="#657987"/></marker></defs>${body}</svg>`;
 const moduleMap=svg('五个业务模块及各自职责',610,
   box(330,25,320,65,['EvolvMem · 个人知识库'],'accent')+
   arrow('M490 90V130H166V160')+arrow('M490 130V160')+arrow('M490 130H814V160')+
   modules.slice(0,3).map((m,i)=>box(26+i*324,160,280,132,[m[0],m[1],m[2]])).join('')+
   text(490,334,['历史说明“发生过什么” · 经验说明“怎么做” · 断点说明“接着做什么”'],'caption')+
   box(70,390,390,124,[modules[3][0],modules[3][1],modules[3][2]],'warm')+
   box(520,390,390,124,[modules[4][0],modules[4][1],modules[4][2]],'accent')+
   arrow('M265 390V362H166V292','整理后供查阅',146,370)+arrow('M520 452H460')+
   text(490,565,['工作原理是本工作台的说明区，与以上五个业务模块共用主界面。'],'caption'));
 const flow=svg('一段对话如何成为可用知识',830,
   box(30,40,250,84,['① 同步对话','接收各智能体的交流'])+arrow('M280 82H350')+
   box(350,40,260,84,['② 判断项目归属','按绑定、名称、别名核对'])+arrow('M610 82H685')+
   box(685,40,265,84,['不明确 → 资料待确认','你选择项目后继续'],'warm')+
   arrow('M480 124V200','归属明确',546,165)+arrow('M818 124V165H650V240H620')+
   box(340,200,280,90,['③ 清洗与整理表达','去噪正文 + 有依据的需求'])+
   arrow('M480 290V345H190V390','历史',295,365)+arrow('M480 345H770V390','经验 / 需求',683,365)+
   box(50,390,280,92,['历史摘要 + 清洗正文','到「项目历史」查看'])+
   box(625,390,300,92,['简洁问答 + 分类 + 来源','明确且无冲突才自动入库'])+
   arrow('M770 482V512')+diamond(770,550,'依据明确无冲突？')+
   arrow('M670 550H620','否',645,534)+box(340,510,280,80,['资料待确认','核对原话，修正后再入库'],'warm')+
   arrow('M190 482V726H350')+arrow('M770 588V726H630','是，自动入库',843,655)+arrow('M480 590V684','确认后',535,636)+
   box(350,684,280,84,['④ 按问题调用对应知识','查历史 / 找经验 / 继续任务'],'accent')+
   text(490,810,['任务断点由开发过程中主动保存，负责续接；项目摘要在项目历史中。'],'caption'));
 const activity=svg('资料入库活动图：系统与用户如何协作',1190,
   '<rect x="24" y="18" width="932" height="1144" rx="16" class="swimlane"/><path d="M650 18V1162 M24 82H956" class="lane-line"/>'+
   text(337,57,['系统处理'])+text(802,57,['用户确认'])+
   '<circle cx="337" cy="112" r="10" class="terminal"/>'+
   arrow('M337 122V150')+box(207,150,260,64,['接收对话，核对所属项目'])+
   arrow('M337 214V246')+diamond(337,284,'归属明确？')+
   arrow('M437 284H688','[否]',563,269)+box(688,249,230,70,['查看归属线索','选择或新增项目'],'warm')+
   arrow('M803 319V366H345')+arrow('M337 322V358','[是]',375,345)+
   '<path d="M337 358L345 366L337 374L329 366Z" class="decision"/>'+
   arrow('M337 374V404')+box(207,404,260,76,['去掉工具记录与系统注入','保留纯对话正文'])+
   arrow('M337 480V514')+box(207,514,260,82,['生成摘要与简洁问答','需求保留原话和适用条件'])+
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
 mount.innerHTML=`<header class="data-page-header"><span class="knowledge-eyebrow">产品说明</span><h1>工作原理</h1><p>先看五个模块各管什么，再看一段对话怎样成为知识，以及你什么时候需要参与。</p></header><nav class="principles-tabs" aria-label="工作原理图类型"><button data-principle="modules" aria-pressed="true">模块职责图</button><button data-principle="flow" aria-pressed="false">业务流程图</button><button data-principle="activity" aria-pressed="false">入库活动图</button></nav><section class="principle-board"><div class="section-head"><div><h2 id="principle-title">五个业务模块，分别负责什么</h2><p id="principle-description" class="hint">项目摘要属于项目历史；任务断点只回答如何继续任务。</p></div><button class="ui-button" data-diagram-size>放大图中文字</button></div><div class="product-diagram" tabindex="0" aria-label="图示，可横向滚动">${moduleMap}</div><p class="diagram-legend">活动图符号：● 开始 · ◎ 结束 · 圆角框为动作 · 菱形为判断 · 黑条为分支汇合。小屏可横向滚动看清文字。</p></section><div class="principle-links">${modules.map(m=>`<a href="${m[3]}"><strong>${m[0]} →</strong><span>${m[1]}</span></a>`).join('')}</div>`;
 mount.onclick=e=>{const b=e.target.closest('[data-principle]');if(b){mount.querySelectorAll('[data-principle]').forEach(t=>t.setAttribute('aria-pressed',String(t===b)));const kind=b.dataset.principle;mount.querySelector('.product-diagram').innerHTML=diagrams[kind];mount.querySelector('#principle-title').textContent=({modules:'五个业务模块，分别负责什么',flow:'一段对话，怎样成为可用知识',activity:'系统自动处理；有疑问时由你决定'})[kind];mount.querySelector('#principle-description').textContent=({modules:'项目摘要属于项目历史；任务断点只回答如何继续任务。',flow:'资料先明确归属，再清洗与提炼，分别保存历史和分类知识。',activity:'左右泳道区分系统和用户；箭头上的条件说明为什么进入待确认。'})[kind];}if(e.target.closest('[data-diagram-size]')){const board=mount.querySelector('.product-diagram');board.classList.toggle('enlarged');e.target.textContent=board.classList.contains('enlarged')?'恢复图大小':'放大图中文字';}};
}};

/* Product diagrams use explicit SVG activity notation, within the main shell. */
window.EvolvPrinciples={render(mount){
 if(mount.dataset.ready)return;mount.dataset.ready='true';
 const modules=[['数据清洗','清洗 Skill · 原文与 AI 预览','缓存草稿 · 批量保存与删除','#knowledge/cleaning'],['项目历史','已整理 · 清洗后待入库项目','归属 Skill · 预览与批量保存','#knowledge/projects'],['经验知识','分类问答 · 适用条件','经验案例 · 来源与验证','#knowledge/qa'],['任务断点','任务目标 · 当前进度','下一步 · 阻塞 · 续接','#progress'],['资料整理','入库复核 · 核对内容','确认入库 · 纠正与归档','#knowledge/intake'],['Skill 规则','经验提取与验证','提取说明 · 准入条件 · 候选核对','#knowledge/skill']];
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
   text(490,566,['清洗、归属、提取与验证由各自 Skill 管理；历史与经验分别检索。'],'caption'));
 const flow=svg('历史和经验并行处理与检索',920,
   box(305,20,370,70,['AI 清洗预览 · 可切页暂存','勾选后批量保存分类与删除弃用项'])+arrow('M490 90V125','[保留]',539,112)+arrow('M675 55H720','[确认删除]',786,15)+box(720,25,230,65,['删除所选弃用项','统一确认；未勾选不处理'],'warm')+
   box(305,125,370,70,['待入库项目 → 确认归属','使用已保存的项目归属 Skill'])+
   arrow('M490 195V230H245V270')+arrow('M490 230H735V270')+
   box(65,270,360,85,['历史分支','原文、清洗稿、会话与项目摘要'])+
   box(555,270,360,85,['经验分支','提取方法、条件与验证依据'])+
   arrow('M245 355V620')+arrow('M735 355V397')+diamond(735,435,'验证依据有效？')+
   arrow('M635 435H500V530H555','[无 / 不足]',495,415)+box(555,495,360,70,['待验证候选','补充实际结果后重新核对'],'warm')+
   arrow('M915 530H945V435H835')+
   arrow('M735 473V480H930V600H735V620','[有效]',866,590)+
   box(65,620,360,80,['历史检索索引','回答：当时讨论和决定了什么'])+
   box(555,620,360,80,['已验证经验索引','回答：什么方法在什么条件下有效'])+
   arrow('M245 700V745H490V785')+arrow('M735 700V745H490')+
   box(280,785,420,80,['按问题选择历史、经验或同时查询','正文与验证依据保存在数据库'],'accent'));
 const activity=svg('历史与经验处理活动图',1320,
   '<rect x="24" y="18" width="932" height="1278" rx="16" class="swimlane"/><path d="M650 18V1296 M24 82H956" class="lane-line"/>'+
   text(337,57,['系统处理'])+text(802,57,['用户核对'])+
   '<circle cx="337" cy="112" r="10" class="terminal"/>'+arrow('M337 122V150')+
   box(207,150,260,70,['按 Skill 清洗并预览','结果暂存，可切换页面'])+arrow('M467 185H688')+
   box(688,150,230,70,['核对并勾选待处理条目','批量保存分类与删除'],'warm')+
   arrow('M803 220V262')+diamond(803,300,'确认永久删除？')+
   arrow('M703 300H570','[是]',626,282)+box(250,265,320,70,['删除已勾选弃用项','未勾选条目不处理'],'warm')+
   arrow('M410 335V361')+'<circle cx="410" cy="376" r="14" fill="none" stroke="#344b5b" stroke-width="2"/><circle cx="410" cy="376" r="9" class="terminal"/>'+
   arrow('M803 338V450H337V475','[保留：保存并确认归属]',788,418)+'<g transform="translate(0 160)">'+'<rect x="125" y="315" width="420" height="7" class="terminal"/>'+
   arrow('M175 322V360')+arrow('M495 322V360')+
   box(45,360,260,90,['历史：保存原文与清洗稿','生成会话和项目摘要'])+
   box(365,360,260,90,['经验：提取与验证','核对方法、条件和实际结果'])+
   arrow('M175 450V880')+arrow('M495 450V492')+diamond(495,530,'有验证依据？')+
   arrow('M595 530H690','[无 / 不足]',648,512)+box(690,490,230,90,['保留待验证候选','补充可核对的实际结果'],'warm')+
   arrow('M805 580V640H595')+diamond(495,640,'补充后有效？')+
   arrow('M595 640H925V535H920','[否]',927,614)+
   arrow('M395 530H350V760H495V800','[有]',360,509)+
   arrow('M495 678V800','[是]',527,715)+box(365,800,260,80,['正式经验','保留验证范围和来源'])+
   arrow('M495 880V922')+box(45,880,260,75,['历史可检索','记录不等于成功经验'])+
   arrow('M175 955V990H329')+arrow('M495 922V990H345')+'<path d="M337 982L345 990L337 998L329 990Z" class="decision"/>'+
   arrow('M337 998V1025')+box(207,1025,260,60,['按历史 / 经验 / 两者检索'])+
   arrow('M337 1085V1100')+'<circle cx="337" cy="1115" r="14" fill="none" stroke="#344b5b" stroke-width="2"/><circle cx="337" cy="1115" r="9" class="terminal"/></g>');
 const diagrams={modules:moduleMap,flow,activity};
 mount.innerHTML=`<header class="data-page-header"><span class="knowledge-eyebrow">产品说明</span><h1>工作原理</h1><p>先看六个模块各管什么，再看一段对话怎样成为知识，以及你什么时候需要参与。</p></header><nav class="principles-tabs" aria-label="工作原理图类型"><button data-principle="modules" aria-pressed="true">模块职责图</button><button data-principle="flow" aria-pressed="false">业务流程图</button><button data-principle="activity" aria-pressed="false">入库活动图</button></nav><section class="principle-board"><div class="section-head"><div><h2 id="principle-title">六个业务模块，分别负责什么</h2><p id="principle-description" class="hint">项目摘要属于项目历史；任务断点只回答如何继续任务。</p></div><button class="ui-button" data-diagram-size>放大图中文字</button></div><p class="diagram-scroll-hint">小屏可横向滚动看清文字。</p><div class="product-diagram" tabindex="0" aria-label="图示，可横向滚动">${moduleMap}</div><p class="diagram-legend" hidden>活动图符号：● 开始 · ◎ 结束 · 圆角框为动作 · 菱形为判断 · 黑条为分支汇合。</p></section><div class="principle-links">${modules.map(m=>`<a href="${m[3]}"><strong>${m[0]} →</strong><span>${m[1]}</span></a>`).join('')}</div>`;
 mount.onclick=e=>{const b=e.target.closest('[data-principle]');if(b){mount.querySelectorAll('[data-principle]').forEach(t=>t.setAttribute('aria-pressed',String(t===b)));const kind=b.dataset.principle;const board=mount.querySelector('.product-diagram');board.innerHTML=diagrams[kind];board.scrollLeft=0;mount.querySelector('.diagram-legend').hidden=kind!=='activity';mount.querySelector('#principle-title').textContent=({modules:'六个业务模块，分别负责什么',flow:'一段对话，怎样成为可用知识',activity:'历史与经验并行，经验核对验证依据'})[kind];mount.querySelector('#principle-description').textContent=({modules:'项目摘要属于项目历史；任务断点只回答如何继续任务。',flow:'预览与修改可切页暂存；批量操作仅处理勾选项，统一确认弃用项删除，保留项保存分类。',activity:'历史保留记录；经验须有实际验证依据。候选保留待核对，不冒充已验证方法。'})[kind];}if(e.target.closest('[data-diagram-size]')){const board=mount.querySelector('.product-diagram');board.classList.toggle('enlarged');e.target.textContent=board.classList.contains('enlarged')?'恢复图大小':'放大图中文字';}};
}};

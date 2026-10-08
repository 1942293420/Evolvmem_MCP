/* Product diagrams use explicit SVG activity notation, within the main shell. */
window.EvolvPrinciples={render(mount){
 if(mount.dataset.ready)return;mount.dataset.ready='true';
 const modules=[['数据清洗','规则 · 原文与 AI 预览','缓存草稿 · 保存与删除','#knowledge/cleaning'],['自动整理','后台任务 · 分段与归属','切页继续 · 查看与纠正','#knowledge/organization'],['项目历史','已整理 · 待入库项目','归属规则 · 批量保存','#knowledge/projects'],['经验知识','分类问答 · 待确认原因','经验案例 · 来源与验证','#knowledge/qa'],['任务断点','任务目标 · 当前进度','下一步 · 阻塞 · 续接','#progress'],['资料整理','入库复核 · 核对内容','确认入库 · 纠正与归档','#knowledge/intake'],['Skill 规则','经验提取与验证','提取说明 · 准入条件','#knowledge/skill']];
 const text=(x,y,lines,cls='')=>`<text x="${x}" y="${y}" text-anchor="middle" class="${cls}">${lines.map((s,i)=>`<tspan x="${x}" dy="${i?24:0}">${s}</tspan>`).join('')}</text>`;
 const box=(x,y,w,h,lines,cls='')=>`<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="12" class="node ${cls}"/>${text(x+w/2,y+h/2-(lines.length-1)*12+6,lines)}`;
 const arrow=(d,label,x,y)=>`<path d="${d}" class="edge" marker-end="url(#arrow)"/>${label?text(x,y,[label],'edge-label'):''}`;
 const diamond=(x,y,label)=>`<path d="M${x} ${y-38} L${x+100} ${y} L${x} ${y+38} L${x-100} ${y}Z" class="decision"/>${text(x,y+6,[label])}`;
 const svg=(title,h,body,width=980)=>`<svg viewBox="0 0 ${width} ${h}" role="img" aria-label="${title}" xmlns="http://www.w3.org/2000/svg"><title>${title}</title><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="#657987"/></marker></defs>${body}</svg>`;
 const moduleMap=svg('七个业务模块及各自职责',610,
   box(330,20,320,65,['EvolvMem · 个人知识库'],'accent')+
   arrow('M490 85V115H129V145')+arrow('M490 115H357V145')+arrow('M490 115H585V145')+arrow('M490 115H813V145')+
   modules.slice(0,4).map((m,i)=>box(26+i*228,145,206,132,[m[0],m[1],m[2]])).join('')+
   text(490,330,['可直接交给后台自动清洗和归属；人工流程仍可使用，原文保留供核对。'],'caption')+
   modules.slice(4).map((m,i)=>box(120+i*250,385,230,132,[m[0],m[1],m[2]],'warm')).join('')+
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
   box(365,360,260,90,['经验：提取与验证','独立核对答案与原话范围','能收窄则修正；歧义待确认'])+
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
  const organization=svg('后台自动整理活动图',1180,
   '<rect x="24" y="18" width="700" height="1140" rx="16" class="swimlane"/><path d="M724 18V1158 M24 78H724" class="lane-line"/>'+
   text(374,52,['系统后台（单个有界工作线程）'])+text(852,52,['用户核对'])+
   '<circle cx="170" cy="108" r="10" class="terminal"/>'+arrow('M170 118V148')+
   box(40,148,260,66,['新会话自动采集 / 手动勾选','记录来源版本与规则版本'])+arrow('M300 181H360')+
   box(360,148,300,66,['排队等待','同来源同版本不重复建任务'])+
   arrow('M510 214V248')+diamond(510,286,'版本仍有效？')+
   arrow('M410 286H300','[否]',352,268)+box(40,251,260,70,['标记待确认','不提交陈旧结果'],'warm')+
   arrow('M510 324V360')+
   box(360,360,300,80,['去除已知注入，保留原文与作者','同任务保持连贯，不同话题拆分'])+
   arrow('M510 440V474')+diamond(510,512,'覆盖校验通过？')+
   arrow('M410 512H300','[否]',352,494)+box(40,460,260,70,['自动纠正一次','带上错误类型与位置重给本段'])+
   arrow('M170 530V560')+diamond(170,598,'纠正后通过？')+
   arrow('M270 598H420V570H510','[是]',340,588)+
   arrow('M170 636V664','[否]',196,656)+box(40,664,260,70,['停止入库并说明原因','查看失败原因，可显式重试'],'warm')+
   arrow('M510 550V586')+
   box(360,586,300,90,['核对项目、问答与用户原话','推断留待确认','保留已有人工决定'],'warm')+
   arrow('M510 676V712')+
   box(360,712,300,86,['先写历史，再按单元提炼问答','批量核对范围与助手建议','未采纳建议只留历史'])+
   arrow('M510 798V930')+box(360,930,300,96,['更新检索索引','补齐重建期间的资料变化','核对一致后用于召回'])+
   arrow('M510 1026V1060')+'<circle cx="510" cy="1075" r="14" fill="none" stroke="#344b5b" stroke-width="2"/><circle cx="510" cy="1075" r="9" class="terminal"/>'+
   arrow('M660 755H790','[结果]',716,738)+
   box(790,712,220,86,['查看运行/提炼/待确认/失败','逐条改项目、暂存或写指导'],'warm')+
   arrow('M900 798V834')+diamond(900,872,'指导适用范围？')+
   arrow('M800 872H660V906H790','[仅本次]',700,854)+
   arrow('M1000 872H1085V906','[以后适用]',1080,854)+
   box(790,906,220,80,['只修正本批单元','不影响其他单元'],'warm')+
   box(1010,906,150,80,['按条件与例外','匹配后续单元'],'warm')+
   arrow('M900 986V1008H967')+arrow('M1085 986V1008H983')+'<path d="M975 1000L983 1008L975 1016L967 1008Z" class="decision"/>'+arrow('M975 1016V1022')+box(790,1022,370,80,['指导可停用；反例与过宽条件留待确认','经验须绑定真实结果；暂存可恢复'])+arrow('M975 1102V1124')+'<circle cx="975" cy="1139" r="14" fill="none" stroke="#344b5b" stroke-width="2"/><circle cx="975" cy="1139" r="9" class="terminal"/>'+text(375,1140,['覆盖失败时最多纠正一次；未通过不入库。'],'caption'),1200);
  const diagrams={modules:moduleMap,flow,activity,organization};
 mount.innerHTML=`<header class="data-page-header"><span class="knowledge-eyebrow">产品说明</span><h1>工作原理</h1><p>先看七个模块各管什么，再看一段对话怎样成为知识，以及你什么时候需要参与。</p></header><nav class="principles-tabs" aria-label="工作原理图类型"><button data-principle="modules" aria-pressed="true">模块职责图</button><button data-principle="flow" aria-pressed="false">业务流程图</button><button data-principle="activity" aria-pressed="false">入库活动图</button><button data-principle="organization" aria-pressed="false">自动整理活动图</button></nav><section class="principle-board"><div class="section-head"><div><h2 id="principle-title">七个业务模块，分别负责什么</h2><p id="principle-description" class="hint">项目摘要属于项目历史；任务断点只回答如何继续任务。</p></div><button class="ui-button" data-diagram-size>放大图中文字</button></div><p class="diagram-scroll-hint">小屏可横向滚动看清文字。</p><div class="product-diagram" tabindex="0" aria-label="图示，可横向滚动">${moduleMap}</div><p class="diagram-legend" hidden>活动图符号：● 开始 · ◎ 结束 · 圆角框为动作 · 菱形为判断 · 黑条为分支汇合。</p></section><div class="principle-links">${modules.map(m=>`<a href="${m[3]}"><strong>${m[0]} →</strong><span>${m[1]}</span></a>`).join('')}</div>`;
 mount.onclick=e=>{const b=e.target.closest('[data-principle]');if(b){mount.querySelectorAll('[data-principle]').forEach(t=>t.setAttribute('aria-pressed',String(t===b)));const kind=b.dataset.principle;const board=mount.querySelector('.product-diagram');board.innerHTML=diagrams[kind];board.scrollLeft=0;mount.querySelector('.diagram-legend').hidden=!['activity','organization'].includes(kind);mount.querySelector('#principle-title').textContent=({modules:'七个业务模块，分别负责什么',flow:'一段对话，怎样成为可用知识',activity:'历史与经验并行，经验核对验证依据',organization:'整篇自动整理，可离开页面稍后核对'})[kind];mount.querySelector('#principle-description').textContent=({modules:'项目摘要属于项目历史；任务断点只回答如何继续任务。',flow:'预览与修改可切页暂存；批量操作仅处理勾选项，统一确认弃用项删除，保留项保存分类。',activity:'历史保留记录；提炼的问答须完整、内容一致，并保留用户原话与适用范围；明确候选另做独立核对，答案超出原话、缺少限定、丢弃否定或后续被用户纠正时保留待确认；忠实归纳需求可省略描述现状的背景词，收窄片段只有在原问题已获正面回答且表达用户已确认的需求时才算成立，只抄疑问或未采纳备选仍保留待确认。推断和缺少用户依据的内容仍保留待确认；只由助手说出、用户尚未明确采纳的建议不生成候选，只保留在对话历史，用户明确采纳后照常提取；助手普通事实回答、用户对建议的明确决策、真实冲突与已验证经验仍按原规则处理。经验还须绑定实际验证结果。待确认问答显示实际原因，可按原因查看同类，筛选不自动确认。',organization:'任务按来源版本持久运行；去噪移除已知注入包装（含应用内浏览器上下文），保留原始资料与真实作者，正常提到该名称或截图需求不删。开启后自动收集本机新对话，处理进度与失败原因在自动整理页可查。已通过的资料自动更新检索索引；暂时不可用时保留资料并重试。索引重建后补齐期间新增、修改和归档的资料，核对一致后更新检索；暂未追上时保留原有索引，显示待同步并稍后重试。系统检查子会话自动跳过，各增量批次独立保留。整篇分段后逐段做覆盖校验：出现缺口或重叠时，带上错误类型与位置让模型重给该段一次；仍失败才停止入库并说明原因，可显式重试，绝不部分写入。重新分段刷新快照，已有人工结论不被重分段覆盖；提炼时检查问答完整、内容一致与用户原话；每批明确候选再做一次独立核对：答案是否超出原话、是否丢掉限定或否定、后续用户是否纠正；核对未通过、缺少核对或引用变化都保留待确认，修正只采用同一条用户消息里的连续原话，助手尚未被用户采纳的建议只留历史（明确采纳后正常提取），本次要求保留任务范围；经验须绑定真实结果，指导默认只影响本批。同一项目同一任务的连续讨论合并保留项目上下文，换项目或证据不足待确认；引用仅校正空白并保留原文。'})[kind];}if(e.target.closest('[data-diagram-size]')){const board=mount.querySelector('.product-diagram');board.classList.toggle('enlarged');e.target.textContent=board.classList.contains('enlarged')?'恢复图大小':'放大图中文字';}};
}};

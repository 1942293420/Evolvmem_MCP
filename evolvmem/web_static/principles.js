/* Business facts and directed routes drive both diagrams and demonstrations. */
window.EvolvPrinciples={render(mount){
 if(mount.dataset.ready)return;mount.dataset.ready='true';
 const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const n=(id,label,x,y,kind='action',facts={})=>({id,label:label.split('|'),x,y,w:260,h:80,kind,...facts});
 const e=(id,from,to,label='',d='',x=0,y=0)=>({id,from,to,label,d,x,y});
 const r=(id,label,nodes,edges)=>({id,label,nodes:nodes.split(' '),edges:edges.split(' ')});
 const facts=(owner,input,output,rule='',tool='',link='')=>({owner,input,output,rule,tool,link});
 const modules=[['home','总览','处理进度与系统问题','#home'],['organization','自动整理','归类、提炼与集中判断','#knowledge/organization'],['history','项目历史','历史对话与项目摘要','#knowledge/projects'],['qa','经验知识','明确问答与适用范围','#knowledge/qa'],['checkpoint','任务断点','当前进度与下一步','#progress'],['cleaning','数据清洗','核对来源、修改与删除','#knowledge/cleaning'],['intake','存量资料','旧资料归类与纠正','#knowledge/intake'],['rules','整理规则','处理规则与样例预览','#knowledge/skill']];
 const diagrams={
 modules:{title:'从业务目标找到负责的页面',type:'职责图',goal:'知道资料在哪里、疑问在哪里处理、任务从哪里继续。',height:600,nodes:modules.map((m,i)=>n(m[0],m[1]+'|'+m[2],40+(i%3)*310,60+Math.floor(i/3)*165,'action',facts('你与 Evo','当前业务资料和处理状态',m[2],'','',m[3]))),edges:[],routes:[]},
 flow:{title:'新会话怎样成为以后可用的资料',type:'业务流程图',goal:'从需求起点理解过程与结果，保留原话；有疑问时直接核查完整上下文。',height:1550,nodes:[
 n('start','开始：产生新会话',350,20,'start',facts('客户端','用户与助手的可见对话','新增会话批次')),
 n('archive','保存会话与来源',350,130,'action',facts('会话接入','会话批次、客户端与会话标识','会话档案、来源引用','系统工具输出不作为用户要求。')),
 n('source','业务会话？',350,260,'decision',facts('会话接入','原生会话身份与来源标记','业务对话；审批等系统子会话','旧上传记录也按原生身份补查，原文保留。')),
 n('excluded','只留原文|不进入业务召回',30,390,'exception',facts('来源核对','审批等系统子会话','保留原文、退出业务整理与历史召回','仅靠这些来源自动生成的资料停止采用；人工确认与其他有效来源保留。')),
 n('clean','按需求识别完整任务',350,390,'action',facts('任务整理','同一会话的已验证原始批次','从需求提出到当前结果的一段任务；插入别的任务后可续接','同一需求反复修复合为一项，保留原文范围；没有结果不补造。')),
 n('narrative','整理需求、原因、过程与结果',350,520,'action',facts('资料整理模型','完整任务的全部原文','精简经过、各次修复、当前结果和逐字证据','先清理重复表达，再按实际影响对象分类；每条引用可回到原文。')),
 n('classify','按实际影响判断项目',350,650,'action',facts('项目归类模型','完整任务资料、证据与项目说明','项目归属、关联场景与判断理由','支持个人通用或多个项目。原有人工判断不可自动扩大到新合并内容。')),
 n('clear','归属与依据明确？',350,780,'decision',facts('自动整理','实际负责项目、关联场景与证据','有业务依据的归属；上下文不足的待判断资料')),
 n('review','你核对有疑问的部分',690,910,'exception',facts('你','疑问原因、关键原话、建议归属','业务依据、适用范围；可选后续沿用','','','#knowledge/organization')),
 n('save','分类后提炼并保存',350,1040,'action',facts('自动整理','明确内容或你保存的决定','项目历史、可用问答、方法候选','归属不明、冲突和依据失效的问答继续待判断。')),
 n('history','项目历史|当时讨论和决定了什么',30,1190,'store',facts('历史管理','整理后的对话与摘要','可读历史、摘要和来源','','','#knowledge/projects')),
 n('qa','可用问答|以后何时遵守什么要求',350,1190,'store',facts('知识提炼','明确要求、约定与关键原话','问题、答案和适用范围','','','#knowledge/qa')),
 n('case','方法经验|有执行证据才算验证',690,1190,'store',facts('经验验证','需求、方法、结果、依据与适用条件','方法候选或已验证案例','问答入库不代表方法验证成功。','','#experiences')),
 n('index','更新可检索内容',350,1330,'action',facts('检索准备','当前有效资料及变化条目','关键词索引与语义索引','只更新变化条目；索引故障或模型变化时恢复或重建。')),
 n('end','完成：以后按问题找回',350,1460,'end',facts('Evo','当前有效的资料及检索准备状态','以后可供助手按问题召回的资料'))],edges:[
 e('f1','start','archive','新增会话'),e('fs','archive','source','核对来源'),e('fx','source','excluded','系统子会话'),e('f2','source','clean','业务会话'),e('fn','clean','narrative'),e('fc','narrative','classify'),e('f3','classify','clear'),e('f4','clear','save','明确'),e('f5','clear','review','有疑问'),e('f6','review','save','保存决定'),e('f7','save','history','历史摘要'),e('f8','save','qa','明确要求'),e('f9','save','case','方法与证据'),e('f10','history','index'),e('f11','qa','index'),e('f12','case','index'),e('f13','index','end')],routes:[
 r('system','系统子会话只保留原文','start archive source excluded','f1 fs fx'),r('auto','明确内容自动入库','start archive source clean narrative classify clear save qa index end','f1 fs f2 fn fc f3 f4 f8 f11 f13'),r('review','有疑问时由你确认','start archive source clean narrative classify clear review save history index end','f1 fs f2 fn fc f3 f5 f6 f7 f10 f13')]},
 activity:{title:'一个知识片段从候选到可用',type:'活动图',goal:'看清入库前必须核对的条件，以及不能通过时资料去哪里。',height:1160,nodes:[
 n('start','开始：收到知识候选',350,20,'start',facts('知识提炼','会话中提炼出的内容与来源','待核对的知识候选')),n('evidence','核对原话与来源',350,130,'action',facts('自动整理','候选答案、关键原话、来源','依据是否支持答案','助手建议或自称完成不能代替用户要求与实际验证。')),
 n('supported','答案被原话支持？',350,270,'decision',facts('依据核对','答案与关键原话的核对结果','有依据的内容；需确认或修正的候选')),n('pending','保留待判断候选',690,420,'exception',facts('你','不支持、冲突或归属不明的候选','修正、确认或暂不采用','','','#knowledge/organization')),
 n('scope','核对项目、条件与有效期',350,460,'action',facts('入库检查','有依据的内容、归属与适用条件','当前可用或暂不可用','纠正、撤回和来源变化后重新核对资格。')),
 n('eligible','当前资格有效？',350,600,'decision',facts('资格核对','归属、来源、状态与有效期','可以当前采用的内容；只保留历史的内容')),n('blocked','保留历史，停止采用',690,740,'exception',facts('知识管理','已失效或范围不匹配的资料','保留可核对历史；不进入当前召回')),
 n('save','保存一份正文与适用范围',350,760,'action',facts('资料存储','当前有效的内容及来源','一条知识、多个项目关联、来源与版本','选择个人通用时供各项目按条件参考；多项目共用一条知识，修改同步生效。')),
 n('index','更新关键词与语义索引',350,900,'action',facts('检索准备','可检索正文及变化条目','可供查询的索引','索引暂不可用时保留待更新状态，由系统恢复；已有核验内容仍可召回。')),
 n('end','完成：可供当前任务召回',350,1050,'end',facts('Evo','已保存的有效资料及检索状态','当前任务可查找的知识'))],edges:[
 e('a1','start','evidence'),e('a2','evidence','supported'),e('a3','supported','scope','支持'),e('a4','supported','pending','需核对'),e('a5','pending','evidence','修正后重查','M950 460H970V170H610',850,155),e('a6','scope','eligible'),e('a7','eligible','save','有效'),e('a8','eligible','blocked','失效或不适用'),e('a9','save','index'),e('a10','index','end')],routes:[
 r('valid','有依据且当前可用','start evidence supported scope eligible save index end','a1 a2 a3 a6 a7 a9 a10'),r('pending','依据不足时待判断','start evidence supported pending','a1 a2 a4'),r('expired','失效内容保留历史','start evidence supported scope eligible blocked','a1 a2 a3 a6 a8')]},
 organization:{title:'谁处理新资料，谁决定例外',type:'泳道图',goal:'客户端提交资料，Evo 自动处理；你只决定无法可靠自动判断的部分。',height:1290,lanes:[['client','客户端 / 助手',20,305],['system','Evo 自动处理',325,330],['user','你：例外判断',655,325]],nodes:[
 n('submit','提交新增会话',43,110,'action',facts('客户端','可见对话与会话标识','可复核会话批次')),
 n('process','完整任务 → 整理经过 → 分类',360,250,'action',facts('Evo','新增会话、规则、已确认指导','完整任务资料与待确认事项')),
 n('clear','需要你判断？',360,390,'decision',facts('Evo','片段归属、关键原话与冲突','自动保存的资料；需你核对的例外')),n('review','原地核对原文与归属',690,530,'exception',facts('你','从需求起点读取的任务原文、来源与疑问原因','个人通用或多个指定项目；单条确认或按业务依据由 AI 批量判断','点击核查记忆直接展开原文。完整内容分页阅读，可回到需求开头或定位当前记忆。沿有效任务关联展示过程与后续结果；同会话中尚未确认任务关系的内容单独标明。来源缺失会提示；缺少的阶段不补造。')),
 n('judge','恢复已有判断，补充变化项',360,600,'action',facts('Evo','业务依据、不适用情况、适用范围与完整任务上下文','逐条建议、理由、原文证据和待判断项','先核对本批资料版本，复用未变化判断；资料重整状态按最新来源更新。每小批结果保存，下一批跳过已判断条目，上一批结果可返回查看。')),
 n('guidance','预览清单后确认归类',690,670,'exception',facts('你','目标项目、查找范围、符合与排除的条目及数量','归入或不归入的逐条决定；可选持续指导','重整后自动更新结果；区分推荐、不符合、需人工判断和尚未判断。在条目内阅读全文，选择归入或不归入；排除按所选范围保存并可撤销，其他项目仍可使用。模型建议不能覆盖人工排除。','','#knowledge/organization')),
 n('save','保存历史与可用知识',360,830,'action',facts('Evo','明确内容或你保存的决定','当前有效资料、来源和待更新索引')),
 n('request','当前任务需要历史',43,980,'action',facts('助手','当前项目与问题','召回请求')),
 n('recall','查找相关且可用的内容',360,980,'action',facts('Evo','问题、项目与资料类型','相关摘要、适用条件和来源引用','','knowledge_recall / experience_recall')),
 n('use','结合当前要求继续任务',43,1150,'action',facts('助手','召回结果或没有匹配内容','当前任务的下一步','历史只能作为参考，当前用户要求优先。'))],edges:[
 e('o1','submit','process','会话与来源'),e('o2','process','clear'),e('o3','clear','save','不需要','M360 430H335V870H360',338,510),e('o4','clear','review','需要'),e('o5','review','judge','依据与范围'),e('o10','judge','guidance','理由、原文与数量'),e('o6','guidance','save','决定与指导'),e('o7','request','recall','问题与项目'),e('o8','save','recall','有效资料'),e('o9','recall','use','摘要、条件与来源')],routes:[
 r('auto','自动整理后供助手使用','submit process clear save recall use','o1 o2 o3 o8 o9'),r('review','有疑问：确认后继续','submit process clear review judge guidance save recall use','o1 o2 o4 o5 o10 o6 o8 o9')]},
 recall:{title:'当前任务怎样找回历史、知识与经验',type:'召回流程图',goal:'先选业务入口，再核对资格、查找和重新排序，最后返回可读结果。',height:1560,nodes:[
 n('task','当前任务与项目',350,20,'start',facts('助手','当前问题、项目与已知条件','需要的历史信息类型')),
 n('intent','要找回什么？',350,140,'decision',facts('助手','当前任务需要的背景','历史或问答、已验证案例、精确任务断点')),
 n('knowledge','历史讨论 / 可用问答',30,300,'action',facts('知识召回','当前项目、问题、历史或问答类型、阅读预算','相关历史摘要、问答和来源引用','历史回答以前讨论、决定了什么；问答提供明确习惯、规则、环境约定，入库不代表验证成功。','knowledge_recall','#knowledge/qa')),
 n('experience','已验证的方法案例',350,300,'action',facts('经验召回','问题、项目与已直接观察到的条件','方法、适用条件、验证证据和来源','新场景仍须比较条件，不把查到方法当执行成功。','experience_recall','#experiences')),
 n('checkpoint','接着原任务做',690,300,'action',facts('任务续接','当前工作区与原任务指针','已保存进度、下一步与版本','按精确任务指针读取，跳过语义检索和重排序。','continuity_resume','#progress')),
 n('eligible','核对来源与当前适用资格',190,480,'action',facts('召回检查','原始资料、派生摘要与来源版本','当前项目可用的候选','来源撤回、改动或换项目后，旧汇总只留历史；自动约定需有用户原话，人工确认保留。经验另核对验证与适用条件。')),
 n('lexical','关键词与明确标识查找',30,650,'action',facts('词面检索','问题、错误码与明确名称','词面候选与命中依据')),
 n('vector','语义查找：换说法也能找',350,650,'action',facts('语义检索','问题表示与已核验索引','当前项目与通用约定各自的语义候选','先分别保留本项目与通用约定的候选，再合并比较，避免通用资料挤掉本项目内容。新增与改动约每分钟补齐，覆盖数量和待补数量可查。')),
 n('rerank','比较问题与内容，重新排序',190,820,'action',facts('相关性排序','有效候选的摘要与关键正文','按问题相关性排序的候选','重排序比较问题与限定长度的正文，避免短标题丢失约束；弱相关资料还需核对问题中的明确对象，不能拿另一对象的数量作答。返回摘要和来源，需要时继续读原文。')),
 n('related','有相关且可用的内容？',190,980,'decision',facts('结果检查','已排序候选及当前适用资格','有依据的相关资料；没有匹配内容')),n('empty','返回没有匹配内容',690,1120,'exception',facts('召回结果','没有通过相关性与资格检查的候选','空结果','不拿无关资料填满结果。')),
 n('dedup','重复内容只占一个位置|保留各自的来源与适用范围',190,1130,'action',facts('结果整理','已排序的当前可用资料','保留各自的来源与适用范围','短摘要相同不等于完整正文重复；公开条目的独立引用和撤回关系保留。')),
 n('return','按阅读预算返回结果',190,1290,'action',facts('召回结果','相关摘要、问答、方法或精确断点','可供助手阅读的内容和来源','历史结果可按引用继续读取整理后的原对话。','conversation_read')),
 n('continue','结合当前要求继续任务',350,1440,'end',facts('助手','召回结果；必要时读取原文','当前任务的下一步行动'))],edges:[
 e('r1','task','intent'),e('r2','intent','knowledge','讨论 / 规则'),e('r3','intent','experience','验证过的方法'),e('r4','intent','checkpoint','任务进度'),e('r5','knowledge','eligible','摘要与问答来源'),e('r6','experience','eligible','案例与验证条件'),e('r7','eligible','lexical'),e('r8','eligible','vector'),e('r9','lexical','rerank','词面候选'),e('r10','vector','rerank','语义候选'),e('r11','rerank','related'),e('r12','related','empty','没有'),e('r13','related','dedup','有'),e('r14','dedup','return'),e('r15','checkpoint','return','精确读取，跳过检索与排序','M820 380H970V1330H450',795,1270),e('r16','return','continue'),e('r17','empty','continue','空结果也继续任务','M820 1200V1480H610',812,1410)],routes:[
 r('knowledge','找回历史与问答','task intent knowledge eligible vector rerank related dedup return continue','r1 r2 r5 r8 r10 r11 r13 r14 r16'),r('case','查找已验证案例','task intent experience eligible lexical rerank related dedup return continue','r1 r3 r6 r7 r9 r11 r13 r14 r16'),r('none','没有相关内容','task intent knowledge eligible vector rerank related empty continue','r1 r2 r5 r8 r10 r11 r12 r17'),r('resume','精确恢复任务断点','task intent checkpoint return continue','r1 r4 r15 r16')]},
 dataflow:{title:'业务资料怎样流向当前任务',type:'数据流图',goal:'区分会话原文、整理成果、检索候选和返回结果，追踪各自的来源。',height:1170,nodes:[
 n('dialogue','客户端提交的会话',350,20,'actor',facts('客户端','用户与助手可见对话','会话批次及来源标识')),
 n('archive','会话档案与原文',30,210,'store',facts('来源保存','原始会话与来源','可复核对话与来源引用')),
 n('organize','完整任务、整理、分类、提炼',350,210,'process',facts('自动整理','会话全文、关联需求与结果、规则与确认指导','有依据的历史与知识成果')),
 n('rules','规则与已确认指导',690,210,'store',facts('你','适用条件、例外与确认决定','符合条件才适用的指导')),
 n('history','项目历史与摘要',30,430,'store',facts('历史管理','整理后的项目对话','摘要、日期、原文引用')),
 n('qa','当前可用问答',350,430,'store',facts('知识管理','明确问题、答案、归属和依据','有效问答及适用条件')),
 n('cases','案例与验证证据',690,430,'store',facts('经验验证','方法候选、执行结果与证据','已验证案例与场景条件')),
 n('prepare','维护检索索引',350,650,'process',facts('检索准备','当前可检索内容及变化条目','关键词索引与语义索引','索引是查找手段；来源和当前资格仍由正式资料决定。')),
 n('question','助手的当前问题与项目',30,840,'actor',facts('助手','当前用户要求与业务任务','问题、资料类型和阅读预算')),
 n('recall','召回与重新排序',350,840,'process',facts('Evo','问题、索引、有效资料与来源','相关内容、条件和来源引用','','knowledge_recall / experience_recall')),
 n('answer','当前任务可读的结果',350,1040,'actor',facts('助手','历史、知识、适用条件和来源','继续任务；按引用读取原文','数据流向不代表所有资料必须串行经过每个节点。'))],edges:[
 e('d1','dialogue','archive','会话与来源'),e('d2','dialogue','organize','新增对话'),e('d3','archive','organize','原文'),e('d4','rules','organize','指导'),e('d5','organize','history','对话与摘要'),e('d6','organize','qa','有效问答'),e('d7','organize','cases','方法与来源'),e('d8','history','prepare','摘要'),e('d9','qa','prepare','问答'),e('d10','cases','prepare','案例'),e('d11','prepare','recall','检索候选'),e('d12','question','recall','问题'),e('d13','recall','answer','相关内容与来源'),e('d14','archive','answer','按引用读取原文','M160 290H20V1140H480V1120',179,1125)],routes:[
 r('qa','新对话成为可用知识','dialogue organize qa prepare recall answer','d2 d6 d9 d11 d13'),r('history','新对话成为项目历史','dialogue organize history prepare recall answer','d2 d5 d8 d11 d13'),r('read','找到来源后读取原文','dialogue archive answer','d1 d14')]}
 };
 const tabs=[['modules','页面职责'],['flow','完整流程'],['activity','入库活动'],['organization','角色泳道'],['recall','知识召回'],['dataflow','资料数据流']];
 let saved={};try{saved=JSON.parse(sessionStorage.getItem('evolvmem-business-diagrams')||'{}');}catch(_){}
 let kind=diagrams[saved.kind]?saved.kind:'flow',routeId=saved.route||'',step=-1,timer=null,zoom=saved.zoom===1.3?1.3:1;
 const reduced=matchMedia('(prefers-reduced-motion: reduce)');
 const remember=()=>{try{sessionStorage.setItem('evolvmem-business-diagrams',JSON.stringify({kind,route:routeId,zoom}));}catch(_){}};
 const text=(x,y,label,cls='')=>`<text x="${x}" y="${y}" text-anchor="middle" class="${cls}">${label.map((s,i)=>`<tspan x="${x}" dy="${i?25:0}">${esc(s)}</tspan>`).join('')}</text>`;
 const shape=n=>{
  if(n.kind==='start'||n.kind==='end')return `<circle cx="${n.x+130}" cy="${n.y+16}" r="${n.kind==='end'?13:10}" class="diagram-${n.kind}"/>${n.kind==='end'?`<circle cx="${n.x+130}" cy="${n.y+16}" r="8" class="diagram-start"/>`:''}${text(n.x+130,n.y+55,n.label)}`;
  let outline=n.kind==='decision'?`<path class="decision" d="M${n.x+130} ${n.y}L${n.x+260} ${n.y+40}L${n.x+130} ${n.y+80}L${n.x} ${n.y+40}Z"/>`:`<rect class="node ${n.kind==='exception'?'warm':''}" x="${n.x}" y="${n.y}" width="260" height="80" rx="${n.kind==='process'?35:n.kind==='actor'?0:12}"/>`;
  if(n.kind==='store')outline+=`<path class="store-rule" d="M${n.x+14} ${n.y+13}H${n.x+246}M${n.x+14} ${n.y+67}H${n.x+246}"/>`;
  return outline+text(n.x+130,n.y+46-(n.label.length-1)*12.5,n.label);
 };
 function geometry(edge,nodes){
  if(edge.d)return edge;
  const a=nodes.find(n=>n.id===edge.from),b=nodes.find(n=>n.id===edge.to),ax=a.x+130,bx=b.x+130;
  if(a.y===b.y){const left=a.x<b.x;return {...edge,d:`M${left?a.x+260:a.x} ${a.y+40}H${left?b.x:b.x+260}`,x:(left?a.x+260+b.x:a.x+b.x+260)/2,y:a.y+22};}
  const middle=(a.y+80+b.y)/2;
  return {...edge,d:`M${ax} ${a.y+80}V${middle}H${bx}V${b.y}`,x:ax===bx?ax+62:(ax+bx)/2,y:middle-14};
 }
 mount.innerHTML=`<header class="data-page-header"><h1>工作原理</h1></header><nav class="principles-tabs" aria-label="工作原理图类型">${tabs.map(([id,label])=>`<button data-principle="${id}" aria-pressed="false">${label}</button>`).join('')}</nav><section class="principle-board"><div class="section-head"><div><h2 data-diagram-title></h2><p class="diagram-goal"></p></div><button class="ui-button" data-diagram-size>放大查看</button></div><div class="diagram-controls"><label>业务路径 <select data-route-select></select></label><button class="ui-button" data-trace-play>演示路径</button><button class="ui-button" data-trace-next>下一步</button><button class="ui-button" data-trace-reset>重置</button><span data-trace-status role="status"></span></div><div class="product-diagram" tabindex="0" aria-label="业务图，可滚动并选择节点"></div><section data-node-detail hidden aria-label="业务节点详情"></section></section><div class="principle-links">${modules.map(m=>`<a href="${m[3]}"><strong>${esc(m[1])} →</strong></a>`).join('')}</div>`;
 const board=mount.querySelector('.product-diagram'),detail=mount.querySelector('[data-node-detail]'),status=mount.querySelector('[data-trace-status]'),play=mount.querySelector('[data-trace-play]'),select=mount.querySelector('[data-route-select]');
 const stop=()=>{clearTimeout(timer);timer=null;play.textContent='演示路径';};
 const currentRoute=()=>diagrams[kind].routes.find(r=>r.id===routeId);
 function showNode(id){
  const node=diagrams[kind].nodes.find(n=>n.id===id);if(!node)return;
  board.querySelectorAll('[data-node]').forEach(el=>el.setAttribute('aria-pressed',String(el.dataset.node===id)));
  const rows=[['负责人',node.owner],['输入',node.input],['输出',node.output],['判断条件',node.rule],['助手入口',node.tool]].filter(([,value])=>value);
  detail.hidden=false;detail.innerHTML=`<div class="section-head"><h3>${esc(node.label.join(' · '))}</h3><button class="ui-button" data-detail-close>收起</button></div><dl>${rows.map(([label,value])=>`<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl>${node.link?`<a class="ui-button" href="${esc(node.link)}">打开对应页面</a>`:''}`;
 }
 function trace(next){
  const route=currentRoute();if(!route)return;
  step=Math.min(route.nodes.length-1,Math.max(0,next));const active=route.nodes[step],connection=step>0?route.edges[step-1]:null;
  board.querySelectorAll('[data-node]').forEach(el=>el.classList.toggle('trace-active',el.dataset.node===active));board.querySelectorAll('[data-edge]').forEach(el=>el.classList.toggle('trace-active',el.dataset.edge===connection));
  const node=diagrams[kind].nodes.find(n=>n.id===active);showNode(active);status.textContent=`${step+1}/${route.nodes.length} · ${node.label.join(' · ')}`;
  board.scrollTo({top:Math.max(0,node.y*board.querySelector('svg').clientWidth/1000-board.clientHeight/2),behavior:reduced.matches?'instant':'smooth'});
 }
 function reset(){stop();step=-1;status.textContent='业务路径演示';detail.hidden=true;board.querySelectorAll('.trace-active').forEach(el=>el.classList.remove('trace-active'));board.querySelectorAll('[data-node]').forEach(el=>el.setAttribute('aria-pressed','false'));}
 function draw(){
  reset();const d=diagrams[kind],ids=new Set(d.nodes.map(n=>n.id));if(d.edges.some(e=>!ids.has(e.from)||!ids.has(e.to)))throw Error('diagram_endpoint_missing');
  for(const route of d.routes)if(route.edges.length!==route.nodes.length-1||route.edges.some((id,i)=>!d.edges.some(e=>e.id===id&&e.from===route.nodes[i]&&e.to===route.nodes[i+1])))throw Error('diagram_route_invalid');
  routeId=d.routes.some(r=>r.id===routeId)?routeId:d.routes[0]?.id||'';
  mount.querySelectorAll('[data-principle]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.principle===kind)));
  mount.querySelector('[data-diagram-title]').textContent=d.title;mount.querySelector('.diagram-goal').textContent=d.goal;select.innerHTML=d.routes.map(r=>`<option value="${r.id}">${esc(r.label)}</option>`).join('');select.value=routeId;
  mount.querySelector('.diagram-controls').hidden=!d.routes.length;play.disabled=reduced.matches;mount.querySelector('[data-diagram-size]').textContent=zoom>1?'恢复大小':'放大查看';
  const lanes=(d.lanes||[]).map(([id,label,x,w])=>`<g data-lane="${id}"><rect class="swimlane" x="${x}" y="20" width="${w}" height="${d.height-45}"/><path class="lane-line" d="M${x} 80H${x+w}"/>${text(x+w/2,57,[label])}</g>`).join('');
  board.innerHTML=`<svg viewBox="0 0 1000 ${d.height}" role="group" aria-label="${esc(d.type+'：'+d.title)}" xmlns="http://www.w3.org/2000/svg"><title>${esc(d.type+'：'+d.title)}</title><defs><marker id="business-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L10 5L0 10Z" fill="#677c90"/></marker></defs>${lanes}${d.edges.map(edge=>{const e=geometry(edge,d.nodes);return `<g data-edge="${e.id}" data-from="${e.from}" data-to="${e.to}"><path d="${e.d}" class="edge" marker-end="url(#business-arrow)"/>${e.label?text(e.x,e.y,[e.label],'edge-label'):''}</g>`;}).join('')}${d.nodes.map(n=>`<g data-node="${n.id}" role="button" tabindex="0" aria-label="${esc(n.label.join(' · '))}" aria-pressed="false">${shape(n)}</g>`).join('')}</svg>`;
  board.scrollTop=0;board.scrollLeft=0;board.classList.toggle('enlarged',zoom>1);remember();
 }
 function tick(){const route=currentRoute();if(!route||document.hidden||!mount.isConnected){stop();return;}trace(step+1);if(step>=route.nodes.length-1){stop();return;}timer=setTimeout(tick,1400);}
 mount.onclick=event=>{const tab=event.target.closest('[data-principle]');if(tab){kind=tab.dataset.principle;routeId='';draw();return;}const node=event.target.closest('[data-node]');if(node){stop();showNode(node.dataset.node);return;}
  if(event.target.closest('[data-detail-close]'))detail.hidden=true;
  if(event.target.closest('[data-diagram-size]')){zoom=zoom>1?1:1.3;board.classList.toggle('enlarged',zoom>1);event.target.closest('button').textContent=zoom>1?'恢复大小':'放大查看';remember();}
  if(event.target.closest('[data-trace-next]')){stop();trace(step+1);}if(event.target.closest('[data-trace-reset]'))reset();
  if(event.target.closest('[data-trace-play]')){if(timer){stop();return;}if(reduced.matches)return;if(step>=currentRoute().nodes.length-1)step=-1;tick();if(timer)play.textContent='暂停演示';}
 };
 mount.onkeydown=event=>{if(['Enter',' '].includes(event.key)&&event.target.matches('[data-node]')){event.preventDefault();stop();showNode(event.target.dataset.node);}if(event.key==='Escape')reset();};
 select.onchange=()=>{reset();routeId=select.value;remember();};const motionChange=()=>{stop();play.disabled=reduced.matches;};reduced.addEventListener('change',motionChange);
 const visibility=()=>{if(document.hidden)stop();};document.addEventListener('visibilitychange',visibility);
 const cleanup=new MutationObserver(()=>{if(!mount.isConnected){stop();reduced.removeEventListener('change',motionChange);document.removeEventListener('visibilitychange',visibility);cleanup.disconnect();}});cleanup.observe(document.body,{childList:true,subtree:true});draw();
}};

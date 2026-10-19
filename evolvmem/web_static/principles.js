/* Business facts and directed routes drive both diagrams and demonstrations. */
window.EvolvPrinciples={render(mount){
 if(mount.dataset.ready)return;mount.dataset.ready='true';
 const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const n=(id,label,x,y,kind='action',facts={})=>({id,label:label.split('|'),x,y,w:260,h:80,kind,...facts});
 const e=(id,from,to,label='',d='',x=0,y=0)=>({id,from,to,label,d,x,y});
 const r=(id,label,nodes,edges)=>({id,label,nodes:nodes.split(' '),edges:edges.split(' ')});
 const facts=(owner,input,output,rule='',tool='',link='')=>({owner,input,output,rule,tool,link});
 const modules=[['home','总览','处理进度与系统问题','#home'],['organization','自动整理','AI 建议、人工归档与提炼','#knowledge/organization'],['history','项目知识','目录、来源与历史变化','#knowledge/projects'],['qa','经验知识','明确问答与适用范围','#knowledge/qa'],['checkpoint','任务断点','当前进度与下一步','#progress'],['cleaning','数据清洗','核对来源、修改与删除','#knowledge/cleaning'],['intake','存量资料','按项目集中归档与纠正','#knowledge/intake'],['rules','整理规则','处理规则与样例预览','#knowledge/skill']];
 const diagrams={
 storage:{title:'保留原文，让重复内容只存一份',type:'存储维护流程图',goal:'各版会话仍能完整读取，知识来源不变；自动回收不再使用的副本。',height:1080,nodes:[
 n('start','收到新的会话内容',350,20,'start',facts('会话接入','新增或修改的会话','待保存的完整内容')),
 n('space','剩余空间足够？',350,150,'decision',facts('存储维护','当前磁盘余量与本次写入量','保存或稍后重试','默认保留至少 1 GB 空间，并预留本次写入所需空间。')),
 n('wait','保留现有资料|稍后重试新写入',690,310,'exception',facts('会话接入','空间不足','旧资料仍可读，客户端保留待传内容','不靠删除有用原文腾出空间。')),
 n('share','重复部分复用|只保存变化内容',350,310,'action',facts('会话归档','完整会话与已有内容','压缩加密的共享内容和版本目录','每个版本独立指向所需内容；中间版本删除不影响其他版本。')),
 n('verify','核对原文能完整还原',350,470,'action',facts('旧资料迁移','旧副本与新存储','逐字一致的原文','核对成功并保存后，才移除旧完整副本；失败保留旧资料。')),
 n('keep','保留历史版本与知识来源',350,630,'store',facts('资料管理','各版原文、知识引用与审核依据','原有资料编号和来源关系','自动维护不按年龄删除独有原文；当前会话和被引用的清洗结果保留。')),
 n('collect','定时回收无用副本',350,790,'action',facts('存储维护','所有现用版本与引用关系','删除无引用的文件、可重建的旧缓存','通常每小时检查，有迁移积压时分批继续；发现资料损坏时停止回收。')),
 n('end','资料可追溯，空间可复用',350,950,'end',facts('Evo','维护结果','完整原文、有效知识和来源'))],edges:[
 e('s1','start','space'),e('s2','space','share','足够'),e('s3','space','wait','不足'),e('s4','share','verify'),e('s5','verify','keep'),e('s6','keep','collect'),e('s7','collect','end')],routes:[
 r('save','正常保存与回收','start space share verify keep collect end','s1 s2 s4 s5 s6 s7'),r('wait','空间不足时保留资料','start space wait','s1 s3')]},
 modules:{title:'从业务目标找到负责的页面',type:'职责图',goal:'知道资料在哪里、疑问在哪里处理、任务从哪里继续。',height:600,nodes:modules.map((m,i)=>n(m[0],m[1]+'|'+m[2],40+(i%3)*310,60+Math.floor(i/3)*165,'action',facts('你与 Evo','当前业务资料和处理状态',m[2],'','',m[3]))),edges:[],routes:[]},
 flow:{title:'新会话怎样成为以后可用的资料',type:'业务流程图',goal:'从需求起点理解过程与结果，保留原话；有疑问时直接核查完整上下文。',height:1550,nodes:[
 n('start','新会话或旧待整理资料',350,20,'start',facts('客户端','用户与助手的可见对话','新增会话与已有待审原文')),
 n('archive','复用重复内容，保存来源',350,130,'action',facts('会话接入','会话批次、客户端与会话标识','可还原的各版会话、来源引用','相同内容共享保存；系统工具输出不作为用户要求。')),
 n('source','业务会话？',350,260,'decision',facts('会话接入','原生会话身份与来源标记','业务对话；审批等系统子会话','旧的本机采集和远端上传都按原生身份补查，内部审批副本不作业务原文。')),
 n('excluded','只留原文|不进入业务召回',30,390,'exception',facts('来源核对','审批等系统子会话','保留原文、退出业务整理与历史召回','仅靠这些来源自动生成的资料停止采用；人工确认与其他有效来源保留。')),
 n('clean','按需求识别完整任务',350,390,'action',facts('任务整理','同一会话的已验证原始批次','从需求提出到当前结果的一段任务；插入别的任务后可续接','同一需求反复修复合为一项，保留原文范围；没有结果不补造。')),
 n('narrative','整理任务与带条件的事实',350,520,'action',facts('资料整理模型','完整任务的全部原文','任务摘要、事实版本、适用条件与来源','同一整理调用提取事实与关系，原话必须能定位；生效日期与收录日期分开。不同环境并存，互斥说法聚成分歧；已确认新版本不被晚到旧资料覆盖。关系只作建议，不改变人工归属。')),
 n('classify','按实际影响建议项目',350,650,'action',facts('项目归类模型','完整任务资料、证据与项目说明','建议录入的项目、关联场景与理由','AI 永远只提供建议，不写入正式项目归属。用户已保存且适用范围未改变的人工决定可以沿用；新增要求或归属冲突回到待归档。')),
 n('clear','这段内容已有归属？',350,780,'decision',facts('自动整理','原文范围、知识身份与人工决定','沿用原归属；新增内容等待你保存','改摘要、重新拆合不改变知识身份。用户在待归档页确认两种表述相同后保存关联，以后已确认表述复用归属并补充来源，目录与 Agent 按一条知识读取；关联可撤销。混合任务按原文范围拆分，只显示新增内容待归档，旧内容折叠为背景。AI 相似建议不会自动归档。')),
 n('review','选择项目并保存归档',690,910,'exception',facts('你','原文、建议录入的项目','用户保存的项目归属；也可暂不归档','列表、详情和状态可同时读取，保存单独执行；已打开资料和未保存草稿保留，读取超时可重试。','','#knowledge/organization')),
 n('save','按人工归属提炼并保存',350,1040,'action',facts('自动整理','你已保存的归档决定','项目历史、可用问答、方法候选','同一知识复用原记录并保留各份来源，重复导入不重复计数。归属不明、冲突和依据失效的问答继续待判断。')),
 n('history','项目历史|当时讨论和决定了什么',30,1190,'store',facts('历史管理','整理后的对话与摘要','可读历史、摘要和来源','','','#knowledge/projects')),
 n('qa','可用问答|以后何时遵守什么要求',350,1190,'store',facts('知识提炼','明确要求、约定与关键原话','问题、答案和适用范围','','','#knowledge/qa')),
 n('case','方法经验|有执行证据才算验证',690,1190,'store',facts('经验验证','需求、方法、结果、依据与适用条件','方法候选或已验证案例','问答入库不代表方法验证成功。','','#experiences')),
 n('index','更新可检索内容',350,1330,'action',facts('检索准备','当前有效资料及变化条目','关键词索引与语义索引','只更新变化条目；索引故障或模型变化时恢复或重建。')),
 n('end','完成：以后按问题找回',350,1460,'end',facts('Evo','当前有效的资料及检索准备状态','以后可供助手按问题召回的资料'))],edges:[
 e('f1','start','archive','新到或待整理'),e('fs','archive','source','核对来源'),e('fx','source','excluded','系统子会话'),e('f2','source','clean','业务会话'),e('fn','clean','narrative'),e('fc','narrative','classify'),e('f3','classify','clear'),e('f4','clear','save','已有人工决定'),e('f5','clear','review','尚未保存'),e('f6','review','save','保存决定'),e('f7','save','history','历史摘要'),e('f8','save','qa','明确要求'),e('f9','save','case','方法与证据'),e('f10','history','index'),e('f11','qa','index'),e('f12','case','index'),e('f13','index','end')],routes:[
 r('system','系统子会话只保留原文','start archive source excluded','f1 fs fx'),r('auto','沿用已保存的人工归属','start archive source clean narrative classify clear save qa index end','f1 fs f2 fn fc f3 f4 f8 f11 f13'),r('review','选择项目后直接保存','start archive source clean narrative classify clear review save history index end','f1 fs f2 fn fc f3 f5 f6 f7 f10 f13')]},
 activity:{title:'一个知识片段从候选到可用',type:'活动图',goal:'看清入库前必须核对的条件，以及不能通过时资料去哪里。',height:1160,nodes:[
 n('start','开始：收到知识候选',350,20,'start',facts('知识提炼','会话中提炼出的内容与来源','待核对的知识候选')),n('evidence','核对原话与来源',350,130,'action',facts('自动整理','候选答案、关键原话、来源','依据是否支持答案','助手建议或自称完成不能代替用户要求与实际验证。')),
 n('supported','答案被原话支持？',350,270,'decision',facts('依据核对','答案与关键原话的核对结果','有依据的内容；需确认或修正的候选')),n('pending','保留待判断候选',690,420,'exception',facts('你','不支持、冲突或归属不明的候选','修正、确认或暂不采用','','','#knowledge/organization')),
 n('scope','核对项目、条件与有效期',350,460,'action',facts('入库检查','有依据的内容、归属与适用条件','当前可用或暂不可用','纠正、撤回和来源变化后重新核对资格。')),
 n('eligible','当前资格有效？',350,600,'decision',facts('资格核对','归属、来源、状态与有效期','可以当前采用的内容；只保留历史的内容')),n('blocked','保留历史，停止采用',690,740,'exception',facts('知识管理','已失效或范围不匹配的资料','保留可核对历史；不进入当前召回')),
 n('save','保存一份正文与适用范围',350,760,'action',facts('资料存储','当前有效的内容及来源','一条知识、多个项目关联、来源与版本','选择个人通用时供各项目按条件参考；多项目共用一条知识，修改同步生效。点击保存即显示处理状态；内容保存成功后关闭编辑并更新列表，刷新失败仍显示保存成功，写入失败保留草稿。问答保存后保留搜索和筛选。')),
 n('index','更新关键词与语义索引',350,900,'action',facts('检索准备','可检索正文及变化条目','可供查询的索引','索引暂不可用时保留待更新状态，由系统恢复；已有核验内容仍可召回。')),
 n('end','完成：可供当前任务召回',350,1050,'end',facts('Evo','已保存的有效资料及检索状态','当前任务可查找的知识'))],edges:[
 e('a1','start','evidence'),e('a2','evidence','supported'),e('a3','supported','scope','支持'),e('a4','supported','pending','需核对'),e('a5','pending','evidence','修正后重查','M950 460H970V170H610',850,155),e('a6','scope','eligible'),e('a7','eligible','save','有效'),e('a8','eligible','blocked','失效或不适用'),e('a9','save','index'),e('a10','index','end')],routes:[
 r('valid','有依据且当前可用','start evidence supported scope eligible save index end','a1 a2 a3 a6 a7 a9 a10'),r('pending','依据不足时待判断','start evidence supported pending','a1 a2 a4'),r('expired','失效内容保留历史','start evidence supported scope eligible blocked','a1 a2 a3 a6 a8')]},
 organization:{title:'AI 提供建议，你决定归档',type:'泳道图',goal:'旧资料按项目集中保存，新任务沿用已确认内容；AI 只建议归属。',height:1290,lanes:[['client','客户端 / 助手',20,305],['system','Evo 自动处理',325,330],['user','你：归档决定',655,325]],nodes:[
 n('submit','采集会话或恢复旧资料',43,110,'action',facts('客户端','可见对话与会话标识','可复核会话批次')),
 n('process','整理任务、事实与项目建议',360,250,'action',facts('Evo','新旧会话、规则、已确认指导','完整任务、事实版本与待确认事项','已有归属继续有效；同一事实补充来源，独立验证按原始事件计数。')),
 n('factview','查看版本与知识关系',690,110,'action',facts('你','带条件的事实、各份原文与关系建议','按条件或生效日期查看结论','修改对象、使用工具和验证场景分开；属于项目只来自人工保存。','','#knowledge/organization')),
 n('conflict','处理分歧或拆开资料',690,250,'exception',facts('你','同条件互斥说法、新生效版本或错误聚合','采用结论、补充适用情况、暂缓或拆开','重复分歧只保留一项，重复来源补到原项；处理结果保留，不因重跑再次催审。拆开不改变原项目，归档不提升验证等级。')),
 n('clear','这段内容已有决定？',360,390,'decision',facts('Evo','原文范围、知识身份与人工决定','沿用归属或暂不归档；新增内容等待保存','改标题、摘要和拆分方式不要求重复决定。只修改一部分的归属时，其余内容保留原决定；含不同归属的整段资料仍需确认。')),n('review','选择资料与归入项目',690,530,'exception',facts('你','从需求起点读取的任务原文、来源与疑问原因','按项目集中归档，或处理单份资料','旧资料默认按项目分组，整组勾选、逐份排除后一次保存。经验待验证、答案需核对、任务记录和归属冲突分别列出；已有人工归属不被覆盖。已打开的资料和判断结果保存在当前页面缓存，来回切换直接显示；首次读取立即显示当前选择和加载状态，切换时停止旧请求。超时或失败可重新打开，未保存的选择仍保留；主动刷新或保存后更新资料，保存时再核对最新状态。打开资料只核对所选任务的来源，原文变化仍需重新核对。点击核查记忆直接展开原文。完整内容分页阅读，可回到需求开头或定位当前记忆。沿有效任务关联展示过程与后续结果；同会话中尚未确认任务关系的内容单独标明。来源缺失会提示；缺少的阶段不补造。')),
 n('judge','相似召回 → 重排 → 分批判断',360,600,'action',facts('Evo','业务依据、适用范围、待确认记忆的持久化向量','召回数量、相关度顺序、每批最多20条判断','列表就绪即可审核，首页数据在进入首页时读取。预览点击即显示准备状态和已用时间；随后显示等待、相似检索、重排和判断。用户可选择自动切换备用模型，或仅使用指定模型。逐次显示实际模型、调用成功或失败与耗时；没有符合条件的资料时明确显示未调用判断模型。完成后仍可查看记录；已完成判断保留，失败可重试。旧结果在后台恢复，不覆盖你当前选择的资料；同一来源在本次恢复中只核对一次，依据已过期的判断直接标为需重查。先过滤已决定范围，向量相似度达标才召回，最多100条，再重排筛选；只判断前20条。展示待确认、召回和本批数量；可随时停止，下一批沿用顺序并跳过已判断条目。检索不可用时不进行全量AI判断；重试会重新核对共享模型是否恢复，启动或网络失败不会永久卡住。失败处可直接归档当前这条或暂不归档，批量建议失败不替其他资料作决定。缓存向量随内容变化更新。')),
 n('guidance','保存归档选择',690,670,'exception',facts('你','你选择的项目，或可选的 AI 批量建议','归入项目或暂不归档','选择项目即可直接保存，无须先保留或预览。缺少项目时在当前页创建并选中，保留其他选择与草稿。保存按钮立即显示正在保存并防止重复提交，失败在按钮旁提示并保留选择。确认两种表述是同一知识时可关联已有知识并沿用归属，两份来源保留；新增要求单独归档。AI 始终只是建议，持续指导也不能自动归属。批量选择由用户保存后生效；旧资料每20份提交一次，每份归属与状态一起保存，失败保留原状，逐份返回结果；保存期间可切页查看，返回后继续看进度，保存成功后退出旧详情。暂不归档保留原文，可返回待归档。','','#knowledge/organization')),
 n('excluded','暂不归档',690,830,'exception',facts('你','明确保存的暂不归档选择','原文保留，退出待归档列表','同一来源重跑或拆合后仍沿用这个决定，不再次催审。可返回待归档，再选择项目保存。')),
 n('save','保存历史与可用知识',360,830,'action',facts('Evo','你已保存的归档决定','当前有效资料、来源和待更新索引','清洗、归类和审核成功后移除已处理条目。列表刷新失败只重试读取，无需重复保存。规则编辑留在原处并显示已保存。')),
 n('request','当前任务需要历史',43,980,'action',facts('助手','当前项目与问题','召回请求')),
 n('recall','从项目目录统一召回',360,980,'action',facts('Evo','问题、项目与资料类型','当前内容、变化、条件、分歧和来源','','knowledge_recall / experience_recall')),
 n('use','结合当前要求继续任务',43,1150,'action',facts('助手','召回结果或没有匹配内容','当前任务的下一步','历史只能作为参考，当前用户要求优先。'))],edges:[
 e('o1','submit','process','会话与来源'),e('of1','process','factview','版本与关系','M620 290H650V150H690',655,215),e('of2','factview','conflict','需要处理时'),e('o2','process','clear'),e('o3','clear','save','已归入项目','M360 430H335V870H360',338,510),e('o4','clear','review','未保存'),e('o13','clear','excluded','已暂不归档','M620 430H970V870H950',900,430),e('o12','review','guidance','直接保存'),e('o5','review','judge','可选 AI 建议'),e('o10','judge','guidance','理由、原文与数量'),e('o6','guidance','save','归入项目'),e('o11','guidance','excluded','暂不归档'),e('o7','request','recall','问题与项目'),e('o8','save','recall','有效资料'),e('o9','recall','use','摘要、条件与来源')],routes:[
 r('facts','查看事实并处理分歧','submit process factview conflict','o1 of1 of2'),r('auto','沿用已保存的人工归属','submit process clear save recall use','o1 o2 o3 o8 o9'),r('deferred','沿用暂不归档的决定','submit process clear excluded','o1 o2 o13'),r('direct','选项目直接归档','submit process clear review guidance save recall use','o1 o2 o4 o12 o6 o8 o9'),r('review','AI 建议后由你保存','submit process clear review judge guidance save recall use','o1 o2 o4 o5 o10 o6 o8 o9'),r('excluded','暂不归档，原文仍在','submit process clear review guidance excluded','o1 o2 o4 o12 o11')]},
 recall:{title:'当前任务怎样找回历史、知识与经验',type:'召回流程图',goal:'先选业务入口，再核对资格、查找和重新排序，最后返回可读结果。',height:1560,nodes:[
 n('task','当前任务与项目',350,20,'start',facts('助手','当前问题、项目与已知条件','需要的历史信息类型')),
 n('intent','要找回什么？',350,140,'decision',facts('助手','当前任务需要的背景','历史或问答、已验证案例、精确任务断点')),
 n('knowledge','项目知识 / 历史 / 问答',30,300,'action',facts('项目知识目录与统一召回','项目、问题、业务日期、适用条件与阅读预算','与项目目录一致的内容、版本、分歧和原文','用户归档决定正式归属；未归档的冲突资料单独提示。关系建议不扩展归属；入库不代表验证成功。','knowledge_recall','#knowledge/projects')),
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
 e('r1','task','intent'),e('r2','intent','knowledge','讨论 / 规则'),e('r3','intent','experience','验证过的方法'),e('r4','intent','checkpoint','任务进度'),e('r5','knowledge','eligible','已归档知识与来源'),e('r6','experience','eligible','案例与验证条件'),e('r7','eligible','lexical'),e('r8','eligible','vector'),e('r9','lexical','rerank','词面候选'),e('r10','vector','rerank','语义候选'),e('r11','rerank','related'),e('r12','related','empty','没有'),e('r13','related','dedup','有'),e('r14','dedup','return'),e('r15','checkpoint','return','精确读取，跳过检索与排序','M820 380H970V1330H450',795,1270),e('r16','return','continue'),e('r17','empty','continue','空结果也继续任务','M820 1200V1480H610',812,1410)],routes:[
 r('knowledge','找回项目知识与历史','task intent knowledge eligible vector rerank related dedup return continue','r1 r2 r5 r8 r10 r11 r13 r14 r16'),r('case','查找已验证案例','task intent experience eligible lexical rerank related dedup return continue','r1 r3 r6 r7 r9 r11 r13 r14 r16'),r('none','没有相关内容','task intent knowledge eligible vector rerank related empty continue','r1 r2 r5 r8 r10 r11 r12 r17'),r('resume','精确恢复任务断点','task intent checkpoint return continue','r1 r4 r15 r16')]},
 dataflow:{title:'业务资料怎样流向当前任务',type:'数据流图',goal:'区分会话原文、整理成果、检索候选和返回结果，追踪各自的来源。',height:1170,nodes:[
 n('dialogue','客户端提交的会话',350,20,'actor',facts('客户端','用户与助手可见对话','会话批次及来源标识')),
 n('archive','会话档案与原文',30,210,'store',facts('来源保存','原始会话与来源','可复核对话与来源引用')),
 n('organize','整理 → 人工归档 → 提炼',350,210,'process',facts('自动整理','会话全文、关联需求与结果、规则与确认指导','有依据的历史与知识成果')),
 n('rules','规则与已确认指导',690,210,'store',facts('你','适用条件、例外与确认决定','符合条件才适用的指导')),
 n('history','项目知识目录与历史',30,430,'store',facts('项目阅读','已保存的归属、事实、版本、原文','目录、当前内容、条件、变化与分歧','页面和 Agent 使用同一阅读视图；旧资料未提炼事实时仍按原内容展示，不能据此判断没有分歧。关系建议不能扩大项目归属。','project_knowledge.directory','#knowledge/projects')),
 n('qa','当前可用问答',350,430,'store',facts('知识管理','明确问题、答案、归属和依据','有效问答及适用条件')),
 n('cases','案例与验证证据',690,430,'store',facts('经验验证','方法候选、执行结果与证据','已验证案例与场景条件')),
 n('prepare','维护检索索引',350,650,'process',facts('检索准备','当前可检索内容及变化条目','关键词索引与语义索引','索引是查找手段；来源和当前资格仍由正式资料决定。')),
 n('question','助手的当前问题与项目',30,840,'actor',facts('助手','当前用户要求与业务任务','问题、资料类型和阅读预算')),
 n('recall','召回与重新排序',350,840,'process',facts('Evo','问题、索引、有效资料与来源','相关内容、条件和来源引用','','knowledge_recall / experience_recall')),
 n('answer','当前任务可读的结果',350,1040,'actor',facts('助手','历史、知识、适用条件和来源','继续任务；按引用读取原文','数据流向不代表所有资料必须串行经过每个节点。'))],edges:[
 e('d1','dialogue','archive','会话与来源'),e('d2','dialogue','organize','新增对话'),e('d3','archive','organize','原文'),e('d4','rules','organize','指导'),e('d5','organize','history','对话与摘要'),e('d6','organize','qa','有效问答'),e('d7','organize','cases','方法与来源'),e('d8','history','prepare','摘要'),e('d9','qa','prepare','问答'),e('d10','cases','prepare','案例'),e('d11','prepare','recall','检索候选'),e('d12','question','recall','问题'),e('d13','recall','answer','相关内容与来源'),e('d14','archive','answer','按引用读取原文','M160 290H20V1140H480V1120',179,1125)],routes:[
 r('qa','新对话成为可用知识','dialogue organize qa prepare recall answer','d2 d6 d9 d11 d13'),r('history','新对话成为项目历史','dialogue organize history prepare recall answer','d2 d5 d8 d11 d13'),r('read','找到来源后读取原文','dialogue archive answer','d1 d14')]}
 };
 const tabs=[['modules','页面职责'],['flow','完整流程'],['activity','入库活动'],['organization','角色泳道'],['recall','知识召回'],['dataflow','资料数据流'],['storage','存储与回收']];
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

"""Business explanations and additional, source-backed views for the current atlas."""
import re


def enhance(charts, chart, node, edge):
    c = chart('journey', '一段会话怎样帮助下一次开发', '业务总览图', '沿资料的变化阅读；AI 采用方法和提交结果，需要客户端在实际任务中调用。', ['evolvmem/auto_organization.py','evolvmem/memory_recall.py','evolvmem/experience_service.py'])
    entries=[
        ('capture','采集会话与保存原文',45,85,'先保留来源会话与原文；进入自动整理的范围由接入方式、启用时间及来源资格决定。','intake','会话消息、来源身份','原文归档、整理任务'),
        ('organize','分段、归属与审核',465,85,'明确单元继续保存；归属不明或依据不足的部分进入管理页面审核。','intake','原文片段、规则与项目线索','明确归属的内容单元、待审单元'),
        ('save','保存项目历史与问答',885,85,'历史先保存；经验提取另有状态。问答与结构化案例是不同业务资料。','intake-data','明确单元、保留方式','历史、问答、候选案例及来源'),
        ('index','维护检索索引',885,310,'当前合格条目同步全文和向量索引；失败保留同步状态，后台独立恢复。','index','有效条目与内容版本','词面索引、向量索引及同步状态'),
        ('recall','按问题找回相关知识',465,310,'调用方选择历史问答、验证案例或精确续接入口；检索有范围、资格和返回预算。','recall-entries','当前问题、项目、已知条件','相关内容、依据与引用'),
        ('use','AI 核对后用于任务',45,310,'AI 需要核对适用条件并按引用读原文；工具返回结果不等于方法已经采用。','continuity','召回结果、当前任务授权','实际采用记录或不适用判断'),
        ('feedback','提交实际结果与证据',45,555,'客户端通过现有工具提交任务、事件、原始来源和结果；助手宣称成功不直接成为成功证据。','experience','实际结果、来源引用、任务身份','经核验的采用、成功、失败等事件'),
        ('verify','更新方法资格与条件',465,555,'记录独立任务证据并重算资格；反驳或失败可以使案例退回候选。','experience','案例版本、有效证据与条件','案例当前状态、依赖方法降级'),
        ('evolve','维护调用归纳新方法',885,555,'满足案例簇与模型条件时生成候选；新方法不会继承源案例的成功，需要自己的验证。','evolution','可归纳的案例簇','带来源的待验证方法')]
    for key,label,x,y,detail,jump,ins,outs in entries:
        node(c,key,label,x,y,detail,w=310,inputs=ins,outputs=outs)
        c.nodes[-1]['jump']=jump
    for a,b,pts,label,at in [
        ('capture','organize',[(355,125),(465,125)],'原文',(410,104)),
        ('organize','save',[(775,125),(885,125)],'明确单元',(830,104)),
        ('save','index',[(1040,165),(1040,310)],'有效条目与版本',(1136,248)),
        ('index','recall',[(885,350),(775,350)],'候选',(830,329)),
        ('recall','use',[(465,350),(355,350)],'内容与引用',(410,329)),
        ('use','feedback',[(200,390),(200,555)],'实际任务结果',(286,474)),
        ('feedback','verify',[(355,595),(465,595)],'合格证据',(410,574)),
        ('verify','evolve',[(775,595),(885,595)],'可归纳案例',(830,574))]:edge(c,a,b,pts,label,at)
    c.route('从采集到实际反馈','capture','organize','save','index','recall','use','feedback','verify','evolve')
    c.route('只看知识怎样进入任务','save','index','recall','use')

    c = chart('intake-data','原文、整理单元和知识怎样衔接','入库数据流图','箭头表示交接的数据；原文、单元和知识分开存储，来源关系用于回溯和纠错。',['evolvmem/auto_organization.py','evolvmem/unit_extraction.py','evolvmem/unit_derivations.py','evolvmem/organization_review.py'])
    rows=[
        ('client','客户端 / 采集器',35,65,260,80,'external','不同接入链路提交会话，来源和批次身份用于资格检查。','会话消息、外部会话 ID','可归档的来源批次'),
        ('collect','归档与登记',465,35,180,135,'process','写入原文快照，登记来源和当前整理资格；不是每个来源都进入业务整理。','来源批次','原文归档、来源身份及整理资格'),
        ('archive','原文归档与来源',885,65,310,80,'store','session_archives 保存元数据，原文另存归档文件。','归档过程写入的原文与来源','归档 ID、原文快照和引用'),
        ('segment','清洗分段',955,285,180,135,'process','按原文跨度形成单元；核对覆盖、项目归属及来源引用。','原文快照、清洗规则、归属线索','带跨度及归属结果的内容单元'),
        ('units','内容单元与决定',455,315,320,85,'store','organization_units 记录单元、项目、保留方式和各阶段状态。','分段结果、用户决定','明确单元或待审内容'),
        ('review','处理审核',50,285,220,135,'process','现有审核服务读取单元及来源，按 revision 应用你保存的决定。','待审单元、原文与用户操作','展示疑点、按版本保存决定'),
        ('user','你 · 管理页面',35,580,260,80,'external','有疑点时核对来源并决定归属、保留方式；不要求所有资料逐条人工审核。','疑点和来源','审核操作与修改决定'),
        ('extract','保存历史|逐段提取',505,555,220,135,'process','先保存历史，再按保留方式提取；来源及派生关系同时连接整理单元和产出条目。','明确单元、项目与保留方式','历史、问答、候选方法及来源关系'),
        ('knowledge','知识、问答与来源关系',885,580,310,85,'store','context_items、knowledge_qa、context_sources 与 unit_derivations 保存内容及出处；关联结构见知识 E-R 图。','保存与提取过程的结果','有项目、类型、状态与出处的知识')]
    for key,label,x,y,w,h,shape,detail,ins,outs in rows:node(c,key,label,x,y,detail,shape=shape,w=w,h=h,inputs=ins,outputs=outs)
    for a,b,pts,label,at in [
        ('client','collect',[(295,105),(465,105)],'会话批次',(380,81)),
        ('collect','archive',[(645,105),(885,105)],'原文与来源',(765,81)),
        ('archive','segment',[(1040,145),(1040,285)],'原文快照',(1115,228)),
        ('segment','units',[(955,345),(775,345)],'片段与归属',(865,320)),
        ('units','review',[(455,340),(270,340)],'待审内容',(365,316)),
        ('review','units',[(270,385),(455,385)],'确认决定',(365,420)),
        ('review','user',[(110,420),(110,580)],'疑点与原文',(78,495)),
        ('user','review',[(230,580),(230,420)],'审核操作',(287,517)),
        ('units','extract',[(615,400),(615,555)],'明确单元',(685,493)),
        ('extract','knowledge',[(725,622),(885,622)],'资料及出处',(806,600))]:edge(c,a,b,pts,label,at)
    c.route('从原文到知识','client','collect','archive','segment','units','extract','knowledge')
    c.route('有疑点时审核再继续','units','review','user','review','units','extract','knowledge')

    c=chart('classes','检索、经验和续接由哪些类负责','类图','矩形分为类名、主要属性、主要方法；连线表示持有引用，不表示执行顺序或继承。',['evolvmem/context_service.py','evolvmem/context_retriever.py','evolvmem/context_store.py','evolvmem/experience_service.py','evolvmem/continuity_service.py'])
    c.height=890
    rows=[
        ('experience','ExperienceService',40,80,['core: ContextService','store: ContextStore'],['record(case, evidence)','recall(project, query)'],'处理有来源证据的方法案例；experience_recall 使用此服务。','evolvmem/experience_service.py'),
        ('core','ContextService',450,80,['store: ContextStore','retriever: ContextRetriever'],['search(request)','run_consolidation(...)'],'组织 Context Core，提供模式与就绪状态边界；可注入依赖，不把所有关系画成组合所有权。','evolvmem/context_service.py'),
        ('retriever','ContextRetriever',860,80,['store: ContextStore','vector_index: VectorIndex'],['search(request, eligible_ids)'],'执行全文、向量、资格过滤和相关性重排；不负责原文 L2 全量加载。','evolvmem/context_retriever.py'),
        ('continuity','ContinuityService',40,590,['_store: ContextStore','_workspace_identity'],['checkpoint(request)','resume(request)'],'借用数据库精确管理任务焦点与断点；不经过语义检索器。','evolvmem/continuity_service.py'),
        ('store','ContextStore',860,590,['config: Config','_conn: SQLite connection'],['search_fts(...)','list_vector_documents(...)'],'保存条目、分层内容、来源、证据和续接数据；同一借用连接按现有事务规则使用。','evolvmem/context_store.py')]
    for key,label,x,y,attrs,methods,detail,ref in rows:
        node(c,key,label,x,y,detail,[ref],shape='class',w=330,h=225)
        c.nodes[-1].update(attributes=attrs,methods=methods)
    for a,b,pts,label,at in [
        ('experience','core',[(370,132),(450,132)],'core',(410,111)),
        ('core','retriever',[(780,132),(860,132)],'retriever',(820,111)),
        ('retriever','store',[(1025,305),(1025,590)],'store',(1085,454)),
        ('core','store',[(615,305),(615,510),(910,510),(910,590)],'store',(775,486)),
        ('core','continuity',[(500,305),(500,400),(205,400),(205,590)],'_continuity_service',(349,376)),
        ('continuity','store',[(370,702),(860,702)],'_store',(615,679))]:edge(c,a,b,pts,label,at,kind='association')
    c.route('按职责了解主要类','core','retriever','store','continuity','experience')

    order=['journey','overview','usecases','intake','intake-data','manual','recall-entries','retrieval','index','continuity','experience','evolution','correction','data','taskdata','classes','deployment','sharing','maintenance']
    charts.sort(key=lambda c:order.index(c.key))
    groups=[('业务全貌',order[:3]),('采集与审核',order[3:6]),('找回与续接',order[6:10]),('经验与纠正',order[10:13]),('数据与运行',order[13:])]
    static={'overview','usecases','data','taskdata','classes','maintenance'}
    for c in charts:
        c.group=next(g for g,ids in groups if c.key in ids)
        c.reading='relationship' if c.key in static else 'sequence' if c.key=='continuity' else 'flow'
        for n in c.nodes:
            detail,sep,refs=n['detail'].partition('\n核对依据：')
            n['detail']=detail
            n['evidence']=refs.split('；') if sep else c.sources
        for e in c.edges:
            e['payload']=e['label'] or ('控制进入下一步骤' if c.reading=='flow' else '图中已声明的关系')

    # Visible tool names are necessary to connect business language to client calls.
    by={c.key:c for c in charts}
    for n in by['recall-entries'].nodes:
        if n['id'] in {'knowledge','cases','task'}:
            n['label']+='|'+{'knowledge':'knowledge_recall','cases':'experience_recall','task':'continuity_resume'}[n['id']]
    contracts={
      'intake':{
       'send':('已落盘会话、来源身份、批次','会话批次与原始来源'),
       'archive':('会话批次、自动整理启用边界','原文归档、来源引用、符合资格的任务'),
       'segment':('原文快照、清洗分段规则','主题单元、原文跨度、覆盖校验结果'),
       'gate':('主题单元、项目线索、有效指导','明确归属的单元或待审疑点'),
       'review':('待审单元、疑点、原文引用、版本','用户确认的归属与保留决定'),
       'history':('明确单元、项目、保留方式','已保存的历史与来源关联'),
       'extract':('该单元消息、提取规则','问答、方法候选或独立失败状态')},
      'recall-entries':{
       'caller':('当前任务和需要找回的内容','查询、项目、类型、条件或工作区'),
       'knowledge':('project、query、kind、max_chars','历史/有效问答、引用、检索通道'),
       'cases':('project、query、已观察到的 constraints','条件匹配的 active 案例及证据，最多 3 条'),
       'task':('workspace_path、项目提示','焦点任务、断点、版本、时效或候选状态'),
       'output':('所选入口的结果','供客户端核对的内容与准确引用')},
      'retrieval':{
       'query':('问题、项目、类型、资格范围','检索请求及合格条目范围'),
       'lex':('问题文字、标识符、合格范围','全文匹配候选、命中层与分数'),
       'vec':('查询向量、范围内已索引文档','语义近邻及距离；不可用时无此路候选'),
       'merge':('词面与向量候选、条目元数据','合并且资格有效的候选'),
       'rerank':('问题、候选 L0 与有限 L1 内容','重排相关性分数或失败回退结果'),
       'output':('候选分数、门槛、版本及内容指纹','排序去重的 L0 结果、引用与命中通道')},
      'index':{
       'db':('提交后的内容、状态和版本','当前合格向量文档及词面内容'),
       'sync':('本次变化的条目、模型信息','更新/移除向量，失败保留 dirty'),
       'vec':('向量、条目 ID、来源指纹','语义检索使用的索引快照'),
       'fts':('L0/L1 分层内容','全文匹配的索引记录'),
       'recover':('当前文档清单、已存索引与指纹','差异更新、过时删除、恢复状态'),
       'search':('查询与可用索引','合格词面/语义候选')},
      'correction':{
       'user':('单元、纠正内容、当前 revision','保存纠正请求'),
       'apply':('纠正请求、版本与已登记依赖','当前决定和需要更新的受影响对象'),
       'links':('单元、条目及前段依据的身份','已登记来源和派生关系'),
       'own':('仅本段支持的条目和新决定','迁移/替代后的内容及检索状态'),
       'shared':('还有其他来源支持的条目','保留其他依据、重新提取本段'),
       'prior':('依赖已失效前段依据的单元','失效依据与需要重新核对的单元')},
      'experience':{
       'candidate':('新案例或降级案例','保持候选，等待自己的有效证据'),
       'active':('任务/来源核验通过且满足启用规则的证据','条件范围内可供 experience_recall 的案例'),
       'dependent':('原案例不再 active、已有依赖关系','相关方法退回 candidate')},
      'evolution':{
       'trigger':('维护调用与当前核心依赖','本次归纳调用'),
       'cluster':('合格成功案例及任务身份','条件兼容且有独立任务支持的案例簇'),
       'gate':('案例簇、现有方法覆盖、模型可用性','生成候选或明确跳过'),
       'generate':('合格案例簇、模型输出','候选方法、来源关系及验证状态'),
       'candidate':('刚生成的方法及来源案例','没有独立成功证据的新候选'),
       'evidence':('客户端在实际任务中采用后的新来源证据','进入方法证据核验和状态判断'),
       'skip':('缺少前提或输出不合格的原因','跳过结果与诊断')},
    }
    contracts.update({
      'manual':{
       'old':('待清洗的存量来源','选定来源与版本'),
       'preview':('来源原文、清洗规则','供人工核对的清洗预览'),
       'save':('确认清洗稿、来源版本','保存后的清洗稿与类别'),
       'classify':('清洗稿、项目登记信息','预览并确认的项目归属'),
       'persist':('确认内容、项目与准入结果','已保存历史/知识，后续提取状态'),
       'paste':('粘贴文字、用户选择的项目','待准入内容'),
       'admit':('新内容与当前入库规则','正式资料或保留的候选')},
      'deployment':{
       'browser':('你在管理页面的操作','发给 Web 服务的请求'),
       'clients':('当前任务、会话批次或工具请求','发给既有接入端的调用'),
       'web':('管理请求、自动整理任务','资料变更、处理状态或模型请求'),
       'mcp':('客户端工具、采集与模型服务请求','记忆结果、接收回执或模型结果'),
       'db':('服务提交的内容、来源与版本','持久化资料和原文引用'),
       'engine':('待编码文本或查询与候选对','向量或重排相关性分数'),
       'llm':('按功能提交的分段/提取任务','模型建议，仍需本地校验')},
      'sharing':{
       'personal':('个人空间内的记忆资料','可由主人明确选择的摘要'),
       'user':('个人资料和共享目的','明确发布、更新或撤回请求'),
       'publish':('主人身份、选定标题与摘要','独立公共摘要或其更新版本'),
       'public':('通过所有权检查的发布/撤回结果','可查询的公共摘要与状态'),
       'recall':('查询、个人与公共范围','内容、space 和可追溯引用'),
       'withdraw':('主人身份与本人公开条目','公共条目撤回状态，个人原文保留')}
    })
    for cid,items in contracts.items():
        for n in by[cid].nodes:
            if n['id'] in items:n['inputs'],n['outputs']=items[n['id']]
    # A trace displays the exact payload along an authored edge, never an inferred shortcut.
    for c in charts:
        for e in c.edges:
            src=next(n for n in c.nodes if n['id']==e['source'])
            if c.reading=='flow' and not e['label'] and src['outputs']:e['payload']=src['outputs']
    for e in by['recall-entries'].edges:
        endpoint=e['target'] if e['source']=='caller' else e['source']
        n=next(n for n in by['recall-entries'].nodes if n['id']==endpoint)
        e['payload']=n['inputs'] if e['source']=='caller' else n['outputs']
    # Relevant detail navigation; no automatic jump during playback.
    jumps={'overview':{'capture':'intake','organize':'intake','history':'data','qa':'intake-data','search':'recall-entries','experience':'experience','continuity':'continuity','rules':'correction','ops':'maintenance'},'recall-entries':{'knowledge':'retrieval','cases':'experience','task':'continuity'},'retrieval':{'vec':'index'},'intake':{'extract':'evolution'}}
    for cid,items in jumps.items():
        for n in by[cid].nodes:
            if n['id'] in items:n['jump']=items[n['id']]


def render(c, viewer):
    svg=viewer.render_chart(c)
    esc=viewer.esc
    for n in c.nodes:
        if n['shape']!='class':continue
        x,y,w,h=n['x'],n['y'],n['w'],n['h']
        parts=[f'<g class="node class-node" data-node="{n["id"]}" role="button" tabindex="0" aria-label="{esc(n["label"])}"><rect class="shape" x="{x}" y="{y}" width="{w}" height="{h}" rx="6"/>',viewer.text_svg(n['label'],x+w/2,y+34),f'<path class="class-rule" d="M{x} {y+55}H{x+w}M{x} {y+134}H{x+w}"/>']
        for row,lines in [(y+86,n['attributes']),(y+168,n['methods'])]:
            for i,line in enumerate(lines):parts.append(f'<text class="class-member" x="{x+16}" y="{row+i*31}">{esc(line)}</text>')
        parts.append('</g>')
        svg=svg.replace(viewer.render_node(n),''.join(parts))
    css='''
.class-rule{fill:none;stroke:var(--node-stroke);stroke-width:1.5}.class-member{font-size:18px}
.edge-hit{fill:none;stroke:transparent;stroke-width:18;cursor:pointer;pointer-events:stroke}
.edge.current{stroke-dasharray:none;animation:none;stroke:var(--accent);stroke-width:3.5}
.edge.near{stroke:var(--accent);stroke-width:3}.node.related .shape{stroke:var(--accent);stroke-width:2.5}
.trace-dot{fill:var(--accent);stroke:var(--surface);stroke-width:3;pointer-events:none}
.class-node .node-label{font-size:22px}.node-label{font-weight:500}
'''
    # Clickable relations retain exact edge identity, even where lines cross.
    hits=''.join(f'<path class="edge-hit" data-relation="{e["id"]}" tabindex="0" role="button" aria-label="{esc(e["label"] or e["payload"])}" d="M'+ 'L'.join(f'{x} {y}' for x,y in e['points'])+'"/>' for e in c.edges)
    return svg.replace('</style>','</style><style>'+css+'</style>',1).replace('</svg>',hits+'</svg>')

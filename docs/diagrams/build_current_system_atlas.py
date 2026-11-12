"""Document the current EvolvMem implementation; never mutate application data."""
from pathlib import Path
import copy
import hashlib
import json
import re
import subprocess

import build_business_atlas as viewer
import current_atlas_enhancements as enhancements

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
CHARTS = []
FILES = {}


def source(filename, symbol=""):
    path = ROOT / filename
    text = path.read_text()
    FILES[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
    if symbol:
        match = re.search(r"^\s*(?:async )?def " + re.escape(symbol) + r"\(", text, re.M)
        if not match:
            raise ValueError(f"Missing evidence: {filename} {symbol}")
        line = text[:match.start()].count("\n") + 1
    else:
        line = 1
    return f"{filename}:{line}"


def evidence(*items):
    return "\n核对依据：" + "；".join(source(*i) if isinstance(i, tuple) else source(i) for i in items)


def chart(key, title, kind, question, refs):
    c = viewer.Chart(key, title, kind, question)
    c.sources = [source(*r) if isinstance(r, tuple) else source(r) for r in refs]
    CHARTS.append(c)
    return c


def node(c, key, label, x, y, detail, refs=(), shape="action", tone="normal", w=270, h=80, inputs="", outputs=""):
    c.node(key, label, x, y, w, h, shape, tone, detail + evidence(*refs) if refs else detail, inputs, outputs)


def edge(c, a, b, points, label="", at=None, kind="arrow"):
    c.edge(a, b, points, label, *(at or (None, None)), kind=kind)


def grid(c, entries):
    for i, (key, label, detail, refs) in enumerate(entries):
        node(c, key, label, 45 + (i % 3) * 410, 100 + (i // 3) * 200, detail, refs, w=320, h=110)
    c.route("逐项查看模块", *(e[0] for e in entries))


c = chart("overview", "现有系统有哪些功能", "功能模块图", "按现有页面、MCP 接口和后台任务分组；点击模块查看功能、入口与代码依据。", ["evolvmem/knowledge_api.py", "evolvmem/web_static/designs/signal.html", "evolvmem/mcp_contract.py"])
grid(c, [
    ("capture", "会话接入与归档", "Linux Codex 增量采集、Windows Codex 分块上传、Kimi hooks、DSH bundle；原文归档与提取状态分开。不同客户端接入能力见核对清单。", ["evolvmem/local_codex_capture.py", "evolvmem/lan_capture.py", "evolvmem/kimi_hooks.py", "dsh/src/extract.js"]),
    ("organize", "自动整理与集中审核", "新增自动发现、显式处理历史积压、主题分段、逐段归属、提取；支持审核分组、预览后应用、有限重试、重新分段、暂存、仅保留历史和持续指导。入口：#knowledge/organization。", ["evolvmem/auto_organization.py", "evolvmem/organization_review.py", "evolvmem/organization_arrival.py"]),
    ("history", "项目历史与来源", "项目登记、名称别名、项目目录绑定；会话摘要、清洗正文、项目滚动摘要与总记忆。入口：#knowledge/projects。精确正文：conversation_read。", ["evolvmem/project_memory.py", "evolvmem/history_memory.py", "evolvmem/project_store.py"]),
    ("qa", "经验问答与资料管理", "问答、触发条件、原话与来源、可用状态；资料创建、编辑、改归属、入库、归档、恢复、软删除和受约束的永久删除。入口：#knowledge/qa、#knowledge/intake、#knowledge/cleaning。", ["evolvmem/qa_memory.py", "evolvmem/knowledge.py", "evolvmem/knowledge_cleaning.py"]),
    ("search", "知识检索与重排", "knowledge_recall 找历史和经验问答；experience_recall 找有证据的方法；context_search 查分层条目，context_read 按 ID 读详情。全文、向量、资格过滤和重排共同参与。", ["evolvmem/memory_recall.py", "evolvmem/context_retriever.py", "evolvmem/experience_service.py"]),
    ("experience", "方法案例与结果反馈", "方法、条件、执行步骤、成功与失败证据；区分实际采用、未知和不适用。符合条件的案例可生成新方法候选，但不会自动继承成功。入口：#experiences。", ["evolvmem/experience_service.py", "evolvmem/experience_sources.py", "evolvmem/context_playbook.py"]),
    ("continuity", "任务断点与项目进展", "登记目标、已完成、下一步和阻塞；精确续接、查找候选、切换焦点、暂停/恢复/结束；远端工作区绑定、旧会话保守补录、可配置项目看板同步。入口：#progress。", ["evolvmem/continuity_service.py", "evolvmem/lan_backfill.py", "evolvmem/project_board_sync.py"]),
    ("rules", "整理规则与人工纠正", "三个有效规则环节：项目归属、数据清洗、经验提取与验证；保存版本、预览、样例验证及 Skill 导出。旧独立入库编辑已合并；旧协作学习写入已停用。", ["evolvmem/pipeline_skills.py", "evolvmem/knowledge_rules.py", "evolvmem/unit_derivations.py"]),
    ("ops", "运行维护与共享", "状态、采集回执、整理统计、索引增量同步与恢复、历史维护、摘要保留；LAN 个人空间与显式发布的公共摘要，发布/更新/撤回。入口：总览、/trust、MCP 状态与维护工具。", ["evolvmem/lan_runtime.py", "evolvmem/lan_sharing.py", "evolvmem/summary_retention.py", "evolvmem/maintenance.py"]),
])

c = chart("usecases", "你和 AI 现在怎样使用系统", "用例图", "框内是当前已有的业务能力；关联线只表示参与，不表示执行先后。", ["evolvmem/knowledge_api.py", "evolvmem/mcp_contract.py"])
c.background.append('<rect class="boundary" x="270" y="45" width="700" height="660" rx="12"/><text class="section" x="300" y="82">EvolvMem 现有能力</text>')
node(c, "user", "你", 45, 280, "通过当前管理页面核对和维护资料。", shape="actor", w=120, h=125)
node(c, "client", "AI 客户端", 1070, 280, "按已配置的适配器与模式调用工具；不代表所有客户端都具备同样的采集与续接能力。", ["evolvmem/mcp_contract.py"], shape="actor", w=125, h=125)
uses = [("review", "查看整理结果与审核疑点", 155), ("manage", "维护项目、资料和问答", 250), ("rules", "调整整理规则并预览", 345), ("recall", "找回知识与方法证据", 440), ("task", "保存并继续任务", 535), ("feedback", "记录经验及实际结果", 630)]
for key, label, y in uses:
    node(c, key, label, 405, y-33, "具体输入、输出及执行条件见对应专题；此处列出现有用例。", shape="usecase", w=430, h=66)
for key, _, y in uses[:3]: edge(c, "user", key, [(165,340),(405,y)], kind="association")
for key, _, y in uses[3:]: edge(c, "client", key, [(1070,340),(835,y)], kind="association")
edge(c,"user","task",[(165,340),(405,535)],kind="association")
c.route("逐项查看用例", *(k for k,_,_ in uses))

c = chart("intake", "新会话怎样被自动整理", "泳道活动图", "按实际后台顺序：归档、分段、归属、历史保存、经验提取；有疑点的单元进入审核。", ["evolvmem/organization_arrival.py", "evolvmem/auto_organization.py", "evolvmem/unit_extraction.py"])
c.height = 805
for x,w,title in [(30,310,"客户端 / 采集器"),(340,500,"EvolvMem 后台"),(840,370,"你 · 管理页面")]:
    c.background.append(f'<rect class="lane" x="{x}" y="25" width="{w}" height="760"/><rect class="lane-head" x="{x}" y="25" width="{w}" height="58"/><text class="lane-title" x="{x+w/2}" y="63">{title}</text>')
node(c,"start","收到新增会话",175,93,"一个来源批次进入当前流程。",shape="start",w=20,h=20)
node(c,"send","提交新增会话",65,125,"本机增量批次或 Windows 已落盘 JSONL 快照；原生身份决定是否为系统子会话。",["evolvmem/local_codex_capture.py","evolvmem/lan_capture.py"],w=240)
node(c,"archive","归档、校验来源并入队",445,125,"原文先归档。自动发现仅处理启用后的新增来源；历史积压需显式入队。排除来源保留原文但不进入业务整理；无对话、旧版本也可在调用模型前结束。",[("evolvmem/organization_arrival.py","discover_new"),("evolvmem/auto_organization.py","_claim")],w=330)
node(c,"segment","清洗并拆分主题单元",445,250,"模型分段后，程序核对原文跨度、覆盖和引用；同会话续段可使用经验证的前段依据。",[("evolvmem/auto_organization.py","_segment")],w=330)
node(c,"gate","归属与依据|明确？",490,380,"模型建议不能单独决定项目；同时核对登记项目、正文线索、用户指导和前段依赖。",[("evolvmem/auto_organization.py","_assign")],shape="decision",w=240,h=100)
node(c,"review","核对疑点 / 保存决定",870,390,"可改单元归属、保留/暂存/仅历史；集中审核需先预览，再应用。未解决单元继续待审，明确单元可以先保存。",["evolvmem/organization_review.py",("evolvmem/auto_organization.py","correct_one")],tone="review",w=310)
node(c,"history","先保存本段历史",445,540,"按已明确单元保存确定性历史，不需等经验模型提取成功。",[("evolvmem/auto_organization.py","_ingest_units"),("evolvmem/unit_extraction.py","write_history")],w=330,tone="good")
node(c,"extract","提取问答与方法候选",445,650,"只处理该单元的消息；只保留历史的单元跳过经验提取。经验需要独立验证依据，提取失败有单独阶段状态。",[("evolvmem/unit_extraction.py","extract_unit")],w=330,h=55)
node(c,"end","本轮处理结束",599,752,"保留每个单元的已保存、待审核或提取失败状态。",shape="end",w=22,h=22)
edge(c,"start","send",[(185,113),(185,125)])
edge(c,"send","archive",[(305,165),(445,165)],"会话",(373,144))
edge(c,"archive","segment",[(610,205),(610,250)])
edge(c,"segment","gate",[(610,330),(610,380)])
edge(c,"gate","history",[(610,480),(610,540)],"[是]",(645,513))
edge(c,"gate","review",[(730,430),(870,430)],"[有疑点]",(800,405))
edge(c,"review","history",[(1025,470),(1025,580),(775,580)],"保存已明确的部分",(976,551))
edge(c,"history","extract",[(610,620),(610,650)])
edge(c,"extract","end",[(610,705),(610,752)])
c.route("明确单元自动处理","start","send","archive","segment","gate","history","extract","end")
c.route("疑点经用户纠正","start","send","archive","segment","gate","review","history","extract","end")

c = chart("manual", "旧资料和粘贴资料怎样处理", "业务流程图", "人工清洗、项目分类和直接新增是三个现有入口；预览结果需保存后才生效。", ["evolvmem/knowledge_cleaning.py","evolvmem/history_organization.py","evolvmem/knowledge.py"])
node(c,"old","选择原始存量资料",55,105,"数据清洗页列出需要清洗的原始来源；已有归属资料不会因该入口自动全量重处理。",[("evolvmem/knowledge_cleaning.py","listing")])
node(c,"preview","AI 清洗预览并人工修改",465,105,"逐块生成清洗稿，对照原文修改；预览不写资料。",[("evolvmem/knowledge_cleaning.py","preview")])
node(c,"save","保存确认清洗稿",885,105,"核对来源版本，保存清洗稿与资料类别；源资料变化会使旧审核结果过期。",[("evolvmem/knowledge_cleaning.py","save")])
node(c,"classify","预览并确认项目归属",885,335,"待入库项目读取已确认清洗稿；项目预览与确认保存分开，批量保存逐条反馈。",[("evolvmem/history_organization.py","preview"),("evolvmem/history_organization.py","save")])
node(c,"persist","保存资料 / 继续提取",465,335,"归属确认与内容准入分开；项目明确不代表方法已验证。",["evolvmem/history_organization.py","evolvmem/session_extraction.py"],tone="good")
node(c,"paste","手动粘贴并选择项目",55,555,"存量资料支持手工创建，输入正文、类型与项目；系统生成资料部分字段受管理约束。",[("evolvmem/knowledge.py","create")])
node(c,"admit","按规则入库或保留候选",465,555,"知识规则检查来源、项目、长度、冲突与门槛；候选不是当前有效知识。",[("evolvmem/knowledge.py","apply_ingestion")],tone="review")
edge(c,"old","preview",[(325,145),(465,145)])
edge(c,"preview","save",[(735,145),(885,145)])
edge(c,"save","classify",[(1020,185),(1020,335)])
edge(c,"classify","persist",[(885,375),(735,375)])
edge(c,"paste","admit",[(325,595),(465,595)])
edge(c,"admit","persist",[(600,555),(600,415)],"正式或候选",(687,494))
c.route("人工整理旧资料","old","preview","save","classify","persist")
c.route("手工新增资料","paste","admit","persist")

c = chart("recall-entries", "knowledge_recall 到底负责什么", "接口数据流图", "调用方按目的选入口；历史问答、验证案例和任务断点使用不同的数据与规则。", ["evolvmem/mcp_server.py","evolvmem/memory_recall.py","evolvmem/experience_service.py","evolvmem/continuity_service.py"])
node(c,"caller","AI 当前问题与项目",35,310,"调用方决定所需记忆类型；不是系统保证每次问题都会遍历所有入口。",shape="external",w=230)
specs=[("knowledge","历史 / 经验问答",120,"knowledge_recall：kind=history / experience / both。这里 experience 指简洁问答，不等同已验证的方法案例。当前项目历史与当前项目/全局有效问答分别排序，每类最多 8 条，受字数预算限制。","evolvmem/memory_recall.py","recall"), ("cases","有验证证据的方法",320,"experience_recall：当前项目或明确可迁移的 active 案例，先核对条件与验证，再排序；最多返回 3 条。不会直接返回所有新方法候选。","evolvmem/experience_service.py","recall"), ("task","原任务的精确断点",520,"continuity_resume：按工作区、项目、焦点指针精确读取，不走关键词/向量/重排；没有明确焦点时返回候选或原因。","evolvmem/continuity_service.py","resume")]
for key,label,y,detail,f,s in specs:
    node(c,key,label,475,y,detail,[(f,s)],shape="process",w=250,h=130)
node(c,"output","内容、引用、条件与状态",925,310,"context_read 按准确 ID 读分层内容；conversation_read 按归档 ID 读清洗正文。context_search 是另一个通用分层查询入口，不替代这里的业务资格判断。",[("evolvmem/mcp_server.py","_context_search"),("evolvmem/memory_recall.py","read_conversation")],shape="external",w=280)
for key,_,y,*_ in specs:
    edge(c,"caller",key,[(265,350),(350,y+65),(475,y+65)])
    edge(c,key,"output",[(725,y+65),(840,y+65),(925,350)])
c.route("查历史与问答","caller","knowledge","output")
c.route("查有证据的方法","caller","cases","output")
c.route("继续同一任务","caller","task","output")

c = chart("retrieval", "相关内容怎样被找回和重排", "检索流程图", "这是当前 Context 检索器的流程；各业务入口还会在外层增加自己的资格和返回预算。", ["evolvmem/context_retriever.py","evolvmem/memory_recall.py","evolvmem/experience_service.py"])
node(c,"query","问题、项目与有效范围",465,45,"先限定候选资格；范围包括项目、类型、有效时间、状态和必要的已知条件。",[("evolvmem/context_retriever.py","search")])
node(c,"lex","关键词与标识符匹配",80,210,"SQLite FTS5 搜索 L0/L1，补充自然语言中的错误码、代码标识符；资格限制在候选预算前生效。",[("evolvmem/context_retriever.py","search")])
node(c,"vec","问题向量与语义近邻",880,210,"对问题编码，按范围查 USearch；向量不可用时不会虚构语义命中。当前模型配置为 BGE-base-zh-v1.5，768 维。",["evolvmem/context_retriever.py","evolvmem/embedding.py","evolvmem/vector_index.py"])
node(c,"merge","合并候选并核对资格",465,365,"合并词面和向量候选；再次核对状态、项目、有效期与被替代时间。这里只收集和过滤候选，还没确定最终顺序。",[("evolvmem/context_retriever.py","search")])
node(c,"rerank","比较问题与候选内容",465,505,"重排器可用时比较问题和 L0 + L1 内容，相关性优先排序；失败时回退既有门槛。当前配置为本地 mMarco 重排目录。",[("evolvmem/context_retriever.py","_rerank_documents"),"evolvmem/reranking.py"])
node(c,"output","筛选、去重并限量返回",465,640,"检查语义/重排门槛，弱语义补召回还检查问题主体；按当前适用版本和完整指纹去重后取 top_k。knowledge_recall 外层还会对历史摘要或问答做排序与预算裁剪。",[("evolvmem/context_retriever.py","_deduplicate"),("evolvmem/memory_recall.py","_rank_lane")],tone="good")
edge(c,"query","lex",[(530,125),(530,170),(215,170),(215,210)],"词面通道",(335,151))
edge(c,"query","vec",[(670,125),(670,170),(1015,170),(1015,210)],"语义通道",(870,151))
edge(c,"lex","merge",[(215,290),(215,405),(465,405)])
edge(c,"vec","merge",[(1015,290),(1015,405),(735,405)])
edge(c,"merge","rerank",[(600,445),(600,505)])
edge(c,"rerank","output",[(600,585),(600,640)])
c.route("词面候选参与排序","query","lex","merge","rerank","output")
c.route("向量候选参与排序","query","vec","merge","rerank","output")

c = chart("index", "已入库内容怎样变成可检索索引", "数据流图", "SQLite 保存内容事实，索引是可恢复的检索缓存；保存成功和同步成功是不同状态。", ["evolvmem/context_vector_sync.py","evolvmem/lan_runtime.py","evolvmem/vector_provenance.py"])
node(c,"db","当前合格知识与版本",45,150,"可检索文档由数据库资格规则产生。候选、无效来源等不会因为已有向量就自动变成有效知识。",["evolvmem/memory_eligibility.py",("evolvmem/context_store.py","list_vector_documents")],shape="store",w=280)
node(c,"sync","增量同步",480,135,"条目提交后增量更新或移除 L0 向量；任何失败保留 dirty。一次条目成功不清除之前全局 dirty。",[("evolvmem/context_vector_sync.py","upsert_active_l0")],shape="process",w=170,h=130)
node(c,"vec","USearch 向量索引",905,155,"索引文件 context_vectors.usearch；模型维度、文档摘要及向量来源指纹一并核对。",["evolvmem/vector_index.py","evolvmem/vector_provenance.py"],shape="store",w=280)
node(c,"fts","FTS5 全文索引",45,500,"Context 层内容提供词面候选；语义通道故障时词面检索仍可能返回结果。",["evolvmem/context_store.py"],shape="store",w=280)
node(c,"recover","后台核对与恢复",470,490,"LAN 运行时独立恢复 worker 比对当前文档与缓存；恢复不应阻塞采集。实际状态见核对快照，不能把 ready=true 单独当成完全同步。",[("evolvmem/lan_runtime.py","_recovery_loop"),"evolvmem/recall_recovery.py"],shape="process",w=190,h=140)
node(c,"search","检索器读取候选",905,500,"两路候选进入检索流程；健康、dirty、待更新数与实际命中通道分别反馈。",["evolvmem/context_retriever.py","evolvmem/context_vector_health.py"],shape="external",w=280)
edge(c,"db","sync",[(325,190),(480,190)],"变更文档",(405,166))
edge(c,"sync","vec",[(650,200),(905,200)],"向量及指纹",(778,175))
edge(c,"db","fts",[(185,230),(185,500)],"词面内容",(260,377))
edge(c,"db","recover",[(325,210),(380,210),(380,560),(470,560)],"当前文档",(430,449))
edge(c,"recover","vec",[(660,560),(790,560),(790,255),(1045,255),(1045,235)],"恢复后的向量",(877,301))
edge(c,"vec","search",[(1135,235),(1135,500)],"语义候选",(1061,404))
edge(c,"fts","search",[(185,580),(185,680),(1045,680),(1045,580)],"词面候选",(650,707))
c.route("单条变化同步","db","sync","vec","search")
c.route("后台恢复语义索引","db","recover","vec","search")
c.route("词面检索","db","fts","search")

c = chart("experience", "方法案例何时可用、何时退回候选", "状态图", "这里画结构化经验服务的实际状态规则；使用次数、助手自述与成功证据是不同概念。", ["evolvmem/experience_service.py"])
node(c,"initial","记录方法",200,55,"experience_record 可以先不带证据创建候选。",[("evolvmem/experience_service.py","record")],shape="start",w=22,h=22)
node(c,"candidate","候选方法",75,240,"未验证；或发生反驳；或失败任务数不少于成功任务数。不会被 experience_recall 当作已验证案例返回。",[("evolvmem/experience_service.py","_write_outcome")],shape="state",tone="candidate",w=300,h=100)
node(c,"active","已验证且可供检索",820,240,"真实规则：有成功/确认任务、无 unresolved contradicted，且失败任务数小于成功任务数。反馈先核对来源、任务、条件与版本，同一任务/事件去重。",[("evolvmem/experience_service.py","_write_outcome"),("evolvmem/experience_service.py","_validate_evidence")],shape="state",tone="good",w=330,h=100)
node(c,"dependent","依赖它的方法也退回候选",795,550,"该案例不再 active 时，已记录依赖关系的方法降为 candidate。这里只代表代码里已建立的依赖链，不等于系统能自动识别所有语义影响。",[("evolvmem/experience_service.py","_write_outcome")],shape="state",tone="review",w=390,h=100)
edge(c,"initial","candidate",[(211,77),(211,240)])
edge(c,"candidate","active",[(375,275),(820,275)],"合格证据满足启用条件",(597,249))
edge(c,"active","candidate",[(820,320),(690,320),(690,440),(225,440),(225,340)],"反驳或失败达到降级条件",(477,474))
edge(c,"candidate","dependent",[(75,290),(35,290),(35,600),(795,600)],"降级时传播已知依赖",(450,638))
c.route("记录后经证据验证","initial","candidate","active")
c.route("证据改变后降级","active","candidate","dependent")

c = chart("evolution", "现有程序怎样归纳新方法", "活动图", "维护调用触发归纳；程序不会自动执行开发任务来替新方法取得成功证据。", ["evolvmem/context_playbook.py","evolvmem/context_lifecycle.py","evolvmem/context_service.py"])
node(c,"start","进入维护归纳",179,30,"维护调用进入方法归纳流程。",shape="start",w=22,h=22)
node(c,"trigger","触发维护归纳",55,90,"ContextService.run_consolidation 由维护调用或接入 hook 提供；缺少 LLM 时生成阶段明确跳过。",[("evolvmem/context_service.py","run_consolidation")])
node(c,"cluster","筛选成功案例并聚类",465,90,"按项目/通用范围和相似度组簇；结构化案例还核对条件和步骤机制，至少涉及两个独立任务。",[("evolvmem/context_lifecycle.py","evaluate_playbook_eligibility")])
node(c,"gate","条件与模型|都可用？",920,80,"聚类、足够案例、嵌入引擎与提取模型是前提；已有完整覆盖的方法不会重复生成。",[("evolvmem/context_playbook.py","generate")],shape="decision",w=210,h=110)
node(c,"skip","跳过并记录原因",885,530,"例如模型不可用、向量不可用、已有覆盖或模型输出不合格。不是虚构一次成功进化。",[("evolvmem/context_playbook.py","_generate_for_cluster")],tone="review")
node(c,"skipend","本次归纳结束",1009,690,"不生成新方法，结束当前归纳调用。",shape="end",w=22,h=22)
node(c,"generate","生成候选并关联来源案例",465,335,"保存新方法及来源关系；旧的重叠候选可被新候选替代，已有 active 方法不会自动覆盖。",[("evolvmem/context_playbook.py","_generate_for_cluster")],tone="candidate")
node(c,"candidate","候选不继承来源成功次数",55,335,"新方法要接受独立来源与结果验证，不能把多个源案例的成功数相加当成新方法已成功。",["evolvmem/context_playbook.py","evolvmem/experience_service.py"],tone="candidate")
node(c,"evidence","调用方提交实际新证据",55,555,"需要具体任务采用后的真实结果；尚无证据就保持候选。本轮正式库快照：30 条 playbook 均为 candidate。",["evolvmem/experience_service.py"],tone="good")
node(c,"end","按证据进入方法状态判断",179,690,"结果进入方法状态图中的资格判定；不保证启用。",shape="end",w=22,h=22)
edge(c,"start","trigger",[(190,52),(190,90)])
edge(c,"trigger","cluster",[(325,130),(465,130)])
edge(c,"cluster","gate",[(735,130),(920,135)])
edge(c,"gate","generate",[(1025,190),(1025,375),(735,375)],"[是]",(1010,280))
edge(c,"gate","skip",[(1130,135),(1200,135),(1200,570),(1155,570)],"[否]",(1170,449))
edge(c,"skip","skipend",[(1020,610),(1020,690)])
edge(c,"generate","candidate",[(465,375),(325,375)])
edge(c,"candidate","evidence",[(190,415),(190,555)])
edge(c,"evidence","end",[(190,635),(190,690)])
c.route("具备条件生成候选","start","trigger","cluster","gate","generate","candidate","evidence","end")
c.route("缺少条件跳过","start","trigger","cluster","gate","skip","skipend")

c = chart("correction", "纠正资料后，哪些内容跟着变化", "数据流图", "当前传播依靠明确的来源、派生与前段依赖关系；不把尚未实现的全面语义复核画进来。", ["evolvmem/auto_organization.py","evolvmem/unit_derivations.py","evolvmem/organization_context.py"])
node(c,"user","你保存单元纠正",45,300,"可改变项目归属、保留方式和适用指导；提交带当前 revision。",[("evolvmem/auto_organization.py","correct_one")],shape="external")
node(c,"apply","核对版本|应用决定",440,275,"先保存用户决定，并检查是否改变项目；旧预览不能覆盖新决定。",[("evolvmem/auto_organization.py","correct_one")],shape="process",w=180,h=130)
node(c,"links","来源及派生关系",425,70,"unit_derivations 记录每段产出的条目；organization_context_basis 记录沿用前段依据的单元。",["evolvmem/unit_derivations.py","evolvmem/organization_context.py"],shape="store",w=270)
node(c,"own","本段独有的派生资料",880,165,"按现有逻辑迁移或替代派生资料；重新核对问答指纹和冲突，更新检索状态。",[("evolvmem/auto_organization.py","_follow_derived"),("evolvmem/unit_derivations.py","retarget")],shape="store",w=320)
node(c,"shared","另有来源支持的资料",880,345,"不直接整体搬走其他来源共同支持的知识；保留来源关系，重提取本段内容。",[("evolvmem/unit_derivations.py","is_shared"),("evolvmem/auto_organization.py","_follow_derived")],shape="store",w=320)
node(c,"prior","依赖前段判断的单元",880,555,"使已失效的前段依据退出使用，并把受影响单元转回待核对；处理范围受限。",[("evolvmem/organization_context.py","invalidate_dependents")],shape="store",tone="review",w=320)
edge(c,"user","apply",[(315,340),(440,340)],"纠正与版本",(376,318))
edge(c,"links","apply",[(555,150),(555,275)],"依赖关系",(635,218))
edge(c,"apply","own",[(620,315),(730,315),(730,205),(880,205)],"迁移 / 替代",(794,182))
edge(c,"apply","shared",[(620,350),(760,350),(760,385),(880,385)],"保留并重提取",(775,425))
edge(c,"apply","prior",[(530,405),(530,595),(880,595)],"退回核对",(710,569))
c.route("修正本段派生资料","user","apply","own")
c.route("共享资料保留其他依据","user","apply","shared")
c.route("使失效前段依据退出使用","user","apply","prior")

c = chart("continuity", "保存断点后怎样继续同一任务", "时序图", "从上到下是调用顺序；任务按焦点精确读取，跨设备时还需要现有工作区绑定。", ["evolvmem/continuity_service.py","evolvmem/lan_context.py"])
participants=[("user","你",120),("old","原客户端",425),("memory","EvolvMem",735),("new","续接客户端",1080)]
for key,label,x in participants:
    node(c,key,label,x-100,40,"continuity_begin 登记/读取项目任务；checkpoint 携带版本保存；resume 读取精确焦点。无焦点、多候选、失效焦点、代码版本变化分别返回状态。",["evolvmem/continuity_service.py"],shape="participant",w=200,h=65)
    c.background.append(f'<path class="lifeline" d="M{x} 105V710"/>')
xs={k:x for k,_,x in participants}
messages=[("user","old",170,"确认任务与进度","arrow"),("old","memory",255,"保存断点及期望版本","arrow"),("memory","old",325,"返回新版本 / 版本冲突","return"),("user","new",415,"继续原任务","arrow"),("new","memory",500,"按工作区与项目读取焦点","arrow"),("memory","new",575,"返回断点、版本与新旧状态","return"),("new","user",660,"核对后续做；歧义时请你选择","return")]
for a,b,y,label,kind in messages:edge(c,a,b,[(xs[a],y),(xs[b],y)],label,((xs[a]+xs[b])/2,y-18),kind)
c.route("按消息顺序查看",*(k for k,_,_ in participants))
c.routes[0]['edge_ids']=[e['id'] for e in c.edges]

c = chart("data", "原文、知识、问答和证据怎样关联", "E-R 关系图", "矩形为已存在的数据实体；连线注明基数。字段与表名放在节点详情中。", ["evolvmem/context_store.py","evolvmem/learning_schema.py"])
entities=[("archive","原始会话归档",50,95,"session_archives：id、adapter、external_session_id、payload_path、状态、到期时间。正文在归档文件中。"),("source","来源关联",480,95,"context_sources：item_id、archive_id（可空）、source_kind、source_ref。多个来源可支持同一条目。"),("item","知识条目",890,95,"context_items：类型、项目、scope、status、条件/经验载荷、有效时间、替代关系。"),("evidence","验证事件",480,345,"context_evidence：item_id、source_id、task_id、event_key、outcome、条件、revision。来源和任务绑定验证。"),("layers","三层内容",890,345,"context_layers：item_id、layer=l0/l1/l2、content。L0 检索摘要，L1 详情，L2 完整内容。"),("unit","整理单元",50,565,"organization_units：task_id + digest 定位一段原文；含来源跨度、归属决定、保留方式和提取状态。"),("derived","派生关系",480,565,"unit_derivations：unit_task_id、unit_digest、item_id。关联原文单元与其生成的知识。"),("qa","经验问答",890,565,"knowledge_qa：item_id、question、answer、source_fingerprint、status；是否有效还要核对底层知识资格。")]
for key,label,x,y,detail in entities:node(c,key,label,x,y,detail,shape="external",w=290)
edge(c,"archive","source",[(340,135),(480,135)],"1 → 0..N",(410,111),"association")
edge(c,"source","item",[(770,135),(890,135)],"0..N → 1",(830,111),"association")
edge(c,"source","evidence",[(625,175),(625,345)],"1 → 0..N",(688,270),"association")
edge(c,"item","layers",[(1035,175),(1035,345)],"1 → 0..3",(1120,270),"association")
edge(c,"item","qa",[(1180,135),(1210,135),(1210,605),(1180,605)],"1 → 0..1",(1143,505),"association")
edge(c,"unit","derived",[(340,605),(480,605)],"1 → 0..N",(410,582),"association")
edge(c,"derived","item",[(770,605),(825,605),(825,185),(925,185),(925,175)],"0..N → 1",(752,474),"association")
edge(c,"evidence","item",[(770,385),(800,385),(800,235),(945,235),(945,175)],"0..N → 1",(884,216),"association")
c.route("知识回溯原文","item","source","archive")
c.route("核对方法证据","item","evidence","source","archive")
c.route("从整理单元追派生资料","unit","derived","item","qa")

c = chart("taskdata", "项目、工作区、焦点和断点怎样关联", "E-R 关系图", "任务不是历史摘要；焦点指向具体任务，任务再指向当前断点条目。", ["evolvmem/context_store.py","evolvmem/continuity_service.py","evolvmem/project_store.py"])
for key,label,x,y,detail in [("project","登记项目",50,110,"context_project_registry：project、display_name、status；别名独立保存在 context_project_aliases。"),("binding","工作区项目绑定",470,110,"context_project_workspace_bindings：workspace_fingerprint、project、state、is_default。目录身份经过散列；跨设备还有显式绑定。"),("focus","当前任务焦点",890,110,"continuity_focus：project + workspace_fingerprint、workstream_id、revision；焦点可为空，切换有版本检查。"),("task","任务",890,470,"continuity_workstreams：id、project、workspace_fingerprint、parent_id、current_context_id、checkpoint_revision、state_version、status、Git 锚点。"),("checkpoint","当前断点条目",470,470,"context_items 中 workstream_checkpoint 类型，context_layers 保存目标、决定、进度、下一步与阻塞。"),("events","任务变更事件",50,470,"continuity_events：workstream_id、event_type、前后 revision/state_version 与错误码。")]:node(c,key,label,x,y,detail,shape="external",w=290,h=100)
edge(c,"project","binding",[(340,160),(470,160)],"1 → N",(405,138),"association")
edge(c,"binding","focus",[(760,160),(890,160)],"同一绑定",(825,137),"association")
edge(c,"focus","task",[(1035,210),(1035,470)],"指向 0..1 个任务",(1127,351),"association")
edge(c,"task","checkpoint",[(890,520),(760,520)],"当前指向 1 条",(825,494),"association")
edge(c,"task","events",[(1035,570),(1035,665),(195,665),(195,570)],"1 → 0..N",(615,696),"association")
c.route("从目录找到当前断点","project","binding","focus","task","checkpoint")
c.route("查看任务变更记录","task","events")

c = chart("deployment", "当前服务和数据部署在哪里", "部署图", "已核对 Linux 两个现有服务在运行；客户端接入差异与本轮未实测项见核对清单。", ["evolvmem/web_server.py","evolvmem/lan_server.py","evolvmem/lan_runtime.py"])
c.background.append('<rect class="boundary" x="380" y="30" width="830" height="680" rx="12"/><text class="section" x="410" y="69">Linux 主机 · 现有部署</text>')
node(c,"browser","你的浏览器",35,130,"访问当前管理页；本轮无登录态请求返回登录页，受保护 API 返回 401，未把这当作接口故障。",shape="external",w=260)
node(c,"clients","AI 客户端 / Windows 采集器",20,475,"Codex 配置通过本地 MCP 入口转发到已配置 LAN；Windows 通过既有远程接入上传。Kimi/DSH 有各自 hooks 或 bundle。",["evolvmem/lan_stdio.py","scripts/windows/evolvmem-codex.ps1"],shape="external",w=325,h=90)
node(c,"web","管理 Web · 9377",425,130,"systemd 用户服务 evolvmem-web.service，Python web_server；管理页面与自动整理 worker。入口 http://192.168.1.135:9377/。",["evolvmem/web_server.py"],w=310)
node(c,"mcp","LAN MCP · 9378",425,480,"systemd 用户服务 evolvmem-lan-mcp.service；个人命名空间、公共摘要、会话上传、共享模型及恢复 worker。",["evolvmem/lan_runtime.py","evolvmem/lan_tools.py"],w=310)
node(c,"db","SQLite 与原文归档",885,130,"主人数据目录 ~/.claude/evolvmem；memory.db 为内容事实源，原文加密归档。其他个人空间与公共摘要独立保存。",["evolvmem/config.py","evolvmem/lan_runtime.py","evolvmem/session_archive.py"],shape="store",w=300)
node(c,"engine","共享本地向量与重排模型",850,475,"LAN 运行时加载 BGE-base-zh-v1.5.f16.gguf（768维）与配置的 mMarco 重排器；Web 经 127.0.0.1:9378 调用。主索引为 context_vectors.usearch。",["evolvmem/lan_runtime.py","evolvmem/embedding_client.py"],w=345,h=90)
node(c,"llm","已配置的外部提取模型",20,310,"清洗、分段、提取和方法归纳按调用需要使用现有配置模型；本次仅绘图，没有发送业务正文或触发模型提取。",["evolvmem/kimi_hooks.py","evolvmem/topic_segmentation.py"],shape="external",w=325)
edge(c,"browser","web",[(295,170),(425,170)],"管理操作",(360,146))
edge(c,"clients","mcp",[(345,520),(425,520)])
edge(c,"web","db",[(735,170),(885,170)],"资料读写",(810,148))
edge(c,"mcp","db",[(735,505),(785,505),(785,250),(1035,250),(1035,210)],"个人库与来源",(971,281))
edge(c,"mcp","engine",[(735,540),(850,540)],"编码 / 重排",(793,590))
edge(c,"web","llm",[(490,210),(490,350),(345,350)],"处理请求",(470,280))
edge(c,"web","mcp",[(580,210),(580,480)],"模型服务",(649,414))
c.route("浏览器管理资料","browser","web","db")
c.route("客户端检索记忆","clients","mcp","engine")
c.route("自动整理调用提取模型","browser","web","llm")

c = chart("sharing", "现有公共摘要如何发布和撤回", "数据流图", "这是已有 LAN 功能，未把你当前的单人使用需求扩展为多人改造；本次未发布任何资料。", ["evolvmem/lan_sharing.py","evolvmem/lan_tools.py"])
node(c,"personal","个人资料与私有来源",50,120,"个人空间由认证身份选择，续接任务仍在个人空间。",["evolvmem/lan_runtime.py"],shape="store",w=290)
node(c,"user","资料主人明确选择",50,490,"显式选择要公开的标题和摘要；源码存在共享能力不等于授权自动发布全部历史。",["evolvmem/lan_sharing.py"],shape="external",w=290)
node(c,"publish","发布 / 更新摘要",490,105,"memory_publish、memory_update_public；只提交整理后的标题和摘要，校验所有者与请求标识。",[("evolvmem/lan_sharing.py","publish"),("evolvmem/lan_sharing.py","update")],shape="process",w=200,h=140)
node(c,"public","独立公共摘要库",880,120,"公共摘要属于参考历史，不能当作已验证经验依据或执行指令。",["evolvmem/lan_sharing.py"],shape="store",w=310)
node(c,"recall","LAN 读取与检索",880,500,"搜索可组合个人与公共摘要；精确读取带 space 或 qualified ref。公共摘要不授予访问原始私人会话。",["evolvmem/lan_tools.py"],shape="external",w=310)
node(c,"withdraw","撤回本人发布的摘要",455,505,"memory_unpublish：核对所有权后撤回公共条目；不会删除对应个人原文。",[("evolvmem/lan_sharing.py","unpublish")],w=280)
edge(c,"personal","publish",[(340,160),(490,160)],"选定摘要",(415,137))
edge(c,"user","publish",[(195,490),(195,330),(590,330),(590,245)],"明确发布请求",(392,306))
edge(c,"publish","public",[(690,175),(880,175)],"标题与摘要",(785,150))
edge(c,"public","recall",[(1035,200),(1035,500)],"公开内容及引用",(1130,352))
edge(c,"user","withdraw",[(340,530),(455,530)],"撤回",(397,508))
edge(c,"withdraw","public",[(735,545),(800,545),(800,270),(1070,270),(1070,200)],"撤回状态",(914,295))
c.route("显式发布摘要","user","publish","public","recall")
c.route("撤回公开内容","user","withdraw","public")

c = chart("maintenance", "运行维护现在提供哪些能力", "维护模块图", "维护入口和保留规则按现有代码列出；规则可用不等于本轮已经执行维护。", ["evolvmem/maintenance.py","evolvmem/session_archive.py","evolvmem/summary_retention.py"])
grid(c,[
    ("health","状态与处理进度","context_status / memory_status；采集回执、同步链、自动整理任务/阶段、质量反馈与统计。ready、dirty、待更新数和实际召回通道分别表达。",["evolvmem/context_vector_health.py","evolvmem/organization_progress.py","evolvmem/organization_metrics.py"]),
    ("retry","失败重试与旧版本收尾","自动整理有限次数重试，失败可显式重试；无对话、被排除来源和旧版本有独立结束原因。重启后 running 任务回到 pending。",["evolvmem/auto_organization.py"]),
    ("recover","索引同步、恢复与迁移","单条增量更新；LAN 恢复 worker；有专用迁移、备份、预览/应用和一致性检查工具。执行前后保留真实回执。",["evolvmem/context_vector_sync.py","evolvmem/recall_recovery.py","evolvmem/cutover_cli.py"]),
    ("rollup","项目滚动摘要","按项目保存汇总与来源覆盖关系；历史快照不自动成为当前有效要求。保留、读取与后续摘要更新有独立状态。",["evolvmem/project_rollup.py","evolvmem/project_memory.py"]),
    ("retention","原文与摘要保留","现代码仍有 TTL：原文默认 30 天，会话摘要默认 30 天、每项目最近 10 条；原文删除受 hold/来源保护，摘要退役需滚动摘要覆盖。本机 config 未覆盖这些默认值；是否实际触发清理另看调用。这与新确认的长期保留目标尚有差距。",["evolvmem/config.py","evolvmem/summary_retention.py","evolvmem/session_archive.py"]),
    ("cleanup","人工归档、恢复与删除","资料状态转换和受约束的永久删除已有入口；被引用/正在处理的原始资料禁止直接删，清洗删除写 tombstone 避免旧快照重现。",["evolvmem/knowledge.py","evolvmem/knowledge_cleaning.py"]),
    ("retired","已停用的旧学习入口","自动协作规则生成、应用与注入已停用；历史版本保留只读。入库条件并入经验提取编辑，不再画成四五条独立运行管线。",["evolvmem/knowledge_api.py","evolvmem/pipeline_skills.py"]),
    ("board","可配置的项目看板同步","project_board_sync/status：同步任务进度到已配置看板，独立队列记录状态。本轮核对代码，未验证看板当前是否配置和最近同步结果。",["evolvmem/project_board_sync.py"]),
    ("modes","旧接口与兼容模式","memory_* 兼容接口保留；Core 工具受 adapter、legacy/compat/shadow/primary 和健康状态控制。独立 continuity 不依赖语义检索就绪。",["evolvmem/mcp_contract.py","evolvmem/legacy_compat.py"]),
])


def build():
    enhancements.enhance(CHARTS, chart, node, edge)
    charts=[]
    for c in CHARTS:
        ids={n['id'] for n in c.nodes}
        assert len(ids)==len(c.nodes)
        assert all(e['source'] in ids and e['target'] in ids for e in c.edges)
        assert c.routes and all(all(i in ids for i in r['ids']) for r in c.routes)
        charts.append(dict(id=c.key,title=c.title,kind=c.kind,question=c.question,width=c.width,height=c.height,nodes=c.nodes,edges=c.edges,routes=c.routes,sources=c.sources,group=c.group,reading=c.reading,svg=enhancements.render(c, viewer)))
    snapshot=json.loads((OUT/'current-system-evidence.json').read_text())
    snapshot['source_files']=FILES
    snapshot['commit']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    template=(OUT/'current_atlas_viewer.html').read_text()
    html=template.replace('__DATA__',json.dumps(charts,ensure_ascii=False).replace('</','<\\/')).replace('__AUDIT__',json.dumps(snapshot,ensure_ascii=False).replace('</','<\\/'))
    (OUT/'evolvmem-current-system.html').write_text(html)
    (OUT/'evolvmem-current-system.json').write_text(json.dumps({'charts':charts,'audit':snapshot},ensure_ascii=False,indent=2))
    print(json.dumps({'charts':len(charts),'nodes':[len(c.nodes) for c in CHARTS],'sources':len(FILES),'artifact':str(OUT/'evolvmem-current-system.html')},ensure_ascii=False))


if __name__=='__main__':
    build()

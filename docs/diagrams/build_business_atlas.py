"""Build the local, offline EvolvMem design atlas. No application data is read."""
from pathlib import Path
import html
import json

OUT = Path(__file__).resolve().parent
CHARTS = []


def esc(value):
    return html.escape(str(value), quote=True)


class Chart:
    def __init__(self, key, title, kind, question):
        self.key, self.title, self.kind, self.question = key, title, kind, question
        self.nodes, self.edges, self.background, self.routes = [], [], [], []
        self.width, self.height = 1240, 740

    def node(self, key, label, x, y, w=230, h=76, shape="action", tone="normal", detail="", inputs="", outputs=""):
        self.nodes.append(dict(id=key, label=label, x=x, y=y, w=w, h=h, shape=shape,
                               tone=tone, detail=detail, inputs=inputs, outputs=outputs))
        return self

    def edge(self, source, target, points, label="", lx=None, ly=None, kind="arrow"):
        self.edges.append(dict(id=f"e{len(self.edges)}", source=source, target=target,
                               points=points, label=label, lx=lx, ly=ly, kind=kind))
        return self

    def route(self, label, *ids):
        self.routes.append(dict(label=label, ids=list(ids)))
        return self

    def finish(self):
        CHARTS.append(self)
        return self


# UML use cases: associations have no directional arrowheads.
c = Chart("usecases", "我能用 EvolvMem 做什么", "用例图", "从用户目标看系统边界；连线表示参与，不表示先后。")
c.background.append('<rect class="boundary" x="280" y="55" width="680" height="630" rx="12"/><text class="section" x="310" y="92">EvolvMem</text>')
c.node("user", "你", 45, 290, 120, 125, "actor", detail="本轮只服务你本人，暂不包含多人共享。")
c.node("client", "AI 客户端", 1060, 290, 140, 125, "actor", detail="Codex、Claude Code、MiMo Code、Hermes、DeepSeek Harness。接入与续接是目标范围，实际能力仍需逐个核对。")
for key, text, y, detail in [
    ("capture", "自动积累会话", 145, "新增持续采集；五客户端尚未入库的历史分批补齐。"),
    ("paste", "手动补充资料", 240, "只需粘贴文字；不把其他文件格式列为本轮必需功能。"),
    ("recall", "找回相关知识", 335, "当前项目与通用记忆优先，必要时跨项目借鉴并核对适用条件。"),
    ("resume", "跨客户端继续任务", 430, "取回已确认需求、已完成、未完成和下一步。"),
    ("review", "审核、纠正与追溯", 525, "在管理页面核对出处、处理疑点并纠正资料。原文与旧版长期保留。"),
    ("evolve", "复用并改进开发经验", 620, "避免重复踩坑，依据实际成败修正经验，提炼待独立验证的新方法。"),
]:
    c.node(key, text, 435, y-32, 370, 64, "usecase", detail=detail)
for target, y in [("paste",240),("recall",335),("resume",430),("review",525)]:
    c.edge("user", target, [(165,350),(435,y)], kind="association")
for target, y in [("capture",145),("recall",335),("resume",430),("evolve",620)]:
    c.edge("client", target, [(1060,350),(805,y)], kind="association")
c.route("逐项查看目标", "capture", "paste", "recall", "resume", "review", "evolve").finish()

# Activity partitions are true responsibility lanes, not clusters of a flowchart.
c = Chart("intake", "资料怎样入库，何时需要我审核", "泳道活动图", "从上向下推进：明确资料自动处理，只有疑点进入人工审核。")
for x, w, title in [(30,340,"AI 客户端"),(370,440,"EvolvMem"),(810,400,"你 · 管理页面")]:
    c.background.append(f'<rect class="lane" x="{x}" y="30" width="{w}" height="685"/><rect class="lane-head" x="{x}" y="30" width="{w}" height="60"/><text class="lane-title" x="{x+w/2}" y="69">{title}</text>')
c.node("start", "开始",190,110,20,20,"start")
c.node("send","提交新增或历史会话",70,165,260,72, detail="包括开发、调研和日常讨论；重复与来源校验属于采集环节。")
c.node("paste","粘贴补充资料",900,165,230,72)
c.node("merge","",410,190,22,22,"merge",detail="任一资料入口均可触发处理，无需两个入口同时提交。")
c.node("save","保存原文与来源",465,165,250,72, detail="保留原始资料；处理出的知识单独保存，并能追溯原文。")
c.node("organize","整理内容、分别归属",465,300,250,72, detail="同一会话可以产生多条项目记忆和通用记忆；归属不明确不硬猜。")
c.node("eligible","可直接参考？",495,410,190,94,"decision", detail="来源清楚、归属明确且无冲突。可参考不等于某个方法已验证有效。")
c.node("review","核对来源并审核纠正",880,415,270,80,tone="review")
c.node("resolved","疑点已解决？",920,530,190,94,"decision",tone="review")
c.node("publish","保存为可参考资料",465,580,250,72,tone="good")
c.node("pending","保留待审",925,665,180,44,tone="review")
c.node("end","本次入库完成",580,685,22,22,"end")
c.edge("start","send",[(200,130),(200,165)])
c.edge("send","merge",[(330,201),(410,201)],"会话",370,180)
c.edge("paste","merge",[(900,201),(855,201),(855,130),(421,130),(421,190)],"补充内容",730,111)
c.edge("merge","save",[(432,201),(465,201)])
c.edge("save","organize",[(590,237),(590,300)])
c.edge("organize","eligible",[(590,372),(590,410)])
c.edge("eligible","publish",[(590,504),(590,580)],"[是]",623,548)
c.edge("eligible","review",[(685,457),(880,457)],"[有疑点]",780,436)
c.edge("review","resolved",[(1015,495),(1015,530)])
c.edge("resolved","publish",[(920,577),(810,577),(810,616),(715,616)],"[是]",813,554)
c.edge("resolved","pending",[(1015,624),(1015,665)],"[否]",1060,657)
c.edge("publish","end",[(590,652),(590,685)])
c.route("明确资料自动入库","start","send","merge","save","organize","eligible","publish","end")
c.route("疑点经人工解决","send","merge","save","organize","eligible","review","resolved","publish","end")
c.route("疑点暂未解决","send","merge","save","organize","eligible","review","resolved","pending").finish()

c = Chart("trial", "新方法怎样获得验证机会", "活动图", "这张图只画一次试用；是否成为可靠方法，见方法状态图。")
c.node("start","开始",610,35,20,20,"start")
c.node("candidate","从已有案例提炼新方法|标明待验证及来源",455,105,330,90,tone="candidate")
c.node("gate","条件相符且|任务已有授权？",500,250,240,115,"decision",detail="先核对适用条件和任务授权。候选方法与来源存疑的待审资料分开管理。")
c.node("hold","本次暂不采用",950,270,240,76,tone="review")
c.node("end2","本次不试用",1060,415,22,22,"end")
c.node("try","AI 在当前任务中试用",455,425,330,76)
c.node("evidence","记录实际采用、结果与证据",455,565,330,80,detail="成功、失败、不适用、结果未知分别记录；助手自称完成不能充当验证证据。")
c.node("end","进入结果核验",610,692,22,22,"end")
c.edge("start","candidate",[(620,55),(620,105)])
c.edge("candidate","gate",[(620,195),(620,250)])
c.edge("gate","try",[(620,365),(620,425)],"[是]",660,400)
c.edge("gate","hold",[(740,307),(950,307)],"[否]",841,288)
c.edge("hold","end2",[(1070,346),(1070,415)])
c.edge("try","evidence",[(620,501),(620,565)])
c.edge("evidence","end",[(620,645),(620,692)])
c.route("有条件试用","start","candidate","gate","try","evidence","end")
c.route("不满足试用条件","candidate","gate","hold","end2").finish()

# DFDs: rectangles=external entities, circles=processes, parallel lines=stores.
c = Chart("data-intake", "会话怎样变成有出处的知识", "数据流图 · 入库", "箭头写明传递的数据；外部参与者、处理过程和资料存储分别绘制。")
c.node("source","客户端会话|手动补充",35,220,215,90,"external")
c.node("collect","采集|保存",310,200,140,140,"process")
c.node("archive","原始记录与来源",270,500,225,74,"store",detail="原文长期保留，只有用户明确清理时删除；不是把每句会话都当作可用知识。")
c.node("organize","整理归属|判定入库",595,200,140,140,"process")
c.node("rules","整理与入库规则",550,55,230,74,"store")
c.node("knowledge","知识条目、版本|来源关联与状态",950,220,250,96,"store",tone="good")
c.node("user","你 · 管理页面",575,510,260,80,"external")
c.node("apply","应用审核|及纠正",1000,495,140,140,"process",tone="review")
c.edge("source","collect",[(250,265),(310,265)],"资料",278,244)
c.edge("collect","archive",[(380,340),(380,500)],"原文及来源",300,426)
c.edge("archive","organize",[(495,520),(530,520),(530,270),(595,270)],"待整理原文",600,400)
c.edge("rules","organize",[(665,129),(665,200)],"整理规则",748,168)
c.edge("organize","knowledge",[(735,270),(950,270)],"条目、归属与状态",842,247)
c.edge("knowledge","user",[(1010,316),(1010,420),(705,420),(705,510)],"待审资料及出处",833,398)
c.edge("archive","user",[(495,550),(575,550)],"原文",535,580)
c.edge("user","apply",[(835,550),(1000,550)],"审核与纠正",915,529)
c.edge("apply","knowledge",[(1100,500),(1140,500),(1140,316)],"新版本与状态",1101,440)
c.route("从会话形成知识","source","collect","archive","organize","knowledge")
c.route("审核资料并纠正","knowledge","user","apply","knowledge")
c.route("核对来源原文","archive","user","apply","knowledge").finish()

c = Chart("data-recall", "知识怎样进入当前任务", "数据流图 · 召回", "先保证资料与索引可用，再查找候选、比较相关性并返回出处。")
c.node("knowledge","当前可用知识|案例、条件与来源",45,215,255,96,"store",tone="good",detail="按资料状态、项目范围和案例适用条件确定可用内容。知识入库正确是检索的前提。")
c.node("sync","维护|检索索引",365,195,150,150,"process",detail="把可检索内容及版本变化同步到索引。尚未同步、失败或降级的显示和验收标准继续细化。")
c.node("index","关键词索引|语义索引",600,215,240,96,"store",detail="索引用来找候选，不替代原始资料与当前有效状态。")
c.node("question","当前问题、项目|需要哪类记忆",920,35,270,86,"external")
c.node("retrieve","按范围和|资格召回",980,195,150,150,"process",detail="优先当前项目和通用记忆，必要时查其他项目。历史与问答入口 knowledge_recall；结构化案例入口 experience_recall。跨项目与候选试用的完整调用链待核对。",inputs="问题、项目、知识类型、当前已知条件",outputs="相关候选、来源、匹配信息")
c.node("rank","比较相关性|重新排序",980,490,150,150,"process",detail="重排比较问题与候选内容。不能用调整一个权重替代资料质量、索引覆盖与整条召回链路核对。")
c.node("result","AI 客户端收到|内容、状态、条件与出处",395,515,330,96,"external",detail="客户端仍需比较当前任务条件；必要时按引用读取原文。任务断点另按精确任务标识读取。")
c.edge("knowledge","sync",[(300,263),(365,263)],"变更",332,241)
c.edge("sync","index",[(515,263),(600,263)],"索引条目",559,240)
c.edge("index","retrieve",[(840,263),(980,263)],"索引内容",910,241)
c.edge("question","retrieve",[(1055,121),(1055,195)],"问题及范围",1133,165)
c.edge("retrieve","rank",[(1055,345),(1055,490)],"相关候选",1120,420)
c.edge("knowledge","rank",[(175,311),(175,675),(1055,675),(1055,640)],"当前内容与有效状态",600,703)
c.edge("rank","result",[(980,565),(725,565)],"排序结果与出处",848,542)
c.route("新资料进入检索","knowledge","sync","index","retrieve","rank","result")
c.route("一次问题召回","question","retrieve","rank","result").finish()

c = Chart("knowledge-state", "一条知识何时可用，何时暂停", "状态图 · 普通知识", "只追踪一条知识的可用状态；原文和旧版本始终独立保留。")
c.node("initial","开始判断",610,32,20,20,"start")
c.node("choose","",595,100,50,50,"decision")
c.node("review","待审",185,240,290,96,"state",tone="review",detail="来源存疑、归属不明或同条件内容冲突时等待审核；保留疑点和来源。")
c.node("ready","可供参考",820,240,290,96,"state",tone="good",detail="来源和范围明确且无冲突，可作为任务参考；不代表开发方法已验证成功。")
c.node("paused","暂停引用并复核",820,525,290,96,"state",tone="review",detail="争议部分不再作为确定结论；错误资料影响到的相关内容暂缓推荐并重新核对。")
c.edge("initial","choose",[(620,52),(620,100)])
c.edge("choose","review",[(595,125),(330,125),(330,240)],"有疑点",444,103)
c.edge("choose","ready",[(645,125),(965,125),(965,240)],"满足可参考条件",805,103)
c.edge("review","ready",[(475,288),(820,288)],"审核解决疑点",646,264)
c.edge("ready","paused",[(965,336),(965,525)],"冲突未解／受纠正影响",819,434)
c.edge("paused","ready",[(1110,573),(1180,573),(1180,288),(1110,288)],"复核通过",1137,458)
c.edge("paused","review",[(820,573),(330,573),(330,336)],"仍有疑点，交用户审核",561,550)
c.route("明确内容直接可参考","initial","choose","ready")
c.route("冲突与人工审核","ready","paused","review","ready")
c.route("自动复核恢复","ready","paused","ready").finish()

c = Chart("method-state", "新方法怎样成为可靠经验", "状态图 · 方法", "待验证与待审分开；具体证据核验与适用范围判定仍需细化。")
c.node("initial","生成方法",330,50,20,20,"start")
c.node("candidate","待验证方法",195,230,290,96,"state",tone="candidate",detail="保留来源案例、条件和例外。允许有条件试用，但不继承来源案例的成功计数。")
c.node("usable","证据支持范围内|可复用的方法",820,230,290,96,"state",tone="good",detail="结果必须与该方法、任务和适用条件相关。何种证据足够、何时扩展范围仍待逐项确认，不设未经确认的固定成功次数。")
c.node("paused","暂停推荐与复核",820,530,290,96,"state",tone="review",detail="当确定受错误知识影响时暂缓推荐，核对通过后恢复，仍有疑点交管理页面审核。")
c.edge("initial","candidate",[(340,70),(340,230)])
c.edge("candidate","usable",[(485,278),(820,278)],"相关独立证据支持",650,252)
c.edge("candidate","candidate",[(340,326),(340,570),(100,570),(100,278),(195,278)],"试用后仍不能确认有效",361,440)
c.edge("usable","paused",[(965,326),(965,530)],"确认受错误来源影响",818,428)
c.edge("paused","usable",[(1110,578),(1180,578),(1180,278),(1110,278)],"复核通过",1138,439)
c.route("在条件内得到验证","initial","candidate","usable")
c.route("试用结果不足","candidate","candidate")
c.route("纠错后重新核对","usable","paused","usable").finish()

c = Chart("continuity", "换个客户端，怎样接着同一任务做", "时序图", "从上向下是时间；此图展示原任务可以明确定位时的续接。")
participants=[("user","你",120),("old","原客户端",425),("memory","EvolvMem",735),("new","新客户端",1080)]
for key,label,x in participants:
    c.node(key,label,x-85,40,170,64,"participant",detail="原任务无法唯一确定时，按已确认的续接规则先让用户选择，不猜测任务。" if key=="memory" else "")
    c.background.append(f'<path class="lifeline" d="M{x} 104V700"/>')
messages=[
 ("user","old",180,"提出任务与确认要求","arrow"),
 ("old","memory",265,"保存需求、进度、下一步","arrow"),
 ("memory","old",325,"返回已保存的任务引用","return"),
 ("user","new",415,"切换客户端，继续原任务","arrow"),
 ("new","memory",500,"定位项目与原任务","arrow"),
 ("memory","new",575,"返回断点、版本及状态","return"),
 ("new","user",660,"核对当前状态并继续任务","return"),
]
xs={key:x for key,_,x in participants}
for s,t,y,label,kind in messages:
    c.edge(s,t,[(xs[s],y),(xs[t],y)],label,(xs[s]+xs[t])/2,y-18,kind)
c.route("跨客户端续接","user","old","memory","new").finish()
c.routes[0]['edge_ids'] = [e['id'] for e in c.edges]


def text_svg(label, x, y, cls="node-label", line_height=27):
    lines=label.split("|")
    start=y-(len(lines)-1)*line_height/2
    return f'<text class="{cls}" text-anchor="middle">'+''.join(f'<tspan x="{x}" y="{start+i*line_height}">{esc(t)}</tspan>' for i,t in enumerate(lines))+'</text>'


def render_node(n):
    x,y,w,h=n['x'],n['y'],n['w'],n['h']; cx=x+w/2;cy=y+h/2
    label=n['label']; shape=n['shape']; body=''
    if shape=='actor':
        body=f'<circle class="actor-line" cx="{cx}" cy="{y+18}" r="15"/><path class="actor-line" d="M{cx} {y+33}V{y+77}M{x+15} {y+51}H{x+w-15}M{cx} {y+77}L{x+27} {y+104}M{cx} {y+77}L{x+w-27} {y+104}"/>'+text_svg(label,cx,y+134)
    elif shape=='start':
        body=f'<circle class="start" cx="{cx}" cy="{cy}" r="{w/2}"/>'
    elif shape=='end':
        body=f'<circle class="end-outer" cx="{cx}" cy="{cy}" r="{w/2}"/><circle class="start" cx="{cx}" cy="{cy}" r="{w/2-5}"/>'
    else:
        if shape in ('decision','merge'):
            body=f'<path class="shape" d="M{cx} {y}L{x+w} {cy}L{cx} {y+h}L{x} {cy}Z"/>'
        elif shape in ('usecase','process'):
            body=f'<ellipse class="shape" cx="{cx}" cy="{cy}" rx="{w/2}" ry="{h/2}"/>'
        elif shape=='store':
            body=f'<rect class="store-bg" x="{x}" y="{y}" width="{w}" height="{h}"/><path class="store-line" d="M{x} {y}H{x+w}M{x} {y+h}H{x+w}"/>'
        else:
            radius=18 if shape in ('action','state') else 0
            body=f'<rect class="shape" x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}"/>'
        if label:
            body+=text_svg(label,cx,cy+7,'node-label'+(' decision-label' if shape=='decision' else ''))
    return f'<g class="node {n["tone"]}" data-node="{n["id"]}" role="button" tabindex="0" aria-label="{esc(label.replace("|","，") or "分支判断")}"><title>{esc(label.replace("|"," "))}</title>{body}</g>'


SVG_STYLE='''
text{font-family:"Noto Sans CJK SC","Microsoft YaHei",sans-serif;fill:var(--ink)}
.node-label{font-size:22px;font-weight:500}.decision-label{font-size:19px}
.shape,.store-bg{fill:var(--surface);stroke:var(--node-stroke);stroke-width:1.8}
.store-bg{stroke:none}.store-line{fill:none;stroke:var(--node-stroke);stroke-width:2.5}
.good .shape,.good .store-bg{fill:var(--good-bg)}.candidate .shape{fill:var(--candidate-bg)}.review .shape{fill:var(--review-bg)}
.actor-line{fill:none;stroke:var(--ink);stroke-width:2.6;stroke-linecap:round}
.start{fill:var(--ink)}.end-outer{fill:var(--surface);stroke:var(--ink);stroke-width:2}
.boundary{fill:none;stroke:var(--node-stroke);stroke-width:1.5}.section{font-size:22px;font-weight:600}
.lane{fill:var(--surface);stroke:var(--border);stroke-width:1.3}.lane-head{fill:var(--lane)}.lane-title{font-size:22px;font-weight:600;text-anchor:middle}
.edge{fill:none;stroke:var(--edge);stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.edge.return{stroke-dasharray:7 5}.lifeline{fill:none;stroke:var(--node-stroke);stroke-dasharray:7 6;stroke-width:1.2}
.edge-label{font-size:18px;paint-order:stroke;stroke:var(--surface);stroke-width:7px;stroke-linejoin:round}
.node{cursor:pointer;outline:none}.node:hover .shape,.node:focus .shape,.node.selected .shape,.node.current .shape{stroke:var(--accent);stroke-width:3.5}
.node.current .store-line,.node.selected .store-line{stroke:var(--accent);stroke-width:4}
.node.current .actor-line{stroke:var(--accent);stroke-width:4}.node.current .node-label{fill:var(--accent);font-weight:700}
.node.current .start{fill:var(--accent)}
.edge.current{stroke:var(--accent);stroke-width:3.5;stroke-dasharray:12 7;animation:trace 1.2s linear infinite}
@keyframes trace{to{stroke-dashoffset:-38}}@media(prefers-reduced-motion:reduce){.edge.current{animation:none}}
'''


def render_chart(c):
    prefix=c.key
    out=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {c.width} {c.height}" width="{c.width}" height="{c.height}" role="img" aria-labelledby="{prefix}-title"><title id="{prefix}-title">{esc(c.kind+"："+c.title)}</title><style>{SVG_STYLE}</style>',
         f'<defs><marker id="{prefix}-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse"><path d="M0 0L10 5L0 10Z" fill="var(--edge)"/></marker><marker id="{prefix}-open" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto"><path d="M1 1L9 5L1 9" fill="none" stroke="var(--edge)" stroke-width="1.4"/></marker></defs>']
    out+=c.background
    for e in c.edges:
        path='M'+'L'.join(f'{x} {y}' for x,y in e['points'])
        marker='' if e['kind']=='association' else f' marker-end="url(#{prefix}-'+('open' if e['kind']=='return' else 'arrow')+')"'
        out.append(f'<path class="edge {e["kind"]}" data-edge="{e["id"]}" data-source="{e["source"]}" data-target="{e["target"]}" d="{path}"{marker}/>')
    out += [render_node(n) for n in c.nodes]
    for e in c.edges:
        if e['label']:
            out.append(text_svg(e['label'],e['lx'],e['ly'],'edge-label',24))
    out.append('</svg>')
    return ''.join(out)


HTML='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EvolvMem · 业务设计图册</title><style>
:root{color-scheme:light;--bg:#f3f5f7;--surface:#fff;--ink:#172d39;--muted:#4b6070;--border:#d8e0e5;--node-stroke:#8ca2af;--edge:#637f8e;--accent:#087c7d;--lane:#eaf1f4;--good-bg:#e7f5ec;--candidate-bg:#eeeaff;--review-bg:#fff2dc}
:root.dark{color-scheme:dark;--bg:#101a22;--surface:#182630;--ink:#e9f1f6;--muted:#b5c9d6;--border:#354853;--node-stroke:#86a2b4;--edge:#a4becd;--accent:#6de0cf;--lane:#243745;--good-bg:#183c35;--candidate-bg:#342d50;--review-bg:#463923}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 "Noto Sans CJK SC","Microsoft YaHei",sans-serif}button,select{font:inherit;color:inherit;cursor:pointer;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 13px;min-height:42px}button:hover,select:hover{border-color:var(--accent)}button:focus-visible,select:focus-visible{outline:3px solid var(--accent);outline-offset:2px}button:disabled{opacity:.45;cursor:default}
header{height:78px;padding:15px 26px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;background:var(--surface);gap:18px}h1{font-size:24px;line-height:1.2;margin:0}.header-left{display:flex;align-items:center;gap:20px;min-width:0}#theme{flex-shrink:0;white-space:nowrap}.badge{font-size:16px;border:1px solid var(--border);border-radius:25px;padding:5px 14px;color:var(--accent)}.layout{display:grid;grid-template-columns:258px minmax(0,1fr);height:calc(100vh - 78px)}nav{padding:22px 14px;border-right:1px solid var(--border);overflow:auto}nav button{width:100%;display:block;text-align:left;padding:12px 15px;margin-bottom:9px;background:transparent;border-color:transparent}nav .nav-kind{display:block;color:var(--muted);font-size:16px}nav .nav-name{display:block;font-weight:600;font-size:17px;margin-top:3px}nav button[aria-selected=true]{border-color:var(--accent);background:var(--surface);color:var(--accent)}main{min-width:0;display:flex;flex-direction:column;overflow:hidden}.chart-heading{padding:19px 24px 12px;background:var(--surface)}h2{font-size:25px;line-height:1.4;margin:0 0 6px}#question{margin:0;font-size:16px;color:var(--muted)}.toolbar{display:flex;align-items:center;flex-wrap:wrap;gap:10px;padding:12px 24px;background:var(--surface);border-bottom:1px solid var(--border)}.tools{display:flex;align-items:center;gap:7px;flex-wrap:wrap;max-width:100%}.tools.right{margin-left:auto}#play{color:var(--accent);min-width:94px}#status{font-size:16px;color:var(--muted);min-width:65px;text-align:center}#viewport{flex:1;min-height:300px;overflow:auto;padding:20px;scrollbar-gutter:stable;background-image:radial-gradient(var(--border) .7px,transparent .7px);background-size:20px 20px}#canvas{background:var(--surface);box-shadow:0 5px 25px #10293809;border:1px solid var(--border);border-radius:10px;width:max-content}#canvas svg{display:block}#details{background:var(--surface);border-top:1px solid var(--border);padding:17px 24px;max-height:260px;overflow:auto}#details[hidden]{display:none}.detail-head{display:flex;align-items:center;justify-content:space-between;gap:20px}h3{font-size:20px;margin:0}#detail-body{margin:7px 0 0;line-height:1.75;max-width:1000px}#detail-io{display:flex;gap:30px;flex-wrap:wrap;margin-top:8px}#detail-io p{margin:0}#toast{position:fixed;right:22px;bottom:24px;background:var(--ink);color:var(--surface);padding:10px 18px;border-radius:8px;z-index:9}#toast:empty{display:none}
@media(max-width:960px){.layout{grid-template-columns:1fr;grid-template-rows:auto minmax(0,1fr)}nav{display:flex;padding:8px 12px;border-right:0;border-bottom:1px solid var(--border);gap:8px;overflow-x:auto}nav button{width:210px;flex:0 0 210px;margin:0;padding:8px 12px}.chart-heading{padding:14px 16px 8px}.toolbar{padding:10px 16px}.header-left{gap:12px}h1{font-size:21px}.badge{display:none}header{padding:14px 16px}#viewport{padding:12px}#details{padding:12px 16px;max-height:220px}h2{font-size:22px}}
@media(max-width:600px){.tools.right{margin-left:0}.toolbar{gap:7px}button,select{padding:7px 10px}.nav-kind{font-size:16px}.toolbar>.tools:first-child{width:100%}#routes{flex-basis:100%;max-width:none}header{height:72px}.layout{height:calc(100dvh - 72px)}#question{line-height:1.5}#viewport{min-height:260px}}
</style></head><body>
<header><div class="header-left"><h1>EvolvMem · 业务设计图册</h1><span class="badge">目标设计 · 待审阅</span></div><button id="theme">深色模式</button></header>
<div class="layout"><nav id="nav" aria-label="选择图纸"></nav><main><section class="chart-heading"><h2 id="title"></h2><p id="question"></p></section><div class="toolbar"><div class="tools"><select id="routes" aria-label="选择演示路径"></select><button id="prev" aria-label="上一步">上一步</button><button id="play">播放过程</button><button id="next" aria-label="下一步">下一步</button></div><div class="tools right"><button id="minus" aria-label="缩小">−</button><span id="status">100%</span><button id="plus" aria-label="放大">＋</button><button id="fit">适合窗口</button><button id="export">下载 SVG</button></div></div><div id="viewport"><div id="canvas"></div></div><section id="details" hidden><div class="detail-head"><h3 id="detail-title"></h3><button id="close">收起详情</button></div><p id="detail-body"></p><div id="detail-io"></div></section></main></div><div id="toast" role="status"></div>
<script id="atlas-data" type="application/json">__DATA__</script><script>
const charts=JSON.parse(document.getElementById('atlas-data').textContent);const $=id=>document.getElementById(id);let active=null,timer=null,states={},toastTimer;const reduced=matchMedia('(prefers-reduced-motion: reduce)');
function cache(){try{localStorage.setItem('ev-business-atlas',JSON.stringify({active:active?.id,dark:document.documentElement.classList.contains('dark'),states}));}catch{}}
let remembered={};try{remembered=JSON.parse(localStorage.getItem('ev-business-atlas')||'{}');states=remembered.states||{};}catch{}
function notify(t){$('toast').textContent=t;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').textContent='',2500)}
function setTheme(dark){document.documentElement.classList.toggle('dark',dark);$('theme').textContent=dark?'浅色模式':'深色模式';cache()}
function pause(){clearInterval(timer);timer=null;$('play').textContent='播放过程'}
function state(){return states[active.id]||(states[active.id]={zoom:null,step:-1,route:0,x:0,y:0,selected:null})}
function saveView(){if(!active)return;const s=state();s.x=$('viewport').scrollLeft;s.y=$('viewport').scrollTop;cache()}
function scale(value){const s=state();s.zoom=Math.max(.5,Math.min(2.2,value));const svg=$('canvas').querySelector('svg');svg.style.width=(active.width*s.zoom)+'px';svg.style.height=(active.height*s.zoom)+'px';$('status').textContent=Math.round(s.zoom*100)+'%';cache()}
function fit(){scale(Math.max(.85,Math.min(1.2,($('viewport').clientWidth-42)/active.width)));$('viewport').scrollTo(0,0)}
function detail(id){const n=active.nodes.find(n=>n.id===id);if(!n)return;state().selected=id;$('detail-title').textContent=n.label.replaceAll('|',' · ')||(n.shape==='merge'?'合并资料入口':'分支判断');$('detail-body').textContent=n.detail||'本节点按图中连线接收输入，并将处理结果交给后续环节。';$('detail-io').replaceChildren();for(const [k,t] of [['inputs','输入'],['outputs','输出']])if(n[k]){const p=document.createElement('p');p.textContent=t+'：'+n[k];$('detail-io').append(p)}$('details').hidden=false;document.querySelectorAll('.node').forEach(g=>g.classList.toggle('selected',g.dataset.node===id));cache()}
function routeLength(){const r=active.routes[state().route];return (r.edge_ids||r.ids).length}
function markStep(){const s=state(),route=active.routes[s.route];document.querySelectorAll('.node,.edge').forEach(n=>n.classList.remove('current'));if(s.step<0)return;let id;if(route.edge_ids){const e=active.edges.find(e=>e.id===route.edge_ids[s.step]);id=e.target;document.querySelector('[data-edge="'+e.id+'"]').classList.add('current');detail(id);$('detail-title').textContent=(s.step+1)+' / '+routeLength()+' · '+e.label;$('detail-body').textContent=active.nodes.find(n=>n.id===e.source).label+' → '+active.nodes.find(n=>n.id===e.target).label;}else{id=route.ids[s.step];detail(id);if(s.step>0){const prev=route.ids[s.step-1];document.querySelectorAll('.edge').forEach(e=>e.classList.toggle('current',e.dataset.source===prev&&e.dataset.target===id))}}document.querySelectorAll('.node').forEach(n=>n.classList.toggle('current',n.dataset.node===id));$('next').disabled=s.step>=routeLength()-1;$('prev').disabled=s.step<=0;requestAnimationFrame(()=>document.querySelector('.node.current')?.scrollIntoView({block:'nearest',inline:'nearest',behavior:'auto'}))}
function step(delta){const s=state(),last=routeLength()-1;s.step=Math.max(0,Math.min(last,s.step+delta));markStep();cache();if(s.step>=last)pause()}
function render(id){saveView();pause();active=charts.find(c=>c.id===id)||charts[0];const s=state();s.route=Math.min(s.route||0,active.routes.length-1);$('title').textContent=active.kind+' · '+active.title;$('question').textContent=active.question;$('canvas').innerHTML=active.svg;$('routes').replaceChildren();active.routes.forEach((r,i)=>{const o=document.createElement('option');o.value=i;o.textContent=r.label;$('routes').append(o)});$('routes').value=s.route;document.querySelectorAll('#nav button').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.chart===active.id)));$('details').hidden=true;$('next').disabled=false;$('prev').disabled=true;if(s.zoom)scale(s.zoom);else fit();$('viewport').scrollTo(s.x||0,s.y||0);if(s.step>=0)markStep();else if(s.selected)detail(s.selected);try{history.replaceState(null,'','#'+active.id)}catch{}cache()}
charts.forEach((c,i)=>{const b=document.createElement('button');b.dataset.chart=c.id;b.setAttribute('role','tab');const a=document.createElement('span'),n=document.createElement('span');a.className='nav-kind';a.textContent=String(i+1).padStart(2,'0')+' · '+c.kind;n.className='nav-name';n.textContent=c.title;b.append(a,n);b.onclick=()=>render(c.id);$('nav').append(b)});$('nav').setAttribute('role','tablist');
$('canvas').addEventListener('click',e=>{const n=e.target.closest('[data-node]');if(n){pause();detail(n.dataset.node)}});$('canvas').addEventListener('keydown',e=>{if((e.key==='Enter'||e.key===' ')&&e.target.dataset.node){e.preventDefault();pause();detail(e.target.dataset.node)}});
$('close').onclick=()=>{$('details').hidden=true;state().selected=null;document.querySelectorAll('.node').forEach(n=>n.classList.remove('selected'));cache()};$('theme').onclick=()=>setTheme(!document.documentElement.classList.contains('dark'));$('plus').onclick=()=>scale(state().zoom+.15);$('minus').onclick=()=>scale(state().zoom-.15);$('fit').onclick=fit;$('next').onclick=()=>{pause();step(1)};$('prev').onclick=()=>{pause();step(-1)};
$('routes').onchange=()=>{pause();const s=state();s.route=Number($('routes').value);s.step=-1;s.selected=null;$('details').hidden=true;document.querySelectorAll('.node,.edge').forEach(n=>n.classList.remove('current','selected'));$('next').disabled=false;$('prev').disabled=true;cache()};
$('play').onclick=()=>{if(timer){pause();return}if(reduced.matches){notify('已按减少动画设置，切换为逐步查看');step(1);return}const s=state();if(s.step>=routeLength()-1)s.step=-1;step(1);if(s.step<routeLength()-1){$('play').textContent='暂停播放';timer=setInterval(()=>step(1),1400)}};
$('export').onclick=()=>{const clone=$('canvas').querySelector('svg').cloneNode(true);clone.style.width='';clone.style.height='';const rootStyle=getComputedStyle(document.documentElement);for(const key of ['ink','surface','node-stroke','edge','accent','lane','border','good-bg','candidate-bg','review-bg'])clone.style.setProperty('--'+key,rootStyle.getPropertyValue('--'+key));clone.querySelectorAll('.current,.selected').forEach(n=>n.classList.remove('current','selected'));const bg=document.createElementNS('http://www.w3.org/2000/svg','rect');bg.setAttribute('width','100%');bg.setAttribute('height','100%');bg.setAttribute('fill',rootStyle.getPropertyValue('--surface'));clone.insertBefore(bg,clone.querySelector('defs').nextSibling);const blob=new Blob([new XMLSerializer().serializeToString(clone)],{type:'image/svg+xml;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='evolvmem-'+active.id+'.svg';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);notify('已导出当前图纸')};
setTheme(Boolean(remembered.dark));render(location.hash.slice(1)||remembered.active||'intake');window.addEventListener('hashchange',()=>{if(location.hash.slice(1)!==active.id)render(location.hash.slice(1))});window.addEventListener('beforeunload',saveView);window.__atlas={charts,getActive:()=>active.id,getState:()=>({...state()}),select:render};
</script></body></html>'''


def build():
    data=[]
    for c in CHARTS:
        ids={n['id'] for n in c.nodes}
        if len(ids)!=len(c.nodes):
            raise ValueError(f'duplicate node: {c.key}')
        for e in c.edges:
            assert e['source'] in ids and e['target'] in ids
        for route in c.routes:
            assert all(n in ids for n in route['ids'])
        data.append(dict(id=c.key,title=c.title,kind=c.kind,question=c.question,width=c.width,height=c.height,
                         nodes=c.nodes,edges=c.edges,routes=c.routes,svg=render_chart(c)))
    payload=json.dumps(data,ensure_ascii=False).replace('</','<\\/')
    (OUT/'evolvmem-business-atlas.html').write_text(HTML.replace('__DATA__',payload),encoding='utf-8')
    (OUT/'evolvmem-business-atlas.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'charts':len(data),'artifact':str(OUT/'evolvmem-business-atlas.html'),'nodes':[len(c.nodes) for c in CHARTS]},ensure_ascii=False))


if __name__=='__main__':
    build()

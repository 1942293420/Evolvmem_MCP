"""Server-rendered, owner-only read entry for the history candidate.

This is the everyday usable surface: after the existing Feishu web login, the
verified owner opens ``/history`` and reads project summaries, short histories
and original Q&A with images. No token pasting, no browser console and no
personal token in HTML, URLs, logs or shared JavaScript.

Fail-closed rules:

* the Web layer passes ``owner_verified=True`` only when the existing Feishu
  session exists and ``WebAuth.can_write`` (owner) is true;
* auth disabled / viewer session / missing session => 403 for HTML pages;
* attachments are served only through the same owner gate and are re-hashed
  from disk against the candidate database before sending;
* JSON ``/api/history-candidate/...`` keeps the owner gate and only falls back
  to the personal token for headless verification (never shown in the UI).
"""
from __future__ import annotations

import html
import json
import pathlib
import re
from urllib.parse import quote, unquote

from evolvmem.history_candidate_read import (
    HistoryCandidateError,
    format_beijing,
    open_reader,
)

PREFIX = "/history"
_MIME = {"image/png": "image/png", "image/jpeg": "image/jpeg", "image/gif": "image/gif",
         "image/webp": "image/webp", "application/pdf": "application/pdf"}

STYLE = """
:root{--bg:#f7f7f5;--fg:#1f2328;--muted:#5b636b;--line:#d9dde2;--card:#fff;--user:#eef4ff;--accent:#26547c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
header{background:var(--accent);color:#fff;padding:14px 22px}header a{color:#fff;text-decoration:none}
header h1{margin:0;font-size:19px}main{max-width:1040px;margin:0 auto;padding:22px 18px 70px}
a{color:#1a5d9e}h2{font-size:19px;margin:26px 0 10px}h3{font-size:16px;margin:18px 0 8px}
h4{font-size:14px;margin:12px 0 4px}.muted{color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 15px;margin:10px 0}
ul.tight{margin:4px 0;padding-left:20px}ul.list{list-style:none;padding:0}
ul.list li{padding:8px 0;border-bottom:1px solid var(--line)}
.msg{border:1px solid var(--line);border-radius:8px;padding:9px 12px;margin:8px 0;background:#fff}
.msg.user{background:var(--user)}.meta{display:flex;gap:10px;color:var(--muted);font-size:12px;margin-bottom:4px}
.text{white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere}
details.mid{margin:6px 0;border-left:3px solid var(--line);padding-left:8px}
details.mid summary{cursor:pointer;color:var(--muted);font-size:13px}
details.source{margin:12px 0;border:1px dashed var(--line);border-radius:6px;padding:8px 10px}
figure.att{margin:8px 0}figure.att img{max-width:100%;height:auto;border:1px solid var(--line);border-radius:6px}
figcaption{font-size:12px;color:var(--muted)}.chip{display:inline-block;padding:2px 8px;border:1px solid var(--line);
border-radius:999px;text-decoration:none;font-size:12px;margin:2px}.notice{color:var(--muted);font-style:italic}
.warn{background:#fff6e5;border-left:4px solid #d99a2b;padding:8px 12px;border-radius:4px}
nav.pager a{margin-right:8px}
"""


def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>{STYLE}</style></head><body>
<header><a href="/history"><h1>EvolvMem 历史知识库（只读版）</h1></a></header>
<main>{body}</main></body></html>"""


def human_bytes(value) -> str:
    value = float(value or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} GB'


def _ul(values) -> str:
    values = [v for v in (values or []) if str(v).strip()]
    if not values:
        return '<p class="notice">暂无记录。</p>'
    return '<ul class="tight">' + ''.join(f'<li>{html.escape(str(v))}</li>' for v in values) + '</ul>'


def _project_link(key: str) -> str:
    return f'/history/project/{html.escape(quote(str(key), safe=""))}'


def render_index(reader) -> str:
    projects = reader.projects()
    with_dialogue = [p for p in projects if p['dialogue_blocks']]
    rows = ''.join(
        f'<li><a href="{_project_link(p["project"])}">{html.escape(p["display_name"] or p["project"])}</a> '
        f'<span class="muted">{html.escape(p["project"])} · {p["dialogue_blocks"]} 个有问答块'
        f'{"" if not p["empty_sources"] else " · " + str(p["empty_sources"]) + " 个空源"} · '
        f'{p["messages"]} 条消息 / {p["turns"]} 轮</span>'
        f'{"" if not p["evidence_status"] else "<span class=chip>" + html.escape(p["evidence_status"]) + "</span>"}</li>'
        for p in sorted(projects, key=lambda x: (-x['dialogue_blocks'], -x['messages'], x['project'])))
    return _page('历史知识库', f"""
<p class="muted">历史整理只读版：原始问答、附件与来源关联保留；页面不写入正式库。</p>
<div class="card"><p>项目 {len(projects)} 个，其中有可读问答的项目 {len(with_dialogue)} 个；
默认只列有可见问答的会话块，空源与来源检查单独保留。</p>
<p><a href="/history/unassigned">未归属会话（有问答 / 空源） →</a></p>
<p class="muted">图片通过同一 owner 门禁的只读附件入口提供；页面不含个人令牌。</p></div>
<div class="card"><h3>项目摘要入口</h3><ul class="list">{rows}</ul></div>
""")


def render_project(reader, identifier: str) -> str:
    data = reader.project_overview(identifier)
    summary = data['summary']
    history = data['history']
    hist_rows = ''.join(
        f'<li><a href="/history/block/{html.escape(b["block_id"])}">{html.escape(b["title"])}</a>'
        f'<div class="muted">{html.escape(b["first_at_beijing"] or "时间缺失")} → '
        f'{html.escape(b["last_at_beijing"] or "未知")}（北京时间） · {b["messages"]} 条消息 · '
        f'{b["turns"]} 轮 · 结束时间未知</div></li>' for b in history)
    refs = summary.get('source_refs') or []
    ref_rows = ''.join(
        f'<li><span class="muted">{html.escape(str(r.get("kind") or ""))}</span> '
        f'{html.escape(str(r.get("title") or r.get("source") or r.get("result") or ""))}</li>'
        for r in refs if r.get('kind') != 'registry')
    conflicts = summary.get('source_conflict_refs') or []
    conflict_html = ''
    if conflicts:
        items = []
        for ref in conflicts:
            tags = []
            if ref.get('scope_conflict'):
                tags.append('平台范围冲突')
            if ref.get('attribution_conflict'):
                tags.append('归属冲突')
            label = ' / '.join(tags) or '来源说明'
            title = ref.get('title') or ref.get('source') or ref.get('result') or ''
            note = ref.get('note') or ''
            items.append(f'<li><strong>{html.escape(label)}</strong>：{html.escape(str(title))}'
                         + (f'——{html.escape(str(note))}' if note else '') + '</li>')
        conflict_html = ('<h4>平台/归属范围冲突（仅作证据参考，不合并、不改历史归属）</h4>'
                         '<ul class="tight">' + ''.join(items) + '</ul>')
    empty_note = (f'<p class="muted">另有 {data["empty_source_count"]} 个无可见问答的空源，'
                  f'仅保留在来源检查中。</p>' if data['empty_source_count'] else '')
    return _page(data['display_name'] or data['project'], f"""
<p><a href="/history">← 返回项目列表</a></p>
<h2>{html.escape(data['display_name'] or data['project'])} <span class="muted">({html.escape(data['project'])} · {html.escape(data['status'])})</span></h2>
<div class="card"><h3>项目摘要</h3>
<p>{html.escape(summary['purpose'])}</p>
<p class="muted">证据等级：{html.escape(data['evidence_status'] or '未标注')} · 来源依据 {len(refs)} 条 · 历史记录 {len(summary.get('historical_notes') or [])} 条</p>
<h4>环境与入口</h4>{_ul(summary['environment'])}
<h4>主要功能 / 业务范围</h4>{_ul(summary['main_features'])}
<h4>必要约定（不含真实密钥）</h4>{_ul(summary['necessary_info'])}
<h4>未核实 / 缺项</h4>{_ul(summary['unknowns'])}
<h4>历史记录（历史时点，不作为当前状态）</h4>{_ul(summary.get('historical_notes'))}
{conflict_html}
<details class="source"><summary>来源依据（{len([r for r in refs if r.get('kind') != 'registry'])} 条，可回查）</summary>
<ul class="tight">{ref_rows or '<li class="notice">仅登记元数据。</li>'}</ul></details>
</div>
<div class="card"><h3>简短历史（一行一条会话记录）</h3>
<ul class="list">{hist_rows or '<li class="notice">本项目当前没有可读的问答会话。</li>'}</ul>
{empty_note}</div>
<p class="muted">会话结束时间未知；项目关联不扩大原会话读取权限；问答原文按原始顺序保存。</p>
""")


def render_unassigned(reader) -> str:
    data = reader.unassigned()
    def rows(items):
        return ''.join(
            f'<li><a href="/history/block/{html.escape(b["block_id"])}">{html.escape(b["title"])}</a>'
            f'<div class="muted">{html.escape(b["block_id"])} · '
            f'{html.escape(b["first_at_beijing"] or "时间缺失")} → {html.escape(b["last_at_beijing"] or "未知")}'
            f'（北京时间） · {b["messages"]} 条消息</div></li>' for b in items)
    return _page('未归属会话', f"""
<p><a href="/history">← 返回项目列表</a></p><h2>未归属会话</h2>
<div class="card"><h3>有问答（{data['dialogue_count']}）</h3>
<ul class="list">{rows(data['dialogue']) or '<li class="notice">无。</li>'}</ul></div>
<div class="card"><h3>空源（{data['empty_count']}，无可见问答）</h3>
<p class="muted">只含元数据，保留在来源检查中；不进入日常历史。</p>
<ul class="list">{rows(data['empty']) or '<li class="notice">无。</li>'}</ul></div>
""")


def _render_message(message, asset_prefix='/history/attachment/') -> str:
    content = message.get('content') or ''
    if message.get('attachments'):
        content = re.sub(r'<image\b[^>]*>.*?</image\s*>|<image\b[^>]*/?>', '', content,
                         flags=re.I | re.S)
    body = html.escape(content)
    figures = []
    for item in message.get('attachments') or []:
        if item.get('status') != 'recovered' or not item.get('sha256'):
            continue
        mime = item.get('mime_type') or ''
        caption = human_bytes(item.get('bytes'))
        if item.get('width') and item.get('height'):
            caption += f" · {item['width']}×{item['height']}"
        url = asset_prefix + html.escape(item['sha256'])
        if mime.startswith('image/') and item.get('format_ok'):
            figures.append(f'<figure class="att"><img loading="lazy" src="{url}" alt="原始附图">'
                           f'<figcaption>原始附图 · {html.escape(caption)}</figcaption></figure>')
        else:
            figures.append(f'<p class="file">附件：<a href="{url}">下载</a> · {html.escape(caption)}</p>')
    role = '用户' if message.get('role') == 'user' else 'AI 助手'
    when = format_beijing(message.get('recorded_at')) or '时间缺失'
    return (f'<div class="msg {"user" if message.get("role") == "user" else "assistant"}">'
            f'<div class="meta"><span>{role}</span><span>{html.escape(when)}</span></div>'
            f'<div class="text">{body}</div>{"".join(figures)}</div>')


def _turns(messages):
    turns, current = [], None
    for message in messages:
        if message.get('role') == 'user':
            current = {'question': message, 'answers': []}
            turns.append(current)
        elif current is None:
            current = {'question': None, 'answers': []}
            turns.append(current)
            current['answers'].append(message)
        else:
            current['answers'].append(message)
    return turns


def render_block(reader, block_id: str, page: int = 1, page_size: int = 50) -> str:
    data = reader.block_turn_page(block_id, page, page_size)
    block = data['block']
    turns = data['turns']
    parts = []
    for turn in turns:
        if turn['question'] is not None:
            parts.append(_render_message(turn['question']))
        else:
            parts.append('<p class="notice">会话原始记录以助手内容开头（没有更早的用户问题）。</p>')
        answers = turn['answers']
        for index, answer in enumerate(answers):
            rendered = _render_message(answer)
            if index < len(answers) - 1:
                parts.append(f'<details class="mid"><summary>中间过程回复 {index + 1}/{len(answers)}</summary>{rendered}</details>')
            else:
                parts.append(rendered)
    pager = ''
    if data['total_pages'] > 1:
        pager = '<nav class="pager">' + ' '.join(
            f'<a href="/history/block/{quote(str(block["block_id"]), safe="")}?page={n}">第 {n} 页</a>'
            for n in range(1, data['total_pages'] + 1)) + '</nav>'
    continuation = ''
    if data['pagination'].get('continuation_note'):
        continuation = f'<p class="notice">{html.escape(data["pagination"]["continuation_note"])}</p>'
    source = (f'<details class="source"><summary>来源与校验说明</summary>'
              f'<p>会话块：{html.escape(block["block_id"])} · {html.escape(block["adapter"])}/{html.escape(block["kind"])}'
              f' · 原生会话：{html.escape(str(block["native_session_id"] or "未映射"))} · '
              f'{block["messages"]} 条消息 / {block["turns"]} 轮</p>'
              f'<p>覆盖：{html.escape(block["coverage"])} · 原生完整覆盖：否 · 结束时间未知</p>'
              f'<p>来源 archive id：{html.escape(str(block["source_archive_ids"]))}</p>'
              f'<p>原始观察时间（后端原值，UTC）：{html.escape(str(block["first_at_raw"] or "缺失"))} → '
              f'{html.escape(str(block["last_at_raw"] or "缺失"))}（页面显示为北京时间）</p></details>')
    return _page(block['title'], f"""
<p><a href="/history">← 返回项目列表</a> · <a href="/history/unassigned">未归属</a></p>
<h2>{html.escape(block['title'])}</h2>
<div class="card"><p class="muted">{block['messages']} 条消息 · {block['turns']} 轮 · 结束时间未知 · 北京时间 · 问答按完整轮分页</p></div>
{continuation}
{pager}
{''.join(parts) if parts else '<p class="notice">本来源没有可见问答。</p>'}
{pager}
{source}
<p class="muted">中间助手回复折叠但顺序保留；不可恢复图片不显示；AI“已修复/已通过”仅当时报告，未独立复测。</p>
""")


def handle(method: str, path: str, params: dict, *, owner_verified: bool,
           auth_enabled: bool = True, environ: dict | None = None):
    """Return ``(status, content_type, body)`` for the HTML/attachment entry."""
    method = str(method).upper()
    if method != 'GET':
        return 405, 'application/json', {'ok': False, 'error': 'history_candidate_method_not_allowed'}
    if path == PREFIX or path.startswith(PREFIX + '/'):
        if not owner_verified:
            return 403, 'text/html; charset=utf-8', _page('需要 owner 登录', (
                '<div class="warn"><p>此入口只对已通过飞书登录并通过当前身份规则核验的 owner 开放；'
                'viewer 或未登录会话不能读取原文。</p>'
                '<p><a href="/login">去登录</a></p></div>'))
    try:
        if path in (PREFIX + '/attachment',) or path.startswith(PREFIX + '/attachment/'):
            sha = unquote(path[len(PREFIX + '/attachment/'):]).strip('/')
            with open_reader(environ) as reader:
                item = reader.attachment(sha)
            return 200, _MIME.get(item['mime_type'], 'application/octet-stream'), item['path'].read_bytes()
        with open_reader(environ) as reader:
            if path.rstrip('/') == PREFIX:
                return 200, 'text/html; charset=utf-8', render_index(reader)
            if path.rstrip('/') == PREFIX + '/unassigned':
                return 200, 'text/html; charset=utf-8', render_unassigned(reader)
            if path.startswith(PREFIX + '/project/'):
                return 200, 'text/html; charset=utf-8', render_project(
                    reader, unquote(path[len(PREFIX + '/project/'):]).strip('/'))
            if path.startswith(PREFIX + '/block/'):
                page = params.get('page', 1)
                turn_page_size = params.get('turn_page_size', 50)
                return 200, 'text/html; charset=utf-8', render_block(
                    reader, unquote(path[len(PREFIX + '/block/'):]).strip('/'), page,
                    turn_page_size)
        return 404, 'text/html; charset=utf-8', _page('未找到', '<p class="notice">没有这个历史页面。</p>')
    except HistoryCandidateError as exc:
        status = 404 if exc.code in ('project_not_registered', 'block_not_found', 'attachment_not_found') else exc.status
        return status, 'text/html; charset=utf-8', _page('未找到', f'<p class="notice">{html.escape(exc.code)}</p>')
    except Exception:
        return 500, 'text/html; charset=utf-8', _page('错误', '<p class="notice">读取失败，请稍后再试。</p>')

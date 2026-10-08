"""Bounded diagnostics: stage, exception class and time, never exception text."""
import json
import re
from evolvmem.context_store import _now_iso

LABELS = {'model_request': '模型请求失败', 'model_parse': '模型响应解析或摘要校验失败',
          'persistence': '入库失败', 'related_lookup': '关联知识读取失败',
          'segmentation': '分段失败', 'assignment': '归属处理失败',
          'cleaning': '清洗失败', 'queued': '任务启动失败', 'extraction': '提炼失败', 'index': '索引同步失败'}


def tag(error, stage):
    error.evolvmem_stage = stage
    return error


def capture(error, stage):
    stage = getattr(error, 'evolvmem_stage', stage)
    if stage not in LABELS:
        stage = 'extraction'
    return json.dumps({'stage': stage, 'exception_type': re.sub(r'[^a-zA-Z0-9_]', '', type(error).__name__)[:64],
                       'occurred_at': _now_iso()}, ensure_ascii=False)


def describe(raw):
    try:
        data = json.loads(raw)
        return f"{LABELS.get(data['stage'], '处理失败')} · {data['exception_type']} · {data['occurred_at']} UTC"
    except (KeyError, ValueError, TypeError):
        return ''

"""Content-free failure categories, retry decisions and code locations."""
import json
import re
import traceback
from pathlib import Path
from urllib.error import HTTPError, URLError
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
    code, can_retry = classify(error)
    frames = [frame for frame in traceback.extract_tb(error.__traceback__)
              if Path(frame.filename).parent.name == 'evolvmem']
    location = f'{Path(frames[-1].filename).name}:{frames[-1].lineno}' if frames else ''
    return json.dumps({'stage': stage, 'exception_type': re.sub(r'[^a-zA-Z0-9_]', '', type(error).__name__)[:64],
                       'occurred_at': _now_iso(), 'failure_code': code,
                       'retryable': can_retry, 'location': location}, ensure_ascii=False)


def classify(error):
    """A class name alone never authorizes another provider call."""
    from evolvmem.lan_sharing import LanError
    if getattr(error, 'halt_run', False):
        return 'provider_unavailable', False
    if getattr(error, 'rate_limited', False):
        return 'rate_limited', True
    if isinstance(error, LanError) and str(error) in ('extraction_summary_missing', 'extraction_summary_rejected'):
        return 'summary_invalid', True
    if isinstance(error, HTTPError):
        return 'provider_http_error', error.code in (408, 429, 500, 502, 503, 504)
    if isinstance(error, (TimeoutError, ConnectionError, URLError)):
        return 'network_unavailable', True
    if error.__cause__ is not None:
        return classify(error.__cause__)
    if isinstance(error, ValueError):
        return 'validation_failed', False
    return 'unclassified_failure', False


def retryable(raw):
    try:
        return json.loads(raw).get('retryable') is True
    except (ValueError, AttributeError, TypeError):
        return False


def describe(raw):
    try:
        data = json.loads(raw)
        return f"{LABELS.get(data['stage'], '处理失败')} · {data['exception_type']} · {data['occurred_at']} UTC"
    except (KeyError, ValueError, TypeError):
        return ''

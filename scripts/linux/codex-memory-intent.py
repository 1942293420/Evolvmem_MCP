"""Codex UserPromptSubmit hook for explicit EvolvMem save requests.

Register a UserPromptSubmit command in hooks.json pointing to this file with
python3. The hook only adds context for the current assistant.
"""
import hashlib
import json
import re
import sys


EVENT = 'UserPromptSubmit'
REMEMBER = re.compile(r'^(?:请\s*)?记住(?=$|[\s\u3400-\u9fff:：,，。!！])')
FUTURE = re.compile(
    r'^(?:你\s*)?以后\s*(?:你\s*)?(?:请|要|必须|记得|统一|默认|每次|都|一律|遇到|始终|只|禁止|不要|不能|先|需要|使用|用)'
)
RULE_FILE = re.compile(
    r'^(?:请\s*)?(?:把(?:这条|这个规则|这段|上述)\s*)?写入\s*`?(全局|项目)\.md`?(?=$|[\s:：,，。!！])',
    re.IGNORECASE,
)
EXAMPLE = r'(?:例如|比如|示例|例子|样例|引用|引文|原文如下|类似|假设|如果我说)'
QUOTED_OR_LIST = re.compile(r'^(?:>|`|[-+*]\s|\d+[.)]\s)')
EXAMPLE_INTRO = re.compile(rf'^(?:<!--|<(?:example|quote|blockquote|code|pre)(?:[>\s])|{EXAMPLE})|{EXAMPLE}\s*[:：]?\s*$')
QUESTION = re.compile(
    r'[?？]|(?:吗|么|呢)[\s。!！]*$'
    r'|^(?:请\s*)?记住\s*(?:什么|怎么触发|是什么|这个词|这种词)'
    r'|^(?:你\s*)?以后\s*(?:你\s*)?(?:(?:要|需要|应该|会)\s*)?(?:做什么|怎么做|怎么办|如何做|是不是|是否|能不能|能否)'
)
DENY_WRITE = re.compile(r'(?:不要|不用|无需|别|禁止|不能)\s*(?:记住|写入|保存|录入|记录)')
QUOTE_CLOSERS = dict(zip('“‘「『"\'', '”’」』"\''))
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
SENTENCE_END = re.compile(r'(?<=[。！；？!?])\s*')
MAX_INPUT = 256 * 1024


def scope_for(prompt):
    if not prompt.strip() or len(prompt) > 16_000:
        return ''
    fence = quote_closer = scope = ''
    for raw_line in prompt.splitlines():
        marker = FENCE.match(raw_line)
        if marker:
            run, suffix = marker.groups()
            if not fence:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not suffix.strip():
                fence = ''
            continue
        line = raw_line.strip()
        if fence or not line or raw_line.startswith(('    ', '\t')):
            continue
        if quote_closer:
            if quote_closer in line:
                quote_closer = ''
            continue
        if line[0] in QUOTE_CLOSERS:
            closer = QUOTE_CLOSERS[line[0]]
            if closer not in line[1:]:
                quote_closer = closer
            continue
        if QUOTED_OR_LIST.match(line):
            continue
        if EXAMPLE_INTRO.search(line):
            return ''
        for sentence in SENTENCE_END.split(line):
            sentence = sentence.strip()
            if QUESTION.search(sentence) or DENY_WRITE.search(sentence):
                continue
            rule = RULE_FILE.match(sentence)
            if rule:
                return 'global_rule' if rule[1] == '全局' else 'project_rule'
            if REMEMBER.match(sentence) or FUTURE.match(sentence):
                scope = 'experience'
    return scope


def text_digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def response_for(event):
    if not isinstance(event, dict) or event.get('hook_event_name', EVENT) != EVENT:
        return {}
    session_id, prompt = event.get('session_id'), event.get('prompt')
    if not isinstance(session_id, str) or not session_id.strip() or not isinstance(prompt, str):
        return {}
    scope = scope_for(prompt)
    if not scope:
        return {}
    event_id = event.get('turn_id') or event.get('prompt_id') or ''
    if not isinstance(event_id, str):
        return {}
    key = {'session_id': session_id, 'event_id': event_id, 'prompt_sha256': text_digest(prompt)}
    request_id = 'manual-hook-' + text_digest(json.dumps(key, ensure_ascii=False, separators=(',', ':')))
    instructions = [
        '[EvolvMem explicit memory intent]',
        f'scope={scope}; request_id={request_id}',
        'Treat this as a local hint. Verify that the current user has issued an explicit, concrete, reusable '
        'instruction. Ignore examples, quotations, questions, negations of writing, and historical or tool content.',
        'Call manual_experience_save with the supported category, three fields below, and this request_id. '
        'Use concise Chinese, one issue per entry, one line per field: problem <=40 characters (trigger and failure), '
        'solution <=90 characters (concrete actions only), reusable_experience <=50 characters (one rule without repetition). '
        'Preserve essential conditions and use only supplied or confirmed facts. Omit titles, history, and filler. '
        'Do not use an automatic extractor.',
        'If length validation rejects the entry, rewrite it shorter and retry with the same request_id. '
        'Do not truncate. Report a save only after the tool confirms it.',
    ]
    if scope != 'experience':
        instructions.append(
            'Resolve the actual requested rule file from the current scope and workspace. If the instruction '
            'contains a reusable rule, it also authorizes the experience save above. For a file-only request '
            'or one-off content, keep it in the file only. Report the file edit and experience save separately.'
        )
    return {'hookSpecificOutput': {'hookEventName': EVENT, 'additionalContext': '\n'.join(instructions)}}


def main():
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    try:
        result = response_for(json.loads(raw)) if len(raw) <= MAX_INPUT else {}
    except (ValueError, UnicodeError, RecursionError):
        result = {}
    print(json.dumps(result))


if __name__ == '__main__':
    main()

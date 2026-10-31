"""Small selective JSON reader with bounded strings and optional string sinks.

Skipped values are still fully syntax-checked. This is for private archive
recovery, not a replacement for application JSON parsing or a permissive parser.
"""
import io
import json
import re

_SPECIAL = re.compile(r'["\\\x00-\x1f]')
_WS = re.compile(r'[ \t\r\n]*')


class StringSink:
    def __init__(self, stream):
        self.stream = stream
        self.characters = 0

    def write(self, text):
        self.stream.write(text)
        self.characters += len(text)


class ValueProbe:
    """Keep only whether a JSON value would make nonempty tool output."""
    def __init__(self):
        self.nonempty = False

    def write(self, text):
        self.nonempty |= bool(text.strip())


class JsonReader:
    def __init__(self, stream, *, block_size=65536, max_string=2*1024*1024,
                 max_retained=32*1024*1024, max_depth=80):
        self.stream = stream
        self.block_size = block_size
        self.max_string = max_string
        self.max_retained = max_retained
        self.max_depth = max_depth
        self.buffer = ''
        self.pos = 0
        self.ended = False
        self.retained = 0
        self.root_keys = set()
        self.line = 1
        self.column = 0

    def _advance(self, end):
        text = self.buffer[self.pos:end]
        lines = text.count('\n')
        self.line += lines
        self.column = len(text.rsplit('\n',1)[-1]) if lines else self.column + len(text)
        self.pos = end

    def _fill(self):
        if self.pos >= len(self.buffer) and not self.ended:
            self.buffer = self.stream.read(self.block_size)
            self.pos = 0
            self.ended = not self.buffer
        return self.pos < len(self.buffer)

    def peek(self):
        return self.buffer[self.pos] if self._fill() else ''

    def take(self):
        char = self.peek()
        if not char:
            raise ValueError('incomplete_archive_json')
        self._advance(self.pos + 1)
        return char

    def white(self):
        while self._fill():
            end = _WS.match(self.buffer, self.pos).end()
            if end == self.pos:
                return
            self._advance(end)

    def _account(self, size):
        self.retained += size
        if self.retained > self.max_retained:
            raise ValueError('cleaned_archive_budget_exceeded')

    def string(self, keep=True, *, key=False):
        if self.take() != '"':
            raise ValueError('invalid_archive_json_string')
        parts, size, high = [], 0, None
        sink = keep if isinstance(keep, StringSink) else None

        def emit(text):
            nonlocal size, high
            if not keep and not key:
                return
            if high is not None:
                if text and 0xdc00 <= ord(text[0]) <= 0xdfff:
                    text = chr(0x10000 + ((high - 0xd800) << 10) + ord(text[0]) - 0xdc00) + text[1:]
                else:
                    text = chr(high) + text
                high = None
            if text and 0xd800 <= ord(text[-1]) <= 0xdbff:
                high = ord(text[-1]); text = text[:-1]
            size += len(text)
            if sink:
                sink.write(text)
            else:
                if size > (1024 if key else self.max_string):
                    raise ValueError('archive_json_string_budget_exceeded')
                if keep:
                    self._account(len(text))
                    parts.append(text)

        while self._fill():
            match = _SPECIAL.search(self.buffer, self.pos)
            end = match.start() if match else len(self.buffer)
            emit(self.buffer[self.pos:end]); self._advance(end)
            if not match:
                continue
            char = self.take()
            if char == '"':
                if high is not None:
                    if sink:
                        sink.write(chr(high))
                    else:
                        if size + 1 > (1024 if key else self.max_string):
                            raise ValueError('archive_json_string_budget_exceeded')
                        if keep:
                            self._account(1); parts.append(chr(high))
                return sink if sink else ''.join(parts) if keep else None
            if char != '\\':
                raise ValueError('invalid_archive_json_control')
            escaped = self.take()
            if escaped == 'u':
                digits = ''.join(self.take() for _ in range(4))
                if not re.fullmatch('[0-9a-fA-F]{4}', digits):
                    raise ValueError('invalid_archive_json_escape')
                emit(chr(int(digits, 16)))
            elif escaped in '"\\/bfnrt':
                emit({'"':'"', '\\':'\\', '/':'/', 'b':'\b', 'f':'\f', 'n':'\n', 'r':'\r', 't':'\t'}[escaped])
            else:
                raise ValueError('invalid_archive_json_escape')
        raise ValueError('incomplete_archive_json_string')

    def value(self, keep=True, depth=0):
        if depth == 0:
            self.root_keys.clear()
        if depth > self.max_depth:
            raise ValueError('archive_json_depth_exceeded')
        self.white(); char = self.peek()
        if isinstance(keep, ValueProbe):
            if char == '"':
                probe = ValueProbe()
                sink = StringSink(probe)
                self.string(sink)
                return probe.nonempty
            self.value(False, depth)
            return True
        if isinstance(keep, StringSink) and char != '"':
            raise ValueError('archive_transcript_not_string')
        if char == '"':
            return self.string(keep)
        if char == '{':
            self.take(); self.white(); result = {} if keep else None
            if self.peek() == '}':
                self.take(); return result
            while True:
                self.white(); key = self.string(bool(keep), key=True)
                if depth == 0 and keep:
                    self.root_keys.add(key)
                self.white()
                if self.take() != ':': raise ValueError('invalid_archive_json_object')
                child = keep.get(key, False) if isinstance(keep, dict) else keep
                if isinstance(child, StringSink) and key in result:
                    raise ValueError('archive_json_duplicate_stream_key')
                value = self.value(child, depth+1)
                if keep and child:
                    self._account(64); result[key] = value
                self.white(); char = self.take()
                if char == '}': return result
                if char != ',': raise ValueError('invalid_archive_json_object')
        if char == '[':
            self.take(); self.white(); result = [] if keep else None
            if self.peek() == ']':
                self.take(); return result
            child = keep[0] if isinstance(keep, list) else keep
            while True:
                value = self.value(child, depth+1)
                if keep:
                    self._account(64); result.append(value)
                self.white(); char = self.take()
                if char == ']': return result
                if char != ',': raise ValueError('invalid_archive_json_array')
        token = ''
        while self.peek() and self.peek() not in ',]} \t\r\n':
            token += self.take()
            if len(token) > 1024: raise ValueError('invalid_archive_json_token')
        if not token or token in {'NaN', 'Infinity', '-Infinity'}:
            raise ValueError('invalid_archive_json_token')
        try:
            value = json.loads(token)
        except (ValueError, RecursionError):
            raise ValueError('invalid_archive_json_token') from None
        if isinstance(value, (dict,list,str)):
            raise ValueError('invalid_archive_json_token')
        if keep: self._account(32)
        return value if keep else None

    def finish(self):
        self.white()
        if self.peek(): raise ValueError('archive_json_trailing_data')

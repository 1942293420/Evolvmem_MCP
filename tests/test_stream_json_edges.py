"""Boundary and differential checks for the selective archive JSON reader."""
import io
import json
import random
import tracemalloc

import pytest

from evolvmem.stream_json import JsonReader, StringSink, ValueProbe


def test_seeded_nested_arrays_and_objects_match_stdlib_and_validate_when_skipped():
    randomizer = random.Random(20261031)
    leaves = [None, True, False, 0, -1024, 1.25, '', '中文🌌', 'line\nquote"\\', '\x00\t\u2028']

    def value(depth):
        if not depth or randomizer.randrange(3) == 0:
            return randomizer.choice(leaves)
        if randomizer.randrange(2):
            return [value(depth - 1) for _ in range(randomizer.randrange(5))]
        return {str(index) + '雪': value(depth - 1) for index in range(randomizer.randrange(5))}

    for index in range(100):
        raw = json.dumps(value(4), ensure_ascii=index % 2 == 0)
        for keep in (True, False):
            reader = JsonReader(io.StringIO(raw), block_size=1 + index % 7)
            parsed = reader.value(keep)
            assert parsed == (json.loads(raw) if keep else None)
            reader.finish()


@pytest.mark.parametrize('block_size', [1, 3, 7])
def test_physical_line_tracking_ignores_escaped_newline_and_unicode_separator(block_size):
    first = '{"v":"line\\ntext\u2028same physical line"}'
    second = '{"v":[1,2]}'
    raw = ' \r\n' + first + ' \r\n\t' + second + '\n'
    reader = JsonReader(io.StringIO(raw), block_size=block_size)
    assert reader.value() == json.loads(first)
    assert (reader.line, reader.column) == (2, len(first))
    reader.white()
    assert (reader.line, reader.column) == (3, 1)
    assert reader.value() == json.loads(second)
    assert (reader.line, reader.column) == (3, len(second) + 1)
    reader.finish()
    assert (reader.line, reader.column) == (4, 0)


class ByteFragments(io.RawIOBase):
    def __init__(self, data, step):
        self.data, self.step, self.offset = data, step, 0

    def readable(self): return True

    def readinto(self, buffer):
        size = min(len(buffer), self.step, len(self.data) - self.offset)
        buffer[:size] = self.data[self.offset:self.offset + size]
        self.offset += size
        return size


@pytest.mark.parametrize('physical_block', [1, 2, 3, 7])
@pytest.mark.parametrize('json_block', [1, 3, 17])
def test_utf8_and_escaped_unicode_across_both_stream_boundaries(physical_block, json_block):
    raw = '{"中文🌌":"雪\\n\\uD83C\\uDF0C","skip":"🙂","tail":[true,false,null,-1.25e+2]}'
    stream = io.TextIOWrapper(io.BufferedReader(ByteFragments(raw.encode(), physical_block), buffer_size=8), encoding='utf-8', errors='strict')
    with stream:
        reader = JsonReader(stream, block_size=json_block)
        assert reader.value() == json.loads(raw)
        reader.finish()


@pytest.mark.parametrize('invalid', [b'\xff', b'\x80', b'\xc0\xaf', b'\xed\xa0\x80', b'\xf4\x90\x80\x80', b'\xe4\xb8'])
def test_skipping_cannot_bypass_strict_utf8_validation(invalid):
    raw = b'{"ignored":"' + invalid + b'","kept":1}'
    stream = io.TextIOWrapper(io.BufferedReader(ByteFragments(raw, 1), buffer_size=8), encoding='utf-8', errors='strict')
    with stream, pytest.raises(UnicodeDecodeError):
        reader = JsonReader(stream, block_size=1)
        reader.value({'kept':True}); reader.finish()


@pytest.mark.parametrize('raw', [
    '', ' ', '[,1]', '[1,,2]', '{"a":1,,"b":2}', '{"a" 1}', '{a:1}',
    '{"a":1]','[true}', 'TRUE', '.1', '+1', '1.', '1e', '1e+', '--1', '1_0',
    '00', '-01', 'truefalse', '\ufeff{}', '\u00a0{}', '"unterminated', '"bad\\',
    '"\\U00000041"', '"\\u0x00"', '"\\u000G"',
])
def test_malformed_skipped_structures_agree_with_stdlib_rejection(raw):
    with pytest.raises((ValueError, json.JSONDecodeError)): json.loads(raw)
    with pytest.raises(ValueError):
        reader = JsonReader(io.StringIO(raw), block_size=1)
        reader.value(False); reader.finish()


@pytest.mark.parametrize('control', range(32))
def test_every_raw_control_is_rejected_even_in_ignored_string(control):
    raw = '{"ignored":"a' + chr(control) + 'b"}'
    with pytest.raises(ValueError):
        reader = JsonReader(io.StringIO(raw), block_size=1)
        reader.value(False); reader.finish()


@pytest.mark.parametrize('raw', [r'"\ud800"',r'"\udc00"',r'"\ud800\udc00"',r'"\ud800x\udc00"',r'"\ud800\ud801"',r'"\ud800\udc00\ud800"'])
@pytest.mark.parametrize('block_size', [1, 2, 9])
def test_escaped_surrogate_sequences_match_stdlib(raw, block_size):
    reader = JsonReader(io.StringIO(raw), block_size=block_size)
    assert reader.value() == json.loads(raw)
    reader.finish()


def test_duplicate_plain_keys_keep_stdlib_last_value():
    raw = '{"x":"old","\\u0078":"new","nested":{"x":1,"x":2}}'
    reader = JsonReader(io.StringIO(raw), block_size=1)
    assert reader.value() == json.loads(raw)
    reader.finish()
    assert reader.root_keys == {'x', 'nested'}


@pytest.mark.parametrize('second_key', ['transcript', r'\u0074ranscript'])
def test_duplicate_streamed_transcript_is_rejected_instead_of_concatenated(second_key):
    output = io.StringIO(); sink = StringSink(output)
    raw = '{"transcript":"old\\n","' + second_key + '":"new\\n"}'
    with pytest.raises(ValueError):
        reader = JsonReader(io.StringIO(raw), block_size=1)
        reader.value({'transcript':sink}); reader.finish()
    assert output.getvalue() != 'old\nnew\n'


def test_trailing_high_surrogate_counts_toward_string_and_key_limits():
    with pytest.raises(ValueError, match='string_budget'):
        JsonReader(io.StringIO(r'"x\ud800"'), max_string=1).value()
    with pytest.raises(ValueError, match='string_budget'):
        JsonReader(io.StringIO('{"' + 'k'*1024 + r'\ud800":0}')).value(False)


@pytest.mark.parametrize('raw,expected', [('""',False), ('"\\u0000"',True), ('null',True), ('false',True), ('0',True), ('{}',True), ('[]',True)])
def test_probe_matches_serialized_nonempty_tool_output_contract(raw, expected):
    reader = JsonReader(io.StringIO(raw), block_size=1)
    assert reader.value(ValueProbe()) is expected
    reader.finish()


@pytest.mark.parametrize('raw,expected', [('" "',False), ('"\\t\\r\\n"',False), ('"\\u2003\\u3000"',False), ('"  x  "',True), ('"\\u0000"',True)])
def test_probe_matches_tool_dialogue_strip_semantics_across_blocks(raw, expected):
    reader = JsonReader(io.StringIO(raw), block_size=1)
    assert reader.value(ValueProbe()) is expected
    reader.finish()


def test_depth_limit_applies_to_ignored_and_probed_values():
    for keep in (False, ValueProbe()):
        with pytest.raises(ValueError, match='depth'):
            JsonReader(io.StringIO('['*100 + '0' + ']'*100), max_depth=20).value(keep)


@pytest.mark.parametrize('probe', [False, True])
def test_discarded_object_keys_do_not_consume_retained_value_budget(probe):
    raw = '[' + ','.join('{"ignored":{"inner":"tool detail"}}' for _ in range(100)) + ']'
    reader = JsonReader(io.StringIO(raw), block_size=7, max_retained=0)
    assert reader.value(ValueProbe() if probe else False) is (True if probe else None)
    reader.finish()
    assert reader.retained == 0


def test_sink_write_failure_is_not_swallowed_as_success():
    class FullDisk:
        def write(self, text):
            if text: raise OSError('synthetic_full_disk')
    with pytest.raises(OSError, match='synthetic_full_disk'):
        JsonReader(io.StringIO('"business text"'), block_size=1).value(StringSink(FullDisk()))


def test_large_sink_is_bounded_in_memory_without_materializing_test_input():
    class VirtualString:
        remaining = 16 * 1024 * 1024
        opened = False
        closed = False
        def read(self, size):
            if not self.opened: self.opened=True; return '"'
            if self.remaining:
                size=min(size,self.remaining); self.remaining-=size; return 'x'*size
            if not self.closed: self.closed=True; return '"'
            return ''
    class CountOnly:
        def write(self, text): pass
    tracemalloc.start()
    try:
        reader=JsonReader(VirtualString(),block_size=4096,max_string=8,max_retained=0)
        sink=StringSink(CountOnly());assert reader.value(sink) is sink;reader.finish()
        assert sink.characters == 16*1024*1024 and reader.retained == 0
        assert tracemalloc.get_traced_memory()[1] < 256*1024
    finally: tracemalloc.stop()

import io
import json
import random
import tracemalloc

import pytest

from evolvmem.stream_json import JsonReader, StringSink


@pytest.mark.parametrize('block_size', [1,3,17,65536])
def test_streamed_strings_and_structures_match_stdlib(block_size):
    values = [None, True, False, 0, -1, 1.3, '', '中文\n\\\"🌌', {'a': [1, None, {'b': '\t🌌'}]},
              {'source': {'subagent': {'parent': 'abc'}}, 'messages': [{'role': 'user', 'content': '真实需求'}]}]
    rng = random.Random(42)
    values += [{'value': ''.join(rng.choices('abc中🌌\n\t\\"',k=80)), 'array':[rng.randint(-100,100) for _ in range(10)]} for i in range(20)]
    for value in values:
        for ascii_only in (False,True):
            raw = json.dumps(value, ensure_ascii=ascii_only)
            reader = JsonReader(io.StringIO(raw), block_size=block_size)
            assert reader.value() == json.loads(raw)
            reader.finish()


def test_skipped_string_never_accumulates_its_full_value():
    source = io.StringIO('{"ignored":"'+'x'*(4*1024*1024)+'","keep":"ok"}')
    tracemalloc.start()
    reader = JsonReader(source, max_string=1024)
    assert reader.value({'keep': True}) == {'keep':'ok'}
    reader.finish()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 1024*1024


def test_transcript_sink_streams_unescaped_text_without_retaining_it():
    text = ('{"type":"event_msg","content":"中文🌌"}\n')*1000
    output = io.StringIO()
    reader = JsonReader(io.StringIO(json.dumps({'transcript':text},ensure_ascii=True)), block_size=3, max_string=64)
    sink = StringSink(output)
    assert reader.value({'transcript':sink})['transcript'] is sink
    reader.finish()
    assert output.getvalue() == text and sink.characters == len(text)


@pytest.mark.parametrize('raw', ['{"a":"bad\\q"}', '{"a":"bad\\uZZZZ"}', '[1,]', '{"a":1,}',
    '{"a":"raw\ncontrol"}', '{"a":NaN}', '{"a":true', '{"a":01}', '{"a":"\\u12"}', '\v{}', '{}junk'])
def test_even_skipped_values_must_be_valid_json(raw):
    reader = JsonReader(io.StringIO(raw), block_size=2)
    with pytest.raises(ValueError):
        reader.value(False);reader.finish()


def test_string_and_retained_output_are_bounded():
    with pytest.raises(ValueError, match='string_budget'):
        JsonReader(io.StringIO('"'+'x'*40+'"'), max_string=20).value()
    with pytest.raises(ValueError, match='budget'):
        JsonReader(io.StringIO('[0,0,0,0]'), max_retained=100).value()
    with pytest.raises(ValueError, match='depth'):
        JsonReader(io.StringIO('['*10+'0'+']'*10), max_depth=5).value(False)

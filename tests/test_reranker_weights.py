"""Absolute relevance gates must not use batch-dependent integer weights."""
import pytest
import math
import numpy as np
from types import SimpleNamespace
from evolvmem.reranking import Reranker


def test_integer_only_directory_is_rejected_before_loading_tokenizer(tmp_path):
    (tmp_path / 'onnx').mkdir()
    (tmp_path / 'onnx/model_quint8_avx2.onnx').write_bytes(b'not-a-floating-model')
    with pytest.raises(FileNotFoundError, match='floating_reranker_weights_required'):
        Reranker(tmp_path)


def test_long_candidate_does_not_pad_entire_batch_and_scores_keep_input_order():
    work = []
    class Tokenizer:
        def encode_batch(self, pairs):
            width = max(len(doc) for _, doc in pairs)
            return [SimpleNamespace(ids=[int(doc[0])]+[1]*(width-1), attention_mask=[1]*width)
                    for _, doc in pairs]
    class Session:
        def run(self, _, inputs):
            work.append(inputs['input_ids'].size)
            return [inputs['input_ids'][:, :1].astype(float)]
    ranker = Reranker.__new__(Reranker)
    ranker.tokenizer, ranker.session = Tokenizer(), Session()
    docs = ['2'+'x'*399] + ['1short']*31
    scores = ranker.score('query', docs)
    assert scores == pytest.approx([1/(1+math.exp(-int(d[0]))) for d in docs])
    assert sum(work) < 32*400/2, 'short passages must not inherit every long passage padding cost'

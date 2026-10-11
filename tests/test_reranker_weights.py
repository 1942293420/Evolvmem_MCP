"""Absolute relevance gates must not use batch-dependent integer weights."""
import pytest
from evolvmem.reranking import Reranker


def test_integer_only_directory_is_rejected_before_loading_tokenizer(tmp_path):
    (tmp_path / 'onnx').mkdir()
    (tmp_path / 'onnx/model_quint8_avx2.onnx').write_bytes(b'not-a-floating-model')
    with pytest.raises(FileNotFoundError, match='floating_reranker_weights_required'):
        Reranker(tmp_path)

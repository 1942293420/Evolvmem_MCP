"""Optional resident local cross-encoder; no downloads or provider calls."""
import math
from pathlib import Path

import numpy as np


def valid_scores(values, count):
    return (isinstance(values, list) and len(values) == count
            and all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in values))


class Reranker:
    def __init__(self, directory):
        from tokenizers import Tokenizer
        import onnxruntime as ort
        directory = Path(directory)
        self.tokenizer = Tokenizer.from_file(str(directory / 'tokenizer.json'))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding(pad_id=1, pad_token='<pad>')
        options = ort.SessionOptions()
        options.intra_op_num_threads = 4
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(directory / 'onnx/model_quint8_avx2.onnx'),
            sess_options=options, providers=['CPUExecutionProvider'])

    def score(self, query, documents):
        if not isinstance(query, str) or len(query) > 4000 or not isinstance(documents, list) \
                or len(documents) > 200 or any(not isinstance(d, str) or len(d) > 4000 for d in documents):
            raise ValueError('invalid_reranking_input')
        scores = []
        for offset in range(0, len(documents), 32):
            batch = self.tokenizer.encode_batch([(query, d) for d in documents[offset:offset+32]])
            inputs = {'input_ids': np.asarray([b.ids for b in batch], dtype=np.int64),
                      'attention_mask': np.asarray([b.attention_mask for b in batch], dtype=np.int64)}
            logits = np.asarray(self.session.run(None, inputs)[0]).reshape(-1)
            if logits.size != len(batch) or not np.isfinite(logits).all():
                raise RuntimeError('invalid_reranking_response')
            scores.extend(float(1 / (1 + math.exp(-max(-80, min(80, float(v)))))) for v in logits)
        if not valid_scores(scores, len(documents)):
            raise RuntimeError('invalid_reranking_response')
        return scores

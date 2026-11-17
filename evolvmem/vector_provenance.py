"""Disposable per-vector proofs for reusing unchanged embeddings across recovery.

Each entry binds the L0 digest, embedding contract and actual stored vector
bytes. Lost/old/corrupt proofs merely require encoding again. In particular,
an index written by an older client cannot make an old L0 proof valid for its
new vector. No source text or credentials are stored here.
"""
import hashlib
import json
import os
import uuid
import numpy as np
from evolvmem.runtime_contract import EMBEDDING_INPUT_TOKENS


def path(config):
    return config.context_vector_path.with_suffix('.provenance.json')


def contract(config):
    return [config.embedding_model_filename, config.embedding_dim,
            config.embedding_doc_prefix, EMBEDDING_INPUT_TOKENS]


def text_digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def vector_digest(vector):
    return hashlib.sha256(np.asarray(vector, dtype='<f4').tobytes()).hexdigest()


def entry(text, vector):
    return {'text': text_digest(text), 'vector': vector_digest(vector)}


def load_with_contract(config):
    """Return proofs and compatibility; None means no trustworthy manifest."""
    try:
        data = json.loads(path(config).read_text())
        if data.get('version') == 1 and isinstance(data.get('items'), dict):
            compatible = data.get('contract') == contract(config)
            return (data['items'] if compatible else {}), compatible
    except (OSError, ValueError, AttributeError):
        pass
    return {}, None


def load(config):
    return load_with_contract(config)[0]


def save(config, entries):
    target = path(config)
    temporary = target.with_name(target.name + '.tmp-' + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps({'version': 1, 'contract': contract(config), 'items': entries}))
        os.replace(temporary, target)
        return True
    except OSError:
        return False  # Index truth stays valid; this optional cache can be rebuilt.
    finally:
        temporary.unlink(missing_ok=True)


def record(config, item_id, l0, vector):
    entries, compatible = load_with_contract(config)
    if compatible is False:
        # One new vector cannot certify the rest of an old vector space.
        # Keep the mismatch visible until recovery replaces the whole cache.
        return
    entries[str(item_id)] = entry(l0, vector)
    save(config, entries)


def recovery_status(config, report):
    target = config.context_vector_path.with_suffix('.recovery.json')
    temporary = target.with_name(target.name + '.tmp-' + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(report))
        os.replace(temporary, target)
    except OSError:
        pass
    finally:
        temporary.unlink(missing_ok=True)


def reusable(config, documents):
    from evolvmem.vector_index import VectorIndex
    proofs = load(config)
    if not proofs or not config.context_vector_path.exists():
        return []
    index = VectorIndex(config, path=config.context_vector_path)
    try:
        index.initialize(dim=config.embedding_dim)
        if index.inspect_metadata().dimension != config.embedding_dim:
            return []
        result = []
        for document in documents:
            proof = proofs.get(str(document.item_id))
            if not isinstance(proof, dict) or proof.get('text') != text_digest(document.l0):
                continue
            vector = index.vector_copy(document.item_id)
            if vector is not None and proof.get('vector') == vector_digest(vector):
                result.append((document, vector))
        return result
    except Exception:
        return []
    finally:
        index.close()

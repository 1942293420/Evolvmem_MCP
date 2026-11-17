"""Explicit trusted embedding contracts; no model loading or text normalization."""
from dataclasses import dataclass
import hashlib
from itertools import islice
import json
import math
from numbers import Real
import struct


class VectorError(RuntimeError):
    def __init__(self,code):
        super().__init__(code)
        self.code=code


def require(condition,code):
    if not condition: raise VectorError(code)


def text_hash(text):
    require(isinstance(text,str),'vector_text_required')
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


@dataclass(frozen=True,slots=True)
class ModelContract:
    model:str
    dimension:int
    document_prefix:str
    query_prefix:str
    input_tokens:int
    model_revision:str = ''

    def __post_init__(self):
        require(isinstance(self.model,str) and 0<len(self.model)<=512,'model_identity_required')
        require(type(self.dimension) is int and self.dimension==768,'unsupported_vector_dimension')
        require(all(isinstance(s,str) and len(s)<=8192 for s in (self.document_prefix,self.query_prefix)),
                'invalid_embedding_prefix')
        require(isinstance(self.model_revision,str) and (not self.model_revision or (len(self.model_revision)==64 and all(c in '0123456789abcdef' for c in self.model_revision))), 'invalid_model_revision')
        require(type(self.input_tokens) is int and 0<self.input_tokens<=32768,'invalid_token_contract')

    @property
    def document_fields(self):
        # Revision-bearing identities never reuse legacy filename-only vectors.
        identity = self.model + '@sha256:' + self.model_revision if self.model_revision else self.model
        return [identity,self.dimension,self.document_prefix,self.input_tokens]

    @property
    def key(self):
        return hashlib.sha256(json.dumps(self.document_fields,ensure_ascii=False,
            separators=(',',':')).encode('utf-8')).hexdigest()

    @classmethod
    def from_config(cls,config,*,input_tokens,model_revision=''):
        return cls(config.embedding_model_filename,config.embedding_dim,
            config.embedding_doc_prefix,config.embedding_query_prefix,input_tokens,model_revision)

    def check_provider(self,provider,*,input_tokens):
        require(getattr(provider,'is_loaded',False) is True,'embedding_provider_unavailable')
        config=getattr(provider,'config',None)
        require(config is not None,'embedding_provider_contract_unavailable')
        require(bool(self.model_revision) and getattr(provider,'model_revision',None)==self.model_revision,'embedding_model_revision_unavailable')
        require(type(self).from_config(config,input_tokens=input_tokens,model_revision=self.model_revision)==self,'embedding_contract_changed')


@dataclass(frozen=True,slots=True)
class Embedding:
    values:tuple
    bytes:bytes
    digest:str

    @property
    def pg_bytes(self):
        # pgvector vector_send: int16 dimension + int16 zero + network float4.
        # The DDL checks this digest against the actual stored PG vector.
        return struct.pack('>hh',len(self.values),0)+struct.pack('>'+str(len(self.values))+'f',*self.values)

    @property
    def pg_digest(self):
        return hashlib.sha256(self.pg_bytes).hexdigest()

    @property
    def literal(self):
        # Nine significant decimal digits round-trip IEEE binary32. This is a
        # bound SQL value, never concatenated into query or schema text.
        return '['+','.join(format(value,'.9g') for value in self.values)+']'


def vector(values,contract):
    require(isinstance(contract,ModelContract),'trusted_model_contract_required')
    try: values=tuple(islice(iter(values),contract.dimension+1))
    except TypeError: raise VectorError('invalid_embedding') from None
    require(len(values)==contract.dimension,'embedding_dimension_mismatch')
    require(all(not isinstance(v,bool) and isinstance(v,Real) and math.isfinite(v) for v in values),
            'invalid_embedding')
    try: payload=struct.pack('<'+str(contract.dimension)+'f',*values)
    except (OverflowError,struct.error): raise VectorError('invalid_embedding') from None
    decoded=struct.unpack('<'+str(contract.dimension)+'f',payload)
    require(all(math.isfinite(v) for v in decoded) and any(v!=0 for v in decoded),'invalid_embedding')
    return Embedding(decoded,payload,hashlib.sha256(payload).hexdigest())


def reusable(text,values,proof,contract):
    if (not isinstance(proof,dict) or proof.get('contract')!=contract.document_fields
            or proof.get('text')!=text_hash(text)):
        return None
    try: result=vector(values,contract)
    except VectorError: return None
    return result if proof.get('vector')==result.digest else None

"""Strict loopback dual-space embedding client; parsing never loads models."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import socket
import struct
import threading
import time
from urllib.parse import urlsplit


class EmbeddingFailure(RuntimeError):
    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


def _require(ok, code):
    if not ok:
        raise EmbeddingFailure(code)


def note_text(note: dict) -> str:
    fields = ("category", "problem", "solution", "reusable_experience")
    _require(isinstance(note, dict) and all(isinstance(note.get(k), str) for k in fields),
             "invalid_embedding_note")
    return "\n".join(note[k] for k in fields)


def note_content_sha256(note: dict) -> str:
    return hashlib.sha256(note_text(note).encode("utf-8")).hexdigest()


def validate_vector(value, dimension):
    try:
        _require(isinstance(value, list) and len(value) == dimension
            and all(type(x) in (float, int) and math.isfinite(x) for x in value),
            "invalid_embedding_vector")
        values = [struct.unpack("!f", struct.pack("!f", float(x)))[0] for x in value]
        norm = sum(x*x for x in values)
        _require(math.isfinite(norm) and norm > 0, "invalid_embedding_vector")
        return values
    except (ValueError, OverflowError, struct.error):
        raise EmbeddingFailure("invalid_embedding_vector") from None


@dataclass(frozen=True)
class ModelSpec:
    model_path: str
    model_sha256: str
    dimension: int
    query_prefix: str
    document_prefix: str
    pooling: str
    input_tokens: int
    query_tokens: int = 128
    n_threads: int = 6

    @classmethod
    def from_dict(cls, raw, dimension):
        try:
            item = cls(**raw)
            _require(item.dimension == dimension and type(item.dimension) is int
                and isinstance(item.model_path, str) and Path(item.model_path).is_absolute()
                and isinstance(item.model_sha256, str) and len(item.model_sha256) == 64
                and all(c in "0123456789abcdef" for c in item.model_sha256)
                and isinstance(item.query_prefix, str) and isinstance(item.document_prefix, str)
                and item.pooling in ("cls", "mean")
                and type(item.input_tokens) is int and 8 <= item.input_tokens <= 512
                and type(item.query_tokens) is int and 1 <= item.query_tokens <= item.input_tokens
                and type(item.n_threads) is int and 1 <= item.n_threads <= 8,
                "invalid_embedding_config")
            return item
        except (TypeError, ValueError):
            raise EmbeddingFailure("invalid_embedding_config") from None

    @property
    def contract_id(self):
        semantic = dict(model_sha256=self.model_sha256, dimension=self.dimension,
            query_prefix=self.query_prefix, document_prefix=self.document_prefix,
            pooling=self.pooling, input_tokens=self.input_tokens, query_tokens=self.query_tokens,
            normalization="l2", input_truncation="prefix_tokens", note_format="category-problem-solution-reusable-v1",
            backend="llama-cpp-gguf-v1")
        raw = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return "exp-embedding-v1:" + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class RuntimeConfig:
    url: str
    legacy: ModelSpec
    retrieval: ModelSpec
    request_timeout_seconds: float = 2.0
    startup_timeout_seconds: float = 60.0
    recovery_interval_seconds: float = 5.0

    @property
    def port(self):
        return urlsplit(self.url).port

    @classmethod
    def from_dict(cls, raw):
        try:
            cfg = cls(url=raw["url"], legacy=ModelSpec.from_dict(raw["legacy"], 768),
                retrieval=ModelSpec.from_dict(raw["retrieval"], 1024),
                **{k: raw[k] for k in ("request_timeout_seconds", "startup_timeout_seconds",
                    "recovery_interval_seconds") if k in raw})
            url = urlsplit(cfg.url)
            _require(url.scheme == "http" and url.hostname == "127.0.0.1"
                and url.port is not None and 1 <= url.port <= 65535 and not url.username
                and not url.password and url.path in ("", "/") and not url.query and not url.fragment,
                "invalid_embedding_config")
            for value, maximum in ((cfg.request_timeout_seconds, 30),
                    (cfg.startup_timeout_seconds, 120), (cfg.recovery_interval_seconds, 300)):
                _require(type(value) in (float, int) and math.isfinite(value) and 0 < value <= maximum,
                         "invalid_embedding_config")
            return cfg
        except (KeyError, TypeError, ValueError):
            raise EmbeddingFailure("invalid_embedding_config") from None

    @classmethod
    def from_file(cls, path):
        try:
            return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            raise EmbeddingFailure("invalid_embedding_config") from None


@dataclass(frozen=True)
class EmbeddingProjection:
    legacy_embedding: list[float]
    retrieval_embedding: list[float]
    contract_id: str
    content_sha256: str


class EmbeddingClient:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.retrieval_contract_id = config.retrieval.contract_id
        self.legacy_contract_id = config.legacy.contract_id

    @classmethod
    def from_environment(cls):
        path = os.environ.get("EVOLVMEM_EXPERIENCE_EMBEDDING_CONFIG")
        return cls(RuntimeConfig.from_file(path)) if path else None

    def _post(self, payload, deadline):
        _require(type(deadline) in (float, int) and math.isfinite(deadline),
                 "invalid_embedding_deadline")
        until = min(deadline, time.monotonic() + self.config.request_timeout_seconds)
        remaining = until - time.monotonic()
        _require(remaining > 0, "embedding_deadline_exceeded")
        payload.update(deadline=until, retrieval_contract_id=self.retrieval_contract_id,
                       legacy_contract_id=self.legacy_contract_id)
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        _require(len(body) <= 65536, "invalid_embedding_input")
        conn = http.client.HTTPConnection("127.0.0.1", self.config.port, timeout=remaining)
        transport = []
        def abort():
            sock = transport[0] if transport else conn.sock
            if sock is not None:
                try: sock.shutdown(socket.SHUT_RDWR)
                except OSError: pass
            conn.close()
        timer = threading.Timer(remaining, abort); timer.daemon = True; timer.start()
        try:
            conn.connect()
            transport.append(conn.sock)
            _require(time.monotonic() < until, "embedding_deadline_exceeded")
            conn.request("POST", "/encode", body, {"Content-Type": "application/json"})
            response = conn.getresponse()
            data = response.read(131073)
            _require(time.monotonic() < until, "embedding_deadline_exceeded")
            _require(len(data) <= 131072, "invalid_embedding_response")
            parsed = json.loads(data)
            _require(isinstance(parsed, dict), "invalid_embedding_response")
            if response.status != 200:
                allowed = {"embedding_not_ready", "embedding_busy", "embedding_deadline_exceeded",
                           "embedding_contract_mismatch", "invalid_embedding_vector", "invalid_embedding_input"}
                code = parsed.get("error")
                raise EmbeddingFailure(code if code in allowed else "embedding_service_failed")
            return parsed
        except EmbeddingFailure:
            raise
        except (OSError, ValueError, http.client.HTTPException, AttributeError):
            code = "embedding_deadline_exceeded" if time.monotonic() >= until else "embedding_http_unavailable"
            raise EmbeddingFailure(code) from None
        finally:
            timer.cancel(); abort()

    def encode_query(self, text, *, deadline):
        _require(isinstance(text, str) and 0 < len(text) <= 32768, "invalid_embedding_input")
        out = self._post(dict(kind="query", text=text), deadline)
        _require(out.get("contract_id") == self.retrieval_contract_id, "embedding_contract_mismatch")
        return validate_vector(out.get("vector"), 1024)

    def encode_note(self, note: dict, *, deadline=None) -> EmbeddingProjection:
        text = note_text(note)
        _require(0 < len(text) <= 32768, "invalid_embedding_input")
        digest = hashlib.sha256(text.encode()).hexdigest()
        out = self._post(dict(kind="note", text=text), deadline if deadline is not None
                         else time.monotonic() + self.config.request_timeout_seconds)
        _require(out.get("contract_id") == self.retrieval_contract_id
                 and out.get("legacy_contract_id") == self.legacy_contract_id,
                 "embedding_contract_mismatch")
        _require(out.get("content_sha256") == digest, "embedding_content_mismatch")
        return EmbeddingProjection(validate_vector(out.get("legacy_embedding"), 768),
            validate_vector(out.get("retrieval_embedding"), 1024), self.retrieval_contract_id, digest)

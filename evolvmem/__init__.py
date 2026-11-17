"""EvolvMem — evolving memory plugin for Claude Code."""

from evolvmem.config import Config
from evolvmem.context_core import ContextCore
from evolvmem.embedding import EmbeddingEngine

__version__ = "0.1.0"
__all__ = ["Config", "ContextCore", "EmbeddingEngine"]

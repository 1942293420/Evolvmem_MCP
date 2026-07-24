"""配置管理：数据目录、模型路径、检索和遗忘阈值。"""

from dataclasses import dataclass, field
from pathlib import Path
import json
import os


@dataclass
class Config:
    """全局配置，可从 config.json 加载或使用默认值。"""

    # 数据目录
    data_dir: Path = Path.home() / ".claude" / "hermes-memory"

    # SQLite 数据库路径
    @property
    def db_path(self) -> Path:
        return self.data_dir / "memory.db"

    # USearch 向量索引路径
    @property
    def vector_path(self) -> Path:
        return self.data_dir / "vectors.usearch"

    # GGUF 模型路径
    @property
    def model_path(self) -> Path:
        return self.data_dir / "models" / "bge-small-zh-Q5_K_M.gguf"

    # 配置文件路径
    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"

    # --- 检索参数 ---
    fts_top_k: int = 20          # FTS5 召回数
    vector_top_k: int = 20       # HNSW 召回数
    fts_weight: float = 0.6      # FTS5 归一化 rank 权重
    vector_weight: float = 0.4   # HNSW 归一化 distance 权重

    # --- 遗忘参数 ---
    forget_days_threshold: int = 90          # 未访问天数阈值
    forget_access_count_threshold: int = 2   # 最大访问次数（低于此值可降级）
    forget_rate_limit_days: int = 7          # 同一记忆两次降级的最小间隔

    # --- embedding 参数 ---
    embedding_dim: int = 512

    # --- 安全 ---
    stop_hook_safe: bool = True  # 防止 Stop Hook 无限循环

    def ensure_dirs(self) -> None:
        """确保数据目录和模型目录存在。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "models").mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_file(cls, path: Path | None = None) -> "Config":
        """从 config.json 加载配置，不存在的字段使用默认值。"""
        config = cls()
        config.ensure_dirs()
        load_path = path or config.config_path
        if load_path.exists():
            with open(load_path) as f:
                data = json.load(f)
            for key, value in data.items():
                if hasattr(config, key):
                    setattr(config, key, value)
        return config

    def save(self) -> None:
        """保存配置到 config.json。"""
        self.ensure_dirs()
        data = {
            "fts_top_k": self.fts_top_k,
            "vector_top_k": self.vector_top_k,
            "fts_weight": self.fts_weight,
            "vector_weight": self.vector_weight,
            "forget_days_threshold": self.forget_days_threshold,
            "forget_access_count_threshold": self.forget_access_count_threshold,
            "forget_rate_limit_days": self.forget_rate_limit_days,
            "embedding_dim": self.embedding_dim,
            "stop_hook_safe": self.stop_hook_safe,
        }
        with open(self.config_path, "w") as f:
            json.dump(data, f, indent=2)

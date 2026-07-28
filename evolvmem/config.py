"""Configuration management: data directory, model path, retrieval and forgetting thresholds."""

from dataclasses import dataclass, field
from pathlib import Path
import json


@dataclass
class Config:
    """Global configuration, loaded from config.json or using defaults."""

    # Data directory
    data_dir: Path = Path.home() / ".claude" / "evolvmem"

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
        return self.data_dir / "models" / "nomic-embed-text-v1.5.f16.gguf"

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
    embedding_dim: int = 768
    # nomic-embed-text-v1.5 任务前缀；置空字符串可关闭
    embedding_query_prefix: str = "search_query: "
    embedding_doc_prefix: str = "search_document: "

    # --- SessionStart 注入限额 ---
    inject_max_count: int = 50     # 最多注入的记忆条数
    inject_max_chars: int = 8000   # 注入内容总字符预算（约 3-4k tokens）

    # --- 分层注入预算 ---
    inject_pinned_max_count: int = 10    # pinned 层最多条数
    inject_pinned_max_chars: int = 2000  # pinned 层字符预算
    inject_index_max_chars: int = 1000   # 索引层字符预算（0 = 关闭索引层）
    inject_key_prefix_quota: int = 3     # 同一 key 前缀（前两段）最多注入条数

    # --- 注入评分权重（三因子） ---
    inject_w_importance: float = 0.5   # importance/10 的权重
    inject_w_recency: float = 0.3      # exp(-age/tau) 的权重
    inject_w_frequency: float = 0.2    # log1p(access_count) 的权重
    inject_recency_tau_days: float = 14.0  # recency 衰减时间常数（天）
    inject_freq_norm_cap: int = 20     # 访问次数归一化上限

    # --- 注入评分第四因子：relevance ---
    inject_w_relevance: float = 0.3   # cwd 项目匹配加分权重
    inject_project_aliases: dict = field(default_factory=dict)  # 目录名 → key 段

    # --- 自动遗忘 ---
    forget_auto_run_hours: int = 24  # SessionStart 自动遗忘的最小间隔（小时）

    # --- 近重复合并 ---
    # 口径说明：similarity = (1+cos)/2（非原始余弦）；0.92 ≈ 真实余弦 0.84，
    # 真实合并建议 threshold ≥ 0.97（≈ cos 0.94）
    consolidate_similarity_threshold: float = 0.92  # 近重复合并的相似度阈值

    # --- 安全 ---
    stop_hook_safe: bool = True  # 防止 Stop Hook 无限循环
    value_max_chars: int = 500  # memory_add/replace 的 value 长度硬上限

    def ensure_dirs(self) -> None:
        """Ensure data directory and model directory exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "models").mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_file(cls, path: Path | None = None) -> "Config":
        """Load config from config.json; missing fields use defaults."""
        config = cls()
        config.ensure_dirs()
        load_path = path or config.config_path
        if load_path.exists():
            with open(load_path, encoding="utf-8") as f:
                data = json.load(f)
            for key, value in data.items():
                if hasattr(config, key):
                    setattr(config, key, value)
        return config

    def save(self) -> None:
        """Save configuration to config.json."""
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
            "embedding_query_prefix": self.embedding_query_prefix,
            "embedding_doc_prefix": self.embedding_doc_prefix,
            "inject_max_count": self.inject_max_count,
            "inject_max_chars": self.inject_max_chars,
            "inject_pinned_max_count": self.inject_pinned_max_count,
            "inject_pinned_max_chars": self.inject_pinned_max_chars,
            "inject_index_max_chars": self.inject_index_max_chars,
            "inject_key_prefix_quota": self.inject_key_prefix_quota,
            "inject_w_importance": self.inject_w_importance,
            "inject_w_recency": self.inject_w_recency,
            "inject_w_frequency": self.inject_w_frequency,
            "inject_recency_tau_days": self.inject_recency_tau_days,
            "inject_freq_norm_cap": self.inject_freq_norm_cap,
            "inject_w_relevance": self.inject_w_relevance,
            "inject_project_aliases": self.inject_project_aliases,
            "forget_auto_run_hours": self.forget_auto_run_hours,
            "consolidate_similarity_threshold": self.consolidate_similarity_threshold,
            "stop_hook_safe": self.stop_hook_safe,
            "value_max_chars": self.value_max_chars,
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

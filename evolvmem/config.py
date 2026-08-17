"""Configuration management: data directory, model path, retrieval and forgetting thresholds."""

from dataclasses import dataclass, field
from pathlib import Path
import json
import os

from evolvmem.runtime_contract import (
    DEFAULT_EMBEDDING_CONTRACT,
    known_embedding_contract,
)


@dataclass
class Config:
    """Global configuration, loaded from config.json or using defaults."""

    # Data directory
    data_dir: Path = Path.home() / ".claude" / "evolvmem"

    def __post_init__(self) -> None:
        """Honour EVOLVMEM_DATA_DIR for all construction paths.

        DSH 薄壳通过该环境变量显式指向共享库；未设置时保持 Claude 侧默认
        路径不变，两侧零影响。
        """
        env_dir = os.environ.get("EVOLVMEM_DATA_DIR")
        if env_dir:
            self.data_dir = Path(env_dir).expanduser()

    # SQLite 数据库路径
    @property
    def db_path(self) -> Path:
        return self.data_dir / "memory.db"

    # USearch 向量索引路径
    @property
    def vector_path(self) -> Path:
        return self.data_dir / "vectors.usearch"

    @property
    def context_vector_path(self) -> Path:
        return self.data_dir / "context_vectors.usearch"

    # GGUF 模型路径
    @property
    def model_path(self) -> Path:
        filename = self.embedding_model_filename
        if not self._is_safe_model_filename(filename):
            return self.data_dir / "models"
        return self.data_dir / "models" / filename

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
    embedding_model_filename: str = DEFAULT_EMBEDDING_CONTRACT.filename
    embedding_dim: int = DEFAULT_EMBEDDING_CONTRACT.dimension
    # nomic-embed-text-v1.5 任务前缀；置空字符串可关闭
    embedding_query_prefix: str = DEFAULT_EMBEDDING_CONTRACT.query_prefix
    embedding_doc_prefix: str = DEFAULT_EMBEDDING_CONTRACT.document_prefix

    # --- Context Core（尚未接入现有检索层）---
    context_l0_max_chars: int = 240
    context_l1_max_chars: int = 1200
    context_l2_max_chars: int = 6000

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

    # --- 最近项目动态层（会话摘要日志） ---
    digest_days: int = 30          # 项目动态层回溯天数（按日志 key 中的日期过滤）
    digest_per_project: int = 2    # 每项目最多展示的日志条数
    digest_max_chars: int = 800    # 项目动态层字符预算（0 = 关闭该层）

    # --- 自动遗忘 ---
    forget_auto_run_hours: int = 24  # SessionStart 自动遗忘的最小间隔（小时）

    # --- 近重复合并 ---
    # 口径说明：similarity = (1+cos)/2（非原始余弦）；0.92 ≈ 真实余弦 0.84，
    # 真实合并建议 threshold ≥ 0.97（≈ cos 0.94）
    consolidate_similarity_threshold: float = 0.92  # 近重复合并的相似度阈值
    consolidate_auto_run_hours: int = 168  # SessionStart 自动合并的最小间隔（小时），0=关闭

    # --- 写入侧语义合并 ---
    add_merge_threshold: float = 0.95  # 写入侧语义合并阈值（≥即 supersede 而非新增）

    # --- 安全 ---
    stop_hook_safe: bool = True  # 防止 Stop Hook 无限循环
    value_max_chars: int = 500  # memory_add/replace 的 value 长度硬上限
    value_min_chars: int = 10  # memory_add/replace 的 value 长度下限（低于视为无信息）

    def ensure_dirs(self) -> None:
        """Ensure data directory and model directory exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "models").mkdir(parents=True, exist_ok=True)

    def validate_runtime(self, *, require_model: bool = False) -> tuple[str, ...]:
        """Return safe, user-actionable embedding/runtime diagnostics.

        Validation intentionally does not exit: MCP startup remains able to serve
        SQLite FTS even while the optional embedding model is unavailable.
        """
        diagnostics: list[str] = []
        filename = self.embedding_model_filename
        filename_is_safe = self._is_safe_model_filename(filename)
        if not filename_is_safe:
            diagnostics.append(
                "embedding_model_filename must be a non-empty filename without path separators"
            )
        if not isinstance(self.embedding_dim, int) or self.embedding_dim <= 0:
            diagnostics.append("embedding_dim must be a positive integer")

        known = known_embedding_contract(filename) if filename_is_safe else None
        if known is not None and self.embedding_dim != known.dimension:
            diagnostics.append(
                f"embedding_dim for '{filename}' must be {known.dimension}; "
                f"configured {self.embedding_dim}"
            )

        layer_limits = (
            self.context_l0_max_chars,
            self.context_l1_max_chars,
            self.context_l2_max_chars,
        )
        if any(not isinstance(limit, int) or limit <= 0 for limit in layer_limits):
            diagnostics.append(
                "context_l0_max_chars, context_l1_max_chars, and "
                "context_l2_max_chars must be positive integers"
            )
        elif not self.context_l0_max_chars <= self.context_l1_max_chars <= self.context_l2_max_chars:
            diagnostics.append(
                "context layer limits must satisfy context_l0_max_chars <= "
                "context_l1_max_chars <= context_l2_max_chars"
            )

        if require_model and filename_is_safe and not self.model_path.is_file():
            diagnostics.append(
                f"Model file not found: embedding_model_filename '{filename}' "
                "is missing from the configured models directory"
            )
        return tuple(diagnostics)

    @staticmethod
    def _is_safe_model_filename(filename: object) -> bool:
        """Accept a filename only when it cannot escape the models directory."""
        return (
            isinstance(filename, str)
            and bool(filename.strip())
            and Path(filename).name == filename
            and "\\" not in filename
        )

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
            "embedding_model_filename": self.embedding_model_filename,
            "embedding_dim": self.embedding_dim,
            "embedding_query_prefix": self.embedding_query_prefix,
            "embedding_doc_prefix": self.embedding_doc_prefix,
            "context_l0_max_chars": self.context_l0_max_chars,
            "context_l1_max_chars": self.context_l1_max_chars,
            "context_l2_max_chars": self.context_l2_max_chars,
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
            "digest_days": self.digest_days,
            "digest_per_project": self.digest_per_project,
            "digest_max_chars": self.digest_max_chars,
            "forget_auto_run_hours": self.forget_auto_run_hours,
            "consolidate_similarity_threshold": self.consolidate_similarity_threshold,
            "consolidate_auto_run_hours": self.consolidate_auto_run_hours,
            "add_merge_threshold": self.add_merge_threshold,
            "stop_hook_safe": self.stop_hook_safe,
            "value_max_chars": self.value_max_chars,
            "value_min_chars": self.value_min_chars,
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

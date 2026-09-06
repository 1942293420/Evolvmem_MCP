"""Configuration management: data directory, model path, retrieval and forgetting thresholds."""

from dataclasses import dataclass, field
from pathlib import Path
import json
import math
import os
import stat
import tempfile

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
        self._apply_context_environment()

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

    # --- Context Core 读取与注入（独立于旧 fts_*/vector_*/inject_* 参数）---
    context_mode: str = "legacy"   # legacy|compat|shadow|primary；未知值 fail-closed
    adapter: str = ""              # 当前适配器标识（codex/claude/kimi/dsh/web），空=未指定
    context_inject_max_chars: int = 6000         # Context 注入总字符预算
    context_inject_max_items: int = 12           # Context 注入最大条目数
    context_inject_pinned_max_chars: int = 1500  # pinned 池字符预算
    context_inject_project_max_chars: int = 3000  # project 池字符预算
    context_inject_related_max_chars: int = 1500  # related 池字符预算
    context_min_confidence: float = 0.55         # 注入/检索最低置信度
    context_vector_min_similarity: float = 0.80  # 纯向量候选最低归一化相似度
    context_fts_weight: float = 0.60    # 词法通道融合权重
    context_vector_weight: float = 0.40  # 向量通道融合权重
    context_score_relevance_weight: float = 0.35
    context_score_project_weight: float = 0.15
    context_score_type_weight: float = 0.10
    context_score_confidence_weight: float = 0.10
    context_score_importance_weight: float = 0.10
    context_score_evidence_weight: float = 0.05
    context_score_recency_weight: float = 0.10
    context_score_frequency_weight: float = 0.05
    context_recency_tau_days: float = 30.0  # recency 衰减时间常数（天）
    context_frequency_cap: int = 20         # 访问次数归一化上限
    context_project_aliases: dict = field(default_factory=dict)  # 工作区名 → 项目名
    context_archive_ttl_days: int = 30      # 原始会话加密归档的保留天数
    context_session_summary_ttl_days: int = 30  # 会话摘要条目的 TTL（到期且被滚动摘要覆盖后才归档）
    context_session_summary_keep: int = 10  # 每项目保留的最近会话摘要条数（超出且被覆盖才归档）

    # --- Context Core 晋升阈值（设计「晋升规则」冻结默认值） ---
    context_promotion_min_successes: int = 2        # 自动晋升所需的不同 archive 成功证据数
    context_playbook_min_experiences: int = 3       # 生成 Playbook 资格簇的最小经验数
    context_promotion_similarity_threshold: float = 0.95  # 资格簇 L0 归一化相似度阈值

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
        dimension_is_valid = self._is_positive_int(self.embedding_dim)
        if not dimension_is_valid:
            diagnostics.append("embedding_dim must be a positive integer")

        known = known_embedding_contract(filename) if filename_is_safe else None
        if known is not None and dimension_is_valid and self.embedding_dim != known.dimension:
            diagnostics.append(
                f"embedding_dim for '{filename}' must be {known.dimension}; "
                f"configured {self.embedding_dim}"
            )

        layer_limits = (
            self.context_l0_max_chars,
            self.context_l1_max_chars,
            self.context_l2_max_chars,
        )
        if any(not self._is_positive_int(limit) for limit in layer_limits):
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
        diagnostics.extend(self._validate_context_config())
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

    @staticmethod
    def _is_positive_int(value: object) -> bool:
        """Reject booleans even though Python models them as integers."""
        return type(value) is int and value > 0

    @staticmethod
    def _is_unit_interval(value: object) -> bool:
        return (
            type(value) in (int, float)
            and math.isfinite(value)
            and 0.0 <= value <= 1.0
        )

    _CONTEXT_MODE_VALUES = ("legacy", "compat", "shadow", "primary")

    def _apply_context_environment(self) -> None:
        """EVOLVMEM_CONTEXT_MODE / EVOLVMEM_ADAPTER override the persisted JSON.

        Unknown values are stored verbatim so validate_runtime() reports a
        structured diagnostic; they are never coerced to primary.
        """
        env_mode = os.environ.get("EVOLVMEM_CONTEXT_MODE")
        if env_mode and env_mode.strip():
            self.context_mode = env_mode.strip()
        env_adapter = os.environ.get("EVOLVMEM_ADAPTER")
        if env_adapter and env_adapter.strip():
            self.adapter = env_adapter.strip()

    def _validate_context_config(self) -> list[str]:
        """Validate the independent Context Core retrieval/injection settings."""
        diagnostics: list[str] = []
        if self.context_mode not in self._CONTEXT_MODE_VALUES:
            diagnostics.append(
                "context_mode must be one of 'legacy', 'compat', 'shadow', 'primary'"
            )
        for name in (
            "context_inject_max_chars",
            "context_inject_max_items",
            "context_inject_pinned_max_chars",
            "context_inject_project_max_chars",
            "context_inject_related_max_chars",
            "context_frequency_cap",
            "context_archive_ttl_days",
            "context_session_summary_ttl_days",
            "context_session_summary_keep",
            "context_promotion_min_successes",
            "context_playbook_min_experiences",
        ):
            if not self._is_positive_int(getattr(self, name)):
                diagnostics.append(f"{name} must be a positive integer")
        for name in (
            "context_min_confidence",
            "context_vector_min_similarity",
            "context_promotion_similarity_threshold",
        ):
            if not self._is_unit_interval(getattr(self, name)):
                diagnostics.append(f"{name} must be a finite number between 0 and 1")
        tau = self.context_recency_tau_days
        if type(tau) not in (int, float) or not math.isfinite(tau) or tau <= 0:
            diagnostics.append("context_recency_tau_days must be a positive finite number")

        retrieval_weights = (self.context_fts_weight, self.context_vector_weight)
        if any(not self._is_unit_interval(weight) for weight in retrieval_weights):
            diagnostics.append(
                "context_fts_weight and context_vector_weight must be "
                "finite numbers between 0 and 1"
            )
        elif abs(sum(retrieval_weights) - 1.0) > 1e-9:
            diagnostics.append("context_fts_weight + context_vector_weight must equal 1.0")

        score_weight_names = (
            "context_score_relevance_weight",
            "context_score_project_weight",
            "context_score_type_weight",
            "context_score_confidence_weight",
            "context_score_importance_weight",
            "context_score_evidence_weight",
            "context_score_recency_weight",
            "context_score_frequency_weight",
        )
        score_weights = tuple(getattr(self, name) for name in score_weight_names)
        invalid_score_weights = [
            name
            for name, weight in zip(score_weight_names, score_weights)
            if not self._is_unit_interval(weight)
        ]
        if invalid_score_weights:
            diagnostics.append(
                "context score weights must be finite numbers between 0 and 1: "
                + ", ".join(invalid_score_weights)
            )
        elif abs(sum(score_weights) - 1.0) > 1e-9:
            diagnostics.append("context_score_*_weight values must sum to 1.0")
        return diagnostics

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
        config._apply_context_environment()
        return config

    def save(self) -> None:
        """Save configuration to config.json atomically.

        The JSON payload goes to a uniquely named sibling temp file that is
        flushed and fsynced, then os.replace()d over config_path with the
        previous mode bits preserved; the parent directory is fsynced so the
        rename itself is durable. On failure only the exact temp file is
        removed, leaving the previous JSON byte-for-byte intact.
        """
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
            "context_mode": self.context_mode,
            "adapter": self.adapter,
            "context_inject_max_chars": self.context_inject_max_chars,
            "context_inject_max_items": self.context_inject_max_items,
            "context_inject_pinned_max_chars": self.context_inject_pinned_max_chars,
            "context_inject_project_max_chars": self.context_inject_project_max_chars,
            "context_inject_related_max_chars": self.context_inject_related_max_chars,
            "context_min_confidence": self.context_min_confidence,
            "context_vector_min_similarity": self.context_vector_min_similarity,
            "context_fts_weight": self.context_fts_weight,
            "context_vector_weight": self.context_vector_weight,
            "context_score_relevance_weight": self.context_score_relevance_weight,
            "context_score_project_weight": self.context_score_project_weight,
            "context_score_type_weight": self.context_score_type_weight,
            "context_score_confidence_weight": self.context_score_confidence_weight,
            "context_score_importance_weight": self.context_score_importance_weight,
            "context_score_evidence_weight": self.context_score_evidence_weight,
            "context_score_recency_weight": self.context_score_recency_weight,
            "context_score_frequency_weight": self.context_score_frequency_weight,
            "context_recency_tau_days": self.context_recency_tau_days,
            "context_frequency_cap": self.context_frequency_cap,
            "context_project_aliases": self.context_project_aliases,
            "context_archive_ttl_days": self.context_archive_ttl_days,
            "context_session_summary_ttl_days": self.context_session_summary_ttl_days,
            "context_session_summary_keep": self.context_session_summary_keep,
            "context_promotion_min_successes": self.context_promotion_min_successes,
            "context_playbook_min_experiences": self.context_playbook_min_experiences,
            "context_promotion_similarity_threshold": self.context_promotion_similarity_threshold,
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
        config_path = self.config_path
        fd, temp_name = tempfile.mkstemp(
            dir=str(config_path.parent),
            prefix=f".{config_path.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            if config_path.exists():
                os.chmod(temp_path, stat.S_IMODE(config_path.stat().st_mode))
            os.replace(temp_path, config_path)
            dir_fd = os.open(str(config_path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            raise

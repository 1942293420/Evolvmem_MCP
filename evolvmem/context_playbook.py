"""LLM-driven playbook generation from eligible experience clusters (P3).

Frozen deterministic rules (pinned by tests, adjustable only via config):

- Degradation: a missing LLM yields an empty report with ``llm_unavailable``;
  a missing/broken embedding engine surfaces P2's ``embedding_unavailable``.
  Neither is an error, and neither writes anything.
- Dedup: when any non-deleted playbook's ``experience`` source set fully
  covers a cluster's experience id set, the cluster is skipped with
  ``already_covered`` and the LLM is never called for it.
- Update semantics: an existing *candidate* playbook whose sources overlap
  the cluster partially is superseded by the new candidate (bidirectional
  links; the successor points at the lowest superseded id). ``active``
  playbooks are never auto-superseded — replacement there is a user-confirmed
  path only. Superseded/archived playbooks are left alone.
- Prompt assembly: the prompt carries only the cluster members' L0/L1 text
  plus the output contract. L2 and raw session material are never sent.
- Output gates (design's safety gates extended to all three layers): JSON
  parse failure, a missing/non-string/empty layer, sensitive content,
  low-information content, or a layer over its configured character budget
  skips the cluster with a stable reason — and changes nothing.
- Success: one transaction creates the candidate playbook (confidence = the
  cluster minimum, importance = the cluster maximum, scope/project = the
  cluster's) and links every member through ``record_experience_source``.
  Any write failure rolls the whole transaction back: experiences and old
  playbooks keep their exact prior state.

Logs and reports carry ids and reason codes only — never prompt text, layer
content, or backend exception messages.
"""

from dataclasses import dataclass
import json
import logging
import re

from evolvmem.config import Config
from evolvmem.context_layers import normalize_content
from evolvmem.context_lifecycle import ContextLifecycle, PlaybookCluster
from evolvmem.context_models import (
    ContextContentType,
    ContextItem,
    ContextItemDraft,
    ContextLayers,
    ContextScope,
    ContextStatus,
    ContextTier,
    ContextValidationError,
)
from evolvmem.context_store import ContextStore
from evolvmem.extraction_policy import contains_cjk, contains_sensitive_text

logger = logging.getLogger(__name__)

_GENERATOR = "playbook-gen-v1"
_EXTRACTION_VERSION = "playbook-gen-v1"

_REASON_OK = "ok"
_REASON_EMBEDDING_UNAVAILABLE = "embedding_unavailable"
_REASON_LLM_UNAVAILABLE = "llm_unavailable"
_GENERATION_REASONS = frozenset(
    {_REASON_OK, _REASON_EMBEDDING_UNAVAILABLE, _REASON_LLM_UNAVAILABLE}
)
_DEGRADED_REASONS = frozenset({_REASON_EMBEDDING_UNAVAILABLE, _REASON_LLM_UNAVAILABLE})

_SKIP_ALREADY_COVERED = "already_covered"
_SKIP_LLM_NO_RESPONSE = "llm_no_response"
_SKIP_INVALID_JSON = "invalid_json"
_SKIP_SENSITIVE_CONTENT = "sensitive_content"
_SKIP_LOW_INFORMATION = "low_information"
_SKIP_LAYER_TOO_LONG = "layer_too_long"
_SKIP_REASONS = frozenset(
    {
        _SKIP_ALREADY_COVERED,
        _SKIP_LLM_NO_RESPONSE,
        _SKIP_INVALID_JSON,
        _SKIP_SENSITIVE_CONTENT,
        _SKIP_LOW_INFORMATION,
        _SKIP_LAYER_TOO_LONG,
    }
)

_LAYER_NAMES = ("l0", "l1", "l2")

# Mirror sanitize_summary's informativeness notion: after removing redaction
# markers and punctuation/whitespace, substantive Chinese text must remain.
_REDACTION_MARKER_RE = re.compile(
    r"\[已脱敏:(?:private_key|url凭据|token|api_key|password|凭据位置)\]"
)
_LOW_INFO_STRIP_RE = re.compile(r"[\s，。；：、.!?！？]+")


@dataclass(frozen=True, slots=True)
class PlaybookSkip:
    """One cluster left untouched, with the stable reason code."""

    item_ids: tuple[int, ...]
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "item_ids", _normalize_id_tuple(self.item_ids, "item_ids")
        )
        if self.reason not in _SKIP_REASONS:
            raise ContextValidationError(
                "reason must be one of " + ", ".join(sorted(_SKIP_REASONS))
            )


@dataclass(frozen=True, slots=True)
class PlaybookGenerationReport:
    """Generation outcome: created candidate ids plus per-cluster skips."""

    created_ids: tuple[int, ...]
    skipped: tuple[PlaybookSkip, ...]
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "created_ids",
            _normalize_id_tuple(self.created_ids, "created_ids"),
        )
        try:
            skipped = tuple(self.skipped)
        except TypeError as exc:
            raise ContextValidationError(
                "skipped must be an iterable of PlaybookSkip"
            ) from exc
        if any(not isinstance(skip, PlaybookSkip) for skip in skipped):
            raise ContextValidationError(
                "skipped must be an iterable of PlaybookSkip"
            )
        object.__setattr__(self, "skipped", skipped)
        if self.reason not in _GENERATION_REASONS:
            raise ContextValidationError(
                "reason must be one of " + ", ".join(sorted(_GENERATION_REASONS))
            )
        if self.reason in _DEGRADED_REASONS and (self.created_ids or self.skipped):
            raise ContextValidationError(
                "degraded reports must not claim creations or cluster outcomes"
            )


@dataclass(frozen=True, slots=True)
class _CoverageEntry:
    """One non-deleted playbook's experience-source footprint."""

    item_id: int
    status: ContextStatus
    supersedes: int | None
    experience_ids: frozenset


class PlaybookGenerator:
    """Generates or updates candidate playbooks from eligible clusters."""

    def __init__(
        self,
        config: Config,
        store: ContextStore,
        lifecycle: ContextLifecycle,
        *,
        llm=None,
        embedding_engine=None,
    ) -> None:
        if not isinstance(config, Config):
            raise ContextValidationError("config must be a Config instance")
        if not isinstance(store, ContextStore):
            raise ContextValidationError("store must be a ContextStore instance")
        if not isinstance(lifecycle, ContextLifecycle):
            raise ContextValidationError(
                "lifecycle must be a ContextLifecycle instance"
            )
        if llm is not None and not callable(llm):
            raise ContextValidationError("llm must be a callable or None")
        self._config = config
        self._store = store
        self._lifecycle = lifecycle
        self._llm = llm
        self._embedding_engine = embedding_engine

    def generate(self) -> PlaybookGenerationReport:
        """Generate candidate playbooks for every eligible, uncovered cluster."""
        if self._llm is None:
            logger.warning("playbook generation skipped: llm unavailable")
            return PlaybookGenerationReport(
                created_ids=(), skipped=(), reason=_REASON_LLM_UNAVAILABLE
            )
        eligibility = self._lifecycle.evaluate_playbook_eligibility(
            embedding_engine=self._embedding_engine
        )
        if eligibility.reason != _REASON_OK:
            logger.warning("playbook generation skipped: embedding unavailable")
            return PlaybookGenerationReport(
                created_ids=(), skipped=(), reason=_REASON_EMBEDDING_UNAVAILABLE
            )

        coverage = self._playbook_coverage()
        created: list[int] = []
        skipped: list[PlaybookSkip] = []
        for cluster in eligibility.clusters:
            outcome = self._generate_for_cluster(cluster, coverage)
            if isinstance(outcome, PlaybookSkip):
                skipped.append(outcome)
            else:
                created.append(outcome)
        return PlaybookGenerationReport(
            created_ids=tuple(created), skipped=tuple(skipped), reason=_REASON_OK
        )

    # ---- per-cluster pipeline ----

    def _generate_for_cluster(
        self, cluster: PlaybookCluster, coverage: list[_CoverageEntry]
    ) -> int | PlaybookSkip:
        member_ids = tuple(sorted(cluster.item_ids))
        member_set = frozenset(member_ids)
        if any(entry.experience_ids >= member_set for entry in coverage):
            logger.warning("playbook generation skipped: %s", _SKIP_ALREADY_COVERED)
            return PlaybookSkip(item_ids=member_ids, reason=_SKIP_ALREADY_COVERED)
        stale_candidates = [
            entry
            for entry in coverage
            if entry.status is ContextStatus.CANDIDATE
            and entry.experience_ids & member_set
        ]

        members = tuple(self._load_member(item_id) for item_id in member_ids)
        prompt = self._build_prompt(cluster, members)
        response = self._call_llm(prompt)
        if response is None:
            logger.warning("playbook generation skipped: %s", _SKIP_LLM_NO_RESPONSE)
            return PlaybookSkip(item_ids=member_ids, reason=_SKIP_LLM_NO_RESPONSE)
        gate_reason, layers = self._gate_output(response)
        if gate_reason is not None:
            logger.warning("playbook generation skipped: %s", gate_reason)
            return PlaybookSkip(item_ids=member_ids, reason=gate_reason)

        draft = ContextItemDraft(
            identity_key=_cluster_identity_key(cluster),
            content_type=ContextContentType.PLAYBOOK,
            layers=ContextLayers(
                l0=layers["l0"],
                l1=layers["l1"],
                l2=layers["l2"],
                generator=_GENERATOR,
            ),
            project=cluster.project,
            scope=cluster.scope,
            status=ContextStatus.CANDIDATE,
            tier=ContextTier.NORMAL,
            tags=(),
            importance=max(member.importance for member in members),
            confidence=min(member.confidence for member in members),
            supersedes=stale_candidates[0].item_id if stale_candidates else None,
        )
        store = self._store
        structured = []
        for member_id in member_ids:
            row = store._connection().execute(
                'SELECT experience_payload FROM context_items WHERE id=?', (member_id,)
            ).fetchone()
            if row and row[0]:
                structured.append(json.loads(row[0]))
        payload = None
        if structured:
            shared = {k:v for k,v in structured[0]['conditions'].items()
                      if all(c['conditions'].get(k)==v for c in structured)}
            payload = json.dumps(dict(
                project=cluster.project,problem=layers['l0'],conditions=shared,
                steps=[layers['l1'][i:i+500] for i in range(0,len(layers['l1']),500)],
                rationale='根据有来源的案例归纳；共同机制与步骤仍须在自然任务独立验证。',
                result='',applicability=['来源案例共同条件'],
                exclusions=list(dict.fromkeys(v for c in structured for v in c['exclusions']))[:15],
                transferable=all(c['transferable'] for c in structured),parent_experience_id=None),
                ensure_ascii=False,sort_keys=True,separators=(',',':'))
            if len(payload) > self._config.context_l2_max_chars:
                return PlaybookSkip(item_ids=member_ids,reason=_SKIP_LAYER_TOO_LONG)
        with store.transaction():
            item = store.create_item(draft)
            if payload is not None:
                store._connection().execute(
                    'UPDATE context_items SET experience_payload=? WHERE id=?', (payload,item.id))
            for experience_id in member_ids:
                store.record_experience_source(
                    item.id, experience_id, extraction_version=_EXTRACTION_VERSION
                )
            for entry in stale_candidates:
                store.set_item_status(entry.item_id, ContextStatus.SUPERSEDED)
                store.set_supersession_links(
                    entry.item_id,
                    supersedes=entry.supersedes,
                    superseded_by=item.id,
                )
        return item.id

    # ---- prompt, LLM call, and output gates ----

    def _build_prompt(
        self, cluster: PlaybookCluster, members: tuple[ContextItem, ...]
    ) -> str:
        """Assemble the prompt from member L0/L1 only — never L2 or raw text."""
        config = self._config
        lines = [
            "你是研发经验巩固助手。以下是同一主题下多条已验证的工程经验"
            "（只含摘要与细节，不含原始会话）。",
            "请将它们提炼成一份可复用的 Playbook。",
            "先核对问题机制、目标、环境和约束。相似报错不代表同根因；若机制不一致请返回空对象。"
            "只归纳来源明确支持的共同步骤，保留成功条件、例外和待验证假设；不得扩大验证范围。"
            "本次归纳是待验证的方法建议，回放旧案例不算新成功。",
            "",
        ]
        if cluster.scope is ContextScope.GLOBAL:
            lines.append("适用范围: 全局")
        else:
            lines.append(f"适用项目: {cluster.project}")
        lines.append("")
        for index, member in enumerate(members, start=1):
            lines.append(f"经验 {index} 摘要: {member.layers.l0}")
            lines.append(f"经验 {index} 细节: {member.layers.l1}")
            lines.append("")
        lines.extend(
            [
                '只返回一个 JSON 对象：{"l0": "...", "l1": "...", "l2": "..."}，'
                "不要输出任何其他文本。",
                f"l0 为一句话要点，不超过 {config.context_l0_max_chars} 字；",
                f"l1 为步骤与适用条件，不超过 {config.context_l1_max_chars} 字；",
                f"l2 为完整细节、证据与反例，不超过 {config.context_l2_max_chars} 字；",
                "全部使用中文；不得包含密钥、token、密码等任何敏感信息。",
            ]
        )
        return "\n".join(lines)

    def _call_llm(self, prompt: str) -> str | None:
        """Invoke the narrow LLM callable; any failure degrades to None."""
        try:
            response = self._llm(prompt)
        except Exception:
            logger.warning("playbook generation llm call failed")
            return None
        return response if isinstance(response, str) else None

    def _gate_output(self, response: str) -> tuple[str | None, dict | None]:
        """Apply the frozen output gates; returns (skip_reason, layers)."""
        try:
            payload = json.loads(response.strip())
        except ValueError:
            return _SKIP_INVALID_JSON, None
        if not isinstance(payload, dict):
            return _SKIP_INVALID_JSON, None
        layers: dict[str, str] = {}
        for name in _LAYER_NAMES:
            raw = payload.get(name)
            if not isinstance(raw, str):
                return _SKIP_INVALID_JSON, None
            content = normalize_content(raw)
            if not content:
                return _SKIP_INVALID_JSON, None
            layers[name] = content
        for name in _LAYER_NAMES:
            if contains_sensitive_text(layers[name]):
                return _SKIP_SENSITIVE_CONTENT, None
        for name in _LAYER_NAMES:
            if _is_low_information(layers[name]):
                return _SKIP_LOW_INFORMATION, None
        limits = {
            "l0": self._config.context_l0_max_chars,
            "l1": self._config.context_l1_max_chars,
            "l2": self._config.context_l2_max_chars,
        }
        for name in _LAYER_NAMES:
            if len(layers[name]) > limits[name]:
                return _SKIP_LAYER_TOO_LONG, None
        return None, layers

    # ---- coverage and member loading ----

    def _playbook_coverage(self) -> list[_CoverageEntry]:
        """Experience-source footprints of every non-deleted playbook."""
        store = self._store
        entries: list[_CoverageEntry] = []
        for item_id in store.list_item_ids(content_type=ContextContentType.PLAYBOOK):
            item = store.get_item(item_id, include_layers=False)
            if item is None or item.status is ContextStatus.DELETED:
                continue
            experience_ids = set()
            for row in store.list_item_sources(item_id):
                if row["source_kind"] != "experience":
                    continue
                try:
                    experience_ids.add(int(row["source_ref"]))
                except (TypeError, ValueError):
                    continue
            entries.append(
                _CoverageEntry(
                    item_id=item.id,
                    status=item.status,
                    supersedes=item.supersedes,
                    experience_ids=frozenset(experience_ids),
                )
            )
        return entries

    def _load_member(self, item_id: int) -> ContextItem:
        item = self._store.get_item(item_id)
        if item is None or item.layers is None:  # pragma: no cover
            raise RuntimeError("eligible cluster member could not be reloaded")
        return item


def _cluster_identity_key(cluster: PlaybookCluster) -> str:
    """Deterministic identity derived from the cluster's member id set."""
    label = cluster.project if cluster.scope is ContextScope.PROJECT else "global"
    ids = "-".join(str(item_id) for item_id in sorted(cluster.item_ids))
    return f"playbook:{label}:exp-{ids}"


def _is_low_information(text: str) -> bool:
    informative = _REDACTION_MARKER_RE.sub("", text)
    informative = _LOW_INFO_STRIP_RE.sub("", informative)
    return not informative or not contains_cjk(informative)


def _normalize_id_tuple(value: object, field_name: str) -> tuple[int, ...]:
    try:
        ids = tuple(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ContextValidationError(
            f"{field_name} must be an iterable of positive integers"
        ) from exc
    for item_id in ids:
        if type(item_id) is not int or item_id <= 0:
            raise ContextValidationError(
                f"{field_name} must be an iterable of positive integers"
            )
    return ids

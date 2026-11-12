"""Configuration-driven model reputation tiers, session quotas, and rolling acceptance adjustment.

Strict invariants (Tasks 1.3 & 1.4):
- Zero hardcoded usernames anywhere in policy or coordinator logic.
- Tier 'user' is exclusively granted by server-authenticated human channels
  (TrustedChannel.WEB_HUMAN or TrustedChannel.EXPLICIT_USER_INTENT); MCP clientInfo
  or CLI hook flags can NEVER escalate to 'user'.
- Unknown (agent_client, model) pairs default to tier='general', quota_per_session=2.
- Rolling acceptance rate (< 30% over >= min_evaluated_samples) suspends quota to 0
  and emits alert='low_acceptance_rate'; (> 80%) marks boost_eligible and boosts quota.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .agent_identity import AgentAttribution, normalize_client_slug, normalize_model_name

VALID_AGENT_TIERS = frozenset({'primary', 'general', 'judge'})


class TrustedChannel(str, Enum):
    WEB_HUMAN = 'web_human'
    EXPLICIT_USER_INTENT = 'explicit_user_intent'
    MCP_CLIENT = 'mcp_client'
    CLI_HOOK = 'cli_hook'


class ProposalOperation(str, Enum):
    ADD = 'add'
    UPDATE = 'update'
    RETIRE = 'retire'
    DELETE = 'delete'
    VERDICT = 'verdict'


_ALLOWED_OPS_BY_TIER: dict[str, frozenset[ProposalOperation]] = {
    'user': frozenset({
        ProposalOperation.ADD,
        ProposalOperation.UPDATE,
        ProposalOperation.RETIRE,
        ProposalOperation.DELETE,
        ProposalOperation.VERDICT,
    }),
    'primary': frozenset({
        ProposalOperation.ADD,
        ProposalOperation.UPDATE,
        ProposalOperation.RETIRE,
    }),
    'general': frozenset({
        ProposalOperation.ADD,
    }),
    'judge': frozenset({
        ProposalOperation.VERDICT,
    }),
}


@dataclass(frozen=True)
class ReputationPolicyConfig:
    model_tiers: dict[tuple[str, str], tuple[str, int]] = field(default_factory=dict)
    default_tier: str = 'general'
    default_quota_per_session: int = 2
    min_evaluated_samples: int = 10
    low_acceptance_threshold: float = 0.30
    high_acceptance_threshold: float = 0.80
    boost_bonus_quota: int = 2
    high_importance_review_threshold: float = 0.85
    trusted_elevated_devices: frozenset[str] | None = None

    def __post_init__(self) -> None:
        if self.default_tier not in VALID_AGENT_TIERS:
            raise ValueError(f'invalid default agent tier (user tier reserved): {self.default_tier!r}')
        if self.default_quota_per_session < 0:
            raise ValueError('default_quota_per_session must be >= 0')
        normalized: dict[tuple[str, str], tuple[str, int]] = {}
        for (client, model), (tier, quota) in self.model_tiers.items():
            if tier not in VALID_AGENT_TIERS:
                raise ValueError(f'agent/model policy cannot assign reserved or unknown tier: {tier!r}')
            if int(quota) < 0:
                raise ValueError('quota_per_session must be >= 0')
            c_norm = normalize_client_slug(client)
            m_norm = normalize_model_name(model)
            normalized[(c_norm, m_norm)] = (tier, int(quota))
        object.__setattr__(self, 'model_tiers', normalized)


@dataclass(frozen=True, slots=True)
class ModelReputationSnapshot:
    agent_client: str
    model: str
    tier: str
    base_quota: int
    proposed: int
    accepted: int
    rejected: int
    acceptance_rate: float | None
    effective_quota: int
    boost_eligible: bool
    alert: str | None = None


@dataclass(frozen=True, slots=True)
class OperationDecision:
    allowed: bool
    actor: str
    tier: str
    effective_quota: int | None
    requires_user_review: bool = False
    reason: str = 'allowed'
    alert: str | None = None


class ReputationCoordinator:
    """PostgreSQL-backed model reputation and operation permission gate."""

    def __init__(self, store, *, config: ReputationPolicyConfig | None = None) -> None:
        self._store = store
        self._config = config or ReputationPolicyConfig()

    @property
    def config(self) -> ReputationPolicyConfig:
        return self._config

    def sync_configured_policies(self) -> None:
        for (client, model), (tier, quota) in self._config.model_tiers.items():
            self._store.mutate(
                'SELECT * FROM em_control.upsert_model_reputation_policy(%s,%s,%s,%s)',
                (client, model, tier, quota),
            )

    def _default_for(self, client: str, model: str) -> tuple[str, int]:
        return self._config.model_tiers.get(
            (client, model),
            (self._config.default_tier, self._config.default_quota_per_session),
        )

    def _build_snapshot(
        self,
        *,
        client: str,
        model: str,
        tier: str,
        base_quota: int,
        proposed: int,
        accepted: int,
        rejected: int,
    ) -> ModelReputationSnapshot:
        evaluated = accepted + rejected
        rate: float | None = (accepted / evaluated) if evaluated > 0 else None
        effective = base_quota
        boost = False
        alert: str | None = None
        if evaluated >= self._config.min_evaluated_samples and rate is not None:
            if rate < self._config.low_acceptance_threshold:
                effective = 0
                alert = 'low_acceptance_rate'
            elif rate > self._config.high_acceptance_threshold:
                boost = True
                if base_quota > 0:
                    effective = base_quota + self._config.boost_bonus_quota
        return ModelReputationSnapshot(
            agent_client=client,
            model=model,
            tier=tier,
            base_quota=base_quota,
            proposed=proposed,
            accepted=accepted,
            rejected=rejected,
            acceptance_rate=rate,
            effective_quota=effective,
            boost_eligible=boost,
            alert=alert,
        )

    def get_reputation(self, agent_client: str, model: str) -> ModelReputationSnapshot:
        c_norm = normalize_client_slug(agent_client)
        m_norm = normalize_model_name(model)
        rows = self._store.query('SELECT * FROM em_control.get_model_reputation(%s,%s)', (c_norm, m_norm))
        if not rows:
            tier, quota = self._default_for(c_norm, m_norm)
            return self._build_snapshot(
                client=c_norm, model=m_norm, tier=tier, base_quota=quota, proposed=0, accepted=0, rejected=0
            )
        r = rows[0]
        return self._build_snapshot(
            client=r['agent_client'],
            model=r['model'],
            tier=r['tier'],
            base_quota=int(r['quota_per_session']),
            proposed=int(r['proposed']),
            accepted=int(r['accepted']),
            rejected=int(r['rejected']),
        )

    def record_outcomes(
        self,
        agent_client: str,
        model: str,
        *,
        proposed: int = 0,
        accepted: int = 0,
        rejected: int = 0,
    ) -> ModelReputationSnapshot:
        c_norm = normalize_client_slug(agent_client)
        m_norm = normalize_model_name(model)
        def_tier, def_quota = self._default_for(c_norm, m_norm)
        rows = self._store.mutate(
            'SELECT * FROM em_control.record_model_reputation_event(%s,%s,%s,%s,%s,%s,%s)',
            (c_norm, m_norm, def_tier, def_quota, int(proposed), int(accepted), int(rejected)),
        )
        r = rows[0]
        return self._build_snapshot(
            client=r['agent_client'],
            model=r['model'],
            tier=r['tier'],
            base_quota=int(r['quota_per_session']),
            proposed=int(r['proposed']),
            accepted=int(r['accepted']),
            rejected=int(r['rejected']),
        )

    def authorize_operation(
        self,
        attribution: AgentAttribution,
        operation: ProposalOperation,
        *,
        channel: TrustedChannel = TrustedChannel.MCP_CLIENT,
        session_proposed_count: int = 0,
        target_pinned: bool = False,
        target_importance: float = 0.0,
    ) -> OperationDecision:
        if not isinstance(attribution, AgentAttribution) or not attribution.actor:
            raise ValueError('verified AgentAttribution required')

        # Only server-authenticated human channels ever receive 'user' fast-lane tier.
        if channel in (TrustedChannel.WEB_HUMAN, TrustedChannel.EXPLICIT_USER_INTENT):
            return OperationDecision(
                allowed=True,
                actor=attribution.actor,
                tier='user',
                effective_quota=None,
                requires_user_review=False,
                reason='allowed',
            )

        snap = self.get_reputation(attribution.client, attribution.model)
        # Anti-spoofing clamp: tampered clientInfo keys OR untrusted device when device allowlist
        # is active cannot claim elevated agent tiers ('primary' / 'judge') and clamp to default general.
        device_untrusted = (
            self._config.trusted_elevated_devices is not None
            and attribution.device_id not in self._config.trusted_elevated_devices
        )
        if attribution.spoof_attempt_stripped or (device_untrusted and snap.tier != self._config.default_tier):
            snap = self._build_snapshot(
                client=snap.agent_client,
                model=snap.model,
                tier=self._config.default_tier,
                base_quota=min(snap.base_quota, self._config.default_quota_per_session),
                proposed=snap.proposed,
                accepted=snap.accepted,
                rejected=snap.rejected,
            )
        allowed_ops = _ALLOWED_OPS_BY_TIER.get(snap.tier, frozenset())
        if operation not in allowed_ops:
            return OperationDecision(
                allowed=False,
                actor=attribution.actor,
                tier=snap.tier,
                effective_quota=snap.effective_quota,
                reason='operation_forbidden_for_tier',
                alert=snap.alert,
            )

        if operation == ProposalOperation.VERDICT:
            return OperationDecision(
                allowed=True,
                actor=attribution.actor,
                tier=snap.tier,
                effective_quota=snap.effective_quota,
                reason='allowed',
                alert=snap.alert,
            )

        high_sensitivity = target_pinned or (target_importance >= self._config.high_importance_review_threshold)
        if snap.tier == 'general' and high_sensitivity:
            return OperationDecision(
                allowed=False,
                actor=attribution.actor,
                tier=snap.tier,
                effective_quota=snap.effective_quota,
                reason='pinned_or_high_importance_forbidden',
                alert=snap.alert,
            )

        if snap.alert == 'low_acceptance_rate' and snap.effective_quota == 0:
            return OperationDecision(
                allowed=False,
                actor=attribution.actor,
                tier=snap.tier,
                effective_quota=0,
                reason='reputation_suspended_low_acceptance',
                alert=snap.alert,
            )

        if session_proposed_count >= snap.effective_quota:
            return OperationDecision(
                allowed=False,
                actor=attribution.actor,
                tier=snap.tier,
                effective_quota=snap.effective_quota,
                reason='session_quota_exceeded',
                alert=snap.alert,
            )

        return OperationDecision(
            allowed=True,
            actor=attribution.actor,
            tier=snap.tier,
            effective_quota=snap.effective_quota,
            requires_user_review=(snap.tier == 'primary' and high_sensitivity),
            reason='allowed',
            alert=snap.alert,
        )

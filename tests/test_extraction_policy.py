import importlib.util
import copy

import pytest

from evolvmem import extraction_policy as policy
from evolvmem.auto_extractor import CandidateMemory


def candidate(value: str, **overrides) -> CandidateMemory:
    values = {
        "key": "project:test:fact:item",
        "value": value,
        "attribute": "fact",
        "importance": 5.0,
        "confidence": 0.8,
        "tier": "normal",
    }
    values.update(overrides)
    return CandidateMemory(**values)


def test_extraction_policy_module_is_available():
    assert importlib.util.find_spec("evolvmem.extraction_policy") is not None


@pytest.mark.parametrize("content,secret,marker", [
    ("Authorization: Bearer synthetic-token-123456", "synthetic-token-123456", "[已脱敏:token]"),
    ("api_key = sk-synthetic-abcdefghijklmnop", "sk-synthetic-abcdefghijklmnop", "[已脱敏:api_key]"),
    ("access_token: synthetic-access-123456", "synthetic-access-123456", "[已脱敏:token]"),
    ("refresh token 是 synthetic-refresh-123456", "synthetic-refresh-123456", "[已脱敏:token]"),
    ("password set to Synthetic-Pass-123!", "Synthetic-Pass-123!", "[已脱敏:password]"),
    ("密码为 合成口令-仅测试-123", "合成口令-仅测试-123", "[已脱敏:password]"),
    ("postgres://synthetic_user:synthetic_pass@example.invalid/db", "synthetic_pass", "[已脱敏:url凭据]"),
    ("密码保存在 /tmp/synthetic-credential.txt", "/tmp/synthetic-credential.txt", "[已脱敏:凭据位置]"),
])
def test_redact_messages_covers_credential_forms(content, secret, marker):
    original = [{"role": "user", "content": content}]
    snapshot = copy.deepcopy(original)

    redacted, count = policy.redact_messages(original)

    assert count >= 1
    assert secret not in redacted[0]["content"]
    assert marker in redacted[0]["content"]
    assert original == snapshot
    assert redacted is not original
    assert redacted[0] is not original[0]


@pytest.mark.parametrize("content,secrets,marker", [
    (
        '{"password":"Synthetic JSON Value"}',
        ("Synthetic JSON Value",),
        "[已脱敏:password]",
    ),
    (
        "OPENAI_API_KEY='sk-Synthetic Namespaced Value'",
        ("sk-Synthetic Namespaced Value",),
        "[已脱敏:api_key]",
    ),
    (
        'DATABASE_PASSWORD="Synthetic Database Value"',
        ("Synthetic Database Value",),
        "[已脱敏:password]",
    ),
    (
        "密钥为 合成短钥",
        ("合成短钥",),
        "[已脱敏:api_key]",
    ),
    (
        "pwd=z9",
        ("z9",),
        "[已脱敏:password]",
    ),
    (
        "The credential is stored in /tmp/synthetic-credential-location.txt",
        ("/tmp/synthetic-credential-location.txt",),
        "[已脱敏:凭据位置]",
    ),
    (
        "https://synthetic-user:synthetic-pass@example.invalid/private",
        ("synthetic-user", "synthetic-pass"),
        "[已脱敏:url凭据]",
    ),
])
def test_redact_messages_covers_adversarial_structured_credentials(
        content, secrets, marker):
    redacted, count = policy.redact_messages([
        {"role": "user", "content": content},
    ])

    assert count == 1
    assert marker in redacted[0]["content"]
    assert all(secret not in redacted[0]["content"] for secret in secrets)


@pytest.mark.parametrize("label", ["passwd", "pwd"])
def test_redact_messages_covers_password_aliases(label):
    secret = "Synthetic-Alias-Value"

    redacted, count = policy.redact_messages([
        {"role": "user", "content": f"{label}: {secret}"},
    ])

    assert count == 1
    assert secret not in redacted[0]["content"]
    assert "[已脱敏:password]" in redacted[0]["content"]


def test_redact_messages_removes_incomplete_private_key_to_eof():
    content = (
        "保留前文。\n-----BEGIN PRIVATE KEY-----\n"
        "SYNTHETIC_UNFINISHED_KEY_DATA\nSYNTHETIC_TRAILING_KEY_DATA"
    )

    redacted, count = policy.redact_messages([
        {"role": "user", "content": content},
    ])

    assert count == 1
    assert "SYNTHETIC_UNFINISHED_KEY_DATA" not in redacted[0]["content"]
    assert "SYNTHETIC_TRAILING_KEY_DATA" not in redacted[0]["content"]
    assert redacted[0]["content"].startswith("保留前文。")


def test_redaction_marker_is_not_reprocessed_or_double_counted():
    token = "eyJhbGciOiJIUzI1NiJ9.c3ludGhldGlj.c2lnbmF0dXJl"

    redacted, count = policy.redact_messages([
        {"role": "user", "content": f"token={token}"},
    ])
    second_pass, second_count = policy.redact_messages(redacted)

    assert count == 1
    assert redacted[0]["content"].count("[已脱敏:token]") == 1
    assert token not in redacted[0]["content"]
    assert second_count == 0
    assert second_pass == redacted


def test_redact_messages_removes_private_key_block_and_jwt():
    content = (
        "-----BEGIN PRIVATE KEY-----\nSYNTHETICKEYDATA\n-----END PRIVATE KEY-----\n"
        "jwt=eyJhbGciOiJIUzI1NiJ9.c3ludGhldGlj.c2lnbmF0dXJl"
    )
    redacted, count = policy.redact_messages([{"role": "user", "content": content}])
    value = redacted[0]["content"]
    assert count == 2
    assert "SYNTHETICKEYDATA" not in value
    assert "eyJhbGci" not in value


def test_password_policy_sentence_is_not_a_secret():
    text = "密码策略要求至少 12 位，并开启多因素认证。"
    assert policy.contains_sensitive_text(text) is False
    redacted, count = policy.redact_messages([{"role": "user", "content": text}])
    assert count == 0
    assert redacted[0]["content"] == text


@pytest.mark.parametrize("text", [
    "password is required for local accounts",
    "API key is required by the policy",
    "token is enabled only after approval",
])
def test_credential_policy_predicate_is_not_a_secret_assignment(text):
    assert policy.contains_sensitive_text(text) is False
    redacted, count = policy.redact_messages([
        {"role": "user", "content": text},
    ])
    assert count == 0
    assert redacted[0]["content"] == text


def test_sanitize_summary_redacts_local_secret_and_requires_chinese():
    summary, count = policy.sanitize_summary(
        "确定采用新架构，password: Synthetic-Pass-123!，并保留回滚路径。"
    )
    assert count == 1
    assert summary is not None
    assert "Synthetic-Pass-123!" not in summary
    assert policy.contains_cjk(summary)


@pytest.mark.parametrize("value", [
    "",
    "only English summary",
    "[已脱敏:password]",
    "password: Synthetic-Pass-Only-123!",
])
def test_sanitize_summary_rejects_empty_low_information_or_non_chinese(value):
    summary, _ = policy.sanitize_summary(value)
    assert summary is None


@pytest.mark.parametrize("item,reason", [
    (candidate("临时密码为 Synthetic-Pass-123!"), "sensitive"),
    (candidate("Waiting for user confirmation"), "language"),
    (candidate("等待用户稍后确认输入。"), "ephemeral"),
    (candidate("本次 184 个测试全部通过。"), "ephemeral"),
    (candidate("本次部署已经完成。"), "ephemeral"),
    (candidate("本次部署已经完成。", attribute="decision"), "ephemeral"),
    (candidate("提交 commit abcdef123456 已完成。"), "ephemeral"),
])
def test_evaluate_candidate_rejects_unsafe_or_ephemeral_items(item, reason):
    assert policy.evaluate_candidate(item).reason == reason
    assert policy.evaluate_candidate(item).accepted is False


def test_evaluate_candidate_keeps_durable_broker_constraint():
    item = candidate(
        "Broker 完成初始化前禁止任何写操作，这是持续有效的安全约束。",
        attribute="constraint",
        importance=10,
        tier="pinned",
    )
    assert policy.evaluate_candidate(item).accepted is True


def test_evaluate_candidate_keeps_password_policy_without_password_value():
    item = candidate(
        "密码策略要求至少 12 位并启用多因素认证。",
        attribute="constraint",
    )
    assert policy.evaluate_candidate(item).accepted is True


@pytest.mark.parametrize("value", [
    "本次 184 个测试全部通过，这是长期规则。",
    "本次部署已经完成，因为这是重要决定。",
    "提交 commit abcdef123456 已完成，这是永久约束。",
])
def test_durable_keywords_do_not_decorate_a_one_off_claim(value):
    decision = policy.evaluate_candidate(candidate(value))

    assert decision == policy.PolicyDecision(False, "ephemeral")


def test_one_off_claim_requires_a_separate_substantive_ongoing_clause():
    item = candidate(
        "本次部署已经完成；后续部署必须先验证回滚路径，防止配置漂移。",
        attribute="constraint",
    )

    assert policy.evaluate_candidate(item).accepted is True


@pytest.mark.parametrize("item,reason", [
    (candidate("这是长期有效的中文事实。", key="not-a-stable-key"), "metadata"),
    (candidate("这是长期有效的中文事实。", attribute="chat"), "metadata"),
    (candidate("这是长期有效的中文事实。", tier="forever"), "metadata"),
    (candidate("这是长期有效的中文事实。", tags=["tag"] * 9), "metadata"),
    (candidate("这是长期有效的中文事实。", tags=["分类:test,split"]), "metadata"),
    (candidate("这是长期有效的中文事实。", importance=float("nan")), "metadata"),
    (candidate("会话继续，后续处理。"), "low_information"),
    (candidate("这是事实。", confidence=0.2), "confidence"),
    (candidate("甲" * 501), "length"),
])
def test_evaluate_candidate_applies_all_deterministic_persistability_gates(
        item, reason):
    decision = policy.evaluate_candidate(item)

    assert decision == policy.PolicyDecision(False, reason)


@pytest.mark.parametrize("item", [
    candidate(
        "这是长期有效的中文事实。",
        key='project:test:fact:password="Synthetic Key Secret"',
    ),
    candidate(
        "这是长期有效的中文事实。",
        tags=['api_key="Synthetic Tag Secret"'],
    ),
])
def test_evaluate_candidate_screens_all_provider_metadata_strings(item):
    decision = policy.evaluate_candidate(item)

    assert decision == policy.PolicyDecision(False, "sensitive")


def test_rank_candidates_deduplicates_same_key_by_quality_tuple():
    low = candidate("采用方案甲。", key="project:x:decision:api", confidence=0.7)
    high = candidate(
        "采用方案乙，因为它避免重复写入。",
        key="PROJECT:X:DECISION:API",
        confidence=0.9,
        importance=8,
    )
    assert policy.rank_candidates([low, high]) == [high]


def test_rank_candidates_promotes_pinned_even_when_model_puts_it_last():
    ordinary = [
        candidate(
            f"第 {index} 条长期架构决定保留原因。",
            key=f"project:x:decision:{index}",
            importance=9,
            confidence=0.9,
        )
        for index in range(9)
    ]
    pinned = candidate(
        "用户长期偏好使用中文沟通。",
        key="user:preference:communication:language",
        attribute="preference",
        tier="pinned",
        importance=7,
    )
    ranked = policy.rank_candidates([*ordinary, pinned], limit=8)
    assert ranked[0] is pinned
    assert len(ranked) == 8


def test_rank_candidates_uses_importance_confidence_then_original_order():
    first = candidate("保留第一项架构决定。", key="a", importance=7, confidence=0.8)
    second = candidate("保留第二项架构决定。", key="b", importance=8, confidence=0.6)
    third = candidate("保留第三项架构决定。", key="c", importance=7, confidence=0.8)
    assert policy.rank_candidates([first, second, third]) == [second, first, third]


def test_rank_candidates_keeps_earlier_candidate_on_identical_quality_tie():
    earlier = candidate(
        "保留较早出现的长期架构决定。",
        key="project:x:decision:api",
        importance=8,
        confidence=0.9,
    )
    later = candidate(
        "不应替换较早候选的同质量决定。",
        key=" PROJECT:X:DECISION:API ",
        importance=8,
        confidence=0.9,
    )

    assert policy.rank_candidates([earlier, later]) == [earlier]

import importlib.util
import copy

import pytest

from evolvmem import extraction_policy as policy


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


def test_sanitize_summary_redacts_local_secret_and_requires_chinese():
    summary, count = policy.sanitize_summary(
        "确定采用新架构，password: Synthetic-Pass-123!，并保留回滚路径。"
    )
    assert count == 1
    assert summary is not None
    assert "Synthetic-Pass-123!" not in summary
    assert policy.contains_cjk(summary)


@pytest.mark.parametrize("value", ["", "only English summary", "[已脱敏:password]"])
def test_sanitize_summary_rejects_empty_low_information_or_non_chinese(value):
    summary, _ = policy.sanitize_summary(value)
    assert summary is None

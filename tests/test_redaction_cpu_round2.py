"""Synthetic regression coverage for URL-free redaction CPU use."""
import base64
import random
import time

import pytest

from evolvmem import extraction_policy as policy


def test_url_free_large_message_avoids_repeated_failed_scheme_scan():
    # Removing the URL-free fast path reintroduces the measured CPU regression.
    text = base64.b64encode(random.Random(4701).randbytes(4 * 1024 * 1024)).decode()
    assert "://" not in text
    start = time.process_time()
    baseline = policy._URL_CREDENTIAL_RE.sub("[synthetic]", text)
    baseline_cpu = time.process_time() - start
    assert baseline == text
    start = time.process_time()
    result = policy.redact_messages([{"role": "user", "content": text}])
    optimized_cpu = time.process_time() - start
    assert result == ([{"role": "user", "content": text}], 0)
    assert optimized_cpu < baseline_cpu * 0.5, (optimized_cpu, baseline_cpu)


@pytest.mark.parametrize("content", [
    "Abcdef" * 40,
    "://",
    "https://example.invalid/path",
    "HTTPS://example.invalid/path",
    "İſK://example.invalid/path",
])
def test_url_literal_check_preserves_noncredential_content(content):
    messages = [{"role": "user", "content": content}]
    assert policy.redact_messages(messages) == ([{"role": "user", "content": content}], 0)
    assert messages == [{"role": "user", "content": content}]

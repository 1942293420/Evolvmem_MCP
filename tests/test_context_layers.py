"""Behavioral contracts for deterministic L0/L1/L2 construction."""

import pytest

from evolvmem.config import Config
from evolvmem.context_layers import (
    layers_from_legacy_value,
    normalize_content,
    validate_layers,
)
from evolvmem.context_models import ContextContentType, ContextLayers, ContextValidationError


def _config(**limits):
    config = Config()
    config.context_l0_max_chars = limits.get("l0", 20)
    config.context_l1_max_chars = limits.get("l1", 40)
    config.context_l2_max_chars = limits.get("l2", 80)
    return config


def test_normalize_content_preserves_interior_newlines_while_normalizing_outer_space():
    """Flattening paragraphs would lose meaningful source formatting."""
    assert normalize_content(" \r\n 第一行\r\n\r\n第二行 \t") == "第一行\n\n第二行"


def test_legacy_conversion_preserves_lone_carriage_returns_in_l2():
    """Changing non-CRLF source bytes would corrupt migrated legacy evidence."""
    layers = layers_from_legacy_value(
        "  Alpha\rBeta\r\nGamma\rDelta  ",
        content_type=ContextContentType.FACT,
        config=_config(l0=40, l1=40, l2=40),
    )

    assert layers.l2 == "Alpha\rBeta\nGamma\rDelta"


@pytest.mark.parametrize(
    "layers, limit_name",
    [
        (ContextLayers("x" * 21, "description", "full source", "user"), "l0"),
        (ContextLayers("summary", "x" * 41, "full source", "user"), "l1"),
        (ContextLayers("summary", "description", "x" * 81, "user"), "l2"),
    ],
)
def test_new_layers_reject_each_configured_size_overflow(layers, limit_name):
    """Accepting an over-budget layer would violate the retrieval budget."""
    with pytest.raises(ContextValidationError, match=limit_name):
        validate_layers(layers, _config())


def test_new_layers_reject_a_canonically_over_budget_layer():
    """Trimming outer whitespace must not hide content that exceeds its stored budget."""
    layers = ContextLayers(" \r\n" + "x" * 21 + " \t", "description", "full source", "user")

    with pytest.raises(ContextValidationError, match="l0"):
        validate_layers(layers, _config())


@pytest.mark.parametrize(
    "layers",
    [
        ContextLayers("", "", "full source", "user"),
        ContextLayers("summary", "", "", "user"),
    ],
)
def test_new_layers_reject_partial_layer_objects(layers):
    """L0-only or L2-only writes cannot serve all retrieval paths."""
    with pytest.raises(ContextValidationError, match="l[012]"):
        validate_layers(layers, _config())


def test_legacy_conversion_preserves_full_source_and_derives_bounded_layers():
    """Migration must retain original evidence while making it indexable."""
    source = "  Prefer explicit configuration. This keeps deployments reproducible.\nDetails follow.  "

    layers = layers_from_legacy_value(
        source, content_type=ContextContentType.PREFERENCE, config=_config(l0=24, l1=40, l2=50)
    )

    assert layers.l2 == "Prefer explicit configuration. This keeps deployments reproducible.\nDetails follow."
    assert layers.l0 == "Prefer explicit configu…"
    assert len(layers.l0) <= 24
    assert len(layers.l1) <= 40
    assert layers.l1.endswith("…")
    assert layers.generator == "migrated"
    validate_layers(layers, _config(l0=24, l1=40, l2=50), allow_legacy_overflow=True)


def test_legacy_conversion_handles_chinese_text_deterministically():
    """Sentence derivation must not depend on an English-only tokenizer."""
    layers = layers_from_legacy_value(
        "先运行测试。再修改实现以保持可验证性。",
        content_type=ContextContentType.PLAYBOOK,
        config=_config(l0=18, l1=30, l2=80),
    )

    assert layers.l0 == "playbook: 先运行测试。"
    assert layers.l1 == "先运行测试。再修改实现以保持可验证性。"


def test_legacy_conversion_rejects_whitespace_only_source():
    """A blank legacy record cannot produce any useful retrieval layer."""
    with pytest.raises(ContextValidationError, match="content"):
        layers_from_legacy_value(" \r\n\t ", content_type=ContextContentType.FACT, config=_config())


def test_legacy_conversion_keeps_exact_limit_and_truncates_one_character_overflow():
    """Off-by-one truncation would produce inconsistent layer budgets."""
    exact = layers_from_legacy_value(
        "12345", content_type=ContextContentType.FACT, config=_config(l0=5, l1=5, l2=5)
    )
    overflow = layers_from_legacy_value(
        "123456", content_type=ContextContentType.FACT, config=_config(l0=5, l1=5, l2=5)
    )

    assert exact.l0 == "12345"
    assert exact.l1 == "12345"
    assert overflow.l0 == "1234…"
    assert overflow.l1 == "1234…"
    assert overflow.l2 == "123456"

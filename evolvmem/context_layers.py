"""Deterministic construction and validation of L0/L1/L2 context layers."""

from evolvmem.config import Config
from evolvmem.context_models import ContextContentType, ContextLayers, ContextValidationError


def normalize_content(value: str) -> str:
    """Canonicalize line endings and outer whitespace without flattening content."""
    if not isinstance(value, str):
        raise ContextValidationError("content must be a string")
    return value.replace("\r\n", "\n").strip()


def validate_layers(
    layers: ContextLayers, config: Config, *, allow_legacy_overflow: bool = False
) -> None:
    """Validate a complete layer object against the configured character budgets."""
    if not isinstance(layers, ContextLayers):
        raise ContextValidationError("layers must be a ContextLayers instance")
    limits = {
        "l0": config.context_l0_max_chars,
        "l1": config.context_l1_max_chars,
        "l2": config.context_l2_max_chars,
    }
    for name, limit in limits.items():
        if type(limit) is not int or limit <= 0:
            raise ContextValidationError(f"{name} maximum must be a positive integer")
        content = normalize_content(getattr(layers, name))
        if not content:
            raise ContextValidationError(f"{name} must not be empty")
        if name != "l2" or not allow_legacy_overflow:
            if len(content) > limit:
                raise ContextValidationError(f"{name} exceeds its configured maximum")


def layers_from_legacy_value(
    value: str, *, content_type: ContextContentType, config: Config
) -> ContextLayers:
    """Build migration-safe retrieval layers without discarding legacy source text."""
    source = normalize_content(value)
    if not source:
        raise ContextValidationError("content must not be empty")
    for name, limit in (
        ("l0", config.context_l0_max_chars),
        ("l1", config.context_l1_max_chars),
    ):
        if type(limit) is not int or limit <= 0:
            raise ContextValidationError(f"{name} maximum must be a positive integer")

    first = _first_meaningful_sentence_or_line(source)
    l0 = _derive_l0(first, content_type, config.context_l0_max_chars)
    l1 = _truncate(source, config.context_l1_max_chars)
    layers = ContextLayers(l0=l0, l1=l1, l2=source, generator="migrated")
    validate_layers(layers, config, allow_legacy_overflow=True)
    return layers


def _first_meaningful_sentence_or_line(value: str) -> str:
    start = 0
    while start < len(value):
        end = value.find("\n", start)
        line = value[start:] if end == -1 else value[start:end]
        line = line.strip()
        if line:
            for index, char in enumerate(line):
                if char in ".!?。！？":
                    return line[:index + 1]
            return line
        if end == -1:
            break
        start = end + 1
    raise ContextValidationError("content must not be empty")


def _derive_l0(first: str, content_type: ContextContentType, maximum: int) -> str:
    cue = f"{content_type.value.replace('_', ' ')}: "
    if len(cue) + len(first) <= maximum:
        return cue + first
    return _truncate(first, maximum)


def _truncate(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    if maximum == 1:
        return "…"
    return value[:maximum - 1].rstrip() + "…"

"""A weak neighbor needs evidence for an explicitly named English subject.

This is an additional check only when neither lexical nor the original vector
gate supports admission. Strong semantic matches may still translate names.
"""
import re

_GRAMMAR = frozenset('How What Which When Where Why Should Would Could Can Does Do Is Are For The This That We You Please'.lower().split())


def supports_named_subject(query, text):
    terms = []
    for match in re.finditer(r'(?<![A-Za-z0-9_])[A-Z][A-Za-z0-9_-]{2,}', query):
        word = match.group()
        if word.lower() in _GRAMMAR or (match.start() == 0 and not word.isupper()):
            continue
        terms.append(word.casefold())
    content = text.casefold()
    return not terms or any(term in content for term in terms)

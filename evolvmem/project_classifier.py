"""Deterministic project classification from written standards.

This is the fallback that runs after the model-based ownership reasoning has
declined to decide a unit. Where that pass needs an LLM and answers "I am not
sure", this pass applies the operator's written project standards and returns a
project only when the evidence is strong enough to justify it.

Design constraints, each learned from the corpus:

* A rule's ``keywords`` are strong evidence; ``weak_keywords`` are shared words
  that nominate but never decide.
* Unknown beats wrong. The full pipeline treats a wrong project as worse than an
  unresolved one, so this module refuses to answer on thin evidence.
* Corrections are layered over the authored standards, so an operator's mistake
  fix applies to every later unit without editing the standards file.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

STANDARDS_FILE = 'project-standards.json'
CORRECTIONS_FILE = 'project-corrections.json'

ENABLED_KEY = 'project_standards_enabled'

# Only this tier may decide a unit automatically.
CONFIDENCE_HIGH = 'high'


def _rules_path(data_dir):
    return Path(data_dir) / STANDARDS_FILE


def _corrections_path(data_dir):
    return Path(data_dir) / CORRECTIONS_FILE


def load_standards(data_dir):
    """Read authored standards and layer the correction ledger on top.

    Both files are optional: a missing standards file means the feature is off,
    and a malformed one is logged and ignored rather than breaking capture.
    """
    path = _rules_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        logger.warning('project standards unreadable: %s', type(error).__name__)
        return {}
    projects = raw.get('projects') if isinstance(raw, dict) else None
    if not isinstance(projects, dict):
        return {}
    merged = {}
    for name, rule in projects.items():
        if not isinstance(rule, dict):
            continue
        merged[name] = {
            'keywords': [str(k) for k in rule.get('keywords') or []],
            'weak_keywords': [str(k) for k in rule.get('weak_keywords') or []],
            'system_hints': [str(k) for k in rule.get('system_hints') or []],
            'symptoms': [str(k) for k in rule.get('symptoms') or []],
            'exclude': [str(k) for k in rule.get('exclude') or []],
            'exclude_to': dict(rule.get('exclude_to') or {}),
            'min_hits': int(rule.get('min_hits') or 1),
            'priority': int(rule.get('priority') or 10),
        }
    try:
        ledger = json.loads(_corrections_path(data_dir).read_text(encoding='utf-8'))
    except (FileNotFoundError, OSError, ValueError):
        return merged

    for entry in (ledger.get('corrections') if isinstance(ledger, dict) else None) or []:
        if not isinstance(entry, dict):
            continue
        project = entry.get('project')
        pattern = [str(p) for p in entry.get('pattern') or [] if str(p).strip()]
        if project not in merged or not pattern:
            continue
        rule = merged[project]
        kind = entry.get('kind')
        if kind == 'demote':
            for term in pattern:
                for field in ('keywords', 'system_hints', 'symptoms'):
                    rule[field] = [t for t in rule[field] if t != term]
                if term not in rule['weak_keywords']:
                    rule['weak_keywords'].append(term)
        elif kind == 'exclude':
            for term in pattern:
                if term not in rule['exclude']:
                    rule['exclude'].append(term)
                target = entry.get('target_project')
                if target:
                    rule['exclude_to'][term] = target
    return merged


class StandardsClassifier:
    """Score unit text against written standards; answer only when confident."""

    def __init__(self, standards, *, margin=5):
        self.margin = margin
        self._rules = []
        for project, rule in sorted(standards.items()):
            strong = [t for t in rule['keywords'] if t.strip()]
            hints = [t for t in rule['system_hints'] if t.strip()]
            if not strong and not hints:
                continue
            self._rules.append({
                'project': project,
                'keywords': [t.lower() for t in strong],
                'weak': [t.lower() for t in rule['weak_keywords']],
                'hints': [t.lower() for t in hints],
                'symptoms': [t.lower() for t in rule['symptoms']],
                'exclude': [t.lower() for t in rule['exclude']],
                'exclude_to': {str(k).lower(): v for k, v in rule['exclude_to'].items()},
                'min_hits': rule['min_hits'],
                'priority': rule['priority'],
            })

    def classify(self, text):
        """Return ``(project, confidence, evidence)``; project is '' when unsure."""
        if not text or not self._rules:
            return '', '', []
        haystack = ' '.join(str(text).lower().split())
        scored = []
        for rule in self._rules:
            if any(term in haystack for term in rule['exclude']):
                continue
            keyword_hits = [t for t in rule['keywords'] if t in haystack]
            hint_hits = [t for t in rule['hints'] if t in haystack]
            symptom_hits = [t for t in rule['symptoms'] if t in haystack]
            hits = list(dict.fromkeys(keyword_hits + hint_hits + symptom_hits))
            if len(hits) < rule['min_hits']:
                continue
            weak_hits = [t for t in rule['weak'] if t in haystack]
            # Hints and symptoms pin a project harder than a bare keyword.
            strong_weight = (len(set(keyword_hits) - set(hint_hits) - set(symptom_hits))
                             + 2 * len(set(hint_hits)) + 2 * len(set(symptom_hits)))
            weak_weight = len(set(weak_hits) - set(hits))
            score = (strong_weight + 0.5 * weak_weight) * rule['priority']
            scored.append({
                'project': rule['project'],
                'score': score,
                'evidence': hits[:5],
                'diversity': (1 if hint_hits else 0) + (1 if symptom_hits else 0),
                'keywords': len(set(keyword_hits) - set(hint_hits) - set(symptom_hits)),
                'weak_only': bool(weak_hits) and not hits,
            })
        if not scored:
            return '', '', []
        scored.sort(key=lambda row: (-row['score'], row['project']))
        top = scored[0]
        runner_up = scored[1] if len(scored) > 1 else None
        margin = top['score'] - runner_up['score'] if runner_up else None
        if margin is not None and margin < self.margin:
            return '', 'low', top['evidence']
        if top['weak_only']:
            return '', 'low', top['evidence']
        # Corroboration: a hint, a symptom, or at least two distinct keywords.
        corroborated = top['diversity'] >= 1 or top['keywords'] >= 2
        value = min(1.0, 0.3 + 0.06 * top['score'] + 0.1 * top['diversity'])
        if value >= 0.75 and corroborated:
            return top['project'], CONFIDENCE_HIGH, top['evidence']
        if value >= 0.5:
            return '', 'medium', top['evidence']
        return '', 'low', top['evidence']


_CACHE = {}


def classifier_for(data_dir):
    """Build (and cache) one classifier per data directory.

    The standards file is read once per worker run: re-reading it for every unit
    would dominate the cost of assignment, and an operator edit takes effect on
    the next worker start, which matches how the knowledge rules already behave.
    """
    key = str(data_dir)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    standards = load_standards(data_dir)
    classifier = StandardsClassifier(standards) if standards else None
    _CACHE[key] = classifier
    return classifier


def enabled(config):
    """Whether the standards fallback is active for this deployment."""
    flag = os.environ.get('EVOLVMEM_PROJECT_STANDARDS', '').strip().lower()
    if flag in ('0', 'false', 'off', 'no'):
        return False
    return bool(load_standards(getattr(config, 'data_dir', '')))


def decide(config, text):
    """Return ``(project, evidence)`` for one unit; ``('', [])`` when unsure.

    Never raises: this runs inside the organizer, where a surprising exception
    would fail an entire task. A disabled or unreadable standards file simply
    means no opinion.
    """
    try:
        if not enabled(config):
            return '', []
        classifier = classifier_for(config.data_dir)
        if classifier is None:
            return '', []
        project, confidence, evidence = classifier.classify(text)
        if project and confidence == CONFIDENCE_HIGH:
            return project, evidence
        return '', []
    except Exception as error:  # pragma: no cover - defensive boundary
        logger.warning('project standards decision failed: %s', type(error).__name__)
        return '', []

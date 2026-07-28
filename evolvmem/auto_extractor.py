"""Auto memory extraction — analyzes conversations, extracts candidate memories worth persisting."""

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CandidateMemory:
    """Candidate memory — information extracted from conversation that may be persisted."""
    key: str
    value: str
    category: str = "fact"
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.5
    importance: float = 5.0
    tier: str = "normal"


class AutoExtractor:
    """Automatic memory extractor.

    Orchestrates Claude through prompts to review conversations and produce candidate memories.
    Actual inference is done by Claude within Claude Code's Stop Hook;
    this module is responsible for building prompts and parsing responses.
    """

    EXTRACTION_PROMPT = """You are a memory management assistant. Review the following conversation and extract information worth persisting.

## Retention Rules
The following should be extracted as memories:
- User explicitly expressed preferences, decisions, constraints
- Project architecture choices, technical decisions and their reasons
- Deprecated old approaches (preserve history, mark as superseded)
- Business logic rules and exceptions
- Important "why" — business reasons behind decisions

## Discard Rules
The following should NOT be extracted:
- Temporary tasks, one-off paths, completed todos
- Casual chat and greetings
- Facts directly obtainable from code/git
- Pure technical implementation details

## Stable Key Format
Use format: `{{project}}:{{domain}}:{{type}}:{{topic}}`
Examples:
- `project:shop:decision:after_sales` — after-sales decision
- `user:preference:communication:language` — language preference
- `project:evolvmem:arch:embedding_model` — architecture choice

## Output Format
Return a JSON array, each entry containing:
- key: stable identifier
- value: memory content — MUST be a single sentence, at most 200 characters. Longer content must be split or condensed.
- category: decision | preference | fact | constraint | user_profile
- tags: list of relevant tags
- confidence: 0.0-1.0 confidence score
- importance: integer 1-10. Guide: 9-10 = hard constraints / make-or-break decisions; 7-8 = important architecture or business decisions; 5-6 = ordinary preferences and facts; 3-4 = marginal reference material
- tier: "pinned" if this memory must be visible in EVERY session (constraints, durable user preferences, user profile); "reference" if it is a long reference document that should only be fetched via memory_search when relevant (never injected); otherwise "normal"

If nothing is worth persisting, return an empty array `[]`.

## Conversation
{conversation}

## Output
Only return the JSON array, no other content:"""

    def build_extraction_prompt(self,
                                messages: list[dict[str, str]]) -> str:
        """Build the extraction prompt."""
        conversation = "\n".join(
            f"[{m.get('role', 'unknown')}]: {m.get('content', '')}"
            for m in messages
        )
        return self.EXTRACTION_PROMPT.format(conversation=conversation)

    def parse_response(self, response_text: str) -> list[CandidateMemory]:
        """Parse the JSON returned by Claude, extract the candidate memory list."""
        # Extract JSON block
        json_match = re.search(
            r'```(?:json)?\s*(\[.*?\])\s*```',
            response_text, re.DOTALL,
        )
        if json_match:
            json_str = json_match.group(1)
        else:
            # Try parsing the entire text directly
            json_str = response_text.strip()

        try:
            items = json.loads(json_str)
            if not isinstance(items, list):
                return []
        except json.JSONDecodeError:
            return []

        candidates = []
        for item in items:
            if not isinstance(item, dict):
                continue
            key = item.get("key", "")
            value = item.get("value", "")
            if not key or not value:
                continue
            try:
                importance = float(item.get("importance", 5.0))
            except (TypeError, ValueError):
                importance = 5.0
            if math.isnan(importance):  # min(10.0, nan) 返回 10.0，必须先拦截
                importance = 5.0
            importance = max(1.0, min(10.0, importance))
            tier = item.get("tier", "normal")
            if tier not in ("pinned", "normal", "reference"):
                tier = "normal"
            candidates.append(CandidateMemory(
                key=key,
                value=value,
                category=item.get("category", "fact"),
                tags=item.get("tags", []),
                confidence=float(item.get("confidence", 0.5)),
                importance=importance,
                tier=tier,
            ))
        return candidates

    def should_persist(self, candidate: CandidateMemory) -> bool:
        """Check whether a candidate memory is worth persisting."""
        # Confidence too low → skip
        if candidate.confidence < 0.3:
            return False
        # Value too long → skip (extraction prompt requires <= 200 chars; hard cap 500)
        if len(candidate.value) > 500:
            return False
        # Casual chat type → skip
        if candidate.category in ("chat", "greeting", "small_talk"):
            return False
        # Key or value too short → skip
        if len(candidate.key) < 5 or len(candidate.value) < 5:
            return False
        return True

    def build_key(self, project: str, domain: str, category: str,
                  topic: str) -> str:
        """Build a standards-compliant stable key."""
        parts = [project, domain, category, topic]
        # Lowercase, replace spaces with underscores, keep only alphanumeric and underscores
        sanitized = []
        for p in parts:
            p = p.lower().strip()
            p = re.sub(r'[^\w一-鿿-]', '_', p)
            p = re.sub(r'_+', '_', p)
            sanitized.append(p.strip('_'))
        return ":".join(sanitized)

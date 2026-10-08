"""Pure policy for recognizing strategic contributor claims in text."""

from __future__ import annotations

import re
from typing import Final

_ClaimPatterns = tuple[re.Pattern[str], ...]


_UNCHECKED_TASK_ITEM_RE: Final = re.compile(r"^\s*[-*+]\s*\[\s\]\s+.*$", re.MULTILINE)


def _patterns(*expressions: str) -> _ClaimPatterns:
    return tuple(re.compile(expression, re.IGNORECASE) for expression in expressions)


_OWNERSHIP_PATTERNS: Final = _patterns(
    r"\bi(?:'d| would) like to (?:work on|take|handle|implement|fix|resolve|pick (?:this|it) up)\b",
    r"^\s*taking (?:this|this one|it)\b",
    r"\bi(?:'d| would) love to (?:work on|take on|handle|implement|fix|resolve)\b",
    r"\bi(?:'m| am) interested in working on\b",
    r"\bi(?:'m| am) (?:taking|working on) (?:this|it|an independent pass)\b",
    r"\bi can (?:take|work on|handle|implement|fix|resolve)\b",
    r"\bmay i take (?:this|the) issue and (?:submit|open|send) "
    r"(?:a |the )?(?:pr|pull request)\b",
    r"\bi(?:'ll| will) (?:take|work on|handle|implement|fix|resolve)\b",
    r"\bi(?:'ll| will) take a look at implementing\b",
    r"\bputting up (?:a |the )?(?:pr|pull request)\b(?=\s*[:.,;—-]|\s+(?:to|with|for)\b|$)",
    r"\bbefore i (?:write|start writing) code\b",
    r"\b(?:please|kindly) assign(?: it| this issue)? to me\b",
    r"\bassign (?:this|it) to me\b",
    r"(?:^\s*claiming this(?:\s+(?:issue|task|dbip))?|\bi(?:'m| am) claiming this"
    r"(?:\s+(?:issue|task|dbip))?|\bi(?:'ll| will) claim this(?:\s+(?:issue|task|dbip))?)"
    r"(?=[ \t]*(?:[.!,:;—-]|$)|\r?\n)",
    r"/attempt\b",
)

_IMPLEMENTATION_EVIDENCE_PATTERNS: Final = _patterns(
    r"\bi(?:'ve| have) implemented\b",
    r"\bi(?:'ve got| have(?: got)?) (?:a |the )?(?:fix|patch)\b",
    r"\bi(?:'ve| have) added (?:unit |e2e |regression )?tests?\b",
    r"\bi(?:'ve| have) tests? ready\b",
    r"\bi(?:'ve| have) (?:this|that|it) working"
    r"(?:\s+locally|\s+with (?:a )?tests?|\s+on (?:a |the )?branch)?\b",
    r"\bi poked at (?:this|it) locally\b[\s\S]{0,800}"
    r"\bi (?:made|moved|added|changed|updated|refactored|implemented)\b",
    r"\bi(?:(?:'ve| have))? prepared (?:a |the )?(?:focused )?"
    r"(?:candidate(?: implementation)?|patch|fix|implementation)\b",
    r"\bi have (?:one|it|this) ready(?: and tested)?\b",
    r"\bi(?:'m| am) (?:currently )?implementing\b",
    r"\bi(?:'m| am) working on (?:a |the )?fix\b",
    r"^\s*(?:currently implementing|working on (?:a |the )?fix)\b",
    r"^\s*planning (?:a |the )?fix\b",
    r"^\s*(?:planning|plan) to (?:fix|work on|implement|handle)\b",
    r"^\s*starting (?:work on|a fix for)\b",
    r"^\s*delivered in pr\b",
    r"^\s*submitted (?:a )?pr\b",
    r"\bpublished (?:a |the )?signed(?:/dco)? branch\b",
)

_PR_INTENT_PATTERNS: Final = _patterns(
    r"\bi(?:'ll| will) open (?:a |the )?(?:pr|pull request)\b",
    r"\bi(?:'d| would) be happy to (?:send|open|submit) (?:a |the )?(?:pr|pull request)\b",
    r"\bi(?:'m| am) willing to contribute (?:a |the )?(?:pr|pull request)\b",
    r"\bi can (?:send|open|submit) (?:a |the )?(?:pr|pull request)\b",
    r"\bi can start (?:working on|preparing) (?:a |the )?(?:pr|pull request)\b",
    r"\bbefore i (?:open|submit) (?:a |the )?(?:pr|pull request)\b",
    r"\bbefore submitting (?:a |the )?(?:pr|pull request)\b",
    r"\bi(?:'m| am) preparing (?:a |the )?(?:pr|pull request)\b",
    r"^\s*preparing (?:a |the )?(?:pr|pull request)\b",
    r"\bwould (?:the )?(?:team|maintainers|you) welcome (?:a |the )?(?:pr|pull request)\b",
    r"\bcan i submit (?:this|it|(?:a |the )?(?:pr|pull request))\b",
)

_FIRST_PERSON_PLAN_PATTERNS: Final = _patterns(
    r"\bmy plan is to\b",
    r"\bi(?:'m| am) going to "
    r"(?:add|change|modify|update|implement|fix|refactor|write|remove|move|introduce)\b",
)


def normalized_claim_text(text: str) -> str:
    """Normalize curly apostrophes so contractions use one recognition path."""
    return text.replace("’", "'").replace("‘", "'")


def _matches_any(text: str, patterns: _ClaimPatterns) -> bool:
    return any(pattern.search(text) is not None for pattern in patterns)


def _takes_ownership(text: str) -> bool:
    return _matches_any(text, _OWNERSHIP_PATTERNS)


def _has_implementation_evidence(text: str) -> bool:
    return _matches_any(text, _IMPLEMENTATION_EVIDENCE_PATTERNS)


def _intends_to_submit_pr(text: str) -> bool:
    return _matches_any(text, _PR_INTENT_PATTERNS)


def _has_concrete_first_person_plan(text: str) -> bool:
    return _matches_any(text, _FIRST_PERSON_PLAN_PATTERNS)


def strategic_claim_text(text: str) -> bool:
    """Return whether text clearly claims ownership or active implementation work."""
    normalized = normalized_claim_text(text)
    active_text = _UNCHECKED_TASK_ITEM_RE.sub("", normalized)
    return (
        _takes_ownership(active_text)
        or _has_implementation_evidence(active_text)
        or _intends_to_submit_pr(active_text)
        or _has_concrete_first_person_plan(active_text)
    )

"""Strategic contributor-claim language detection.

This module is intentionally pure: it converts issue/comment text into claim signals
without performing network I/O or depending on scanner state.
"""

from __future__ import annotations

import re


def normalized_claim_text(text: str) -> str:
    """Normalize apostrophes so common contractions share the same match path."""
    return text.replace("’", "'").replace("‘", "'")


def explicit_ownership_claim(text: str) -> bool:
    """Recognize first-person statements that take ownership of implementation."""
    patterns = (
        r"\bi(?:'d| would) like to (?:work on|take|handle|implement|fix|resolve|pick (?:this|it) up)\b",
        r"^\s*taking (?:this|this one|it)\b",
        r"\bi(?:'d| would) love to (?:work on|take on|handle|implement|fix|resolve)\b",
        r"\bi(?:'m| am) interested in working on\b",
        r"\bi(?:'m| am) (?:taking|working on) (?:this|it|an independent pass)\b",
        r"\bi can (?:take|work on|handle|implement|fix|resolve)\b",
        r"\bi(?:'ll| will) (?:take|work on|handle|implement|fix|resolve)\b",
        r"\bi(?:'ll| will) take a look at implementing\b",
        r"\bbefore i (?:write|start writing) code\b",
        r"\b(?:please|kindly) assign(?: it| this issue)? to me\b",
        r"\bassign (?:this|it) to me\b",
        r"(?:^\s*claiming this(?:\s+(?:issue|task|dbip))?|\bi(?:'m| am) claiming this(?:\s+(?:issue|task|dbip))?|\bi(?:'ll| will) claim this(?:\s+(?:issue|task|dbip))?)(?=[ \t]*(?:[.!,:;—-]|$)|\r?\n)|/attempt\b",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def implementation_underway_claim(text: str) -> bool:
    """Recognize first-person evidence that implementation already exists or is underway."""
    patterns = (
        r"\bi(?:'ve| have) implemented\b",
        r"\bi(?:'ve got| have(?: got)?) (?:a |the )?(?:fix|patch)\b",
        r"\bi(?:'ve| have) added (?:unit |e2e |regression )?tests?\b",
        r"\bi(?:'ve| have) tests? ready\b",
        r"\bi(?:'ve| have) (?:this|that|it) working(?:\s+locally|\s+with (?:a )?tests?|\s+on (?:a |the )?branch)?\b",
        r"(?s)\bi poked at (?:this|it) locally\b.{0,800}\bi (?:made|moved|added|changed|updated|refactored|implemented)\b",
        r"\bi(?:(?:'ve| have))? prepared (?:a |the )?(?:focused )?(?:candidate(?: implementation)?|patch|fix|implementation)\b",
        r"\bi have (?:one|it|this) ready(?: and tested)?\b",
        r"\bi(?:'m| am) (?:currently )?implementing\b",
        r"\bi(?:'m| am) working on (?:a |the )?fix\b",
        r"^\s*(?:currently implementing|working on (?:a |the )?fix)\b",
        r"^\s*planning (?:a |the )?fix\b",
        r"^\s*(?:planning|plan) to (?:fix|work on|implement|handle)\b",
        r"^\s*starting (?:work on|a fix for)\b",
        r"^\s*delivered in pr\b",
        r"^\s*submitted (?:a )?pr\b",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def pr_intent_claim(text: str) -> bool:
    """Recognize language that says the author intends to submit implementation work."""
    patterns = (
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
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def concrete_first_person_plan_claim(text: str) -> bool:
    """Recognize a concrete implementation plan only when the author owns the work."""
    return bool(
        re.search(r"\bmy plan is to\b", text, re.IGNORECASE)
        or re.search(
            r"\bi(?:'m| am) going to "
            r"(?:add|change|modify|update|implement|fix|refactor|write|remove|move|introduce)\b",
            text,
            re.IGNORECASE,
        )
    )


def strategic_claim_text(text: str) -> bool:
    """Return True only for language that clearly claims or performs implementation work."""
    normalized = normalized_claim_text(text)
    return (
        explicit_ownership_claim(normalized)
        or implementation_underway_claim(normalized)
        or pr_intent_claim(normalized)
        or concrete_first_person_plan_claim(normalized)
    )

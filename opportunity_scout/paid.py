"""Pure policy for basic paid-opportunity eligibility and issue payment evidence."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from opportunity_scout.types import GitHubIssue, GitHubLabel

MAX_COMMENTS: Final = 25
PAYMENT_TERM_RE: Final = r"(?:bounty|reward|payout|compensation|pay(?:ment|s|ing|s)?|paid)"
AMOUNT_RE: Final = (
    r"(?:[$€£]\s*\d[\d,]*(?:\.\d+)?|"
    r"(?<!ERC-)(?<!\d)\d+(?:\.\d+)?\s*(?:usd|usdc|usdt|eur|gbp|xmr|sol|eth|btc)\b)"
)

_GENERATED_ALERT_MARKERS: Final = (
    "bounty alert:",
    "active bounty scan results",
    "new opportunities found",
    "new opportunityies found",
)
_BLOCKED_CONTENT_TERMS: Final = (
    "airdrop",
    "referral",
    "casino",
    "gambling",
    "trading bot",
    "blog post",
    "article writing",
    "tutorial proposal",
    "content creator",
)
_HISTORICAL_SCOUT_ISSUE_PATH: Final = "/bountyscout/issues/"

_BOUNTY_COMMAND = re.compile(r"/bounty\s+(" + AMOUNT_RE + r")", re.IGNORECASE)
_TERM_THEN_AMOUNT = re.compile(
    PAYMENT_TERM_RE + r".{0,80}?(" + AMOUNT_RE + r")",
    re.IGNORECASE | re.DOTALL,
)
_AMOUNT_THEN_TERM = re.compile(
    r"(" + AMOUNT_RE + r").{0,80}?" + PAYMENT_TERM_RE,
    re.IGNORECASE | re.DOTALL,
)
_LABEL_AMOUNT = re.compile(AMOUNT_RE, re.IGNORECASE)
_LABEL_PAYMENT_TERM = re.compile(r"\b(?:bounty|reward|payout)\b", re.IGNORECASE)
_NAMED_PLATFORM = re.compile(r"\b(?:algora|opire)\b", re.IGNORECASE)
_PLATFORM_FUNDING = re.compile(r"\b(?:funded|bounty|reward|payout)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _IssueEvidence:
    text: str
    text_lower: str
    labels_lower: str
    url_lower: str
    pull_request: bool
    assigned: bool
    comments: int


def _label_name(label: GitHubLabel | str) -> str:
    return str(label.get("name", "")) if isinstance(label, dict) else str(label)


def _evidence(item: GitHubIssue) -> _IssueEvidence:
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    text = f"{title}\n{body}"
    labels = item.get("labels") or []
    return _IssueEvidence(
        text=text,
        text_lower=text.lower(),
        labels_lower=" ".join(_label_name(label) for label in labels).lower(),
        url_lower=str(item.get("html_url", "")).lower(),
        pull_request="pull_request" in item,
        assigned=bool(item.get("assignees")),
        comments=int(item.get("comments", 0)),
    )


def _explicit_bounty_command(evidence: _IssueEvidence) -> str | None:
    match = _BOUNTY_COMMAND.search(evidence.text)
    return f"explicit bounty command: {match.group(1).strip()}" if match else None


def _payment_term_before_amount(evidence: _IssueEvidence) -> str | None:
    match = _TERM_THEN_AMOUNT.search(evidence.text)
    return f"payment term + amount: {match.group(1).strip()}" if match else None


def _amount_before_payment_term(evidence: _IssueEvidence) -> str | None:
    match = _AMOUNT_THEN_TERM.search(evidence.text)
    return f"amount + payment term: {match.group(1).strip()}" if match else None


def _bounty_labels(evidence: _IssueEvidence) -> str | None:
    amount = _LABEL_AMOUNT.search(evidence.labels_lower)
    if amount is None or _LABEL_PAYMENT_TERM.search(evidence.labels_lower) is None:
        return None
    return f"bounty labels: {amount.group(0).strip()}"


def _named_platform_funding(evidence: _IssueEvidence) -> str | None:
    if _NAMED_PLATFORM.search(evidence.text_lower) is None:
        return None
    if _PLATFORM_FUNDING.search(evidence.text_lower) is None:
        return None
    return "named bounty platform + funding language"


_PaymentRule = Callable[[_IssueEvidence], str | None]
_PAYMENT_RULES: Final[tuple[_PaymentRule, ...]] = (
    _explicit_bounty_command,
    _payment_term_before_amount,
    _amount_before_payment_term,
    _bounty_labels,
    _named_platform_funding,
)


def payment_signal(item: GitHubIssue) -> str | None:
    """Return the first explicit issue-level payment signal by policy precedence."""
    evidence = _evidence(item)
    for rule in _PAYMENT_RULES:
        signal = rule(evidence)
        if signal is not None:
            return signal
    return None


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _is_pull_request(evidence: _IssueEvidence) -> bool:
    return evidence.pull_request


def _is_historical_scout_report(evidence: _IssueEvidence) -> bool:
    return _HISTORICAL_SCOUT_ISSUE_PATH in evidence.url_lower


def _is_generated_alert(evidence: _IssueEvidence) -> bool:
    return _contains_any(evidence.text_lower, _GENERATED_ALERT_MARKERS)


def _is_assigned(evidence: _IssueEvidence) -> bool:
    return evidence.assigned


def _has_excessive_comments(evidence: _IssueEvidence) -> bool:
    return evidence.comments > MAX_COMMENTS


def _has_blocked_content(evidence: _IssueEvidence) -> bool:
    return _contains_any(evidence.text_lower, _BLOCKED_CONTENT_TERMS)


_RejectionRule = Callable[[_IssueEvidence], bool]
_REJECTION_RULES: Final[tuple[_RejectionRule, ...]] = (
    _is_pull_request,
    _is_historical_scout_report,
    _is_generated_alert,
    _is_assigned,
    _has_excessive_comments,
    _has_blocked_content,
)


def is_clean_candidate(item: GitHubIssue) -> bool:
    """Return whether an issue passes every basic paid-opportunity eligibility rule."""
    evidence = _evidence(item)
    return not any(rule(evidence) for rule in _REJECTION_RULES)

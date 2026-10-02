"""Paid-opportunity eligibility and payment-signal policy.

This module operates only on already-fetched GitHub issue evidence.
No network I/O belongs here.
"""

from __future__ import annotations

import re
from typing import Final

from bountyscout.types import GitHubIssue

MAX_COMMENTS: Final = 25
PAYMENT_TERM_RE: Final = r"(?:bounty|reward|payout|compensation|pay(?:ment|s|ing|s)?|paid)"
AMOUNT_RE: Final = (
    r"(?:[$€£]\s*\d[\d,]*(?:\.\d+)?|"
    r"(?<!ERC-)(?<!\d)\d+(?:\.\d+)?\s*(?:usd|usdc|usdt|eur|gbp|xmr|sol|eth|btc)\b)"
)


def payment_signal(item: GitHubIssue) -> str | None:
    """Return a strong payment signal, or None when payment is not explicit."""
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    labels = item.get("labels") or []
    label_names = [
        str(label.get("name", "")) if isinstance(label, dict) else str(label) for label in labels
    ]

    text = f"{title}\n{body}"
    lower = text.lower()
    labels_text = " ".join(label_names).lower()

    # Explicit bounty commands used by Algora and similar integrations.
    slash_match = re.search(r"/bounty\s+(" + AMOUNT_RE + r")", text, re.IGNORECASE)
    if slash_match:
        return f"explicit bounty command: {slash_match.group(1).strip()}"

    # Require a payment/reward term close to an actual amount/currency.
    near_amount = re.search(
        PAYMENT_TERM_RE + r".{0,80}?(" + AMOUNT_RE + r")",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if near_amount:
        return f"payment term + amount: {near_amount.group(1).strip()}"

    amount_near_term = re.search(
        r"(" + AMOUNT_RE + r").{0,80}?" + PAYMENT_TERM_RE,
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if amount_near_term:
        return f"amount + payment term: {amount_near_term.group(1).strip()}"

    # Some bounty systems put the amount in a label and "bounty" in another.
    amount = re.search(AMOUNT_RE, labels_text, re.IGNORECASE)
    if amount and re.search(r"\b(?:bounty|reward|payout)\b", labels_text, re.IGNORECASE):
        return f"bounty labels: {amount.group(0).strip()}"

    # Platform-backed language can be explicit even when the exact amount is
    # stored outside the issue body.
    if re.search(r"\b(?:algora|opire)\b", lower) and re.search(
        r"\b(?:funded|bounty|reward|payout)\b", lower
    ):
        return "named bounty platform + funding language"

    return None


def is_clean_candidate(item: GitHubIssue) -> bool:
    """Return whether an issue passes the package-owned paid eligibility policy."""
    return not any(reject(item) for reject in _ELIGIBILITY_REJECTION_RULES)


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


def _issue_text(item: GitHubIssue) -> tuple[str, str]:
    return str(item.get("title", "")).lower(), str(item.get("body", "")).lower()


def _contains_issue_text_term(item: GitHubIssue, terms: tuple[str, ...]) -> bool:
    title, body = _issue_text(item)
    return any(term in title or term in body for term in terms)


def _is_pull_request(item: GitHubIssue) -> bool:
    return "pull_request" in item


def _is_historical_scout_report(item: GitHubIssue) -> bool:
    return _HISTORICAL_SCOUT_ISSUE_PATH in str(item.get("html_url", "")).lower()


def _contains_generated_alert(item: GitHubIssue) -> bool:
    return _contains_issue_text_term(item, _GENERATED_ALERT_MARKERS)


def _is_assigned(item: GitHubIssue) -> bool:
    return bool(item.get("assignees"))


def _exceeds_comment_limit(item: GitHubIssue) -> bool:
    return int(item.get("comments", 0)) > MAX_COMMENTS


def _contains_blocked_content(item: GitHubIssue) -> bool:
    return _contains_issue_text_term(item, _BLOCKED_CONTENT_TERMS)


_ELIGIBILITY_REJECTION_RULES: Final = (
    _is_pull_request,
    _is_historical_scout_report,
    _contains_generated_alert,
    _is_assigned,
    _exceeds_comment_limit,
    _contains_blocked_content,
)

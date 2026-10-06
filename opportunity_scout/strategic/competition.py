"""Strategic competition and implementation-PR detection.

This module interprets claim and pull-request evidence for OSS opportunities. It
uses package-owned GitHub utilities and paid-verification policy, never the root scanner
or application orchestrator, and is directly testable with mocked GitHub responses.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable, Iterable

from opportunity_scout import github, paid_verification
from opportunity_scout.strategic.claims import strategic_claim_text
from opportunity_scout.types import GitHubComment, GitHubIssue, SourceFailureReason

STRATEGIC_CLAIM_MAX_AGE_DAYS = 365

SUPPLEMENTAL_CLAIM_PATTERNS = (
    r"\bplanning (?:a |the )?fix\b",
    r"\bplanning to (?:fix|work on|implement|handle)\b",
    r"\bplan to (?:fix|work on|implement|handle)\b",
    r"\bstarting (?:work on|a fix for)\b",
    r"\bi(?:'ll| will) (?:fix|work on|implement|handle)\b",
    r"\bi can take (?:this|it|this one)\b",
    r"\bi(?:'ll| will) take (?:this|it|this one)\b",
    r"\bi(?:'ll| will) take a look at (?:this|it|this one)\b",
    r"\bimplementing (?:this|a fix)\b",
    r"\bworking on (?:a |the )?fix\b",
)

_ISSUE_BODY_IMPLEMENTATION_CONTEXT = re.compile(
    r"\b(?:fix(?:es|ed|ing)?|implementation|patch|solution|"
    r"address(?:es|ed|ing)?|resolv(?:es|ed|ing)?)\b",
    re.IGNORECASE,
)
_STRONG_COMMENT_PR_REFERENCE = re.compile(
    r"\b(?:related|implementation|opened|submitted)\s+"
    r"(?:pr|pull request)\s*:?\s*#(\d+)\b",
    re.IGNORECASE,
)
_FIX_COMMENT_PR_REFERENCE = re.compile(
    r"\b(?:related\s+)?(?:draft\s+)?"
    r"(?:fix|patch|implementation)"
    r"(?:\s+(?:is\s+)?(?:in|at))?\s*[:(]?\s*#(\d+)\b",
    re.IGNORECASE,
)

LinkedPrChecker = Callable[[GitHubIssue, str | None, list[GitHubComment]], str | None]
ClaimChecker = Callable[[GitHubIssue, list[GitHubComment]], str | None]
SupplementalClaimChecker = Callable[[GitHubIssue, list[GitHubComment]], str | None]
TimelinePrChecker = Callable[[GitHubIssue, str | None], str | None]
ExistingPrChecker = Callable[[str, int, str | None], str | None]


def claim_source_is_recent(
    source: GitHubIssue | GitHubComment, *, issue_body: bool = False
) -> bool:
    """Keep old claims from permanently suppressing strategic opportunities."""
    timestamp = (
        source.get("created_at")
        if issue_body
        else (source.get("updated_at") or source.get("created_at"))
    )
    stamp = github.parse_github_datetime(timestamp)
    if stamp is None:
        return True
    age_days = max(0, (datetime.now(timezone.utc) - stamp).days)
    return age_days <= STRATEGIC_CLAIM_MAX_AGE_DAYS


def _comment_author(comment: GitHubComment) -> str:
    return str((comment.get("user") or {}).get("login", "someone"))


def _implementation_branch_owner(body: str, issue_number: int) -> str | None:
    branch = re.search(
        rf"https://github\.com/([^/\s]+)/[^/\s]+/tree/"
        rf"[^\s)]*(?:issue|fix)[-_/]?{issue_number}\b",
        body,
        re.IGNORECASE,
    )
    return branch.group(1) if branch else None


def strategic_claim_reason(
    item: GitHubIssue,
    comments: list[GitHubComment],
) -> str | None:
    """Detect active implementation ownership in the issue body and recent comments."""
    body = str(item.get("body", ""))
    if body and claim_source_is_recent(item, issue_body=True) and strategic_claim_text(body):
        return "issue author already has an implementation/fix in progress"

    _, number = github.issue_repo_and_number(item)
    for comment in comments:
        if not claim_source_is_recent(comment):
            continue

        body = str(comment.get("body", ""))
        author = _comment_author(comment)
        if strategic_claim_text(body):
            return f"active claim by @{author}"

        if number:
            branch_owner = _implementation_branch_owner(body, number)
            if branch_owner and branch_owner.lower() == author.lower():
                return f"active implementation branch linked by @{author}"
    return None


def _same_repo_pull_pattern(repo: str) -> re.Pattern[str]:
    return re.compile(
        rf"https://github\.com/{re.escape(repo)}/pull/(\d+)",
        re.IGNORECASE,
    )


def _issue_body_pr_candidates(repo: str, issue_body: str) -> list[str]:
    candidates: list[str] = []
    for match in _same_repo_pull_pattern(repo).finditer(issue_body):
        start = max(0, match.start() - 160)
        end = min(len(issue_body), match.end() + 160)
        if _ISSUE_BODY_IMPLEMENTATION_CONTEXT.search(issue_body[start:end]):
            candidates.append(match.group(1))
    return candidates


def _comment_pr_candidates(repo: str, comments: list[GitHubComment]) -> list[str]:
    candidates: list[str] = []
    same_repo_pull = _same_repo_pull_pattern(repo)

    for comment in comments:
        body = str(comment.get("body", ""))
        candidates.extend(match.group(1) for match in same_repo_pull.finditer(body))
        candidates.extend(match.group(1) for match in _STRONG_COMMENT_PR_REFERENCE.finditer(body))
        candidates.extend(match.group(1) for match in _FIX_COMMENT_PR_REFERENCE.finditer(body))

    return candidates


def _linked_pr_candidates(
    repo: str,
    item: GitHubIssue,
    comments: list[GitHubComment],
) -> list[str]:
    candidates = _issue_body_pr_candidates(repo, str(item.get("body", "")))
    candidates.extend(_comment_pr_candidates(repo, comments))
    return list(dict.fromkeys(candidates))


def _verify_linked_pr_candidates(
    repo: str,
    token: str | None,
    candidates: Iterable[str],
) -> str | None:
    for pr_number in candidates:
        pr = github.github_get(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
            token,
        )
        if not isinstance(pr, dict):
            return SourceFailureReason("could not verify linked implementation PR")

        state = pr.get("state")
        if state == "open":
            url = pr.get("html_url") or f"https://github.com/{repo}/pull/{pr_number}"
            return f"existing open implementation PR: {url}"
        if state != "closed":
            return SourceFailureReason("could not verify linked implementation PR")

    return None


def linked_open_pr_reason(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment],
) -> str | None:
    """Detect explicit implementation PR links in the issue body or comments."""
    repo, number = github.issue_repo_and_number(item)
    if not repo or not number:
        return None

    candidates = _linked_pr_candidates(repo, item, comments)
    return _verify_linked_pr_candidates(repo, token, candidates)


def timeline_open_pr_reason(item: GitHubIssue, token: str | None) -> str | None:
    """Adapt strategic issue identity to the canonical implementation-PR timeline check."""
    repo, number = github.issue_repo_and_number(item)
    if not repo or not number:
        return "could not identify repository/issue number"

    return paid_verification.has_existing_implementation_pr(repo, number, token)


def _claim_reason_from_patterns(
    comments: list[GitHubComment],
    patterns: Iterable[str],
) -> str | None:
    for comment in comments:
        body = str(comment.get("body", ""))
        for pattern in patterns:
            if re.search(pattern, body, re.IGNORECASE):
                return f"active claim by @{_comment_author(comment)}"
    return None


def supplemental_claim_reason(
    item: GitHubIssue,
    comments: list[GitHubComment],
) -> str | None:
    """Detect clear work claims not covered by the upstream scanner."""
    if not int(item.get("comments") or 0):
        return None

    return _claim_reason_from_patterns(comments, SUPPLEMENTAL_CLAIM_PATTERNS)


def extended_competition_reason(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment],
    *,
    existing_pr_checker: ExistingPrChecker | None = None,
    linked_pr_checker: LinkedPrChecker = linked_open_pr_reason,
    supplemental_claim_checker: SupplementalClaimChecker = supplemental_claim_reason,
) -> str | None:
    """Apply paid-lane competition checks using supplied issue comments."""
    repo, number = github.issue_repo_and_number(item)
    if not repo or not number:
        return "could not identify repository/issue number"

    checker = existing_pr_checker or paid_verification.has_existing_implementation_pr
    reason = checker(repo, number, token)
    if reason:
        return reason

    reason = linked_pr_checker(item, token, comments)
    if reason:
        return reason

    reason = _claim_reason_from_patterns(comments, paid_verification.CLAIM_PATTERNS)
    if reason:
        return reason

    return supplemental_claim_checker(item, comments)


def strategic_competition_reason(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment],
    *,
    timeline_pr_checker: TimelinePrChecker = timeline_open_pr_reason,
    linked_pr_checker: LinkedPrChecker = linked_open_pr_reason,
    strategic_claim_checker: ClaimChecker = strategic_claim_reason,
) -> str | None:
    """Apply strategic-only competition checks without changing paid-bounty behavior."""
    repo, number = github.issue_repo_and_number(item)
    if not repo or not number:
        return "could not identify repository/issue number"

    reason = strategic_claim_checker(item, comments)
    if reason:
        return reason

    reason = linked_pr_checker(item, token, comments)
    if reason:
        return reason

    return timeline_pr_checker(item, token)

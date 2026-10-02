"""Strategic competition and implementation-PR detection.

This module interprets claim and pull-request evidence for OSS opportunities. It
uses package-owned GitHub utilities and paid-verification policy, never the root scanner
or application orchestrator, and is directly testable with mocked GitHub responses.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable

from bountyscout import github, paid_verification
from bountyscout.strategic.claims import strategic_claim_text
from bountyscout.types import GitHubComment, GitHubIssue

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
        author = str((comment.get("user") or {}).get("login", "someone"))
        if strategic_claim_text(body):
            return f"active claim by @{author}"

        if number:
            branch = re.search(
                rf"https://github\.com/([^/\s]+)/[^/\s]+/tree/"
                rf"[^\s)]*(?:issue|fix)[-_/]?{number}\b",
                body,
                re.IGNORECASE,
            )
            if branch and branch.group(1).lower() == author.lower():
                return f"active implementation branch linked by @{author}"
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

    repo_pattern = re.escape(repo)
    candidates: list[str] = []

    issue_body = str(item.get("body", ""))
    for match in re.finditer(
        rf"https://github\.com/{repo_pattern}/pull/(\d+)",
        issue_body,
        re.IGNORECASE,
    ):
        start = max(0, match.start() - 160)
        end = min(len(issue_body), match.end() + 160)
        context = issue_body[start:end]
        if re.search(
            r"\b(?:fix(?:es|ed|ing)?|implementation|patch|solution|"
            r"address(?:es|ed|ing)?|resolv(?:es|ed|ing)?)\b",
            context,
            re.IGNORECASE,
        ):
            candidates.append(match.group(1))

    for comment in comments:
        body = str(comment.get("body", ""))
        candidates.extend(
            re.findall(
                rf"https://github\.com/{repo_pattern}/pull/(\d+)",
                body,
                re.IGNORECASE,
            )
        )
        candidates.extend(
            re.findall(
                r"\b(?:related|implementation|opened|submitted)\s+"
                r"(?:pr|pull request)\s*:?\s*#(\d+)\b",
                body,
                re.IGNORECASE,
            )
        )
        candidates.extend(
            re.findall(
                r"\b(?:related\s+)?(?:draft\s+)?"
                r"(?:fix|patch|implementation|pr|pull request)"
                r"(?:\s+(?:is\s+)?(?:in|at))?\s*[:(]?\s*#(\d+)\b",
                body,
                re.IGNORECASE,
            )
        )

    for pr_number in dict.fromkeys(candidates):
        pr = github.github_get(
            f"https://api.github.com/repos/{repo}/pulls/{pr_number}",
            token,
        )
        if isinstance(pr, dict) and pr.get("state") == "open":
            url = pr.get("html_url") or f"https://github.com/{repo}/pull/{pr_number}"
            return f"existing open implementation PR: {url}"

    return None


def timeline_open_pr_reason(item: GitHubIssue, token: str | None) -> str | None:
    """Detect open timeline-linked PRs and fail closed when timeline evidence is unavailable."""
    repo, number = github.issue_repo_and_number(item)
    if not repo or not number:
        return "could not identify repository/issue number"

    timeline = github.github_get(
        f"https://api.github.com/repos/{repo}/issues/{number}/timeline?per_page=100",
        token,
    )
    if not isinstance(timeline, list):
        return "could not verify open implementation PR timeline"

    for event in timeline:
        if event.get("event") != "cross-referenced":
            continue
        source = event.get("source") or {}
        source_issue = source.get("issue") if isinstance(source, dict) else None
        if not isinstance(source_issue, dict) or not source_issue.get("pull_request"):
            continue
        if source_issue.get("state") != "open":
            continue
        url = source_issue.get("html_url")
        if url:
            return f"existing open implementation PR: {url}"

    return None


def supplemental_claim_reason(
    item: GitHubIssue,
    comments: list[GitHubComment],
) -> str | None:
    """Detect clear work claims not covered by the upstream scanner."""
    if not int(item.get("comments") or 0):
        return None

    for comment in comments:
        body = str(comment.get("body", ""))
        for pattern in SUPPLEMENTAL_CLAIM_PATTERNS:
            if re.search(pattern, body, re.IGNORECASE):
                author = (comment.get("user") or {}).get("login", "someone")
                return f"active claim by @{author}"
    return None


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

    for comment in comments:
        body = str(comment.get("body", ""))
        for pattern in paid_verification.CLAIM_PATTERNS:
            if re.search(pattern, body, re.IGNORECASE):
                author = (comment.get("user") or {}).get("login", "someone")
                return f"active claim by @{author}"

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

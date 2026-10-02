"""Paid opportunity rejection and competition verification.

Owns paid-specific rejection precedence and GitHub-backed competition checks.
Transport is injectable so compatibility callers can preserve historical request
behavior.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable
from typing import Any, TypedDict, cast

from bountyscout import github, paid
from bountyscout.types import GitHubComment, GitHubIssue

FetchJson = Callable[[str, str | None], Any]
ExistingPrChecker = Callable[[str, int, str | None], str | None]
ActiveClaimChecker = Callable[[str, int, int, str | None], str | None]

CLAIM_PATTERNS = [
    r"/attempt\b",
    r"\bi(?:'d| would) like to (?:take|work on|handle|resolve)",
    r"\bi(?:'m| am) taking (?:this|an independent pass)",
    r"\bi(?:'m| am) working on (?:this|it)",
    r"\bplease assign(?: this issue)? to me\b",
    r"\bkindly assign(?: it| this issue)? to me\b",
    r"\bassign (?:this|it) to me\b",
    r"\bi can work on this\b",
    r"\bi have implemented\b",
    r"\bdelivered in pr\b",
    r"\bsubmitted (?:a )?pr\b",
]

UNFUNDED_PROPOSAL_PATTERNS = [
    r"\[bounty proposal\]",
    r"(?m)^\s*(?:\*\*)?bounty proposal(?:\*\*)?\s*$",
    r"\bwould you approve\s+(?:\*\*)?(?:us\$|\$)\s*\d[\d,]*(?:\.\d+)?(?:\*\*)?\s+cash\b",
    r"\bproposed amount, not an existing award\b",
    r"\$\s*\d[\d,]*(?:\.\d+)?\s+proposed\b",
    r"\bwould (?:a |an )?(?:us\$|\$)?\s*\d[\d,]*(?:\.\d+)? bounty be appropriate\b",
    r"\bpropos(?:e|ed|ing) (?:a )?(?:paid work|bounty)\b",
]

META_ALERT_MARKERS = [
    "new in-scope",
    "bug bounty program(s) added",
    "bounty-watch",
    "bounty watch",
]


class _TimelineIssue(TypedDict, total=False):
    pull_request: object
    state: str
    html_url: str


class _TimelineSource(TypedDict, total=False):
    issue: object


class _TimelineEvent(TypedDict, total=False):
    event: str
    source: object


def has_existing_implementation_pr(
    repo: str,
    issue_number: int,
    token: str | None,
    *,
    fetch_json: FetchJson | None = None,
) -> str | None:
    """Return a reason when the issue timeline references an open implementation PR."""
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/timeline?per_page=100"
    timeline: object = (
        github.github_get(url, token) if fetch_json is None else fetch_json(url, token)
    )
    if not isinstance(timeline, list):
        return None

    for raw_event in timeline:
        if not isinstance(raw_event, dict):
            continue
        event = cast(_TimelineEvent, raw_event)
        if event.get("event") != "cross-referenced":
            continue
        raw_source = event.get("source")
        if not isinstance(raw_source, dict):
            continue
        source = cast(_TimelineSource, raw_source)
        raw_issue = source.get("issue")
        if not isinstance(raw_issue, dict):
            continue
        source_issue = cast(_TimelineIssue, raw_issue)
        if "pull_request" not in source_issue or source_issue.get("state") != "open":
            continue
        pr_url = source_issue.get("html_url")
        if pr_url:
            return f"existing open implementation PR: {pr_url}"

    return None


def active_claim_reason(
    repo: str,
    issue_number: int,
    comments_count: int,
    token: str | None,
    *,
    fetch_json: FetchJson | None = None,
) -> str | None:
    """Return a reason when recent comments clearly claim or implement the task."""
    if not comments_count:
        return None

    params = urllib.parse.urlencode(
        {
            "per_page": min(int(comments_count), 30),
            "sort": "created",
            "direction": "desc",
        }
    )
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments?{params}"
    comments: object = (
        github.github_get(url, token) if fetch_json is None else fetch_json(url, token)
    )
    if not isinstance(comments, list):
        return None

    for raw_comment in comments:
        if not isinstance(raw_comment, dict):
            continue
        comment = cast(GitHubComment, raw_comment)
        body = str(comment.get("body", ""))
        for pattern in CLAIM_PATTERNS:
            if re.search(pattern, body, re.IGNORECASE):
                user = comment.get("user")
                author = user.get("login", "someone") if isinstance(user, dict) else "someone"
                return f"active claim by @{author}"

    return None


def candidate_rejection_reason(
    item: GitHubIssue,
    token: str | None,
    *,
    fetch_json: FetchJson | None = None,
    existing_pr_checker: ExistingPrChecker | None = None,
    active_claim_checker: ActiveClaimChecker | None = None,
) -> tuple[str | None, str | None]:
    """Apply strict money + competition checks and return a rejection reason."""
    title = str(item.get("title", ""))
    body = str(item.get("body", ""))
    labels = item.get("labels") or []
    label_names = [
        str(label.get("name", "")) if isinstance(label, dict) else str(label) for label in labels
    ]
    combined = f"{title}\n{body}"
    lower_combined = combined.lower()
    lower_labels = " ".join(label_names).lower()

    if any(re.search(pattern, combined, re.IGNORECASE) for pattern in UNFUNDED_PROPOSAL_PATTERNS):
        return "unfunded bounty proposal, not an existing award", None

    if any(marker in lower_combined or marker in lower_labels for marker in META_ALERT_MARKERS):
        return "meta/monitoring alert, not a contributor task", None

    signal = paid.payment_signal(item)
    if not signal:
        return "no explicit payment signal", None

    repo, issue_number = github.issue_repo_and_number(item)
    if not repo or not issue_number:
        return "could not identify repository/issue number", signal

    if existing_pr_checker is None:
        pr_reason = has_existing_implementation_pr(
            repo,
            issue_number,
            token,
            fetch_json=fetch_json,
        )
    else:
        pr_reason = existing_pr_checker(repo, issue_number, token)
    if pr_reason:
        return pr_reason, signal

    comments_count = int(item.get("comments", 0))
    if active_claim_checker is None:
        claim_reason = active_claim_reason(
            repo,
            issue_number,
            comments_count,
            token,
            fetch_json=fetch_json,
        )
    else:
        claim_reason = active_claim_checker(repo, issue_number, comments_count, token)
    if claim_reason:
        return claim_reason, signal

    return None, signal

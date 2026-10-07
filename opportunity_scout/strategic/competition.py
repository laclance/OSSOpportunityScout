"""Strategic competition and implementation-PR evidence policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
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
_COMMENT_PR_SHORTHAND = (
    re.compile(
        r"\b(?:related|implementation|opened|submitted)\s+"
        r"(?:pr|pull request)\s*:?\s*#(\d+)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:related\s+)?(?:draft\s+)?"
        r"(?:fix|patch|implementation)"
        r"(?:\s+(?:is\s+)?(?:in|at))?\s*[:(]?\s*#(\d+)\b",
        re.IGNORECASE,
    ),
)
_BRANCH_URL = re.compile(
    r"https://github\.com/(?P<owner>[^/\s]+)/[^/\s]+/tree/(?P<branch>[^\s)]+)",
    re.IGNORECASE,
)
_REPORTER_IMPLEMENTATION_FORK_RE = re.compile(
    r"\bimplementation\s+(?:pr|pull request)\s+will\s+follow\s+from\s+fork\s+"
    r'[\x60"]?(?P<owner>[A-Za-z0-9-]+)/',
    re.IGNORECASE,
)
_CANONICAL_ISSUE_LEAD_RE = re.compile(
    r"\b(?:existing|canonical|original|earlier|previous)\s+(?:open\s+)?issue\b",
    re.IGNORECASE,
)
_CANONICAL_ISSUE_PURPOSE_RE = re.compile(
    r"\b(?:request(?:s|ed|ing)?|track(?:s|ed|ing)?|cover(?:s|ed|ing)?|"
    r"(?:same|this)\s+(?:feature|bug|problem|work))\b",
    re.IGNORECASE,
)
_COMMENT_DUPLICATE_RE = re.compile(
    r"\b(?:seems?|appears?)\s+(?:possible\s+|likely\s+)?(?:that\s+)?"
    r"(?:this\s+)?(?:is\s+)?(?:a\s+)?duplicate\s+of\b",
    re.IGNORECASE,
)
_COMMENT_DUPLICATE_REDIRECT_RE = re.compile(
    r"\b(?:move|continue)\s+(?:the\s+)?discussion\s+(?:to|in)\s+"
    r"(?:there|that\s+issue|#\d+)\b",
    re.IGNORECASE,
)
_CANONICAL_COMMENT_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"}
_LINKED_PR_FAILURE = "could not verify linked implementation PR"
_CANONICAL_ISSUE_FAILURE = "could not verify canonical issue reference"
_UNIDENTIFIABLE_ISSUE = "could not identify repository/issue number"

LinkedPrChecker = Callable[[GitHubIssue, str | None, list[GitHubComment]], str | None]
CanonicalIssueChecker = Callable[[GitHubIssue, str | None, list[GitHubComment]], str | None]
ClaimChecker = Callable[[GitHubIssue, list[GitHubComment]], str | None]
SupplementalClaimChecker = Callable[[GitHubIssue, list[GitHubComment]], str | None]
TimelinePrChecker = Callable[[GitHubIssue, str | None], str | None]
ExistingPrChecker = Callable[[str, int, str | None], str | None]


@dataclass(frozen=True, slots=True)
class _IssueIdentity:
    repo: str
    number: int

    @classmethod
    def from_issue(cls, item: GitHubIssue) -> _IssueIdentity | None:
        repo, number = github.issue_repo_and_number(item)
        if not repo or not number:
            return None
        return cls(repo=repo, number=number)

    def pull_api_url(self, number: str) -> str:
        return f"https://api.github.com/repos/{self.repo}/pulls/{number}"

    def pull_web_url(self, number: str) -> str:
        return f"https://github.com/{self.repo}/pull/{number}"

    def issue_api_url(self, number: int) -> str:
        return f"https://api.github.com/repos/{self.repo}/issues/{number}"

    def issue_web_url(self, number: int) -> str:
        return f"https://github.com/{self.repo}/issues/{number}"


class _LinkedPrEvidenceSource(Enum):
    ISSUE_BODY_URL = auto()
    COMMENT_URL = auto()
    COMMENT_SHORTHAND = auto()


@dataclass(frozen=True, slots=True)
class _LinkedPrEvidence:
    number: str
    source: _LinkedPrEvidenceSource


@dataclass(frozen=True, slots=True)
class _CommentEvidence:
    text: str
    author: str

    @classmethod
    def from_comment(cls, comment: GitHubComment) -> _CommentEvidence:
        user = comment.get("user") or {}
        return cls(
            text=str(comment.get("body", "")),
            author=str(user.get("login", "someone")),
        )


def claim_source_is_recent(
    source: GitHubIssue | GitHubComment, *, issue_body: bool = False
) -> bool:
    """Return whether claim evidence is current enough to suppress an opportunity."""
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


def _issue_body_claim_reason(item: GitHubIssue) -> str | None:
    body = str(item.get("body", ""))
    if not body or not claim_source_is_recent(item, issue_body=True):
        return None
    if strategic_claim_text(body):
        return "issue author already has an implementation/fix in progress"

    reporter = item.get("user") or {}
    reporter_login = str(reporter.get("login", "")) if isinstance(reporter, dict) else ""
    planned_pr = _REPORTER_IMPLEMENTATION_FORK_RE.search(body)
    if (
        reporter_login
        and planned_pr is not None
        and planned_pr.group("owner").lower() == reporter_login.lower()
    ):
        return "issue author already has an implementation/fix in progress"
    return None


def _implementation_branch_owner(text: str, issue_number: int) -> str | None:
    target = re.compile(rf"(?:issue|fix)[-_/]?{issue_number}\b", re.IGNORECASE)
    for match in _BRANCH_URL.finditer(text):
        if target.search(match.group("branch")):
            return match.group("owner")
    return None


def _comment_claim_reason(comment: GitHubComment, issue_number: int | None) -> str | None:
    if not claim_source_is_recent(comment):
        return None

    evidence = _CommentEvidence.from_comment(comment)
    if strategic_claim_text(evidence.text):
        return f"active claim by @{evidence.author}"

    if issue_number is None:
        return None
    branch_owner = _implementation_branch_owner(evidence.text, issue_number)
    if branch_owner and branch_owner.lower() == evidence.author.lower():
        return f"active implementation branch linked by @{evidence.author}"
    return None


def strategic_claim_reason(
    item: GitHubIssue,
    comments: list[GitHubComment],
) -> str | None:
    """Detect current strategic claim or branch-ownership evidence."""
    issue_reason = _issue_body_claim_reason(item)
    if issue_reason is not None:
        return issue_reason

    identity = _IssueIdentity.from_issue(item)
    issue_number = identity.number if identity is not None else None
    for comment in comments:
        reason = _comment_claim_reason(comment, issue_number)
        if reason is not None:
            return reason
    return None


def _same_repository_issue_pattern(identity: _IssueIdentity) -> re.Pattern[str]:
    return re.compile(
        rf"https://github\.com/{re.escape(identity.repo)}/issues/(\d+)|(?<!\w)#(\d+)\b",
        re.IGNORECASE,
    )


def _canonical_issue_numbers(identity: _IssueIdentity, item: GitHubIssue) -> list[int]:
    text = str(item.get("body", ""))
    pattern = _same_repository_issue_pattern(identity)
    numbers: list[int] = []
    seen: set[int] = set()

    for lead in _CANONICAL_ISSUE_LEAD_RE.finditer(text):
        window = text[lead.start() : lead.start() + 360]
        for reference in pattern.finditer(window):
            number = int(reference.group(1) or reference.group(2))
            if number == identity.number or number in seen:
                continue
            if (
                _CANONICAL_ISSUE_PURPOSE_RE.search(window[reference.end() : reference.end() + 180])
                is None
            ):
                continue
            seen.add(number)
            numbers.append(number)
    return numbers


def _comment_canonical_issue_numbers(
    identity: _IssueIdentity,
    comments: list[GitHubComment],
) -> list[int]:
    pattern = _same_repository_issue_pattern(identity)
    numbers: list[int] = []
    seen: set[int] = set()

    for comment in comments:
        association = str(comment.get("author_association", "")).upper()
        if association not in _CANONICAL_COMMENT_ASSOCIATIONS:
            continue

        text = str(comment.get("body", ""))
        duplicate = _COMMENT_DUPLICATE_RE.search(text)
        if duplicate is None or "not a duplicate" in text.lower():
            continue

        window = text[duplicate.start() : duplicate.start() + 420]
        if _COMMENT_DUPLICATE_REDIRECT_RE.search(window) is None:
            continue

        for reference in pattern.finditer(window):
            number = int(reference.group(1) or reference.group(2))
            if number == identity.number or number in seen:
                continue
            seen.add(number)
            numbers.append(number)
    return numbers


def canonical_open_issue_reason(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment] | None = None,
) -> str | None:
    """Reject explicit canonical redirects when the referenced issue is still open."""
    identity = _IssueIdentity.from_issue(item)
    if identity is None:
        return None

    numbers = _canonical_issue_numbers(identity, item)
    for number in _comment_canonical_issue_numbers(identity, comments or []):
        if number not in numbers:
            numbers.append(number)

    for number in numbers:
        canonical = github.github_get(identity.issue_api_url(number), token)
        if not isinstance(canonical, dict):
            return SourceFailureReason(_CANONICAL_ISSUE_FAILURE)

        state = canonical.get("state")
        if state == "open":
            url = canonical.get("html_url") or identity.issue_web_url(number)
            return f"same work is already tracked by open canonical issue: {url}"
        if state != "closed":
            return SourceFailureReason(_CANONICAL_ISSUE_FAILURE)
    return None


def _same_repository_pr_pattern(identity: _IssueIdentity) -> re.Pattern[str]:
    return re.compile(
        rf"https://github\.com/{re.escape(identity.repo)}/pull/(\d+)",
        re.IGNORECASE,
    )


def _issue_body_pr_evidence(
    identity: _IssueIdentity,
    item: GitHubIssue,
) -> list[_LinkedPrEvidence]:
    text = str(item.get("body", ""))
    evidence: list[_LinkedPrEvidence] = []
    for match in _same_repository_pr_pattern(identity).finditer(text):
        context_start = max(0, match.start() - 160)
        context_end = min(len(text), match.end() + 160)
        if _ISSUE_BODY_IMPLEMENTATION_CONTEXT.search(text[context_start:context_end]):
            evidence.append(
                _LinkedPrEvidence(match.group(1), _LinkedPrEvidenceSource.ISSUE_BODY_URL)
            )
    return evidence


def _comment_pr_evidence(
    identity: _IssueIdentity,
    comments: list[GitHubComment],
) -> list[_LinkedPrEvidence]:
    evidence: list[_LinkedPrEvidence] = []
    same_repo_url = _same_repository_pr_pattern(identity)
    for comment in comments:
        text = str(comment.get("body", ""))
        evidence.extend(
            _LinkedPrEvidence(match.group(1), _LinkedPrEvidenceSource.COMMENT_URL)
            for match in same_repo_url.finditer(text)
        )
        for shorthand in _COMMENT_PR_SHORTHAND:
            evidence.extend(
                _LinkedPrEvidence(match.group(1), _LinkedPrEvidenceSource.COMMENT_SHORTHAND)
                for match in shorthand.finditer(text)
            )
    return evidence


def _linked_pr_evidence(
    identity: _IssueIdentity,
    item: GitHubIssue,
    comments: list[GitHubComment],
) -> list[_LinkedPrEvidence]:
    observed = _issue_body_pr_evidence(identity, item)
    observed.extend(_comment_pr_evidence(identity, comments))

    unique: list[_LinkedPrEvidence] = []
    seen: set[str] = set()
    for evidence in observed:
        if evidence.number in seen:
            continue
        seen.add(evidence.number)
        unique.append(evidence)
    return unique


def _verify_linked_pr_evidence(
    identity: _IssueIdentity,
    token: str | None,
    evidence: Iterable[_LinkedPrEvidence],
) -> str | None:
    for candidate in evidence:
        pull = github.github_get(identity.pull_api_url(candidate.number), token)
        if not isinstance(pull, dict):
            return SourceFailureReason(_LINKED_PR_FAILURE)

        state = pull.get("state")
        if state == "open":
            url = pull.get("html_url") or identity.pull_web_url(candidate.number)
            return f"existing open implementation PR: {url}"
        if state != "closed":
            return SourceFailureReason(_LINKED_PR_FAILURE)
        if (
            candidate.source is _LinkedPrEvidenceSource.ISSUE_BODY_URL
            and pull.get("merged") is True
        ):
            url = pull.get("html_url") or identity.pull_web_url(candidate.number)
            return f"linked implementation PR is already merged: {url}"
    return None


def linked_open_pr_reason(
    item: GitHubIssue,
    token: str | None,
    comments: list[GitHubComment],
) -> str | None:
    """Verify explicit same-repository implementation-PR evidence."""
    identity = _IssueIdentity.from_issue(item)
    if identity is None:
        return None
    evidence = _linked_pr_evidence(identity, item, comments)
    return _verify_linked_pr_evidence(identity, token, evidence)


def timeline_open_pr_reason(item: GitHubIssue, token: str | None) -> str | None:
    """Delegate timeline relationship evidence to the canonical verifier."""
    identity = _IssueIdentity.from_issue(item)
    if identity is None:
        return _UNIDENTIFIABLE_ISSUE
    return paid_verification.has_existing_implementation_pr(identity.repo, identity.number, token)


def _claim_reason_from_patterns(
    comments: list[GitHubComment],
    patterns: Iterable[str],
) -> str | None:
    for comment in comments:
        evidence = _CommentEvidence.from_comment(comment)
        if any(re.search(pattern, evidence.text, re.IGNORECASE) for pattern in patterns):
            return f"active claim by @{evidence.author}"
    return None


def supplemental_claim_reason(
    item: GitHubIssue,
    comments: list[GitHubComment],
) -> str | None:
    """Detect paid-compatible work claims outside the canonical paid patterns."""
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
    """Apply paid-compatible competition evidence in stable precedence order."""
    identity = _IssueIdentity.from_issue(item)
    if identity is None:
        return _UNIDENTIFIABLE_ISSUE

    checker = existing_pr_checker or paid_verification.has_existing_implementation_pr
    reason = checker(identity.repo, identity.number, token)
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
    canonical_issue_checker: CanonicalIssueChecker = canonical_open_issue_reason,
    strategic_claim_checker: ClaimChecker = strategic_claim_reason,
) -> str | None:
    """Apply strategic competition evidence in stable precedence order."""
    if _IssueIdentity.from_issue(item) is None:
        return _UNIDENTIFIABLE_ISSUE

    reason = strategic_claim_checker(item, comments)
    if reason:
        return reason

    reason = canonical_issue_checker(item, token, comments)
    if reason:
        return reason

    reason = linked_pr_checker(item, token, comments)
    if reason:
        return reason

    return timeline_pr_checker(item, token)

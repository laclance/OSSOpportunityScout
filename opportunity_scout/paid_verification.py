"""Paid opportunity rejection and competition verification.

Owns paid-specific rejection precedence and GitHub-backed competition checks.
Transport remains injectable so callers can verify behavior without network access.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Final

from opportunity_scout import github, paid
from opportunity_scout.types import GitHubIssue, SourceFailureReason

FetchJson = Callable[[str, str | None], Any]
ExistingPrChecker = Callable[[str, int, str | None], str | None]
ActiveClaimChecker = Callable[[str, int, int, str | None], str | None]

_TIMELINE_FAILURE: Final = "could not verify open implementation PR timeline"
_OPEN_PULL_REQUEST_FAILURE: Final = "could not verify repository open implementation PRs"
_CLAIM_FAILURE: Final = "could not verify active claim comments"
_MAX_RELATIONSHIP_GAP: Final = 120
_OPEN_PULL_REQUESTS_CACHE: dict[str, object] = {}
_OPEN_PULL_REQUESTS_LOCKS = github.KeyedLockPool()

CLAIM_PATTERNS: Final[tuple[str, ...]] = (
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
)
_CLAIM_MATCHERS: Final = tuple(re.compile(pattern, re.IGNORECASE) for pattern in CLAIM_PATTERNS)

UNFUNDED_PROPOSAL_PATTERNS: Final[tuple[str, ...]] = (
    r"\[bounty proposal\]",
    r"(?m)^\s*(?:\*\*)?bounty proposal(?:\*\*)?\s*$",
    r"\bwould you approve\s+(?:\*\*)?(?:us\$|\$)\s*\d[\d,]*(?:\.\d+)?(?:\*\*)?\s+cash\b",
    r"\bproposed amount, not an existing award\b",
    r"\$\s*\d[\d,]*(?:\.\d+)?\s+proposed\b",
    r"\bwould (?:a |an )?(?:us\$|\$)?\s*\d[\d,]*(?:\.\d+)? bounty be appropriate\b",
    r"\bpropos(?:e|ed|ing) (?:a )?(?:paid work|bounty)\b",
)
_UNFUNDED_PROPOSAL_MATCHERS: Final = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in UNFUNDED_PROPOSAL_PATTERNS
)

META_ALERT_MARKERS: Final[tuple[str, ...]] = (
    "new in-scope",
    "bug bounty program(s) added",
    "bounty-watch",
    "bounty watch",
)

_RELATIONSHIP_TERM: Final = re.compile(
    r"\b(?:fix(?:es|ed|ing)?|close(?:s|d|ing)?|resolve(?:s|d|ing)?|"
    r"implement(?:s|ed|ing|ation)?|address(?:es|ed|ing)?|part\s+of)\b",
    re.IGNORECASE,
)
_SENTENCE_BOUNDARIES: Final = ".!?\n"
_ASSOCIATED_ISSUE_CONTEXT: Final = re.compile(
    r"\b(?:which\s+issue\(s\)\s+this\s+pr\s+is\s+related\s+to|"
    r"related\s+issues?|associated\s+issues?|as\s+i\s+mentioned\s+in)\b",
    re.IGNORECASE,
)
_ASSOCIATED_ISSUE_CONTEXT_GAP: Final = 900


@dataclass(frozen=True, slots=True)
class _PaidIssueEvidence:
    text: str
    text_lower: str
    labels_lower: str

    @classmethod
    def from_issue(cls, item: GitHubIssue) -> _PaidIssueEvidence:
        title = str(item.get("title", ""))
        body = str(item.get("body", ""))
        text = f"{title}\n{body}"
        labels = item.get("labels") or []
        label_names = (
            str(label.get("name", "")) if isinstance(label, dict) else str(label)
            for label in labels
        )
        return cls(text=text, text_lower=text.lower(), labels_lower=" ".join(label_names).lower())

    def early_rejection(self) -> str | None:
        if any(pattern.search(self.text) for pattern in _UNFUNDED_PROPOSAL_MATCHERS):
            return "unfunded bounty proposal, not an existing award"
        if any(
            marker in self.text_lower or marker in self.labels_lower
            for marker in META_ALERT_MARKERS
        ):
            return "meta/monitoring alert, not a contributor task"
        return None


@dataclass(frozen=True, slots=True)
class _TargetIssue:
    repo: str
    number: int

    @property
    def repository_api_url(self) -> str:
        return f"https://api.github.com/repos/{self.repo}"

    def same_repository(self, repository_url: str) -> bool:
        return repository_url.rstrip("/").casefold() == self.repository_api_url.casefold()

    def reference_patterns(self, *, allow_bare: bool) -> tuple[re.Pattern[str], ...]:
        escaped_repo = re.escape(self.repo)
        patterns = [
            re.compile(
                rf"https?://github\.com/{escaped_repo}/issues/{self.number}\b",
                re.IGNORECASE,
            ),
            re.compile(rf"{escaped_repo}#{self.number}\b", re.IGNORECASE),
        ]
        if allow_bare:
            patterns.append(re.compile(rf"(?<![\w/-])#{self.number}\b", re.IGNORECASE))
        return tuple(patterns)


class _TimelineEvidenceState(Enum):
    IRRELEVANT = auto()
    INCOMPLETE = auto()
    OPEN_PR = auto()


@dataclass(frozen=True, slots=True)
class _OpenPullRequestEvidence:
    url: str
    repository_url: str
    title: str
    body: str | None

    @classmethod
    def classify(
        cls,
        raw_event: object,
    ) -> tuple[_TimelineEvidenceState, _OpenPullRequestEvidence | None]:
        if not isinstance(raw_event, dict) or raw_event.get("event") != "cross-referenced":
            return _TimelineEvidenceState.IRRELEVANT, None

        source = raw_event.get("source")
        if not isinstance(source, dict):
            return _TimelineEvidenceState.IRRELEVANT, None
        source_issue = source.get("issue")
        if not isinstance(source_issue, dict):
            return _TimelineEvidenceState.IRRELEVANT, None
        if "pull_request" not in source_issue or source_issue.get("state") != "open":
            return _TimelineEvidenceState.IRRELEVANT, None

        url = source_issue.get("html_url")
        if not isinstance(url, str) or not url:
            return _TimelineEvidenceState.IRRELEVANT, None

        title = source_issue.get("title")
        repository_url = source_issue.get("repository_url")
        body = source_issue.get("body")
        if not isinstance(title, str) or not isinstance(repository_url, str):
            return _TimelineEvidenceState.INCOMPLETE, None
        if body is not None and not isinstance(body, str):
            return _TimelineEvidenceState.INCOMPLETE, None

        return (
            _TimelineEvidenceState.OPEN_PR,
            cls(url=url, repository_url=repository_url, title=title, body=body),
        )

    def implements(self, target: _TargetIssue) -> bool:
        text = f"{self.title}\n{self.body or ''}"
        patterns = target.reference_patterns(allow_bare=target.same_repository(self.repository_url))
        return any(
            _relationship_precedes_reference(text, match.start())
            for pattern in patterns
            for match in pattern.finditer(text)
        )

    def implements_from_repository_listing(self, target: _TargetIssue) -> bool:
        """Recognize PR associations omitted from same-repository issue timelines."""
        text = f"{self.title}\n{self.body or ''}"
        for pattern in target.reference_patterns(allow_bare=True):
            for match in pattern.finditer(text):
                if _relationship_precedes_reference(text, match.start()):
                    return True
                context_start = max(0, match.start() - _ASSOCIATED_ISSUE_CONTEXT_GAP)
                if _ASSOCIATED_ISSUE_CONTEXT.search(text[context_start : match.start()]):
                    return True
        return False


def _relationship_precedes_reference(text: str, reference_start: int) -> bool:
    sentence_start = 0
    for boundary in _SENTENCE_BOUNDARIES:
        sentence_start = max(sentence_start, text.rfind(boundary, 0, reference_start) + 1)

    prefix = text[sentence_start:reference_start]
    matches = list(_RELATIONSHIP_TERM.finditer(prefix))
    if not matches:
        return False
    return reference_start - (sentence_start + matches[-1].end()) <= _MAX_RELATIONSHIP_GAP


def existing_implementation_pr_reason(
    timeline: object,
    repo: str,
    issue_number: int,
) -> str | None:
    """Evaluate complete timeline evidence for an open implementation PR."""
    if not isinstance(timeline, list):
        return SourceFailureReason(_TIMELINE_FAILURE)

    target = _TargetIssue(repo=repo, number=issue_number)
    for raw_event in timeline:
        state, candidate = _OpenPullRequestEvidence.classify(raw_event)
        if state is _TimelineEvidenceState.INCOMPLETE:
            return SourceFailureReason(_TIMELINE_FAILURE)
        if state is _TimelineEvidenceState.OPEN_PR and candidate is not None:
            if candidate.implements(target):
                return f"existing open implementation PR: {candidate.url}"
    return None


def repository_open_implementation_pr_reason(
    repo: str,
    issue_number: int,
    token: str | None,
    *,
    fetch_open_pulls: FetchJson | None = None,
) -> str | None:
    """Check cached same-repository open PRs when issue timelines omit relationships."""
    url = f"https://api.github.com/repos/{repo}/pulls?state=open&per_page=100"
    if fetch_open_pulls is None:
        open_pulls = github.cached_value(
            _OPEN_PULL_REQUESTS_CACHE,
            repo,
            lambda: github.github_collection(url, token),
            _OPEN_PULL_REQUESTS_LOCKS,
            namespace="open-pulls",
        )
    else:
        open_pulls = fetch_open_pulls(url, token)

    if not isinstance(open_pulls, list):
        return SourceFailureReason(_OPEN_PULL_REQUEST_FAILURE)

    target = _TargetIssue(repo=repo, number=issue_number)
    for raw_pull in open_pulls:
        if not isinstance(raw_pull, dict):
            return SourceFailureReason(_OPEN_PULL_REQUEST_FAILURE)

        html_url = raw_pull.get("html_url")
        title = raw_pull.get("title")
        body = raw_pull.get("body")
        if not isinstance(html_url, str) or not html_url or not isinstance(title, str):
            return SourceFailureReason(_OPEN_PULL_REQUEST_FAILURE)
        if body is not None and not isinstance(body, str):
            return SourceFailureReason(_OPEN_PULL_REQUEST_FAILURE)

        evidence = _OpenPullRequestEvidence(
            url=html_url,
            repository_url=target.repository_api_url,
            title=title,
            body=body,
        )
        if evidence.implements_from_repository_listing(target):
            return f"existing open implementation PR: {html_url}"
    return None


def has_existing_implementation_pr(
    repo: str,
    issue_number: int,
    token: str | None,
    *,
    fetch_json: FetchJson | None = None,
    fetch_open_pulls: FetchJson | None = None,
) -> str | None:
    """Verify implementation competition from timeline and repository PR evidence."""
    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}/timeline?per_page=100"
    timeline = (
        github.github_collection(url, token) if fetch_json is None else fetch_json(url, token)
    )
    reason = existing_implementation_pr_reason(timeline, repo, issue_number)
    if reason is not None:
        return reason

    # Existing timeline-only test/injection callers stay bounded unless they
    # explicitly provide the repository fallback transport as well.
    if fetch_json is not None and fetch_open_pulls is None:
        return None
    return repository_open_implementation_pr_reason(
        repo,
        issue_number,
        token,
        fetch_open_pulls=fetch_open_pulls,
    )


def _claim_reason(raw_comment: object) -> str | None:
    if not isinstance(raw_comment, dict):
        return None
    body = str(raw_comment.get("body", ""))
    if not any(pattern.search(body) for pattern in _CLAIM_MATCHERS):
        return None

    user = raw_comment.get("user")
    author = user.get("login", "someone") if isinstance(user, dict) else "someone"
    return f"active claim by @{author}"


def _comments_url(repo: str, issue_number: int, comments_count: int) -> str:
    params = urllib.parse.urlencode(
        {
            "per_page": min(int(comments_count), 30),
            "sort": "created",
            "direction": "desc",
        }
    )
    return f"https://api.github.com/repos/{repo}/issues/{issue_number}/comments?{params}"


def active_claim_reason(
    repo: str,
    issue_number: int,
    comments_count: int,
    token: str | None,
    *,
    fetch_json: FetchJson | None = None,
) -> str | None:
    """Inspect the existing bounded comment request for the first recognized claim."""
    if not comments_count:
        return None

    url = _comments_url(repo, issue_number, comments_count)
    comments = github.github_get(url, token) if fetch_json is None else fetch_json(url, token)
    if not isinstance(comments, list):
        return SourceFailureReason(_CLAIM_FAILURE)

    for raw_comment in comments:
        reason = _claim_reason(raw_comment)
        if reason is not None:
            return reason
    return None


def candidate_rejection_reason(
    item: GitHubIssue,
    token: str | None,
    *,
    fetch_json: FetchJson | None = None,
    existing_pr_checker: ExistingPrChecker | None = None,
    active_claim_checker: ActiveClaimChecker | None = None,
) -> tuple[str | None, str | None]:
    """Apply paid-candidate verification in stable rejection-precedence order."""
    early_reason = _PaidIssueEvidence.from_issue(item).early_rejection()
    if early_reason is not None:
        return early_reason, None

    signal = paid.payment_signal(item)
    if not signal:
        return "no explicit payment signal", None

    repo, issue_number = github.issue_repo_and_number(item)
    if not repo or not issue_number:
        return "could not identify repository/issue number", signal

    pr_reason = (
        has_existing_implementation_pr(repo, issue_number, token, fetch_json=fetch_json)
        if existing_pr_checker is None
        else existing_pr_checker(repo, issue_number, token)
    )
    if pr_reason:
        return pr_reason, signal

    comments_count = int(item.get("comments", 0))
    claim_reason = (
        active_claim_reason(
            repo,
            issue_number,
            comments_count,
            token,
            fetch_json=fetch_json,
        )
        if active_claim_checker is None
        else active_claim_checker(repo, issue_number, comments_count, token)
    )
    if claim_reason:
        return claim_reason, signal

    return None, signal

"""Canonical static domain records for OSS Opportunity Scout.

External JSON remains dynamic until transport/source code validates the fields it
needs. These types describe trusted mapping shapes passed between scanner policy,
scoring, reporting, and orchestration while preserving the existing dictionary runtime
representation.
"""

from __future__ import annotations

from typing import Literal, NotRequired, TypeAlias, TypedDict

EffortBucket: TypeAlias = Literal["<1h", "1–3h", "3–6h", "6–12h", "1d+"]
CompetitionLevel: TypeAlias = Literal["none", "low", "medium", "high"]
CandidateLane: TypeAlias = Literal["paid", "strategic"]
IssueLifecycleStatus: TypeAlias = Literal["open", "closed", "not_found", "failed"]


class GitHubLabel(TypedDict, total=False):
    """Normalized GitHub label fields consumed by scanner policy."""

    name: str


class GitHubUser(TypedDict, total=False):
    """Normalized GitHub user fields consumed by scanner policy."""

    login: str


class GitHubComment(TypedDict, total=False):
    """Partial normalized issue-comment shape used after JSON validation."""

    body: str | None
    author_association: str
    user: GitHubUser | None
    created_at: str | None
    updated_at: str | None


class GitHubIssue(TypedDict, total=False):
    """Partial normalized issue shape accepted from GitHub and platform sources."""

    html_url: str
    title: str
    body: str | None
    author_association: str
    state: str
    number: int
    comments: int
    labels: list[GitHubLabel | str]
    assignees: list[GitHubUser]
    user: GitHubUser | None
    updated_at: str | None
    created_at: str | None
    repository_url: str
    pull_request: object


class RepositoryMetadata(TypedDict, total=False):
    """Repository fields used by filtering, activity checks, and scoring."""

    stargazers_count: int
    pushed_at: str | None
    archived: bool
    language: str | None


class GitHubSearchResult(TypedDict, total=False):
    """GitHub Search payload fields consumed by discovery orchestration."""

    items: list[GitHubIssue]


IssueRow: TypeAlias = tuple[int, int, int, GitHubIssue]
SearchBatch: TypeAlias = tuple[str, GitHubSearchResult]


class Candidate(TypedDict):
    """Canonical ranked candidate shape shared by ranking and presentation."""

    repo: str
    issue_number: int
    title: str | None
    url: str
    paid: bool
    reward: str | None
    payment_confidence: int
    cash_score: int
    career_score: int
    priority_score: int
    priority_reasons: list[str]
    effort: EffortBucket
    effort_reasons: list[str]
    expected_hourly: float | None
    competition: CompetitionLevel
    stars: int
    recent_activity: str
    language: str
    labels: list[str]
    cash_reasons: list[str]
    career_reasons: list[str]
    contribution_guide: str | None
    comments: int
    updated_at: str | None
    rejection_reason: str | None


class RejectionRecord(TypedDict):
    """Stable rejection/audit example rendered in reports."""

    reason: str
    url: NotRequired[str | None]
    title: NotRequired[str | None]


AuditRecord: TypeAlias = RejectionRecord

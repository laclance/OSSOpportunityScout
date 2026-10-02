from __future__ import annotations

from typing import Any, Literal, cast

from bountyscout.types import Candidate, GitHubComment, GitHubIssue


class FakeResponse:
    """Minimal urllib response context manager used across transport tests."""

    def __init__(self, body: bytes = b"{}") -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: Any) -> Literal[False]:
        return False

    def read(self) -> bytes:
        return self.body


def candidate(**overrides: Any) -> Candidate:
    """Build a transparent canonical candidate for cross-module tests."""

    item: dict[str, Any] = {
        "repo": "example/project",
        "issue_number": 42,
        "title": "Fix deterministic network regression",
        "url": "https://github.com/example/project/issues/42",
        "paid": False,
        "reward": None,
        "payment_confidence": 0,
        "cash_score": 0,
        "career_score": 80,
        "priority_score": 93,
        "priority_reasons": ["1–3h execution bonus", "no visible competition bonus"],
        "effort": "1–3h",
        "effort_reasons": ["bounded deterministic bug signal"],
        "expected_hourly": None,
        "competition": "none",
        "stars": 1500,
        "recent_activity": "active in last 7d",
        "language": "Go",
        "labels": ["help wanted"],
        "cash_reasons": [],
        "career_reasons": ["target repo bonus", "Go codebase"],
        "contribution_guide": "https://github.com/example/project/CONTRIBUTING.md",
        "comments": 0,
        "updated_at": "2026-10-01T00:00:00Z",
        "rejection_reason": None,
    }
    item.update(overrides)
    return cast(Candidate, item)


def issue(**overrides: Any) -> GitHubIssue:
    """Build a transparent normalized GitHub issue for policy tests."""

    item: dict[str, Any] = {
        "html_url": "https://github.com/example/project/issues/42",
        "title": "Network regression",
        "body": "",
        "state": "open",
        "comments": 1,
        "labels": [],
        "assignees": [],
        "user": {"login": "reporter"},
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
    }
    item.update(overrides)
    return cast(GitHubIssue, item)


def comment(**overrides: Any) -> GitHubComment:
    """Build a transparent normalized GitHub comment for policy tests."""

    item: dict[str, Any] = {
        "body": "",
        "author_association": "NONE",
        "user": {"login": "commenter"},
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-10-01T00:00:00Z",
    }
    item.update(overrides)
    return cast(GitHubComment, item)

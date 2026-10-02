"""Safely close historical generated OSS Opportunity Scout report issues."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Sequence, cast

API_ROOT = "https://api.github.com"
REPORT_LABEL = "bounty-alert"
AUTOMATION_AUTHOR = "github-actions[bot]"
LEGACY_TITLE_PREFIX = "🎯 OSS Opportunity Queue:"
LEGACY_BODY_MARKER = "### Ranked OSS Opportunity Queue"
CURRENT_TITLE_PREFIX = "📊 SCAN REPORT — OSS Opportunity Queue:"
CURRENT_BODY_MARKER = "<!-- bountyscout-report: automated; actionable: false -->"
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REPOSITORY_URL_RE = re.compile(r"^https://api\.github\.com/repos/([^/]+/[^/]+)$")


@dataclass(frozen=True)
class ReportIssue:
    """Validated issue evidence used by generated-report identification policy."""

    repository: str
    number: int
    title: str
    body: str
    state: str
    labels: frozenset[str]
    author_login: str
    is_pull_request: bool


@dataclass(frozen=True)
class EnumerationResult:
    """Normalized open issue enumeration plus safely skipped malformed items."""

    issues: tuple[ReportIssue, ...]
    malformed_count: int


@dataclass(frozen=True)
class CleanupResult:
    """Deterministic summary of one dry-run or apply execution."""

    selected: tuple[ReportIssue, ...]
    skipped_count: int
    closed_numbers: tuple[int, ...]
    failed_numbers: tuple[int, ...]


class CleanupError(RuntimeError):
    """Raised when enumeration or mutation cannot be completed safely."""


def validate_repository(repository: str) -> str:
    """Validate and normalize the configured GitHub owner/repository name."""
    candidate = repository.strip()
    if not REPOSITORY_RE.fullmatch(candidate):
        raise CleanupError("GITHUB_REPOSITORY must be in owner/repository form")
    return candidate


def _string_field(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _label_names(value: object) -> frozenset[str] | None:
    if not isinstance(value, list):
        return None
    names: set[str] = set()
    for label in value:
        if isinstance(label, str):
            names.add(label)
            continue
        if not isinstance(label, dict):
            return None
        label_payload = cast(dict[str, object], label)
        name = _string_field(label_payload, "name")
        if name is None:
            return None
        names.add(name)
    return frozenset(names)


def _repository_from_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = REPOSITORY_URL_RE.fullmatch(value)
    return match.group(1) if match else None


def normalize_issue(value: object) -> ReportIssue | None:
    """Validate the fields required for report identity; reject incomplete evidence."""
    if not isinstance(value, dict):
        return None
    payload = cast(dict[str, object], value)
    repository = _repository_from_url(payload.get("repository_url"))
    number = payload.get("number")
    title = _string_field(payload, "title")
    body = payload.get("body")
    state = _string_field(payload, "state")
    labels = _label_names(payload.get("labels"))
    user = payload.get("user")
    if not isinstance(user, dict):
        return None
    author_login = _string_field(cast(dict[str, object], user), "login")
    if (
        repository is None
        or not isinstance(number, int)
        or isinstance(number, bool)
        or number <= 0
        or title is None
        or not isinstance(body, str)
        or state is None
        or labels is None
        or author_login is None
    ):
        return None
    return ReportIssue(
        repository=repository,
        number=number,
        title=title,
        body=body,
        state=state,
        labels=labels,
        author_login=author_login,
        is_pull_request="pull_request" in payload,
    )


def is_confirmed_generated_report(issue: ReportIssue, repository: str) -> bool:
    """Return whether all required signals identify a generated queue report."""
    if (
        issue.state != "open"
        or issue.repository.casefold() != repository.casefold()
        or issue.is_pull_request
        or REPORT_LABEL not in issue.labels
        or issue.author_login != AUTOMATION_AUTHOR
    ):
        return False

    legacy_match = issue.title.startswith(LEGACY_TITLE_PREFIX) and LEGACY_BODY_MARKER in issue.body
    current_match = (
        issue.title.startswith(CURRENT_TITLE_PREFIX) and CURRENT_BODY_MARKER in issue.body
    )
    return legacy_match or current_match


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "OSSOpportunityScout-report-cleanup",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _request_json(
    url: str,
    token: str,
    *,
    method: str = "GET",
    payload: dict[str, str] | None = None,
) -> object:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, headers=_headers(token), data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return cast(object, json.loads(response.read().decode("utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CleanupError(f"GitHub API request failed for {url}: {exc}") from exc


def fetch_open_bounty_alert_issues(repository: str, token: str) -> EnumerationResult:
    """Enumerate open labeled issues from only the configured repository."""
    validated_repository = validate_repository(repository)
    issues: list[ReportIssue] = []
    malformed_count = 0
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {
                "state": "open",
                "labels": REPORT_LABEL,
                "per_page": 100,
                "page": page,
            }
        )
        url = f"{API_ROOT}/repos/{validated_repository}/issues?{query}"
        payload = _request_json(url, token)
        if not isinstance(payload, list):
            raise CleanupError(f"GitHub issues response was not a list for page {page}")
        for raw_issue in payload:
            issue = normalize_issue(raw_issue)
            if issue is None:
                malformed_count += 1
            else:
                issues.append(issue)
        if len(payload) < 100:
            break
        page += 1
    return EnumerationResult(tuple(issues), malformed_count)


def close_report(repository: str, token: str, issue_number: int) -> None:
    """Close one already-verified generated report as not planned."""
    validated_repository = validate_repository(repository)
    url = f"{API_ROOT}/repos/{validated_repository}/issues/{issue_number}"
    payload = _request_json(
        url,
        token,
        method="PATCH",
        payload={"state": "closed", "state_reason": "not_planned"},
    )
    if not isinstance(payload, dict):
        raise CleanupError(f"GitHub close response was not an object for issue #{issue_number}")


def cleanup_reports(repository: str, token: str, *, apply: bool) -> CleanupResult:
    """Select strongly verified reports and optionally close them deterministically."""
    validated_repository = validate_repository(repository)
    enumeration = fetch_open_bounty_alert_issues(validated_repository, token)
    selected = tuple(
        issue
        for issue in enumeration.issues
        if is_confirmed_generated_report(issue, validated_repository)
    )
    skipped_count = len(enumeration.issues) - len(selected) + enumeration.malformed_count
    if not apply:
        return CleanupResult(selected, skipped_count, (), ())

    closed: list[int] = []
    failed: list[int] = []
    for issue in selected:
        try:
            close_report(validated_repository, token, issue.number)
        except CleanupError as exc:
            failed.append(issue.number)
            print(f"Failed to close #{issue.number}: {exc}", file=sys.stderr)
        else:
            closed.append(issue.number)
    return CleanupResult(selected, skipped_count, tuple(closed), tuple(failed))


def _print_result(repository: str, result: CleanupResult, *, apply: bool) -> None:
    mode = "apply" if apply else "dry-run"
    print(f"Repository: {repository}")
    print(f"Mode: {mode}")
    for issue in result.selected:
        print(f"SELECT #{issue.number}: {issue.title}")
    print(f"Selected: {len(result.selected)}")
    print(f"Skipped/non-matching: {result.skipped_count}")
    print(f"Closed: {len(result.closed_numbers)}")
    print(f"Failed: {len(result.failed_numbers)}")
    if not apply:
        print("No mutations made. Re-run with --apply to close selected reports.")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the one-time cleanup in dry-run mode unless --apply is explicit."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="close confirmed generated reports")
    args = parser.parse_args(argv)

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not repository or not token:
        print("GITHUB_REPOSITORY and GITHUB_TOKEN are required", file=sys.stderr)
        return 2

    try:
        validated_repository = validate_repository(repository)
        result = cleanup_reports(validated_repository, token, apply=args.apply)
    except CleanupError as exc:
        print(f"Cleanup failed: {exc}", file=sys.stderr)
        return 1

    _print_result(validated_repository, result, apply=args.apply)
    return 1 if result.failed_numbers else 0


if __name__ == "__main__":
    raise SystemExit(main())

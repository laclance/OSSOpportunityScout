"""Shared GitHub HTTP access and per-scan cache primitives.

OSS Opportunity Scout uses this module for GitHub JSON transport and keyed cache
fills. It deliberately does not own scanner policy, ranking, or long-lived state.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any, Final, TypeVar, cast

from bountyscout.types import (
    GitHubComment,
    GitHubIssue,
    GitHubSearchResult,
    IssueLifecycleStatus,
    RepositoryMetadata,
)

T = TypeVar("T")

GITHUB_ACCEPT: Final = "application/vnd.github+json"
GITHUB_USER_AGENT: Final = "OSSOpportunityScout"
GITHUB_API_VERSION: Final = "2022-11-28"
GITHUB_API_HEADERS: Final[Mapping[str, str]] = {
    "Accept": GITHUB_ACCEPT,
    "User-Agent": GITHUB_USER_AGENT,
    "X-GitHub-Api-Version": GITHUB_API_VERSION,
}
GITHUB_SAFE_READ_MAX_ATTEMPTS: Final = 3
GITHUB_MAX_RETRY_DELAY_SECONDS: Final = 120.0
GITHUB_SECONDARY_RETRY_BASE_SECONDS: Final = 60.0
GITHUB_TRANSIENT_RETRY_BASE_SECONDS: Final = 1.0
_TRANSIENT_HTTP_STATUSES: Final = frozenset({500, 502, 503, 504})


@dataclass(frozen=True)
class IssueLifecycleResult:
    status: IssueLifecycleStatus


@dataclass(frozen=True)
class _GitHubReadResult:
    payload: object | None
    failure: str | None = None
    status: int | None = None


class KeyedLockPool:
    """Provide stable per-key locks while allowing unrelated cache fills in parallel."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._locks: dict[str, Lock] = {}

    def lock_for(self, key: str) -> Lock:
        """Return one shared lock for a cache key."""
        with self._guard:
            return self._locks.setdefault(key, Lock())


def cached_value(
    cache: MutableMapping[str, T],
    key: str,
    loader: Callable[[], T],
    locks: KeyedLockPool,
    *,
    namespace: str,
) -> T:
    """Load a missing cache value once, serialized only against the same key."""
    if key in cache:
        return cache[key]

    with locks.lock_for(f"{namespace}:{key}"):
        if key not in cache:
            cache[key] = loader()
        return cache[key]


def _github_headers(token: str | None) -> dict[str, str]:
    headers = dict(GITHUB_API_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def github_get(
    url: str,
    token: str | None = None,
    timeout: int = 20,
    *,
    log_errors: bool = True,
) -> Any:
    """Fetch JSON from GitHub with bounded retries for safe GET failures."""
    result = _github_json_get(url, token, timeout)
    if result.failure is not None and log_errors:
        print(f"GitHub API Error ({result.failure}) for {url}.")
    return result.payload


def search_github(
    query: str,
    token: str | None = None,
    per_page: int = 15,
    *,
    fetch_json: Callable[[str, str | None], Any] | None = None,
) -> GitHubSearchResult:
    """Fetch one GitHub Issues Search page with canonical request normalization."""
    url = "https://api.github.com/search/issues?" + urllib.parse.urlencode(
        {"q": query, "per_page": per_page}
    )
    data = github_get(url, token) if fetch_json is None else fetch_json(url, token)
    return cast(GitHubSearchResult, data) if isinstance(data, dict) else {}


def _issue_repo_and_number_from_url(url: str) -> tuple[str | None, int | None]:
    match = re.fullmatch(r"https://github\.com/([^/]+/[^/]+)/issues/(\d+)", url)
    if not match:
        return None, None
    return match.group(1), int(match.group(2))


def issue_repo_and_number(
    item: GitHubIssue,
) -> tuple[str | None, int | None]:
    """Extract owner/repo and issue number from a canonical GitHub issue URL."""
    return _issue_repo_and_number_from_url(str(item.get("html_url", "")))


def parse_github_datetime(value: Any) -> datetime | None:
    """Parse a GitHub ISO timestamp, returning None when unavailable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def issue_lifecycle(
    url: str,
    token: str | None,
    timeout: int = 20,
) -> IssueLifecycleResult:
    """Fetch direct issue lifecycle evidence without equating failures with closure."""
    repo, number = _issue_repo_and_number_from_url(url)
    if not repo or number is None:
        return IssueLifecycleResult("failed")

    endpoint = f"https://api.github.com/repos/{repo}/issues/{number}"
    result = _github_json_get(endpoint, token, timeout)
    if result.status == 404:
        return IssueLifecycleResult("not_found")
    payload = result.payload
    if not isinstance(payload, dict) or "pull_request" in payload:
        return IssueLifecycleResult("failed")
    state = payload.get("state")
    if state == "open":
        return IssueLifecycleResult("open")
    if state == "closed":
        return IssueLifecycleResult("closed")
    return IssueLifecycleResult("failed")


def repo_metadata(repo: str, token: str | None) -> RepositoryMetadata:
    """Fetch lightweight repository metadata used for filtering and ranking."""
    data = github_get(f"https://api.github.com/repos/{repo}", token)
    return cast(RepositoryMetadata, data) if isinstance(data, dict) else {}


def issue_comments_checked(
    item: GitHubIssue,
    token: str | None,
) -> tuple[list[GitHubComment], str | None]:
    """Fetch issue comments and distinguish source failure from a real empty thread."""
    repo, number = issue_repo_and_number(item)
    if not repo or not number:
        return [], "could not identify repository/issue number"
    if not int(item.get("comments") or 0):
        return [], None

    comments = github_get(
        f"https://api.github.com/repos/{repo}/issues/{number}/comments?per_page=100",
        token,
    )
    if not isinstance(comments, list):
        return [], "could not refresh issue comments"
    return cast(list[GitHubComment], comments), None


def issue_comments(
    item: GitHubIssue,
    token: str | None,
) -> list[GitHubComment]:
    """Compatibility helper returning an empty list when comment fetching fails."""
    comments, _ = issue_comments_checked(item, token)
    return comments


def contribution_guide(
    repo: str,
    token: str | None,
    getter: Callable[[str, str | None], Any] | None = None,
) -> str | None:
    """Return the first contribution guide found at common repository paths."""
    load = getter or (lambda url, auth: github_get(url, auth, timeout=10, log_errors=False))
    for path in ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md"):
        data = load(
            f"https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(path)}",
            token,
        )
        if isinstance(data, dict) and data.get("html_url"):
            return str(data["html_url"])
    return None


def issue_from_github_url(url: str, token: str | None) -> GitHubIssue | None:
    """Fetch a GitHub issue object from its canonical issue URL."""
    repo, number = _issue_repo_and_number_from_url(str(url))
    if not repo or not number:
        return None
    item = github_get(
        f"https://api.github.com/repos/{repo}/issues/{number}",
        token,
    )
    return cast(GitHubIssue, item) if isinstance(item, dict) else None


def _header_seconds(headers: Any, name: str) -> float | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read()
    except Exception:
        return ""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message")
    return str(message).lower() if isinstance(message, str) else ""


def _rate_limit_retry_delay(
    exc: urllib.error.HTTPError,
    *,
    retry_index: int,
) -> tuple[bool, float | None]:
    if exc.code not in {403, 429}:
        return False, None

    headers: Any = exc.headers or {}
    retry_after = _header_seconds(headers, "Retry-After")
    if retry_after is not None:
        return True, retry_after

    remaining = headers.get("X-RateLimit-Remaining")
    if remaining == "0":
        reset_at = _header_seconds(headers, "X-RateLimit-Reset")
        if reset_at is None:
            return True, None
        return True, max(0.0, reset_at - time.time())

    message = _http_error_message(exc)
    secondary = exc.code == 429 or "secondary rate limit" in message or "abuse detection" in message
    if secondary:
        return True, GITHUB_SECONDARY_RETRY_BASE_SECONDS * (2.0**retry_index)
    return False, None


def _transient_retry_delay(retry_index: int) -> float:
    return GITHUB_TRANSIENT_RETRY_BASE_SECONDS * (2.0**retry_index)


def _retry_allowed(delay: float | None) -> bool:
    return delay is not None and delay <= GITHUB_MAX_RETRY_DELAY_SECONDS


def _url_error_is_transient(exc: urllib.error.URLError) -> bool:
    return isinstance(exc.reason, (TimeoutError, ConnectionError))


def _github_json_get(
    url: str,
    token: str | None,
    timeout: int,
) -> _GitHubReadResult:
    attempt = 0
    while True:
        request = urllib.request.Request(url, headers=_github_headers(token), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            try:
                return _GitHubReadResult(json.loads(raw.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return _GitHubReadResult(None, "malformed response", 200)
        except urllib.error.HTTPError as exc:
            is_rate_limited, delay = _rate_limit_retry_delay(exc, retry_index=attempt)
            if is_rate_limited:
                failure = "rate limited"
            elif exc.code == 401:
                return _GitHubReadResult(None, "authentication failure", exc.code)
            elif exc.code == 403:
                return _GitHubReadResult(None, "forbidden", exc.code)
            elif exc.code == 404:
                return _GitHubReadResult(None, "not found", exc.code)
            elif exc.code in _TRANSIENT_HTTP_STATUSES:
                failure = "temporary server failure"
                retry_after = _header_seconds(exc.headers or {}, "Retry-After")
                delay = retry_after if retry_after is not None else _transient_retry_delay(attempt)
            else:
                return _GitHubReadResult(None, "HTTP failure", exc.code)
            status = exc.code
        except (TimeoutError, ConnectionError):
            failure = "temporary transport failure"
            delay = _transient_retry_delay(attempt)
            status = None
        except urllib.error.URLError as exc:
            if not _url_error_is_transient(exc):
                return _GitHubReadResult(None, "transport failure")
            failure = "temporary transport failure"
            delay = _transient_retry_delay(attempt)
            status = None
        except Exception:
            return _GitHubReadResult(None, "transport failure")

        attempt += 1
        if attempt >= GITHUB_SAFE_READ_MAX_ATTEMPTS or not _retry_allowed(delay):
            return _GitHubReadResult(None, failure, status)
        time.sleep(cast(float, delay))

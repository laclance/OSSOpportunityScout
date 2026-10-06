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

from opportunity_scout.types import (
    GitHubComment,
    GitHubIssue,
    GitHubSearchResult,
    IssueLifecycleStatus,
    RepositoryMetadata,
    SourceFailureReason,
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
_REQUEST_STATS_LOCK = Lock()
_REQUEST_COUNTS: dict[str, int] = {
    "search": 0,
    "issues_list": 0,
    "issue": 0,
    "comments": 0,
    "timeline": 0,
    "pull": 0,
    "repository": 0,
    "contents": 0,
    "other": 0,
}


@dataclass(frozen=True)
class GitHubRequestStats:
    """Bounded per-process counters for outbound GitHub API request attempts."""

    search: int
    issues_list: int
    issue: int
    comments: int
    timeline: int
    pull: int
    repository: int
    contents: int
    other: int

    @property
    def total(self) -> int:
        return (
            self.search
            + self.issues_list
            + self.issue
            + self.comments
            + self.timeline
            + self.pull
            + self.repository
            + self.contents
            + self.other
        )


@dataclass(frozen=True)
class IssueLifecycleResult:
    status: IssueLifecycleStatus


@dataclass(frozen=True)
class _GitHubReadResult:
    payload: object | None
    failure: str | None = None
    status: int | None = None
    link: str | None = None


class _NoPaginationRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        """Pagination fails closed on redirects before forwarding credentials."""
        raise urllib.error.URLError("pagination redirect refused")


def _pagination_open(request: urllib.request.Request, *, timeout: int) -> Any:
    return urllib.request.build_opener(_NoPaginationRedirect()).open(request, timeout=timeout)


def _next_link(link: str | None) -> str | None:
    """Reject unusable pagination metadata rather than claiming complete evidence."""
    if link is None:
        return None
    next_url = None
    for part in link.split(","):
        match = re.fullmatch(r'\s*<([^<>\s]+)>;\s*rel="(next|prev|first|last)"\s*', part)
        if match is None:
            raise ValueError("malformed pagination Link")
        if match.group(2) == "next":
            if next_url is not None:
                raise ValueError("multiple next links")
            next_url = match.group(1)
    return next_url


def _pagination_identity(url: str) -> tuple[str, tuple[tuple[str, str], ...], int]:
    """Only HTTPS GitHub API URLs with an unambiguous numeric page are usable."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.fragment:
        raise ValueError("untrusted pagination URL")
    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    pages = [value for key, value in params if key == "page"]
    if len(pages) > 1 or (pages and not re.fullmatch(r"[1-9]\d*", pages[0])):
        raise ValueError("unusable pagination page")
    return (
        parsed.path,
        tuple(sorted((k, v) for k, v in params if k != "page")),
        int(pages[0] if pages else "1"),
    )


def _verified_repository_alias(path: str, next_path: str, token: str | None) -> bool:
    """GitHub's numeric repository Links must identify the requested repository."""
    original = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)", path)
    alias = re.fullmatch(r"/repositories/([1-9]\d*)(/.*)", next_path)
    if original is None or alias is None or original[2] != alias[2]:
        return False
    metadata = _github_json_get(
        f"https://api.github.com/repos/{original[1]}", token, 20, pagination=True
    ).payload
    return isinstance(metadata, dict) and metadata.get("id") == int(alias[1])


def github_collection(
    url: str,
    token: str | None = None,
    *,
    max_pages: int | None = None,
) -> list[Any] | None:
    """Follow collection Links explicitly; discard partial evidence on failure.

    max_pages is an intentional caller bound. Complete traversal has a 1000-page
    safety ceiling that fails closed, rather than returning truncated evidence.
    """
    items: list[Any] = []
    if max_pages is not None and max_pages < 1:
        return None
    try:
        path, query, page = _pagination_identity(url)
        allowed_paths = {path}
        visited = {page}
        for count in range(1, 1001):
            result = _github_json_get(url, token, 20, pagination=True)
            if result.failure is not None or not isinstance(result.payload, list):
                return None
            items.extend(result.payload)
            if max_pages is not None and count >= max_pages:
                return items
            next_url = _next_link(result.link)
            if next_url is None:
                return items
            next_path, next_query, next_page = _pagination_identity(next_url)
            if next_path not in allowed_paths:
                if not _verified_repository_alias(path, next_path, token):
                    return None
                allowed_paths.add(next_path)
            if next_query != query or next_page in visited or next_page != page + 1:
                return None
            visited.add(next_page)
            page = next_page
            url = next_url
    except ValueError:
        return None
    return None


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


def _request_category(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    if path == "/search/issues":
        return "search"
    if re.fullmatch(r"/repos/[^/]+/[^/]+/issues", path):
        return "issues_list"
    if re.fullmatch(r"/repos/[^/]+/[^/]+/issues/\d+/comments", path):
        return "comments"
    if re.fullmatch(r"/repos/[^/]+/[^/]+/issues/\d+/timeline", path):
        return "timeline"
    if re.fullmatch(r"/repos/[^/]+/[^/]+/issues/\d+", path):
        return "issue"
    if re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/\d+", path):
        return "pull"
    if re.fullmatch(r"/repos/[^/]+/[^/]+/contents(?:/.*)?", path):
        return "contents"
    if re.fullmatch(r"/repos/[^/]+/[^/]+", path):
        return "repository"
    return "other"


def _record_request_attempt(url: str) -> None:
    category = _request_category(url)
    with _REQUEST_STATS_LOCK:
        _REQUEST_COUNTS[category] += 1


def request_stats_snapshot() -> GitHubRequestStats:
    """Return a thread-safe immutable snapshot of GitHub request attempts."""
    with _REQUEST_STATS_LOCK:
        return GitHubRequestStats(
            search=_REQUEST_COUNTS["search"],
            issues_list=_REQUEST_COUNTS["issues_list"],
            issue=_REQUEST_COUNTS["issue"],
            comments=_REQUEST_COUNTS["comments"],
            timeline=_REQUEST_COUNTS["timeline"],
            pull=_REQUEST_COUNTS["pull"],
            repository=_REQUEST_COUNTS["repository"],
            contents=_REQUEST_COUNTS["contents"],
            other=_REQUEST_COUNTS["other"],
        )


def request_stats_delta(
    before: GitHubRequestStats,
    after: GitHubRequestStats,
) -> GitHubRequestStats:
    """Return the bounded request-attempt delta between two snapshots."""
    return GitHubRequestStats(
        search=after.search - before.search,
        issues_list=after.issues_list - before.issues_list,
        issue=after.issue - before.issue,
        comments=after.comments - before.comments,
        timeline=after.timeline - before.timeline,
        pull=after.pull - before.pull,
        repository=after.repository - before.repository,
        contents=after.contents - before.contents,
        other=after.other - before.other,
    )


def format_request_stats(stats: GitHubRequestStats) -> str:
    """Format stable, machine-readable request-attempt counters for run logs."""
    return (
        f"github_requests={stats.total} search={stats.search} "
        f"issues_list={stats.issues_list} issue={stats.issue} comments={stats.comments} "
        f"timeline={stats.timeline} pull={stats.pull} repository={stats.repository} "
        f"contents={stats.contents} other={stats.other}"
    )


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
    """Fetch one intentionally bounded GitHub Issues Search page."""
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
    """Consume all comment pages for verification; distinguish failure from emptiness."""
    repo, number = issue_repo_and_number(item)
    if not repo or not number:
        return [], "could not identify repository/issue number"
    if not int(item.get("comments") or 0):
        return [], None

    comments = github_collection(
        f"https://api.github.com/repos/{repo}/issues/{number}/comments?per_page=100",
        token,
    )
    if not isinstance(comments, list):
        return [], SourceFailureReason("could not refresh issue comments")
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


def issue_from_github_url_checked(
    url: str,
    token: str | None,
) -> tuple[GitHubIssue | None, SourceFailureReason | None]:
    """Fetch a source issue while preserving failures distinct from stale references."""
    repo, number = _issue_repo_and_number_from_url(str(url))
    if not repo or not number:
        return None, None

    endpoint = f"https://api.github.com/repos/{repo}/issues/{number}"
    result = _github_json_get(endpoint, token, 20)
    if result.failure is not None:
        print(f"GitHub API Error ({result.failure}) for {endpoint}.")
    if result.status == 404:
        return None, None
    if result.failure is not None:
        return None, SourceFailureReason(f"GitHub issue source fetch failed: {result.failure}")
    if not isinstance(result.payload, dict):
        return None, SourceFailureReason("GitHub issue source fetch failed: malformed response")
    return cast(GitHubIssue, result.payload), None


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
    *,
    pagination: bool = False,
) -> _GitHubReadResult:
    attempt = 0
    while True:
        _record_request_attempt(url)
        request = urllib.request.Request(url, headers=_github_headers(token), method="GET")
        try:
            open_url = _pagination_open if pagination else urllib.request.urlopen
            with open_url(request, timeout=timeout) as response:
                raw = response.read()
                link = response.headers.get("Link")
            try:
                return _GitHubReadResult(json.loads(raw.decode("utf-8")), status=200, link=link)
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
